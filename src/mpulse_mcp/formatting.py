"""Response shaping.

Two modes:

* ``raw=True``  -> the mPulse JSON is returned unchanged under ``data``.
* ``raw=False`` -> a predictable, flat envelope with explicit metadata plus the
  data body. Normalization strips mPulse's presentation envelope and lifts the
  meaningful arrays into compact, explicitly-keyed structures.

**Loss-free is on demand.** Aggregate/period values (``latest``, percentiles,
histogram buckets) are *never* rounded, summarized, or dropped. But the
per-minute *time series* (``timers-metrics.history`` / ``by-minute.aPoints``) is
by default **downsampled** (see ``history_mode``), because the dominant workload
reads only ``latest`` and full per-minute arrays are ~1,440 points/day of mostly
unused tokens. The complete series is always recoverable via ``raw=True`` or
``history_mode='full'``. ``histogram`` buckets (needed for exact p75) and
``summary`` are treated as aggregates and kept in full regardless of mode.

Every result carries an ``empty`` flag and, for drilldowns, a hint that an
empty body may mean the dimension combination is unsupported (mPulse returns no
data rather than an error for those).
"""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .query_types import DRILLDOWN_PARAMS

# --- History (time-series) volume control ----------------------------------
# 'full'       -> keep every per-minute point (loss-free, largest).
# 'downsample' -> keep 'latest' + a <=N-point even-spaced series + peak.
# 'none'       -> drop the series; keep 'latest', endpoints, n_points, peak.
HISTORY_MODES = ("full", "downsample", "none")
DEFAULT_HISTORY_MODE = "downsample"
DOWNSAMPLE_MAX_POINTS = 60

# Only these query-types carry a per-minute time series to reduce. Distribution
# (histogram) and single-value (summary) shapes are always kept whole.
_TEMPORAL_QUERY_TYPES = ("timers-metrics", "by-minute", "dimension-over-time", "dimensions-over-time")


def _period(params: dict[str, Any]) -> dict[str, Any]:
    """Extract the time selection from request params for the metadata block."""
    period: dict[str, Any] = {}
    for key in (
        "date",
        "date-comparator",
        "trailing-seconds",
        "date-start",
        "date-end",
        "timezone",
    ):
        if params.get(key) is not None:
            period[key] = params[key]
    return period


# Rolling windows that end "now", in seconds.
_ROLLING_SECONDS = {
    "Last30Minutes": 1800,
    "LastHour": 3600,
    "Last3Hours": 10800,
    "Last12Hours": 43200,
    "Last24Hours": 86400,
}


def _months_back(t: dt.datetime, months: int) -> dt.datetime:
    """Same day-of-month `months` earlier, clamped to that month's length."""
    years, month0 = divmod(t.month - 1 - months, 12)
    year, month = t.year + years, month0 + 1
    return t.replace(year=year, month=month, day=min(t.day, calendar.monthrange(year, month)[1]))


def resolve_period(
    params: dict[str, Any], now: dt.datetime | None = None
) -> dict[str, Any] | None:
    """Concrete start/end of the time selection, in the request's timezone.

    Relative comparators are easy to misread, so the result states what was
    actually queried. Semantics verified live on 2026-10-29 by matching counts
    against explicit Between ranges: `ThisWeek` starts at 00:00 seven days ago
    and `ThisMonth` at 00:00 on the same day last month, both ending now. Neither
    is a calendar week or month. Windows that end now use the request time.
    Returns ``None`` when the selection or timezone can't be resolved.
    """
    tzname = str(params.get("timezone") or "UTC")
    try:
        tz = ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    now = (now or dt.datetime.now(dt.UTC)).astimezone(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def day(value: Any) -> dt.datetime:
        return dt.datetime.combine(dt.date.fromisoformat(str(value)), dt.time(), tz)

    dc, date = params.get("date-comparator"), params.get("date")
    try:
        if date and not dc:
            start, end, basis = day(date), day(date) + dt.timedelta(days=1), "calendar day"
        elif dc in (None, "Today"):
            start, end, basis = midnight, now, "today so far (also the default when no date is given)"
        elif dc == "ThisWeek":
            start, end = midnight - dt.timedelta(days=7), now
            basis = "00:00 seven days ago until now; not a Monday- or Sunday-based calendar week"
        elif dc == "ThisMonth":
            start, end = _months_back(midnight, 1), now
            basis = "00:00 on the same day last month until now; not the calendar month"
        elif dc in _ROLLING_SECONDS:
            start, end = now - dt.timedelta(seconds=_ROLLING_SECONDS[dc]), now
            basis = "rolling window ending now"
        elif dc == "Last":
            start = now - dt.timedelta(seconds=float(params["trailing-seconds"]))
            end, basis = now, "rolling window ending now"
        elif dc == "Between":
            def boundary(value: Any) -> dt.datetime:
                parsed = dt.datetime.fromisoformat(str(value))
                return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)
            start, end = boundary(params["date-start"]), boundary(params["date-end"])
            basis = "explicit range"
        else:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "start": start.isoformat(timespec="minutes"),
        "end": end.isoformat(timespec="minutes"),
        "end_exclusive": True,
        "timezone": tzname,
        "basis": basis,
    }


