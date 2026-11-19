"""Regression coverage at the MCP schema and HTTP wire boundaries."""
import json

import httpx
import pytest
import respx

from mpulse_mcp import server
from mpulse_mcp.auth import TOKENS_PATH
from mpulse_mcp.client import MpulseClient
from mpulse_mcp.errors import ValidationError

BASE = "https://mpulse.soasta.com"
URL = BASE + "/concerto/mpulse/api/v2/KEY-ALPHA/"


@pytest.fixture
async def wired(registry, monkeypatch):
    client = MpulseClient(registry, http=httpx.AsyncClient(base_url=BASE))
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_registry", registry)
    with respx.mock:
        respx.put(BASE + TOKENS_PATH).respond(201, json={"token": "T"})
        yield client
    await client.aclose()


def echo(request):
    p = request.url.params
    names = p.get_list("metric") + p.get_list("timer")
    return httpx.Response(200, json={"values": [{"id": "ExtraDefaultSeries", "latest": 999}] + [
        {"id": name, "latest": int(p.get("percentile", 50)), "history": []}
        for name in reversed(names)
    ]})


@pytest.mark.parametrize("timers,cells", [(None, 8), (["PageLoad", "LargestContentfulPaint"], 16)])
async def test_mcp_batch_uses_four_queries(wired, timers, cells):
    route = respx.get(URL + "timers-metrics").mock(side_effect=echo)
    content = await server.mcp.call_tool("get_aggregate", {
        "metrics": ["Beacons", "AppErrors"], "timers": timers,
        "periods": [{"date": "2026-11-08"}, {"date": "2026-11-09"}],
        "percentiles": [50, 75],
    })
    result = json.loads(content[0].text)
    assert len(result["table"]) == cells
    assert result["requests"] == {"planned": 4, "executed": 4, "recovery": 0}
    assert route.call_count == 4
    assert all(r["value"] == r["percentile"] for r in result["table"])
    for call in route.calls:
        assert call.request.url.params.get_list("metric") == ["Beacons", "AppErrors"]
        assert call.request.url.params.get_list("timer") == (timers or [])


async def test_mcp_metrics_lists_filters_and_boundaries(wired):
    route = respx.get(URL + "timers-metrics").mock(side_effect=echo)
    content = await server.mcp.call_tool("get_metrics", {
        "metric": ["beacons", "app_errors"],
        "timer": ["page_load", "largest_contentful_paint"],
        "date_comparator": "Between", "date_start": "2026-11-08", "date_end": "2026-11-10",
        "country": ["KR", "US"], "network_error_code": ["404", "500"],
        "custom_dimension_filters": {"mobile speed": ["4g", "5g"]},
        "history_mode": "none",
    })
    result = json.loads(content[0].text)
    assert not result.get("warning")
    p = route.calls.last.request.url.params
    assert p.get_list("metric") == ["Beacons", "AppErrors"]
    assert p.get_list("timer") == ["PageLoad", "LargestContentfulPaint"]
    assert p.get_list("country") == ["KR", "US"]
    assert p.get_list("custom-dimension-mobile_speed") == ["4g", "5g"]
    assert p.get_list("network-error-code") == ["404", "500"]
    assert p["date-end"] == "2026-11-10"


@pytest.mark.parametrize("name,endpoint", [("get_summary", "summary"), ("get_histogram", "histogram"),
                                         ("get_timers", "by-minute")])
async def test_shared_tools_forward_last_and_lists(wired, name, endpoint):
    route = respx.get(URL + endpoint).respond(200, json={"n": 5, "median": 12})
    await server.mcp.call_tool(name, {
        "date_comparator": "Last", "trailing_seconds": 3600,
        "browser_family": ["Chrome", "Safari"],
        "custom_dimension_filters": {"branch": ["a", "b"]},
    })
    p = route.calls.last.request.url.params
    assert p["trailing-seconds"] == "3600"
    assert p.get_list("browser-family") == ["Chrome", "Safari"]
    assert p.get_list("custom-dimension-branch") == ["a", "b"]


@pytest.mark.parametrize("params", [
    {"date-comparator": "Between", "date-start": "2026-11-08"},
    {"date-comparator": "Between", "date-start": "2026-11-10", "date-end": "2026-11-08"},
    {"date-comparator": "Last"}, {"percentile": 100}, {"percentile": 0},
    {"country": []}, {"country": [None]},
])
async def test_known_query_invalid_params_never_reach_api(wired, params):
    route = respx.get(URL + "timers-metrics").mock(side_effect=echo)
    with pytest.raises(ValidationError):
        await server.query("timers-metrics", params=params)
    assert route.call_count == 0


