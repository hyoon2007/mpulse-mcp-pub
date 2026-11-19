"""Regression tests for the reference catalog and its wiring.

Covers the additions made when the enum catalog was introduced:
* ``catalog.json`` parses and the built-in enums have their expected sizes.
* ``catalog.enrich_describe`` returns the right valid-value hints per endpoint.
* ``describe_query`` actually merges those hints into its output.
* The ``bandwith-block`` (misspelled, verified-live) wire param and the
  ``ThisMonth`` date-comparator are present in the metadata tables.

These lock in the known-good state; if the catalog is edited the counts here
must be updated deliberately.
"""

from __future__ import annotations

import pytest

from mpulse_mcp import catalog
from mpulse_mcp.errors import ValidationError


# --- name resolution (auto-correct + suggest) ------------------------------
def test_resolve_timer_casing_and_separator_corrected() -> None:
    # snake/lower input -> canonical CamelCase timer.
    assert catalog.resolve_value("timer", "largest_contentful_paint", "summary") == (
        "ok",
        "LargestContentfulPaint",
    )
    assert catalog.resolve_value("timer", "pageload", "by-minute") == ("ok", "PageLoad")


def test_resolve_metric_is_endpoint_specific() -> None:
    # Same concept, different name space per endpoint.
    assert catalog.resolve_value(
        "metric", "largest_contentful_paint", "metrics-by-dimension"
    ) == ("ok", "largest_contentful_paint")
    # verified_live_extra entry is accepted for timers-metrics.
    assert catalog.resolve_value("metric", "TotalRequestCount", "timers-metrics") == (
        "ok",
        "TotalRequestCount",
    )


def test_resolve_custom_timer_pattern_is_ok() -> None:
    assert catalog.resolve_value("timer", "CustomTimer3", "summary") == (
        "ok",
        "CustomTimer3",
    )


def test_resolve_none_or_empty_skips() -> None:
    assert catalog.resolve_value("timer", None, "summary") == ("skip", None)
    assert catalog.resolve_value("metric", "", "timers-metrics") == ("skip", None)


def test_resolve_unknown_returns_suggestions() -> None:
    status, suggestions = catalog.resolve_value(
        "timer", "LargestContentofulPaint", "summary"  # typo
    )
    assert status == "unknown"
    assert "LargestContentfulPaint" in suggestions


# --- metrics-by-dimension uses `metrics` (plural); param mechanics ----------
def test_mbd_metric_param_is_plural_comma() -> None:
    c = catalog.load()
    mbd = c["metrics_for_metrics_by_dimension__metric_param"]
    assert "metrics (PLURAL" in mbd["param_name"]
    assert mbd["supports_percentile"] is True
    e = catalog.enrich_describe("metrics-by-dimension")
    assert e["metric_param_name"].startswith("metrics")
    assert "ignored" in e["metric_note"]


def test_metric_per_plt_distinct_enum() -> None:
    # BounceRate + CustomMetric0-9, NOT the snake_case metrics-by-dimension set.
    assert catalog.resolve_value(
        "metric", "BounceRate", "metric-per-page-load-time"
    ) == ("ok", "BounceRate")
    e = catalog.enrich_describe("metric-per-page-load-time")
    assert "BounceRate" in e["valid_metrics"]
    assert "beacons" not in e["valid_metrics"]


def test_enrich_surfaces_parameter_mechanics() -> None:
    e = catalog.enrich_describe("metrics-by-dimension")
    mech = e["parameter_mechanics"]
    assert mech["comma_separated_params"] == ["metrics"]
    assert any("custom-dimension" in p for p in mech["multiple_via_repeated_key"])
    # a non-comma endpoint has mechanics but no comma_separated_params key
    e2 = catalog.enrich_describe("timers-metrics")
    assert "comma_separated_params" not in e2["parameter_mechanics"]


# --- endpoint-specific parameter specs -------------------------------------
def test_endpoint_params_metrics_by_dimension() -> None:
    ep = catalog.enrich_describe("metrics-by-dimension")["endpoint_params"]
    assert "metrics" in ep["value_params"]
    assert set(ep["special_params"]) == {"limit", "sortby"}
    assert ep["filters"] is True
    assert any("apperror" in c for c in ep["constraints"])
    assert "columnNames" in ep["response_shape"]


