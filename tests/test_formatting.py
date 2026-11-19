"""Lossless normalization, raw passthrough, empty detection, input validation."""

from __future__ import annotations

from copy import deepcopy

import pytest

from mpulse_mcp.errors import ValidationError
from mpulse_mcp.formatting import (
    _apply_limit,
    _classify_empty,
    _detect_silent_fallback,
    normalize,
)
from mpulse_mcp.server import _check_single_day, _validate_and_build_time


# --- limit / truncation ----------------------------------------------------
def test_apply_limit_truncates_longest_list() -> None:
    body = {"meta": {"x": 1}, "rows": list(range(200))}
    info = _apply_limit(body, 50)
    assert info == {"by": "client", "key": "rows", "total": 200, "returned": 50}
    assert body["rows"] == list(range(50))
    assert body["meta"] == {"x": 1}  # untouched


def test_apply_limit_sorts_by_count_before_truncating() -> None:
    # geography-shaped rows (verified live): {country, timerN, ...}. Low-volume
    # rows come first in input; the top-by-volume must survive the cap.
    rows = [{"country": f"C{i}", "timerN": str(i)} for i in range(150)]  # ascending
    metadata = list(range(200))
    out = normalize(
        app="alpha", query_type="geography", request_params={}, aggregation=None,
        data={"data": rows, "metadata": metadata}, raw=False, limit=10,
    )
    body, info = out["body"], out["truncated"]
    assert info == {"by": "client", "key": "data", "total": 150, "returned": 10, "sorted_by": "timerN"}
    kept = body["data"]
    assert len(kept) == 10
    # highest timerN (149) is first; smallest kept is 140 — none of the tiny ones.
    assert kept[0]["timerN"] == "149"
    assert all(int(r["timerN"]) >= 140 for r in kept)
    assert body["metadata"] == metadata


@pytest.mark.parametrize("row_data", [
    {"data": [["US", 1, 1200]]},
    {"data": [["US", 1], ["KR", 2, 900], ["JP", 3, 800]]},
    {"data": []}, {"data": None}, {}, {"data": "unexpected"},
    {"data": {"series": [{"name": "unexpected", "aPoints": []}]}},
])
def test_table_limit_preserves_columns_and_row_values(row_data) -> None:
    payload = {"columnNames": ["Country", "Beacons", "LCP"], **row_data}
    original = deepcopy(payload)
    out = normalize(
        app="alpha", query_type="metrics-by-dimension", request_params={},
        aggregation=None, data=payload, raw=False, limit=1,
    )
    expected = deepcopy(original)
    if isinstance(expected.get("data"), list):
        expected["data"] = expected["data"][:1]
    assert out["body"] == expected
    assert payload == original
    if isinstance(row_data.get("data"), list) and len(row_data["data"]) > 1:
        assert out["truncated"] == {
            "by": "client", "key": "data", "total": len(row_data["data"]), "returned": 1,
        }
    else:
        assert "truncated" not in out


@pytest.mark.parametrize("query_type,payload", [
    ("dimension-values", {"values": ["US", "KR"]}),
    ("timers-metrics", {"values": [
        {"id": name, "history": [1, 2, 3], "latest": 3} for name in ("A", "B")
    ]}),
    ("dimension-over-time", {"labelNames": ["A", "B"], "data": {"series": [
        {"name": name, "aPoints": [{"x": i, "y": i} for i in range(3)]}
        for name in ("A", "B")
    ]}}),
    ("histogram", {"series": {"series": [
        {"name": name, "buckets": [{"s": 0, "e": 1, "c": 3}]}
        for name in ("A", "B")
    ]}}),
    ("by-minute", {"series": {"series": [
        {"name": name, "aPoints": [{"x": i, "y": i} for i in range(3)]}
        for name in ("A", "B")
    ]}}),
])
def test_non_table_limit_caps_series_not_inner_values(query_type, payload) -> None:
    original = deepcopy(payload)
    full = normalize(
        app="alpha", query_type=query_type, request_params={}, aggregation=None,
        data=payload, raw=False, history_mode="full",
    )
    out = normalize(
        app="alpha", query_type=query_type, request_params={}, aggregation=None,
        data=payload, raw=False, history_mode="full", limit=1,
    )
    assert out["body"] == {**full["body"], "series": full["body"]["series"][:1]}
    assert out["truncated"] == {"by": "client", "key": "series", "total": 2, "returned": 1}
    assert payload == original


