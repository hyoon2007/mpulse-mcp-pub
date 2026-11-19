"""Server-level integration for name validation in the shared _run path."""

from __future__ import annotations

import httpx
import pytest
import respx

from mpulse_mcp import server
from mpulse_mcp.auth import TOKENS_PATH
from mpulse_mcp.client import MpulseClient
from mpulse_mcp.config import AppConfig, Registry
from mpulse_mcp.errors import MpulseError, ValidationError

BASE = "https://mpulse.soasta.com"
TOKENS_URL = BASE + TOKENS_PATH


def _q_url(api_key: str, qt: str) -> str:
    return f"{BASE}/concerto/mpulse/api/v2/{api_key}/{qt}"


@pytest.fixture
def wired_client(registry: Registry, monkeypatch: pytest.MonkeyPatch):
    """Install a respx-mockable client as the server's global singleton."""
    client = MpulseClient(registry, http=httpx.AsyncClient(base_url=BASE))
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_registry", registry)
    return client


@respx.mock
async def test_run_autocorrects_timer_on_the_wire(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "summary")).mock(
        return_value=httpx.Response(200, json={"median": "1", "n": "5"})
    )
    try:
        result = await server._run(
            app=None,
            query_type="summary",
            value_params={"timer": "pageload"},  # wrong casing
            date="2026-08-03",
            date_comparator=None,
            timezone=None,
            drilldowns={},
            raw=False,
        )
    finally:
        await wired_client.aclose()

    # The corrected name reached mPulse (no silent PageLoad fallback risk).
    assert route.calls.last.request.url.params["timer"] == "PageLoad"
    assert "corrections" in result
    assert "PageLoad" in result["corrections"][0]


@respx.mock
async def test_run_rejects_unknown_metric_without_calling_api(wired_client) -> None:
    query_route = respx.get(_q_url("KEY-ALPHA", "timers-metrics")).mock(
        return_value=httpx.Response(200, json={"values": []})
    )
    try:
        with pytest.raises(ValidationError, match="totally_bogus_metric_xyz"):
            await server._run(
                app=None,
                query_type="timers-metrics",
                value_params={"metric": "totally_bogus_metric_xyz"},
                date=None,
                date_comparator="LastHour",
                timezone=None,
                drilldowns={},
                raw=False,
            )
    finally:
        await wired_client.aclose()
    assert query_route.call_count == 0  # rejected before any upstream call


@respx.mock
async def test_run_valid_metric_passes_through(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "timers-metrics")).mock(
        return_value=httpx.Response(
            200, json={"values": [{"id": "Beacons", "history": [1, 2], "latest": 2}]}
        )
    )
    try:
        result = await server._run(
            app=None,
            query_type="timers-metrics",
            value_params={"metric": "Beacons"},
            date=None,
            date_comparator="LastHour",
            timezone=None,
            drilldowns={},
            raw=False,
        )
    finally:
        await wired_client.aclose()

    assert route.calls.last.request.url.params["metric"] == "Beacons"
    assert "corrections" not in result  # exact match -> no correction note
    assert "error" not in result


# --- get_aggregate ---------------------------------------------------------
def _echo_latest(request: httpx.Request) -> httpx.Response:
    """Echo the requested metric/timer as the series id, latest == percentile."""
    p = request.url.params
    names = p.get_list("metric") + p.get_list("timer")
    pct = int(p.get("percentile", "50"))
    return httpx.Response(
        200, json={"values": [{"id": name, "history": [1], "latest": pct} for name in names]}
    )