def test_endpoint_params_report_endpoints_have_no_native_limit() -> None:
    for qt in ("geography", "page-groups", "browsers", "ab-tests", "bandwidth"):
        ep = catalog.enrich_describe(qt)["endpoint_params"]
        assert "limit" not in ep["special_params"]
        assert "sortby" not in ep["special_params"]
    # bandwidth: series-format but NO percentile
    bw = catalog.enrich_describe("bandwidth")["endpoint_params"]
    assert "series-format" in bw["special_params"]
    assert "percentile" not in bw["value_params"]


def test_endpoint_params_dimension_values_has_no_filters() -> None:
    ep = catalog.enrich_describe("dimension-values")["endpoint_params"]
    assert ep["filters"] is False


def test_dimension_over_time_uses_own_enum() -> None:
    e = catalog.enrich_describe("dimension-over-time")
    dims = e["valid_dimensions"]
    assert "bandwidth_block" in dims and "url" in dims  # dot-specific names
    ep = e["endpoint_params"]
    assert "interval" in ep["special_params"]
    assert ep["special_params"]["limit"].startswith("1-10")
    # custom dimension is NOT allowed here -> rejected
    assert catalog.resolve_dimension("branch", "dimension-over-time")[0] == (
        "unknown_no_custom"
    )


# --- custom dimension support ----------------------------------------------
def test_custom_dimension_supported_flags() -> None:
    assert catalog.custom_dimension_supported("metrics-by-dimension") is True
    assert catalog.custom_dimension_supported("dimension-values") is False
    assert catalog.custom_dimension_supported("dimension-over-time") is False
    assert catalog.custom_dimension_supported("summary") is None  # not a dim endpoint


def test_resolve_dimension_custom_allowed_for_mbd() -> None:
    # metrics-by-dimension accepts a custom name (e.g. branch) as a pass-through.
    assert catalog.resolve_dimension("branch", "metrics-by-dimension") == (
        "ok_custom",
        "branch",
    )


def test_resolve_dimension_custom_rejected_elsewhere() -> None:
    for qt in ("dimension-values", "dimension-over-time"):
        status, _ = catalog.resolve_dimension("branch", qt)
        assert status == "unknown_no_custom"


def test_resolve_dimension_builtin_casing_corrected() -> None:
    assert catalog.resolve_dimension("Browser", "dimension-values") == ("ok", "browser")


@pytest.mark.parametrize("query_type", [
    "metrics-by-dimension", "dimension-values", "dimension-over-time", "dimensions-over-time",
])
@pytest.mark.parametrize("dimension", [
    "device_memory", "page_domain", "page_cdn_cache_status",
    "bfcache_navigation", "bfcache_not_restored_reason",
])
def test_system_dimensions_are_discoverable_and_resolve_as_builtins(query_type, dimension) -> None:
    from mpulse_mcp.server import describe_query

    # Discovery lists the canonical endpoint name; the resolver also tolerates
    # the legacy plural spelling.
    described_type = "dimension-over-time" if query_type == "dimensions-over-time" else query_type
    assert dimension in describe_query(described_type)["valid_dimensions"]
    assert dimension in catalog.enrich_describe(query_type)["valid_dimensions"]
    assert catalog.resolve_dimension(dimension, query_type) == ("ok", dimension)
    assert catalog.resolve_dimension(dimension.upper().replace("_", "-"), query_type) == (
        "ok", dimension,
    )


def test_enrich_describe_exposes_custom_dimension_flag() -> None:
    dv = catalog.enrich_describe("dimension-values")
    assert dv["custom_dimension_supported"] is False
    assert dv.get("custom_dimension_redirect") == "metrics-by-dimension"
    mbd = catalog.enrich_describe("metrics-by-dimension")
    assert mbd["custom_dimension_supported"] is True


# --- catalog.json integrity ------------------------------------------------
def test_catalog_json_loads() -> None:
    data = catalog.load()
    assert data, "catalog.json failed to load / parse"
    # a few required top-level sections
    for key in (
        "metrics_for_timers_metrics__metric_param",
        "metrics_for_metrics_by_dimension__metric_param",
        "timers_for_timer_param",
        "dimensions",
        "custom_dimensions",
    ):
        assert key in data, f"missing catalog section: {key}"