def _active_drilldowns(params: dict[str, Any]) -> dict[str, Any]:
    dd = {k: params[k] for k in DRILLDOWN_PARAMS if params.get(k) is not None}
    # custom-dimension-* are dynamic
    dd.update(
        {k: v for k, v in params.items() if k.startswith("custom-dimension-")}
    )
    return dd


def _norm_name(name: str) -> str:
    """Casing/separator-insensitive key: 'largest_contentful_paint' == 'LargestContentfulPaint'."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _returned_series_ids(data: dict[str, Any]) -> list[str]:
    """Series identifiers echoed by mPulse, across the id/name-bearing shapes."""
    ids: list[str] = []
    values = data.get("values")
    if isinstance(values, list):
        ids += [v["id"] for v in values if isinstance(v, dict) and v.get("id")]
    series = data.get("series")
    if isinstance(series, dict) and isinstance(series.get("series"), list):
        ids += [
            s["name"] for s in series["series"] if isinstance(s, dict) and s.get("name")
        ]
    return ids


def _detect_silent_fallback(
    request_params: dict[str, Any], data: dict[str, Any]
) -> str | None:
    """Warn when a requested built-in timer/metric was silently replaced.

    mPulse answers an unrecognized ``timer``/``metric`` with a PageLoad (or
    default) series instead of erroring. We compare the requested name against
    the series id/name echoed in the response; a casing/separator-only
    difference is treated as a match (same metric), so only a genuinely
    different id triggers the warning. Custom timers/dimensions are not checked
    (their echoed id may legitimately differ from the param value).
    """
    returned = _returned_series_ids(data)
    if not returned:  # summary/flat/empty shapes carry no id to compare
        return None
    returned_norm = {_norm_name(r) for r in returned}

    missing: list[str] = []
    for key in ("timer", "metric"):
        requested = request_params.get(key)
        requested_values = requested if isinstance(requested, (list, tuple)) else [requested]
        for value in requested_values:
            if isinstance(value, str) and value and _norm_name(value) not in returned_norm:
                if value not in missing:
                    missing.append(value)
    if not missing:
        return None
    return (
        f"Requested {', '.join(repr(m) for m in missing)} but mPulse returned "
        f"series {returned!r} — likely a SILENT FALLBACK to a default "
        f"(unrecognized timer/metric names are not rejected). Verify the exact "
        f"name via describe_query; the returned data is NOT for the requested "
        f"name."
    )


def _is_empty(query_type: str, data: dict[str, Any]) -> bool:
    """Best-effort emptiness detection across the known response shapes."""
    if not data:
        return True
    # timers-metrics
    if "values" in data and isinstance(data["values"], list):
        vals = data["values"]
        if not vals:
            return True
        series = [v for v in vals if isinstance(v, dict)]
        if not series:  # dimension-values: a plain list of strings
            return False
        return all(not v.get("history") and v.get("latest") is None for v in series)
    # series-based (histogram, by-minute)
    series = data.get("series")
    if isinstance(series, dict):
        inner = series.get("series")
        if isinstance(inner, list):
            if not inner:
                return True
            return all(
                not (s.get("aPoints") or s.get("buckets")) for s in inner if isinstance(s, dict)
            )
    # dimension-over-time: {labelNames, data:{series:[{name, aPoints}]}}
    inner = data.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("series"), list):
        return not any(isinstance(s, dict) and s.get("aPoints") for s in inner["series"])
    # table-shaped (metrics-by-dimension: {columnNames, data:[[...]]};
    # geography: {data:[...]}). An absent/null/empty row list is empty.
    if "columnNames" in data or "data" in data:
        return not data.get("data")
    # summary: all numeric fields absent/zero-count
    if query_type == "summary":
        n = data.get("n")
        return n in (None, "", "0", 0)
    return False


def normalize(
    *,
    app: str,
    query_type: str,
    request_params: dict[str, Any],
    aggregation: str | None,
    data: dict[str, Any],
    raw: bool,
    history_mode: str = DEFAULT_HISTORY_MODE,
    limit: int | None = None,
) -> dict[str, Any]:
    """Produce the tool's return payload.

    ``request_params`` are the wire params actually sent (hyphenated keys).

    ``history_mode`` controls per-minute time-series volume for the temporal
    query-types (``timers-metrics``, ``by-minute``) — see :data:`HISTORY_MODES`.
    It is ignored for ``raw=True`` (untouched) and for non-temporal shapes
    (``summary``/``histogram`` distributions are always kept in full).

    ``limit`` caps the row list in known table/report shapes, falling back to
    the largest top-level list for other shapes, and records a ``truncated``
    note. ``None`` means no cap; ignored for ``raw=True``.
    """
    if history_mode not in HISTORY_MODES:
        history_mode = DEFAULT_HISTORY_MODE

    # Silent-fallback detection is additive metadata (never mutates data), so it
    # applies in raw mode too.
    warning = _detect_silent_fallback(request_params, data)

    if raw:
        out: dict[str, Any] = {
            "app": app,
            "query_type": query_type,
            "raw": True,
            "data": data,
        }
        if warning:
            out["warning"] = warning
        return out

    period = _period(request_params)
    resolved = resolve_period(request_params)
    if resolved:
        period["resolved"] = resolved
    drilldowns = _active_drilldowns(request_params)
    empty = _is_empty(query_type, data)

    body = _apply_history_mode(
        query_type,
        _strip_envelope(query_type, data),
        history_mode,
        request_params.get("timezone"),
    )
    truncation = _apply_limit(body, limit, query_type=query_type)

    envelope: dict[str, Any] = {
        "app": app,
        "query_type": query_type,
        "period": period,
        "timezone": request_params.get("timezone", "UTC"),
        "drilldowns": drilldowns,
        "empty": empty,
        "history_mode": history_mode,
        # 'body' holds the envelope-stripped data (loss-free when
        # history_mode='full' or raw=True).
        "body": body,
    }
    # How to read the numbers (period aggregate vs per-minute series, …);
    # omitted rather than emitting a meaningless placeholder.
    if aggregation:
        envelope["aggregation"] = aggregation
    if warning:
        envelope["warning"] = warning
    if truncation:
        envelope["truncated"] = truncation
    if empty:
        reason, note = _classify_empty(drilldowns)
        envelope["empty_reason"] = reason
        envelope["note"] = note
    return envelope


def _classify_empty(drilldowns: dict[str, Any]) -> tuple[str, str]:
    """Heuristically explain an empty result (cheap, no extra network call).

    mPulse returns *no data* rather than an error for an unsupported dimension
    combination, which is indistinguishable from genuine no-traffic without a
    probe. Single-dimension filters are generally supported, so one drilldown is
    most likely no-traffic; multiple drilldowns raise the chance of an
    unsupported combination. Callers can pass ``probe=true`` for a definitive
    answer (one extra query without the drilldowns).
    """
    n = len(drilldowns)
    if n == 0:
        return "no_data", "Empty result: no data for this query/period."
    if n == 1:
        return (
            "likely_no_traffic",
            ("Empty result with a single drilldown. Single-dimension filters are "
            "generally supported, so this most likely means no matching traffic "
            "— not an unsupported combination. Pass probe=true to confirm."),
        )
    return (
        "possibly_unsupported_combo",
        ("Empty result with multiple drilldowns. mPulse returns no data (not an "
        "error) for unsupported dimension combinations, so this may be an "
        "unsupported combo OR simply no matching traffic. Pass probe=true to "
        "disambiguate, or test the dimensions individually."),
    )


# Count fields seen on high-cardinality rows (verified live: geography rows use
# 'timerN'). When present we keep the TOP rows by volume, not an arbitrary slice.
_COUNT_KEYS = ("timerN", "n", "count", "beacons")


def _row_count(row: dict[str, Any], count_key: str) -> float:
    try:
        return float(row.get(count_key))
    except (TypeError, ValueError):
        return float("-inf")


def _select_limit_key(
    body: dict[str, Any], *, query_type: str | None = None,
) -> str | None:
    """Select rows without mistaking table columns or report metadata for data."""
    if "columnNames" in body or query_type == "metrics-by-dimension":
        return "data" if isinstance(body.get("data"), list) else None
    if query_type == "geography" and "data" in body:
        return "data" if isinstance(body["data"], list) else None

    # Preserve the legacy fallback, including first-key wins for equal lengths.
    # Normalized chart/value responses have a single top-level list: series.
    key = None
    longest = -1
    for k, v in body.items():
        if isinstance(v, list) and len(v) > longest:
            key, longest = k, len(v)
    return key


def _apply_limit(
    body: dict[str, Any], limit: int | None, *, query_type: str | None = None,
) -> dict[str, Any] | None:
    """Truncate the selected top-level row list in ``body`` to ``limit`` items.

    Returns truncation metadata ``{key, total, returned[, sorted_by]}`` when it
    trims something, else ``None``. Mutates ``body`` in place. Non-list bodies
    and ``limit is None`` are no-ops. Tables only limit the outer ``data`` list;
    absent or malformed rows never fall back to another list in the table.

    When the rows are objects carrying a count field (e.g. geography's
    ``timerN``), the rows are sorted by that count **descending** before
    trimming, so the highest-volume rows survive rather than an alphabetical
    prefix. Rows without a count (e.g. dimension-values' plain strings) keep
    their original order.
    """
    if limit is None or limit < 0 or not isinstance(body, dict):
        return None
    key = _select_limit_key(body, query_type=query_type)
    if key is None or len(body[key]) <= limit:
        return None

    rows = body[key]
    total = len(rows)
    sorted_by = None
    if rows and all(isinstance(r, dict) for r in rows):
        count_key = next((ck for ck in _COUNT_KEYS if ck in rows[0]), None)
        if count_key:
            rows = sorted(rows, key=lambda r: _row_count(r, count_key), reverse=True)
            sorted_by = count_key

    body[key] = rows[:limit]
    info: dict[str, Any] = {"by": "client", "key": key, "total": total, "returned": limit}
    if sorted_by:
        info["sorted_by"] = sorted_by
    return info


def _strip_envelope(query_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Lift the meaningful payload out of mPulse's presentation wrapper.

    Loss-free: the full original numbers are preserved; we only remove chart
    chrome (titles, dataset names) into a compact ``meta`` and expose the data
    arrays under stable keys. If a shape is unrecognized, the original ``data``
    is passed through untouched.
    """
    # A table marker takes precedence over chart-like fields in malformed data.
    # summary is already flat {median, moe, n, p95, p98}.
    if "columnNames" in data or query_type == "summary":
        return dict(data)

    # dimension-over-time: {labelNames, data:{series:[{name, aPoints}]}}
    inner = data.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("series"), list):
        meta = {"labelNames": data["labelNames"]} if data.get("labelNames") else {}
        return {"meta": meta, "series": inner["series"]}

    # timers-metrics: {dataTimeZone, values:[{id, history[], latest}]}
    if "values" in data and isinstance(data.get("values"), list):
        return {
            "dataTimeZone": data.get("dataTimeZone"),
            "series": data["values"],  # each: {id, history:[...], latest}
        }

    # series-based: histogram / by-minute
    series = data.get("series")
    if isinstance(series, dict) and isinstance(series.get("series"), list):
        meta = {
            k: data.get(k)
            for k in (
                "chartTitle",
                "chartTitleSuffix",
                "datasetName",
                "reportType",
                "resultName",
            )
            if data.get(k) is not None
        }
        return {"meta": meta, "series": series["series"]}

    # Unknown shape -> pass through unchanged (never drop data).
    return dict(data)