@respx.mock
async def test_get_aggregate_matrix(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    respx.get(_q_url("KEY-ALPHA", "timers-metrics")).mock(side_effect=_echo_latest)
    try:
        result = await server.get_aggregate(
            metrics=["Beacons"],
            timers=["PageLoad"],
            periods=[
                {"date_comparator": "ThisMonth", "label": "thismonth"},
                {"date": "2026-06-15"},
            ],
            percentiles=[50, 75],
        )
    finally:
        await wired_client.aclose()

    # 2 targets × 2 periods × 2 percentiles = 8 cells, all successful.
    assert len(result["table"]) == 8
    assert "errors" not in result
    assert set(result["targets"]) == {"Beacons", "PageLoad"}
    # latest echoes the percentile, so each cell's value == its percentile.
    for row in result["table"]:
        assert row["value"] == row["percentile"]


@respx.mock
async def test_get_aggregate_max_combos_guard(wired_client) -> None:
    query = respx.get(_q_url("KEY-ALPHA", "timers-metrics")).mock(
        side_effect=_echo_latest
    )
    try:
        with pytest.raises(ValidationError, match="max_combos"):
            await server.get_aggregate(
                metrics=["Beacons", "AppErrors"],
                periods=[{"date": "2026-06-15"}, {"date": "2026-07-15"}],
                percentiles=[50, 75],
                max_combos=3,  # 2×2×2 = 8 > 3
            )
    finally:
        await wired_client.aclose()
    assert query.call_count == 0  # rejected before any upstream call


@respx.mock
async def test_get_aggregate_rejects_unknown_metric(wired_client) -> None:
    query = respx.get(_q_url("KEY-ALPHA", "timers-metrics")).mock(
        side_effect=_echo_latest
    )
    try:
        with pytest.raises(ValidationError, match="totally_bogus_metric_xyz"):
            await server.get_aggregate(
                metrics=["totally_bogus_metric_xyz"],
                periods=[{"date_comparator": "LastHour"}],
            )
    finally:
        await wired_client.aclose()
    assert query.call_count == 0


@respx.mock
async def test_get_aggregate_partial_failure(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))

    def _one_fails(request: httpx.Request) -> httpx.Response:
        if "AppErrors" in request.url.params.get_list("metric"):
            return httpx.Response(400, text="Invalid metric")
        return _echo_latest(request)

    respx.get(_q_url("KEY-ALPHA", "timers-metrics")).mock(side_effect=_one_fails)
    try:
        result = await server.get_aggregate(
            metrics=["Beacons", "AppErrors"],
            periods=[{"date_comparator": "LastHour"}],
            percentiles=[50],
        )
    finally:
        await wired_client.aclose()

    # Beacons cell succeeds; AppErrors cell fails independently.
    assert len(result["table"]) == 1
    assert result["table"][0]["target"] == "Beacons"
    assert len(result["errors"]) == 1
    assert result["errors"][0]["target"] == "AppErrors"


@respx.mock
async def test_get_aggregate_autocorrects_metric(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "timers-metrics")).mock(
        side_effect=_echo_latest
    )
    try:
        result = await server.get_aggregate(
            metrics=["beacons"],  # wrong casing
            periods=[{"date_comparator": "LastHour"}],
        )
    finally:
        await wired_client.aclose()
    assert "corrections" in result
    assert route.calls.last.request.url.params["metric"] == "Beacons"


# --- limit (#4) ------------------------------------------------------------
@respx.mock
async def test_query_caps_high_cardinality_by_default(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    respx.get(_q_url("KEY-ALPHA", "geography")).mock(
        return_value=httpx.Response(200, json=[{"c": i} for i in range(150)])
    )
    try:
        result = await server.query(
            query_type="geography", params={"date-comparator": "LastHour"}
        )
    finally:
        await wired_client.aclose()
    # geography is high-cardinality -> auto-capped at 100 with a truncation note.
    assert result["truncated"]["total"] == 150
    assert result["truncated"]["returned"] == 100


# --- custom dimension registry ---------------------------------------------
def _registry_with_custom_dims() -> Registry:
    apps = {
        "alpha": AppConfig(
            name="alpha",
            api_key="KEY-ALPHA",
            tenant="tenant-a",
            api_token_env="MPULSE_API_TOKEN",
            custom_dimensions={
                "branch": {"display": "Branch"},
                "mobile_speed": {"display": "mobile speed"},
            },
        )
    }
    return Registry(default_app="alpha", apps=apps)


def test_list_custom_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_registry", _registry_with_custom_dims())
    out = server.list_custom_dimensions(app="alpha")
    assert out["app"] == "alpha"
    assert {d["label"] for d in out["custom_dimensions"]} == {"branch", "mobile_speed"}


def test_describe_query_includes_app_custom_dims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "_registry", _registry_with_custom_dims())
    out = server.describe_query("metrics-by-dimension", app="alpha")
    labels = {d["label"] for d in out.get("app_custom_dimensions", [])}
    assert "mobile_speed" in labels
    # without app -> no app-specific block
    plain = server.describe_query("metrics-by-dimension")
    assert "app_custom_dimensions" not in plain


@pytest.fixture
def wired_client_custom(monkeypatch: pytest.MonkeyPatch):
    reg = _registry_with_custom_dims()
    client = MpulseClient(reg, http=httpx.AsyncClient(base_url=BASE))
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_registry", reg)
    return client