def test_unknown_response_keeps_largest_list_fallback() -> None:
    out = normalize(
        app="alpha", query_type="future-type", request_params={}, aggregation=None,
        data={"data": [1, 2], "labels": ["a", "b", "c"]}, raw=False, limit=1,
    )
    assert out["body"] == {"data": [1, 2], "labels": ["a"]}
    assert out["truncated"]["key"] == "labels"


def test_apply_limit_no_count_keeps_order() -> None:
    # dimension-values shape: plain strings, no volume -> alphabetical prefix.
    body = {"values": [f"Chrome/{i}" for i in range(150)]}
    info = _apply_limit(body, 10)
    assert "sorted_by" not in info
    assert body["values"] == [f"Chrome/{i}" for i in range(10)]


def test_apply_limit_noop_when_under_limit_or_none() -> None:
    body = {"rows": [1, 2, 3]}
    assert _apply_limit(body, 50) is None
    assert _apply_limit({"rows": [1, 2, 3]}, None) is None


def test_normalize_reports_truncation() -> None:
    data = {"countries": [{"c": i} for i in range(150)]}
    out = normalize(
        app="alpha",
        query_type="geography",
        request_params={"date": "2026-08-03"},
        aggregation="unspecified",
        data=data,
        raw=False,
        limit=100,
    )
    assert out["truncated"] == {"by": "client", "key": "countries", "total": 150, "returned": 100}
    assert len(out["body"]["countries"]) == 100


def test_limit_ignored_in_raw_mode() -> None:
    data = {"countries": list(range(150))}
    out = normalize(
        app="alpha",
        query_type="geography",
        request_params={},
        aggregation="unspecified",
        data=data,
        raw=True,
        limit=10,
    )
    assert out["data"]["countries"] == list(range(150))  # raw untouched


# --- empty_reason heuristic ------------------------------------------------
def test_classify_empty_levels() -> None:
    assert _classify_empty({})[0] == "no_data"
    assert _classify_empty({"browser": "X"})[0] == "likely_no_traffic"
    assert _classify_empty({"browser": "X", "ab-test": "Y"})[0] == (
        "possibly_unsupported_combo"
    )


def test_normalize_sets_empty_reason() -> None:
    out = normalize(
        app="alpha",
        query_type="timers-metrics",
        request_params={"date-comparator": "LastHour", "browser": "X", "ab-test": "Y"},
        aggregation="per-minute",
        data={"values": []},
        raw=False,
    )
    assert out["empty"] is True
    assert out["empty_reason"] == "possibly_unsupported_combo"


# --- silent-fallback detection ---------------------------------------------
def test_fallback_warns_when_returned_id_differs() -> None:
    # Requested a (typo) timer; mPulse silently answered with PageLoad.
    data = {"series": {"series": [{"name": "PageLoad", "aPoints": [{"x": 0, "y": 1}]}]}}
    out = normalize(
        app="alpha",
        query_type="by-minute",
        request_params={"timer": "LargestContentofulPaint", "date": "2026-08-03"},
        aggregation="per-minute",
        data=data,
        raw=False,
    )
    assert "warning" in out
    assert "SILENT FALLBACK" in out["warning"]


def test_no_fallback_warning_on_casing_or_separator_difference() -> None:
    # snake_case request vs CamelCase returned id = SAME metric, must not warn.
    data = {"values": [{"id": "LargestContentfulPaint", "history": [1], "latest": 1}]}
    assert (
        _detect_silent_fallback(
            {"metric": "largest_contentful_paint"}, data
        )
        is None
    )