# --- history_mode application ----------------------------------------------
# by-minute point fields that only serve chart tooltips (label, userdata with
# an HTML stringTemplate + full-precision MoE). Dropped in the lossy modes;
# `full`/`raw` keep every field.
_CHART_CHROME_KEYS = (
    "chartTitle",
    "chartTitleSuffix",
    "datasetName",
    "reportType",
    "resultName",
)


def _slim_point(p: Any) -> Any:
    """Reduce a chart point to its data: {x, y}."""
    if not isinstance(p, dict):
        return p
    return {k: p[k] for k in ("x", "y") if k in p}


def _epoch_ms_to_iso(ms: Any, tzname: str | None) -> str | None:
    """Epoch milliseconds -> ISO time in the request timezone, or None."""
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    try:
        tz = ZoneInfo(str(tzname or "UTC"))
        return dt.datetime.fromtimestamp(ms / 1000, tz).isoformat(timespec="minutes")
    except (ZoneInfoNotFoundError, ValueError, OverflowError, OSError):
        return None


def _downsample(seq: list[Any], max_points: int = DOWNSAMPLE_MAX_POINTS) -> list[Any]:
    """Even-spaced downsample preserving the first and last element.

    Returns the input unchanged when it already fits within ``max_points``.
    """
    n = len(seq)
    if n <= max_points:
        return list(seq)
    step = (n - 1) / (max_points - 1)
    idx = sorted({round(i * step) for i in range(max_points)})
    return [seq[i] for i in idx]