@respx.mock
async def test_query_normalizes_custom_dimension_value(wired_client_custom) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    try:
        result = await server.query(
            query_type="metrics-by-dimension",
            params={"dimension": "mobile speed", "metrics": "beacons",
                    "date-comparator": "LastHour"},
        )
    finally:
        await wired_client_custom.aclose()
    # 'mobile speed' -> wire label 'mobile_speed'; declared, so no unknown flag.
    assert route.calls.last.request.url.params["dimension"] == "mobile_speed"
    assert any("normalized" in n for n in result.get("dimension_notes", []))


@respx.mock
async def test_query_advises_on_unclassified_dimension_but_passes(
    wired_client_custom,
) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    try:
        result = await server.query(
            query_type="metrics-by-dimension",
            params={"dimension": "not_declared", "metrics": "beacons",
                    "date-comparator": "LastHour"},
        )
    finally:
        await wired_client_custom.aclose()
    # Missing from the catalog does not prove it is a custom dimension.
    assert route.calls.last.request.url.params["dimension"] == "not_declared"
    assert any("not listed in the built-in catalog" in n for n in result["dimension_notes"])
    assert any("uncatalogued built-in" in n for n in result["dimension_notes"])
    assert not any("not a declared custom dimension" in n for n in result["dimension_notes"])


@pytest.mark.parametrize("tool", ["get_breakdown", "query"])
@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("dimension", [
    "device_memory", "page_domain", "page_cdn_cache_status",
    "bfcache_navigation", "bfcache_not_restored_reason",
])
@respx.mock
async def test_system_dimension_split_never_gets_custom_warning(
    wired_client_custom, tool, alias, dimension,
) -> None:
    respx.put(TOKENS_URL).respond(201, json={"token": "T"})
    route = respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).respond(
        200, json={"columnNames": [dimension, "beacons"], "data": [["sample", 42]]},
    )
    name = dimension.replace("_", "-") if alias else dimension
    try:
        if tool == "get_breakdown":
            result = await server.get_breakdown(
                dimension=name, metrics=["beacons"], date="2026-11-08", country="US",
            )
        else:
            result = await server.query("metrics-by-dimension", params={
                "dimension": name, "metrics": "beacons", "date": "2026-11-08", "country": "US",
            })
    finally:
        await wired_client_custom.aclose()
    assert route.calls.last.request.url.params["dimension"] == dimension
    assert result["body"]["data"] == [["sample", 42]]
    assert not any("custom" in note.lower() or "uncatalogued" in note.lower()
                   for note in result.get("dimension_notes", []))
    if alias:
        assert any("auto-corrected" in note for note in result.get("dimension_notes", []))
    else:
        assert not result.get("dimension_notes")


def test_explicit_unknown_custom_filter_keeps_custom_advice() -> None:
    wire = {"dimension": "device_memory", "custom-dimension-not_declared": "value"}
    notes = server._normalize_custom_dimension_params(
        "metrics-by-dimension", wire, _registry_with_custom_dims().get("alpha"),
    )
    assert len(notes) == 1
    assert "custom-dimension filter 'not_declared' is not a declared custom dimension" in notes[0]


@respx.mock
async def test_query_normalizes_custom_dimension_filter_label(
    wired_client_custom,
) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "summary")).mock(
        return_value=httpx.Response(200, json={"n": "5", "median": "1"})
    )
    try:
        result = await server.query(
            query_type="summary",
            params={"custom-dimension-Mobile Speed": "4g",
                    "date-comparator": "LastHour"},
        )
    finally:
        await wired_client_custom.aclose()
    params = dict(route.calls.last.request.url.params)
    assert params.get("custom-dimension-mobile_speed") == "4g"
    assert any("normalized" in n for n in result.get("dimension_notes", []))


@respx.mock
async def test_query_rejects_custom_dimension_for_dimension_values(wired_client) -> None:
    route = respx.get(_q_url("KEY-ALPHA", "dimension-values")).mock(
        return_value=httpx.Response(200, json={"values": []})
    )
    try:
        with pytest.raises(ValidationError, match="metrics-by-dimension"):
            await server.query(
                query_type="dimension-values",
                params={"dimension": "branch", "date-comparator": "LastHour"},
            )
    finally:
        await wired_client.aclose()
    assert route.call_count == 0  # rejected before any upstream call


@respx.mock
async def test_query_allows_custom_dimension_for_metrics_by_dimension(
    wired_client,
) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    try:
        result = await server.query(
            query_type="metrics-by-dimension",
            params={"dimension": "branch", "metrics": "beacons",
                    "date-comparator": "LastHour"},
        )
    finally:
        await wired_client.aclose()
    # custom dim passes through untouched to the API.
    assert "error" not in result
    assert route.calls.last.request.url.params["dimension"] == "branch"