def test_no_fallback_warning_when_id_matches() -> None:
    data = {"values": [{"id": "PageLoad", "history": [1], "latest": 1}]}
    assert _detect_silent_fallback({"timer": "PageLoad"}, data) is None


def test_no_fallback_warning_for_summary_shape() -> None:
    # Flat summary carries no series id -> cannot (and must not) warn.
    data = {"median": "1", "n": "5"}
    assert _detect_silent_fallback({"timer": "PageLoad"}, data) is None


def test_fallback_warning_present_in_raw_mode() -> None:
    data = {"values": [{"id": "PageLoad", "history": [1], "latest": 1}]}
    out = normalize(
        app="alpha",
        query_type="timers-metrics",
        request_params={"metric": "not_a_real_metric"},
        aggregation="per-minute",
        data=data,
        raw=True,
    )
    assert out["raw"] is True
    assert out["data"] == data  # data still untouched
    assert "warning" in out


# --- normalization ---------------------------------------------------------
def test_summary_values_preserved_exactly() -> None:
    data = {"median": "1234", "moe": "12.5", "n": "98765", "p95": "4096", "p98": "5120"}
    out = normalize(
        app="alpha",
        query_type="summary",
        request_params={"date": "2026-08-03", "timezone": "UTC"},
        aggregation="single-value",
        data=data,
        raw=False,
    )
    assert out["app"] == "alpha"
    assert out["query_type"] == "summary"
    resolved = out["period"].pop("resolved")
    assert out["period"] == {"date": "2026-08-03", "timezone": "UTC"}
    assert (resolved["start"], resolved["end"]) == ("2026-08-03T00:00+00:00", "2026-08-04T00:00+00:00")
    assert out["empty"] is False
    # Values are byte-for-byte the originals — no rounding/summarizing.
    assert out["body"] == data


def test_timers_metrics_history_preserved_in_full_mode() -> None:
    history = list(range(1440))  # a full day, per-minute
    data = {
        "dataTimeZone": "UTC",
        "values": [{"id": "PageLoad", "history": history, "latest": 1439}],
    }
    out = normalize(
        app="alpha",
        query_type="timers-metrics",
        request_params={"date-comparator": "Last24Hours"},
        aggregation="per-minute",
        data=data,
        raw=False,
        history_mode="full",
    )
    # full mode is loss-free: every point preserved, byte-for-byte.
    assert out["body"]["series"][0]["history"] == history
    assert out["history_mode"] == "full"
    assert out["empty"] is False


def test_series_envelope_stripped_but_points_kept_in_full_mode() -> None:
    data = {
        "chartTitle": "PageLoad",
        "datasetName": "ds",
        "series": {"series": [{"name": "PageLoad", "aPoints": [{"x": 0, "y": 100}]}]},
    }
    out = normalize(
        app="alpha",
        query_type="by-minute",
        request_params={"date": "2026-08-03"},
        aggregation="per-minute",
        data=data,
        raw=False,
        history_mode="full",
    )
    assert out["body"]["meta"]["chartTitle"] == "PageLoad"
    assert out["body"]["series"][0]["aPoints"] == [{"x": 0, "y": 100}]


# --- history_mode ----------------------------------------------------------
def _tm_day(peak_at: int = 800, peak_val: int = 9999) -> dict:
    history = list(range(1440))
    history[peak_at] = peak_val
    return {
        "dataTimeZone": "UTC",
        "values": [{"id": "PageLoad", "history": history, "latest": 3441}],
    }


def test_downsample_is_default_and_shrinks_history() -> None:
    out = normalize(
        app="alpha",
        query_type="timers-metrics",
        request_params={"date-comparator": "Last24Hours"},
        aggregation="per-minute",
        data=_tm_day(),
        raw=False,  # no history_mode -> default
    )
    s = out["body"]["series"][0]
    assert out["history_mode"] == "downsample"
    assert s["latest"] == 3441  # aggregate always preserved
    assert s["n_points"] == 1440
    assert "history" not in s
    assert len(s["history_downsampled"]) <= 60
    assert s["peak"] == {"index": 800, "value": 9999}  # spike location kept