def _argmax_numeric(values: list[Any]) -> tuple[int | None, Any]:
    best_i: int | None = None
    best_v: Any = None
    for i, v in enumerate(values):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if best_v is None or v > best_v:
            best_i, best_v = i, v
    return best_i, best_v


def _peak_point(points: list[Any]) -> dict[str, Any] | None:
    """Point with the largest y among {x, y} dicts (spike location)."""
    best: dict[str, Any] | None = None
    for p in points:
        if not isinstance(p, dict):
            continue
        y = p.get("y")
        if isinstance(y, bool) or not isinstance(y, (int, float)):
            continue
        if best is None or y > best.get("y"):
            best = p
    return dict(best) if best is not None else None


def _reduce_tm_series(s: dict[str, Any], mode: str) -> dict[str, Any]:
    """Reduce a timers-metrics series {id, history:[int], latest}."""
    if not isinstance(s, dict):
        return s
    history = s.get("history")
    if not isinstance(history, list) or not history:
        return s
    out = {k: v for k, v in s.items() if k != "history"}  # keep id, latest, …
    out["n_points"] = len(history)
    peak_i, peak_v = _argmax_numeric(history)
    if peak_i is not None:
        out["peak"] = {"index": peak_i, "value": peak_v}
    if mode == "none":
        out["first"] = history[0]
        out["last"] = history[-1]
    else:  # downsample
        out["history_downsampled"] = _downsample(history)
    return out