@respx.mock
async def test_query_autocorrects_builtin_dimension_casing(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "dimension-values")).mock(
        return_value=httpx.Response(200, json={"values": ["Chrome/1"]})
    )
    try:
        result = await server.query(
            query_type="dimension-values",
            params={"dimension": "Browser", "date-comparator": "LastHour"},
        )
    finally:
        await wired_client.aclose()
    assert route.calls.last.request.url.params["dimension"] == "browser"
    assert any("auto-corrected" in c for c in result.get("dimension_notes", []))


# --- probe / empty_reason (#5) ---------------------------------------------
def _summary_drilldown_probe(request: httpx.Request):
    """Empty when drilled down (page-group present), data at the base."""
    if "page-group" in request.url.params:
        return httpx.Response(200, json={"n": "0"})
    return httpx.Response(200, json={"n": "100", "median": "1200"})


@respx.mock
async def test_probe_detects_unsupported_combo(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    respx.get(_q_url("KEY-ALPHA", "summary")).mock(
        side_effect=_summary_drilldown_probe
    )
    try:
        result = await server._run(
            app=None,
            query_type="summary",
            value_params={"timer": "PageLoad"},
            date="2026-08-03",
            date_comparator=None,
            timezone=None,
            drilldowns={"page-group": "Home", "browser": "X"},
            raw=False,
            probe=True,
        )
    finally:
        await wired_client.aclose()
    assert result["empty"] is True
    assert result["empty_reason"] == "filtered_empty"
    assert result["probe"] == {"ran": True, "base_empty": False}


@respx.mock
async def test_probe_detects_no_traffic(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    respx.get(_q_url("KEY-ALPHA", "summary")).mock(
        return_value=httpx.Response(200, json={"n": "0"})  # empty everywhere
    )
    try:
        result = await server._run(
            app=None,
            query_type="summary",
            value_params={"timer": "PageLoad"},
            date="2026-08-03",
            date_comparator=None,
            timezone=None,
            drilldowns={"page-group": "Home", "browser": "X"},
            raw=False,
            probe=True,
        )
    finally:
        await wired_client.aclose()
    assert result["empty_reason"] == "no_data"
    assert result["probe"] == {"ran": True, "base_empty": True}


# --- fix 1: singular `metric` on metrics-by-dimension is rejected ----------
@respx.mock
async def test_query_rejects_singular_metric_for_mbd(wired_client) -> None:
    route = respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(
        return_value=httpx.Response(200, json={"columnNames": [], "data": []})
    )
    try:
        with pytest.raises(ValidationError, match="PLURAL"):
            await server.query(
                query_type="metrics-by-dimension",
                params={"dimension": "browser", "metric": "beacons",
                        "date-comparator": "LastHour"},
            )
    finally:
        await wired_client.aclose()
    assert route.call_count == 0  # never sent: mPulse would silently ignore it


@respx.mock
async def test_query_joins_list_metrics_into_comma_string(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(
        return_value=httpx.Response(
            200,
            json={"columnNames": ["Browser", "Beacons", "LargestContentfulPaint"],
                  "data": [["Chrome", 1, 2]]},
        )
    )
    try:
        await server.query(
            query_type="metrics-by-dimension",
            params={"dimension": "browser",
                    "metrics": ["beacons", "largest_contentful_paint"],
                    "date-comparator": "LastHour"},
        )
    finally:
        await wired_client.aclose()
    items = route.calls.last.request.url.params.multi_items()
    # one comma-separated `metrics` value, not repeated keys
    assert [v for (k, v) in items if k == "metrics"] == [
        "beacons,largest_contentful_paint"
    ]


# --- fix 2: table-shaped empty result drives the probe -----------------------
@respx.mock
async def test_probe_detects_unsupported_combo_for_table_shape(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))

    def _mbd(request: httpx.Request) -> httpx.Response:
        if "custom-dimension-branch" in request.url.params:
            return httpx.Response(200, json={"columnNames": ["Branch", "Beacons"], "data": []})
        return httpx.Response(200, json={"columnNames": ["Branch", "Beacons"], "data": [["uk", 5]]})

    route = respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(side_effect=_mbd)
    try:
        result = await server.query(
            query_type="metrics-by-dimension",
            params={"dimension": "branch", "metrics": "beacons",
                    "custom-dimension-branch": "zz", "date-comparator": "LastHour"},
            probe=True, limit=1,
        )
    finally:
        await wired_client.aclose()
    assert result["empty"] is True  # table-shaped empty is now detected
    assert result["empty_reason"] == "filtered_empty"
    assert result["probe"] == {"ran": True, "base_empty": False}
    assert result["body"] == {"columnNames": ["Branch", "Beacons"], "data": []}
    assert "truncated" not in result
    assert route.call_count == 2


# --- fix 3: invalid history_mode is rejected, not silently downsampled -------
@respx.mock
async def test_get_timers_rejects_invalid_history_mode(wired_client) -> None:
    route = respx.get(_q_url("KEY-ALPHA", "by-minute")).mock(
        return_value=httpx.Response(200, json={})
    )
    try:
        with pytest.raises(ValidationError, match="history_mode"):
            await server.get_timers(date="2026-08-03", history_mode="Full")
    finally:
        await wired_client.aclose()
    assert route.call_count == 0


@respx.mock
async def test_query_rejects_invalid_history_mode(wired_client) -> None:
    route = respx.get(_q_url("KEY-ALPHA", "timers-metrics")).mock(
        return_value=httpx.Response(200, json={"values": []})
    )
    try:
        with pytest.raises(ValidationError, match="history_mode"):
            await server.query(
                query_type="timers-metrics",
                params={"date-comparator": "LastHour"},
                history_mode="bogus",
            )
    finally:
        await wired_client.aclose()
    assert route.call_count == 0


# --- unsupported date comparators are rejected (mPulse answers HTTP 500) ----
@respx.mock
async def test_unsupported_date_comparator_rejected_everywhere(wired_client) -> None:
    route = respx.get(url__startswith=BASE + "/concerto/mpulse/api/v2/").mock(
        return_value=httpx.Response(500)
    )
    try:
        with pytest.raises(ValidationError, match="Unsupported date_comparator"):
            await server.get_summary(date_comparator="Yesterday")
        with pytest.raises(ValidationError, match="Unsupported date_comparator"):
            await server.query(query_type="summary", params={"date-comparator": "Last7Days"})
        with pytest.raises(ValidationError, match="Unsupported date_comparator"):
            await server.get_aggregate(metrics=["Beacons"],
                                       periods=[{"date_comparator": "Last6Hours"}])
    finally:
        await wired_client.aclose()
    assert route.call_count == 0  # no 500 + five retries


# --- native `limit` is sent to mPulse where it decides the row count --------
def _rows(n: int) -> dict:
    return {"columnNames": ["Branch", "Beacons"], "data": [[f"b{i}", n - i] for i in range(n)]}


@pytest.mark.parametrize("limit,n_rows,raw", [
    (50, 50, False), (1, 1, False), (1, 1, True), (1, 3, False),
])
@respx.mock
async def test_top_level_limit_is_sent_to_mpulse(wired_client, limit, n_rows, raw) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(
        return_value=httpx.Response(200, json=_rows(n_rows))
    )
    try:
        result = await server.query(
            query_type="metrics-by-dimension",
            params={"dimension": "branch", "metrics": "beacons", "date-comparator": "ThisWeek"},
            limit=limit, raw=raw, probe=True,
        )
    finally:
        await wired_client.aclose()
    assert route.calls.last.request.url.params["limit"] == str(limit)
    assert result["data" if raw else "body"] == {
        **_rows(n_rows), "data": _rows(n_rows)["data"][:limit],
    }
    assert route.call_count == 1
    if n_rows > limit:
        # Preserve client limiting when the upstream endpoint ignores its cap.
        assert result["truncated"] == {
            "by": "client", "key": "data", "total": n_rows, "returned": limit,
        }
    else:
        assert result["truncated"]["by"] == "server"
        assert result["truncated"]["limit"] == limit


@respx.mock
async def test_default_server_limit_per_query_type(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    sent = {}
    for qt, expected in (("metrics-by-dimension", "100"), ("app-error-summary", "100"),
                         ("dimension-over-time", "10")):
        route = respx.get(_q_url("KEY-ALPHA", qt)).mock(
            return_value=httpx.Response(200, json=_rows(3))
        )
        params = {"date-comparator": "LastHour"}
        if qt == "metrics-by-dimension":
            params |= {"dimension": "country", "metrics": "beacons"}
        if qt == "dimension-over-time":
            params |= {"dimension": "country"}
        result = await server.query(query_type=qt, params=params)
        sent[qt] = route.calls.last.request.url.params.get("limit")
        assert sent[qt] == expected, (qt, sent[qt])
        assert "truncated" not in result  # 3 rows < limit: complete
    await wired_client.aclose()


@pytest.mark.parametrize("wire_limit,top_limit,n_rows,returned,by", [
    (30, 80, 5, 5, None),
    (3, 1, 3, 1, "client"),
    (1, 80, 1, 1, "server"),
    (3, 0, 3, 0, "client"),
])
@respx.mock
async def test_params_limit_wins_over_top_level(
    wired_client, wire_limit, top_limit, n_rows, returned, by,
) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(
        return_value=httpx.Response(200, json=_rows(n_rows))
    )
    try:
        result = await server.query(
            query_type="metrics-by-dimension",
            params={"dimension": "branch", "metrics": "beacons", "limit": wire_limit,
                    "date-comparator": "ThisWeek"},
            limit=top_limit,
        )
    finally:
        await wired_client.aclose()
    assert route.calls.last.request.url.params["limit"] == str(wire_limit)
    assert result["body"] == {**_rows(n_rows), "data": _rows(n_rows)["data"][:returned]}
    assert result.get("truncated", {}).get("by") == by
    assert route.call_count == 1


@respx.mock
async def test_over_max_limit_is_lowered_with_a_note(wired_client) -> None:
    # A big limit means "as many as possible": lower it instead of failing.
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    labels = [f"C{i}" for i in range(10)]
    series = [{"name": name, "aPoints": [{"x": 0, "y": 1}]} for name in labels]
    route = respx.get(_q_url("KEY-ALPHA", "dimension-over-time")).mock(
        return_value=httpx.Response(200, json={"labelNames": labels, "data": {"series": series}})
    )
    try:
        result = await server.query(query_type="dimension-over-time",
                                    params={"dimension": "country", "date-comparator": "LastHour"},
                                    limit=20, history_mode="full")
    finally:
        await wired_client.aclose()
    assert route.calls.last.request.url.params["limit"] == "10"
    assert any("lowered to 10" in c for c in result["corrections"])
    assert result["body"] == {"meta": {"labelNames": labels}, "series": series}
    assert result["truncated"]["by"] == "server"


@respx.mock
async def test_below_one_limit_rejected_before_calling(wired_client) -> None:
    route = respx.get(url__startswith=BASE + "/concerto/mpulse/api/v2/").mock(
        return_value=httpx.Response(400)
    )
    try:
        with pytest.raises(ValidationError, match="1-100"):
            await server.query(query_type="metrics-by-dimension",
                               params={"dimension": "country", "metrics": "beacons",
                                       "date-comparator": "LastHour"},
                               limit=0)
    finally:
        await wired_client.aclose()
    assert route.call_count == 0


@respx.mock
async def test_report_endpoint_limit_stays_client_side(wired_client) -> None:
    # geography ignores a native limit (verified live), so none is sent.
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "geography")).mock(
        return_value=httpx.Response(200, json=[{"c": i} for i in range(30)])
    )
    try:
        result = await server.query(query_type="geography",
                                    params={"date-comparator": "LastHour"}, limit=10)
    finally:
        await wired_client.aclose()
    assert "limit" not in route.calls.last.request.url.params
    assert result["truncated"]["by"] == "client"
    assert result["truncated"]["returned"] == 10


@respx.mock
async def test_generic_query_states_aggregation_semantics(wired_client) -> None:
    # 'unspecified' placeholder is gone: known query-types state how to read
    # the numbers; unknown (forward-compatible) ones omit the field entirely.
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(
        return_value=httpx.Response(200, json={"data": [["uk", 1]]})
    )
    respx.get(_q_url("KEY-ALPHA", "future-type")).mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    try:
        mbd = await server.query(
            query_type="metrics-by-dimension",
            params={"date-comparator": "LastHour", "dimension": "country",
                    "metrics": "beacons"},
        )
        unknown = await server.query(
            query_type="future-type", params={"date-comparator": "LastHour"}
        )
    finally:
        await wired_client.aclose()
    assert mbd["aggregation"] == "period aggregate per dimension value"
    assert "aggregation" not in unknown


@respx.mock
async def test_mbd_invalid_metric_400_gains_suggestions(wired_client) -> None:
    # mPulse rejects a wrong mbd metric name itself (verified live 2026-11-02);
    # the server enriches that bare 400 with did-you-mean candidates so the
    # model can retry without a describe_query round trip.
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(
        return_value=httpx.Response(
            400,
            json={"code": "MPulseAPIException.InvalidParameter",
                  "message": "Invalid Parameter: Invalid metric, cannot set "
                             "metric as img_transfer_size"},
        )
    )
    try:
        with pytest.raises(MpulseError) as exc:
            await server.query(
                query_type="metrics-by-dimension",
                params={"date-comparator": "LastHour", "dimension": "country",
                        "metrics": "beacons,img_transfer_size"},
            )
    finally:
        await wired_client.aclose()
    msg = str(exc.value)
    assert "image_transfer_size" in msg  # the suggestion
    assert "snake_case" in msg
    assert exc.value.hint is not None
    assert "beacons" not in exc.value.hint  # valid names produce no noise


def test_enrich_skips_non_metric_errors() -> None:
    err = MpulseError("mPulse Query API error 400: Invalid Parameter: Limit cannot be set as 200")
    server._enrich_invalid_metric_error(err, {"metrics": "img_transfer_size"})
    assert err.hint is None  # untouched: not an Invalid-metric error


# --- get_breakdown ---------------------------------------------------------
@respx.mock
async def test_get_breakdown_joins_metrics_and_corrects_dimension(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(
        return_value=httpx.Response(
            200,
            json={
                "columnNames": ["Country", "Beacons", "LargestContentfulPaint"],
                "data": [["US", 10, 1200], ["KR", 20, 1100]],
            },
        )
    )
    try:
        result = await server.get_breakdown(
            dimension="Country",
            metrics=["beacons", "largest_contentful_paint"],
            percentile=75,
            date_comparator="Between",
            date_start="2026-06-01",
            date_end="2026-07-01",
            limit=2,
            sortby="largest_contentful_paint",
        )
    finally:
        await wired_client.aclose()

    params = route.calls.last.request.url.params
    assert params["metrics"] == "beacons,largest_contentful_paint"
    assert params["dimension"] == "country"
    assert params["percentile"] == "75"
    assert params["date-start"] == "2026-06-01"
    assert params["date-end"] == "2026-07-01"
    assert params["sortby"] == "largest_contentful_paint"
    assert result["body"] == {
        "columnNames": ["Country", "Beacons", "LargestContentfulPaint"],
        "data": [["US", 10, 1200], ["KR", 20, 1100]],
    }
    assert result["corrections"] == result["dimension_notes"] == [
        "dimension 'Country' auto-corrected to 'country'"
    ]
    assert result["truncated"]["by"] == "server"
    assert route.call_count == 1


@respx.mock
async def test_get_breakdown_passes_custom_dimension(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(
        return_value=httpx.Response(200, json={"columnNames": [], "data": []})
    )
    try:
        await server.get_breakdown(dimension="branch")  # No period remains valid.
    finally:
        await wired_client.aclose()

    params = route.calls.last.request.url.params
    assert params["dimension"] == "branch"
    assert params["metrics"] == "beacons"
    assert params["percentile"] == "50"
    assert params["limit"] == "100"


@respx.mock
async def test_get_breakdown_sends_custom_dimension_filter(
    wired_client,
) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(
        return_value=httpx.Response(200, json={"columnNames": [], "data": []})
    )
    try:
        await server.get_breakdown(
            dimension="country",
            custom_dimension_filters={"branch": "uk"},
            date_comparator="LastHour",
        )
    finally:
        await wired_client.aclose()

    assert route.calls.last.request.url.params["custom-dimension-branch"] == "uk"


@respx.mock
async def test_get_breakdown_default_limit_marks_full_page_as_truncated(
    wired_client,
) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(
        return_value=httpx.Response(200, json=_rows(100))
    )
    try:
        result = await server.get_breakdown(
            dimension="country", date_comparator="LastHour"
        )
    finally:
        await wired_client.aclose()

    assert route.calls.last.request.url.params["limit"] == "100"
    assert result["truncated"]["by"] == "server"
    assert result["truncated"]["returned"] == 100


@respx.mock
async def test_get_breakdown_invalid_metric_includes_suggestions(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(
        return_value=httpx.Response(
            400,
            json={
                "code": "MPulseAPIException.InvalidParameter",
                "message": "Invalid Parameter: Invalid metric, cannot set "
                "metric as img_transfer_size",
            },
        )
    )
    try:
        with pytest.raises(MpulseError) as exc:
            await server.get_breakdown(
                dimension="country",
                metrics=["beacons", "img_transfer_size"],
                date_comparator="LastHour",
            )
    finally:
        await wired_client.aclose()

    assert "image_transfer_size" in str(exc.value)
    assert exc.value.hint is not None
    assert "beacons" not in exc.value.hint


@pytest.mark.parametrize("period,match", [
    ({"date_comparator": "Between", "date_start": "2026-06-01"}, "requires"),
    ({"date_comparator": "Between", "date_end": "2026-07-01"}, "requires"),
    ({"date_comparator": "Last"}, "requires"),
    ({"date_comparator": "Between", "date_start": "not-a-date",
      "date_end": "2026-07-01"}, "date_start"),
    ({"date_comparator": "Between", "date_start": "2026-07-01",
      "date_end": "2026-06-01"}, "greater"),
    ({"date_comparator": "Between", "date_start": "2026-06-01",
      "date_end": "2026-06-01"}, "greater"),
    ({"date_comparator": "Between", "date_start": "2026-06-01",
      "date_end": "2026-07-01", "trailing_seconds": 600}, "trailing_seconds"),
    ({"date_comparator": "Last", "trailing_seconds": 600,
      "date_start": "2026-06-01"}, "date_start"),
    ({"date_comparator": "LastHour", "trailing_seconds": 600}, "trailing_seconds"),
    ({"date": "2026-06-01", "date_end": "2026-07-01"}, "date_end"),
    ({"date_start": "2026-06-01"}, "date_start"),
])
async def test_breakdown_rejects_invalid_period_without_http(wired_client, period, match) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
        router.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(
            return_value=httpx.Response(200, json=_rows(1))
        )
        try:
            with pytest.raises(ValidationError, match=match):
                await server.get_breakdown(dimension="country", raw=True, probe=True, **period)
        finally:
            await wired_client.aclose()
        assert len(router.calls) == 0  # Neither authentication nor Query API was called.


@pytest.mark.parametrize("period", [
    {"date_comparator": "Last", "trailing_seconds": 0},
    {"date_comparator": "Between", "date_start": "20260601", "date_end": "2026-07-01"},
    {"date_comparator": "Between", "date_start": "2026-06-01T00:00:00.000Z",
     "date_end": "2026-07-01T00:00:00.000Z"},
])
@respx.mock
async def test_breakdown_preserves_period_wire_values(wired_client, period) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(
        return_value=httpx.Response(200, json=_rows(1))
    )
    try:
        await server.get_breakdown(dimension="country", **period)
    finally:
        await wired_client.aclose()
    params = route.calls.last.request.url.params
    assert {key.replace("_", "-"): str(value) for key, value in period.items()}.items() <= params.items()
    assert route.call_count == 1


@respx.mock
async def test_generic_query_rejects_incomplete_known_period(wired_client) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    route = respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(
        return_value=httpx.Response(200, json=_rows(1))
    )
    params = {"dimension": "country", "date-comparator": "Between",
              "date-start": "2026-06-01", "trailing-seconds": "upstream-defined"}
    original = dict(params)
    try:
        with pytest.raises(ValidationError):
            await server.query("metrics-by-dimension", params=params)
    finally:
        await wired_client.aclose()
    assert params == original
    assert route.call_count == 0


@pytest.mark.parametrize("dimension,corrected", [
    ("Country", True), ("mobile speed", False), ("auto-corrected", False),
])
@respx.mock
async def test_dimension_corrections_keep_tool_specific_contract(
    wired_client_custom, dimension, corrected,
) -> None:
    respx.put(TOKENS_URL).mock(return_value=httpx.Response(201, json={"token": "T"}))
    payload = {"columnNames": ["Country", "Beacons"], "data": []}
    route = respx.get(_q_url("KEY-ALPHA", "metrics-by-dimension")).mock(
        return_value=httpx.Response(200, json=payload)
    )
    try:
        generic = await server.query(
            "metrics-by-dimension", params={"dimension": dimension, "country": "US"},
            raw=True, limit=101, probe=True,
        )
        breakdown = await server.get_breakdown(
            dimension=dimension, country="US", raw=True, limit=101, probe=True,
        )
    finally:
        await wired_client_custom.aclose()
    assert breakdown["data"] == generic["data"] == payload
    assert breakdown["dimension_notes"] == generic["dimension_notes"]
    limit_note = "limit 101 lowered to 100, the maximum mPulse accepts for metrics-by-dimension"
    assert generic["corrections"] == [limit_note]
    assert breakdown["corrections"] == [limit_note] + (
        ["dimension 'Country' auto-corrected to 'country'"] if corrected else []
    )
    assert route.call_count == 2  # Raw empty responses do not trigger either probe.