def test_none_mode_drops_series_keeps_aggregates() -> None:
    out = normalize(
        app="alpha",
        query_type="timers-metrics",
        request_params={"date-comparator": "Last24Hours"},
        aggregation="per-minute",
        data=_tm_day(),
        raw=False,
        history_mode="none",
    )
    s = out["body"]["series"][0]
    assert "history" not in s and "history_downsampled" not in s
    assert s["latest"] == 3441
    assert s["n_points"] == 1440
    assert s["first"] == 0 and s["last"] == 1439
    assert s["peak"]["value"] == 9999


def test_by_minute_downsample_keeps_statistics_and_peak() -> None:
    pts = [{"x": i, "y": i} for i in range(1440)]
    pts[500]["y"] = 8888
    data = {
        "series": {
            "series": [
                {"name": "PageLoad", "aPoints": pts, "pointCount": 1440,
                 "statistics": {"yMin": 0, "yMax": 8888, "ySum": 1, "yAvg": 1}}
            ]
        }
    }
    out = normalize(
        app="alpha",
        query_type="by-minute",
        request_params={"date": "2026-08-03"},
        aggregation="per-minute",
        data=data,
        raw=False,
    )
    s = out["body"]["series"][0]
    assert "aPoints" not in s
    assert len(s["aPoints_downsampled"]) <= 60
    assert s["statistics"]["yMax"] == 8888  # mPulse aggregates untouched
    assert s["peak"]["y"] == 8888


def test_histogram_never_reduced_even_at_none() -> None:
    # histogram buckets are a distribution (needed for exact p75), not a time
    # series -> must survive any history_mode intact.
    buckets = [{"s": i, "e": i + 1, "c": i} for i in range(200)]
    data = {"series": {"series": [{"name": "PageLoad", "aPoints": buckets}]}}
    out = normalize(
        app="alpha",
        query_type="histogram",
        request_params={"date": "2026-08-03"},
        aggregation="per-bucket",
        data=data,
        raw=False,
        history_mode="none",
    )
    assert out["body"]["series"][0]["aPoints"] == buckets


def test_raw_passthrough_is_untouched() -> None:
    data = {"anything": {"nested": [1, 2, 3]}, "median": "1"}
    out = normalize(
        app="alpha",
        query_type="summary",
        request_params={"date": "2026-08-03"},
        aggregation="single-value",
        data=data,
        raw=True,
    )
    assert out == {"app": "alpha", "query_type": "summary", "raw": True, "data": data}


def test_empty_drilldown_gets_note() -> None:
    data = {"values": []}
    out = normalize(
        app="alpha",
        query_type="timers-metrics",
        request_params={"date-comparator": "LastHour", "ab-test": "X", "browser": "Y"},
        aggregation="per-minute",
        data=data,
        raw=False,
    )
    assert out["empty"] is True
    assert "drilldown" in out["note"].lower()
    assert out["drilldowns"] == {"browser": "Y", "ab-test": "X"}


def test_unknown_shape_passthrough() -> None:
    data = {"weird": [1, 2, 3]}
    out = normalize(
        app="alpha",
        query_type="geography",
        request_params={"date": "2026-08-03"},
        aggregation="unspecified",
        data=data,
        raw=False,
    )
    assert out["body"] == data  # never dropped


# --- input validation ------------------------------------------------------
def test_date_and_comparator_mutually_exclusive() -> None:
    with pytest.raises(ValidationError):
        _validate_and_build_time("2026-08-03", "Last24Hours", None)


def test_one_time_selection_required() -> None:
    with pytest.raises(ValidationError):
        _validate_and_build_time(None, None, None)