def test_builtin_enum_sizes() -> None:
    data = catalog.load()
    tm = data["metrics_for_timers_metrics__metric_param"]
    mbd = data["metrics_for_metrics_by_dimension__metric_param"]
    timers = data["timers_for_timer_param"]
    dims = data["dimensions"]

    # 97 documented CamelCase metrics (+ TotalRequestCount is surfaced separately)
    assert len(tm["enum"]) == 97
    assert tm["verified_live_extra"] == ["TotalRequestCount"]
    assert "Beacons" in tm["enum"] and "TotalTransferSize" in tm["enum"]

    assert len(mbd["enum"]) == 82
    assert "asset_requests_per_page" in mbd["enum"]

    assert len(timers["enum"]) == 24
    assert timers["default"] == "PageLoad"
    assert "TotalBlockingTime" in timers["enum"]

    assert len(dims["dimension_values__dimension_enum"]["enum"]) == 29
    assert len(dims["metrics_by_dimension__dimension_split_enum"]["enum"]) == 38


# --- enrich_describe -------------------------------------------------------
def test_enrich_timers_metrics() -> None:
    out = catalog.enrich_describe("timers-metrics")
    # 97 docs enum + TotalRequestCount
    assert len(out["valid_metrics"]) == 98
    assert "TotalRequestCount" in out["valid_metrics"]
    assert len(out["valid_timers"]) == 24
    assert "custom_dimensions" in out
    assert out["gotchas"]
    # Total* trio (app-owner verified) + ImgTransferSize (verified live 2026-11-02)
    assert len(out["metric_name_differs_by_endpoint"]) == 4


def test_enrich_metrics_by_dimension() -> None:
    out = catalog.enrich_describe("metrics-by-dimension")
    assert len(out["valid_metrics"]) == 82
    assert "asset_transfer_size" in out["valid_metrics"]
    assert len(out["valid_dimensions"]) == 38
    assert "custom_dimensions" in out


def test_enrich_dimension_values() -> None:
    out = catalog.enrich_describe("dimension-values")
    assert len(out["valid_dimensions"]) == 29
    # connection_type is intentionally NOT a valid dimension-values dimension
    assert "connection_type" not in out["valid_dimensions"]
    assert "os" in out["valid_dimensions"]


def test_enrich_unrelated_query_type_is_safe() -> None:
    # A report with no metric/timer/dimension enums still returns a dict with the
    # broadly-useful reference bits, and never raises.
    out = catalog.enrich_describe("geography")
    assert isinstance(out, dict)
    assert "valid_metrics" not in out
    assert "valid_timers" not in out
    assert out["gotchas"]  # gotchas are always attached


# --- metadata tables (query_types.py) --------------------------------------
def test_bandwidth_param_uses_api_misspelling() -> None:
    from mpulse_mcp.query_types import DRILLDOWN_PARAMS

    assert "bandwith-block" in DRILLDOWN_PARAMS  # verified-live misspelling
    assert "bandwidth-block" not in DRILLDOWN_PARAMS  # correct spelling doesn't work


def test_this_month_comparator_present() -> None:
    from mpulse_mcp.query_types import KNOWN_DATE_COMPARATORS

    assert "ThisMonth" in KNOWN_DATE_COMPARATORS


def test_server_wire_mapping_for_bandwidth() -> None:
    # Friendly arg name stays correct; only the wire value carries the typo.
    from mpulse_mcp.server import _DRILLDOWN_ARG_TO_WIRE

    assert _DRILLDOWN_ARG_TO_WIRE["bandwidth_block"] == "bandwith-block"


# --- describe_query wiring -------------------------------------------------
def test_describe_query_merges_catalog() -> None:
    from mpulse_mcp import server

    # describe_query is registered via @mcp.tool(); depending on the SDK version
    # that may return the plain function or a wrapper exposing it as .fn.
    describe = getattr(server.describe_query, "fn", server.describe_query)

    result = describe("timers-metrics")
    assert "valid_metrics" in result
    assert "TotalRequestCount" in result["valid_metrics"]
    assert "valid_timers" in result
    assert "gotchas" in result
    # base fields are still present
    assert result["slug"] == "timers-metrics"
    assert "parameters" in result


def test_describe_query_unknown_type_unchanged() -> None:
    from mpulse_mcp import server

    describe = getattr(server.describe_query, "fn", server.describe_query)
    with pytest.raises(ValidationError, match="Unknown query-type"):
        describe("not-a-real-query-type")

def test_mbd_limit_reflects_live_behavior() -> None:
    # Live (2026-10-29): limit is honored for country/page_group/browser despite
    # the docs, and omitting it returns only 10 rows. Don't steer the model away.
    lim = catalog.enrich_describe("metrics-by-dimension")["endpoint_params"][
        "special_params"
    ]["limit"]
    assert "top 10" in lim
    assert "IS honored for page_group, browser and country" in lim