def _reduce_bm_series(s: dict[str, Any], mode: str, tzname: str | None) -> dict[str, Any]:
    """Reduce a by-minute series {name, aPoints:[{x,y}], statistics, ...}.

    Points are slimmed to ``{x, y}`` (tooltip chrome dropped) and the ``peak``
    gains a human-readable ``time`` in the request timezone — "when did it
    spike?" should not require epoch-millisecond arithmetic.
    """
    if not isinstance(s, dict):
        return s
    pts = s.get("aPoints")
    if not isinstance(pts, list) or not pts:
        return s
    out = {k: v for k, v in s.items() if k != "aPoints"}  # keep statistics, …
    out.setdefault("pointCount", len(pts))
    peak = _peak_point(pts)
    if peak is not None:
        peak = _slim_point(peak)
        iso = _epoch_ms_to_iso(peak.get("x"), tzname)
        if iso:
            peak["time"] = iso
        out["peak"] = peak
    if mode == "none":
        out["first"] = _slim_point(pts[0])
        out["last"] = _slim_point(pts[-1])
    else:  # downsample
        out["aPoints_downsampled"] = [_slim_point(p) for p in _downsample(pts)]
    return out


def _apply_history_mode(
    query_type: str, body: dict[str, Any], mode: str, tzname: str | None = None
) -> dict[str, Any]:
    """Apply the time-series volume policy to a stripped body.

    No-op for ``mode='full'`` and for non-temporal query-types (their aggregate
    data must never be reduced). Always preserves ``latest``/statistics. In the
    lossy modes, chart chrome (titles, dataset names, tooltip userdata) is
    dropped too; meaningful ``meta`` such as ``labelNames`` survives.
    """
    if mode == "full" or query_type not in _TEMPORAL_QUERY_TYPES:
        return body
    series = body.get("series")
    if not isinstance(series, list):
        return body
    body = dict(body)
    if query_type == "timers-metrics":
        body["series"] = [_reduce_tm_series(s, mode) for s in series]
    else:
        body["series"] = [_reduce_bm_series(s, mode, tzname) for s in series]
    meta = body.get("meta")
    if isinstance(meta, dict):
        slim = {k: v for k, v in meta.items() if k not in _CHART_CHROME_KEYS}
        if slim:
            body["meta"] = slim
        else:
            body.pop("meta", None)
    return body