def test_valid_date_builds_params() -> None:
    params = _validate_and_build_time("2026-08-03", None, "America/New_York")
    assert params == {"date": "2026-08-03", "timezone": "America/New_York"}


def test_comparator_builds_wire_name() -> None:
    params = _validate_and_build_time(None, "Last24Hours", None)
    assert params == {"date-comparator": "Last24Hours"}


@pytest.mark.parametrize("bad", ["2026-08-01..2026-08-03", "2026/08/03", "not-a-date"])
def test_range_or_bad_date_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        _check_single_day(bad)


def test_single_day_ok() -> None:
    _check_single_day("2026-08-03")  # no raise


# --- table-shaped emptiness (fix 2) -----------------------------------------
def test_table_shape_empty_is_flagged() -> None:
    for payload in (
        {"columnNames": ["Branch", None], "data": None},
        {"columnNames": ["Branch", "Beacons"], "data": []},
    ):
        out = normalize(
            app="a",
            query_type="metrics-by-dimension",
            request_params={"dimension": "branch", "custom-dimension-branch": "zz"},
            aggregation="unspecified",
            data=payload,
            raw=False,
        )
        assert out["empty"] is True
        assert out["empty_reason"] == "likely_no_traffic"  # one drilldown


def test_table_shape_with_rows_is_not_empty() -> None:
    out = normalize(
        app="a",
        query_type="metrics-by-dimension",
        request_params={"dimension": "branch"},
        aggregation="unspecified",
        data={"columnNames": ["Branch", "Beacons"], "data": [["uk", 5]]},
        raw=False,
    )
    assert out["empty"] is False
    assert "empty_reason" not in out


def test_dimension_values_with_data_is_not_empty() -> None:
    # dimension-values returns {"values": [str, ...]} (verified live); a
    # non-empty string list must not be flagged empty.
    out = normalize(
        app="a",
        query_type="dimension-values",
        request_params={"dimension": "browser"},
        aggregation="unspecified",
        data={"values": ["Chrome/120", "Edge/119"]},
        raw=False,
    )
    assert out["empty"] is False
    assert "empty_reason" not in out


# --- dimension-over-time (live shape: {labelNames, data:{series:[...]}}) ----
def _dot_payload(points: int) -> dict:
    pts = [{"x": i, "y": i, "label": "", "userdata": "{}"} for i in range(points)]
    if pts:
        pts[700]["y"] = 99999
    return {"labelNames": ["India"], "data": {"series": [{"name": "India", "aPoints": pts}]}}


def test_dimension_over_time_is_downsampled() -> None:
    out = normalize(app="a", query_type="dimension-over-time",
                    request_params={"dimension": "country"}, aggregation="",
                    data=_dot_payload(1440), raw=False)
    s = out["body"]["series"][0]
    assert out["body"]["meta"] == {"labelNames": ["India"]}
    assert "aPoints" not in s and len(s["aPoints_downsampled"]) <= 60
    assert s["peak"]["y"] == 99999
    assert out["empty"] is False


def test_dimension_over_time_empty_series_is_empty() -> None:
    out = normalize(app="a", query_type="dimension-over-time",
                    request_params={"dimension": "country"}, aggregation="",
                    data={"labelNames": [], "data": {"series": []}}, raw=False)
    assert out["empty"] is True


# --- period.resolved (semantics verified live 2026-10-29) --------------------
import datetime as _dt

from mpulse_mcp.formatting import resolve_period

# 2026-10-30 00:30 in Seoul
_NOW = _dt.datetime(2026, 10, 29, 15, 30, tzinfo=_dt.UTC)


def _range(params: dict) -> tuple[str, str]:
    r = resolve_period(params, now=_NOW)
    return r["start"], r["end"]


def test_this_week_is_seven_days_back_from_midnight() -> None:
    assert _range({"date-comparator": "ThisWeek", "timezone": "Asia/Seoul"}) == (
        "2026-10-23T00:00+09:00", "2026-10-30T00:30+09:00")