async def test_single_timer_endpoint_does_not_accept_repeated_timer(wired):
    route = respx.get(URL + "summary").respond(200, json={})
    with pytest.raises(ValidationError, match="single value"):
        await server.query("summary", params={"timer": ["PageLoad", "LargestContentfulPaint"]})
    assert route.call_count == 0


@pytest.mark.parametrize("status", [403, 429, 500])
async def test_group_failure_does_not_fan_out(wired, status, monkeypatch):
    import mpulse_mcp.client as client_module
    monkeypatch.setattr(client_module, "MAX_RETRIES", 0)
    route = respx.get(URL + "timers-metrics").respond(status, text="failure")
    result = await server.get_aggregate(metrics=["Beacons", "AppErrors"],
                                        periods=[{"date": "2026-11-08"}])
    assert result["table"] == []
    assert len(result["errors"]) == 2
    assert result["requests"] == {"planned": 1, "executed": 1, "recovery": 0}
    assert all(len(c.request.url.params.get_list("metric")) == 2 for c in route.calls)


async def test_bad_batch_recovers_only_on_400(wired):
    def fail_bad(request):
        if "AppErrors" in request.url.params.get_list("metric"):
            return httpx.Response(400, text="bad metric")
        return echo(request)
    route = respx.get(URL + "timers-metrics").mock(side_effect=fail_bad)
    result = await server.get_aggregate(metrics=["Beacons", "AppErrors"],
                                        periods=[{"date": "2026-11-08"}])
    assert len(result["table"]) == len(result["errors"]) == 1
    assert result["requests"] == {"planned": 1, "executed": 3, "recovery": 2}
    assert route.call_count == 3


@pytest.mark.parametrize("values", [
    [{"id": "PageLoad", "latest": 999}],
    [{"id": "Beacons", "latest": 1}, {"id": "Beacons", "latest": 2}],
    [{"id": "Beacons", "history": []}],
])
async def test_missing_or_ambiguous_series_never_substitutes(wired, values):
    respx.get(URL + "timers-metrics").respond(200, json={"values": values})
    result = await server.get_aggregate(metrics=["Beacons"], periods=[{"date": "2026-11-08"}])
    assert result["table"] == []
    assert len(result["errors"]) == 1


async def test_zero_latest_is_valid_and_list_identity_check_reports_missing(wired):
    respx.get(URL + "timers-metrics").respond(200, json={"values": [
        {"id": "Beacons", "latest": 0, "history": []}
    ]})
    result = await server.query("timers-metrics", params={"metric": ["Beacons", "AppErrors"]})
    assert result["empty"] is False
    assert "AppErrors" in str(result.get("warning"))
    aggregate = await server.get_aggregate(metrics=["Beacons"], periods=[{"date": "2026-11-08"}])
    assert aggregate["table"][0]["value"] == 0


async def test_duplicate_aliases_reuse_requests_but_keep_cells(wired):
    route = respx.get(URL + "timers-metrics").mock(side_effect=echo)
    result = await server.get_aggregate(metrics=["Beacons", "beacons"],
        periods=[{"date": "2026-11-08", "label": "a"}, {"date": "2026-11-08", "label": "b"}],
        percentiles=[50, 50])
    assert route.call_count == 1
    assert route.calls.last.request.url.params.get_list("metric") == ["Beacons"]
    assert len(result["table"]) == 8


async def test_breakdown_custom_timer_and_repeated_filters(wired):
    route = respx.get(URL + "metrics-by-dimension").respond(200, json={"columnNames": [], "data": []})
    await server.get_breakdown(dimension="country", metrics=["custom"], custom_timer="checkout",
                               date="2026-11-08", country=["KR", "US"])
    assert route.calls.last.request.url.params["custom-timer"] == "checkout"
    assert route.calls.last.request.url.params.get_list("country") == ["KR", "US"]


def test_discovery_contract_matches_endpoint():
    breakdown = server.describe_query("metrics-by-dimension")
    assert "metrics" in breakdown["parameters"]
    assert "metric" not in breakdown["parameters"]
    values = server.describe_query("dimension-values")
    assert values["supports_drilldown"] is False
    assert "country" not in values["parameters"]