def test_catalog_has_no_disproven_claims() -> None:
    from mpulse_mcp.query_types import KNOWN_DATE_COMPARATORS

    c = catalog.load()
    # these comparators return HTTP 500 live
    for bad in ("Yesterday", "Last7Days", "Last6Hours"):
        assert bad not in KNOWN_DATE_COMPARATORS
    assert "mcp_also_accepts" not in c["common_params"]["date_comparator"]
    gotchas = " ".join(g["text"] for g in c["behavioral_gotchas"]["items"])
    # list values now expand to repeated keys; mixing count+percentile works
    assert "JSON list is ignored" not in gotchas
    # All catalog descriptions must agree with the implemented repeated-key path,
    # not only the gotcha text that the older regression checked.
    import json

    reference_text = json.dumps(c)
    assert "one metric per call" not in c["metrics_for_timers_metrics__metric_param"]["multiple"]
    assert "A JSON list is silently ignored" not in reference_text
    assert "without `interval` returns 1-minute" not in reference_text
    assert "JSON list" in c["metrics_for_timers_metrics__metric_param"]["multiple"]
    assert "get_metrics" in c["metrics_for_timers_metrics__metric_param"]["multiple"]
    assert "lowers" in c["endpoint_params"]["dimension-over-time"]["special_params"]["limit"]
    assert "list" not in c["query_types"]  # canonical types live in endpoint_params
    assert "dimensions-over-time" not in c["endpoint_params"]
    assert "dimension-over-time" in c["endpoint_params"]
    assert "can yield a null column" not in gotchas
    assert "beacon_type DOES support" in " ".join(
        c["metrics_for_metrics_by_dimension__metric_param"]["constraints"]
    )


def test_compact_describe_keeps_general_and_own_gotchas() -> None:
    from mpulse_mcp import server

    describe = getattr(server.describe_query, "fn", server.describe_query)
    full, compact = describe("summary", verbose=True), describe("summary")
    assert len(compact["gotchas"]) < len(full["gotchas"])
    joined = " ".join(compact["gotchas"])
    assert "bandwith-block" in joined  # general (applies to all), must stay
    assert "timers-metrics `latest`" not in joined  # timers-metrics only
    assert "metric_name_differs_by_endpoint" not in compact
    assert "verbose_hint" in compact and "verbose_hint" not in full
    # metrics-by-dimension keeps its own constraints and the name-mapping table
    mbd = describe("metrics-by-dimension")
    assert any("PLURAL" in g for g in mbd["gotchas"])
    assert "metric_name_differs_by_endpoint" in mbd


def test_every_gotcha_has_valid_scope() -> None:
    from mpulse_mcp.query_types import QUERY_TYPES

    for g in catalog.load()["behavioral_gotchas"]["items"]:
        scope = g["applies_to"]
        assert scope == "all" or (scope and set(scope) <= set(QUERY_TYPES)), g["text"][:60]


def test_compact_mechanics_drops_other_endpoints_entries() -> None:
    from mpulse_mcp import server

    describe = getattr(server.describe_query, "fn", server.describe_query)
    # 'metric (timers-metrics)' / 'timer (timers-metrics)' are noise elsewhere
    mbd = describe("metrics-by-dimension")["parameter_mechanics"]
    assert not any("(timers-metrics)" in e for e in mbd["multiple_via_repeated_key"])
    # ... but stay on their own endpoint, and in verbose mode everywhere
    tm = describe("timers-metrics")["parameter_mechanics"]
    assert any(e == "metric (timers-metrics)" for e in tm["multiple_via_repeated_key"])
    full = describe("metrics-by-dimension", verbose=True)["parameter_mechanics"]
    assert any("(timers-metrics)" in e for e in full["multiple_via_repeated_key"])
    # unqualified filter params survive compaction
    assert "country" in mbd["multiple_via_repeated_key"]


def test_scoped_mechanics_keeps_non_query_type_qualifiers() -> None:
    entries = ["country", "metric (timers-metrics)", "timer (not-a-real-slug)"]
    kept = catalog._scoped_mechanics(entries, "metrics-by-dimension")
    assert kept == ["country", "timer (not-a-real-slug)"]


def test_describe_query_omits_empty_fields() -> None:
    from mpulse_mcp import server

    describe = getattr(server.describe_query, "fn", server.describe_query)
    for qt in ("metrics-by-dimension", "summary"):
        out = describe(qt)
        for key in ("notes", "value_parameters"):
            assert key not in out or out[key], f"{qt}: empty '{key}' emitted"