def test_this_month_starts_same_day_last_month() -> None:
    assert _range({"date-comparator": "ThisMonth", "timezone": "Asia/Seoul"}) == (
        "2026-09-30T00:00+09:00", "2026-10-30T00:30+09:00")
    # clamps to the shorter month: Mar 31 -> Feb 28
    mar31 = _dt.datetime(2027, 3, 31, 12, 0, tzinfo=_dt.UTC)
    r = resolve_period({"date-comparator": "ThisMonth"}, now=mar31)
    assert r["start"] == "2027-02-28T00:00+00:00"


def test_rolling_between_date_and_default() -> None:
    assert _range({"date-comparator": "LastHour"}) == ("2026-10-29T14:30+00:00", "2026-10-29T15:30+00:00")
    assert _range({"date-comparator": "Last", "trailing-seconds": 600}) == (
        "2026-10-29T15:20+00:00", "2026-10-29T15:30+00:00")
    assert _range({"date-comparator": "Between", "date-start": "2026-06-01",
                   "date-end": "2026-07-01", "timezone": "Asia/Seoul"}) == (
        "2026-06-01T00:00+09:00", "2026-07-01T00:00+09:00")
    assert _range({}) == ("2026-10-29T00:00+00:00", "2026-10-29T15:30+00:00")  # Today


def test_unresolvable_period_is_omitted() -> None:
    assert resolve_period({"date-comparator": "ThisWeek", "timezone": "Not/AZone"}) is None
    assert resolve_period({"date-comparator": "Between", "date-start": "2026-06-01"}) is None


def test_downsample_slims_points_and_resolves_peak_time() -> None:
    # Live by-minute points carry chart-tooltip chrome (label, userdata with an
    # HTML template + MoE) that made up ~56% of the payload. The lossy modes
    # drop it and the peak gains a human-readable time in the request timezone.
    base_ms = 1793458800000  # 2026-11-01 00:00 KST
    pts = [
        {"x": base_ms + i * 60000, "y": i, "label": "", "userdata": '{"name":"MoE"}'}
        for i in range(1440)
    ]
    pts[847]["y"] = 8888  # 14:07 KST
    data = {
        "chartTitle": "Time in ms",
        "resultName": "null",
        "series": {"series": [{"name": "PageLoad", "aPoints": pts}]},
    }
    out = normalize(
        app="alpha",
        query_type="by-minute",
        request_params={"date": "2026-11-01", "timezone": "Asia/Seoul"},
        aggregation="per-minute",
        data=data,
        raw=False,
    )
    s = out["body"]["series"][0]
    assert all(set(p) == {"x", "y"} for p in s["aPoints_downsampled"])
    assert s["peak"] == {"x": base_ms + 847 * 60000, "y": 8888,
                         "time": "2026-11-01T14:07+09:00"}
    assert "meta" not in out["body"]  # chart chrome gone with it


def test_full_mode_keeps_point_chrome_and_meta() -> None:
    pts = [{"x": 0, "y": 1, "label": "", "userdata": '{"name":"MoE"}'}]
    data = {"chartTitle": "Time in ms",
            "series": {"series": [{"name": "PageLoad", "aPoints": pts}]}}
    out = normalize(
        app="alpha",
        query_type="by-minute",
        request_params={"date": "2026-11-01"},
        aggregation="per-minute",
        data=data,
        raw=False,
        history_mode="full",
    )
    assert out["body"]["series"][0]["aPoints"] == pts  # loss-free
    assert out["body"]["meta"]["chartTitle"] == "Time in ms"


def test_lossy_mode_keeps_meaningful_meta() -> None:
    # dimension-over-time labelNames are data, not chrome -> must survive.
    out = normalize(
        app="alpha",
        query_type="dimension-over-time",
        request_params={"date": "2026-11-01"},
        aggregation="per-minute",
        data=_dot_payload(1440),
        raw=False,
    )
    assert out["body"]["meta"] == {"labelNames": ["India"]}
