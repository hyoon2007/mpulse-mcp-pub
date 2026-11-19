"""FastMCP server exposing mPulse Query API tools over stdio.

Entry point: the ``mpulse-mcp`` console script → :func:`main`.

stdio safety: this module never prints to stdout. FastMCP owns the stdout
JSON-RPC stream; all logging goes to stderr (see ``mpulse_mcp.log``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from . import __version__, log
from .client import MpulseClient
from .config import AppConfig, Registry, load_registry
from .errors import MpulseError, QueryError, ValidationError
from .formatting import HISTORY_MODES

# Exposed as an enum in the tool input schema.
HistoryMode = Literal["downsample", "none", "full"]
FilterValue = str | list[str]
from .query_types import DRILLDOWN_PARAMS, KNOWN_DATE_COMPARATORS, QUERY_TYPES, QUERY_CONSTRAINTS
from .query_types import describe as describe_qt

mcp = FastMCP("mpulse")

# Every tool is registered with `structured_output=False`. With a dict[str, Any]
# return type FastMCP would otherwise emit each result twice — pretty-printed
# TextContent plus structuredContent (the MCP spec mandates a text mirror of
# structured output) — and LLM hosts feed both blocks to the model, roughly
# doubling the tokens per call, while the dict[str, Any] output schema validates
# nothing. If you consume this server from scripts/automation and want
# machine-readable structuredContent (and ideally real output models), register
# the tools with `structured_output=True` and accept the duplicated payload.

# --- Lazily-initialized singletons -----------------------------------------
_registry: Registry | None = None
_client: MpulseClient | None = None


def _get_registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = load_registry()
    return _registry


def _get_client() -> MpulseClient:
    global _client
    if _client is None:
        _client = MpulseClient(_get_registry())
    return _client


# --- Shared argument handling ----------------------------------------------
# Map friendly (underscore) tool argument names to mPulse wire (hyphen) params.
_DRILLDOWN_ARG_TO_WIRE = {
    "page_group": "page-group",
    "browser": "browser",
    "browser_family": "browser-family",
    "ab_test": "ab-test",
    "country": "country",
    "region": "region",
    "device_type": "device-type",
    "device_model": "device-model",
    "device_manufacturer": "device-manufacturer",
    "os": "os",
    "os_family": "os-family",
    "connection_type": "connection-type",
    "isp": "isp",
    "network_error_code": "network-error-code",
    "bandwidth_block": "bandwith-block",  # API misspells 'bandwidth' (verified live)
    "beacon_type": "beacon-type",
    "site_version": "site-version",
}


def _validate_and_build_time(
    date: str | None, date_comparator: str | None, timezone: str | None,
    date_start: str | None = None, date_end: str | None = None,
    trailing_seconds: int | float | None = None,
) -> dict[str, Any]:
    """Validate the mutually-exclusive time selection and build wire params."""
    if date and date_comparator:
        raise ValidationError(
            "Provide either 'date' or 'date_comparator', not both.",
            hint="mPulse queries cover a single time selection.",
        )
    if not date and not date_comparator:
        raise ValidationError(
            "One of 'date' or 'date_comparator' is required.",
            hint="e.g. date='2026-08-03' or date_comparator='Last24Hours'.",
        )

    _validate_breakdown_period(date=date, date_comparator=date_comparator,
                               date_start=date_start, date_end=date_end,
                               trailing_seconds=trailing_seconds)

    params: dict[str, Any] = {}
    if date:
        _check_single_day(date)
        params["date"] = date
    if date_comparator:
        _check_date_comparator(date_comparator)
        params["date-comparator"] = date_comparator
    if timezone:
        params["timezone"] = timezone
    if date_start is not None:
        params["date-start"] = date_start
    if date_end is not None:
        params["date-end"] = date_end
    if trailing_seconds is not None:
        params["trailing-seconds"] = trailing_seconds
    return params


def _check_date_comparator(dc: str) -> None:
    """Reject a comparator mPulse doesn't support before calling it.

    An unsupported value makes mPulse return HTTP 500, which the client would
    retry five times, so it is caught here instead.
    """
    if dc not in KNOWN_DATE_COMPARATORS:
        raise ValidationError(
            f"Unsupported date_comparator {dc!r}.",
            hint=(
                f"Use one of: {', '.join(KNOWN_DATE_COMPARATORS)}. For yesterday, "
                "pass date='YYYY-MM-DD'; for a multi-day range, use 'Between' with "
                "date_start/date_end (date_end is exclusive)."
            ),
        )


def _check_single_day(date: str) -> None:
    """Enforce a single calendar day in YYYY-MM-DD form.

    Range syntax in ``date`` is rejected; use Between with explicit boundaries
    for one period aggregate, or separate calls for a daily comparison.
    """
    if ".." in date or "," in date or "/" in date:
        raise ValidationError(
            f"'{date}' looks like a range. The date parameter selects one calendar day.",
            hint=(
                "Use a single YYYY-MM-DD date or Between with date_start/date_end."
            ),
        )
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        raise ValidationError(
            f"'{date}' is not a valid date. Use YYYY-MM-DD (a single calendar "
            "day).",
        ) from None


def _parse_breakdown_boundary(value: str, field: str) -> dt.datetime:
    """Parse for comparison only; preserve the caller's original wire value."""
    if not isinstance(value, str):
        raise ValidationError(f"'{field}' must be an ISO date or timestamp string.")
    try:
        _check_single_day(value)
    except ValidationError:
        # The endpoint docs also show ISO timestamps for Between boundaries.
        try:
            return dt.datetime.fromisoformat(value)
        except ValueError:
            raise ValidationError(
                f"'{field}' must be a valid ISO date or timestamp, got {value!r}."
            ) from None
    return dt.datetime.combine(dt.date.fromisoformat(value), dt.time())


def _validate_breakdown_period(
    *,
    date: str | None = None,
    date_comparator: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    trailing_seconds: int | float | None = None,
) -> None:
    """Validate a time selection without inventing API timezone defaults.

    Shared by dedicated tools, known generic endpoints, and aggregate periods.
    Omitting the time selection remains valid for tools using the API default.
    """
    if date is not None:
        if not isinstance(date, str):
            raise ValidationError("'date' must be a YYYY-MM-DD string.")
        _check_single_day(date)
    if date is not None and date_comparator is not None:
        raise ValidationError("Provide either 'date' or 'date-comparator', not both.")
    if date_comparator is not None:
        _check_date_comparator(date_comparator)

    if date_comparator != "Between" and (date_start is not None or date_end is not None):
        raise ValidationError("'date_start' and 'date_end' require date_comparator='Between'.")
    if date_comparator != "Last" and trailing_seconds is not None:
        raise ValidationError("'trailing_seconds' requires date_comparator='Last'.")

    if date_comparator == "Between":
        if not date_start or not date_end:
            raise ValidationError(
                "date_comparator='Between' requires 'date_start' and 'date_end' "
                "(date_end is exclusive)."
            )
        start = _parse_breakdown_boundary(date_start, "date_start")
        end = _parse_breakdown_boundary(date_end, "date_end")
        # Do not invent timezone semantics for mixed offset-aware/naive inputs.
        # The API accepts Java timezone names, not necessarily ZoneInfo names.
        comparable = (start.utcoffset() is None) == (end.utcoffset() is None)
        if comparable and end <= start:
            raise ValidationError("'date_end' must be greater than 'date_start' (exclusive end).")
    elif date_comparator == "Last":
        if trailing_seconds is None:
            raise ValidationError("date_comparator='Last' requires 'trailing_seconds'.")
        if (isinstance(trailing_seconds, bool)
                or not isinstance(trailing_seconds, (int, float))
                or not math.isfinite(trailing_seconds)):
            raise ValidationError("'trailing_seconds' must be a finite number.")
        # The API documents a number, without an additional positivity bound.


def _validate_percentile(value: Any) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 99
    ):
        raise ValidationError("percentile must be an integer from 1 to 99.")


def _validate_parameter_shapes(query_type: str, params: dict[str, Any]) -> None:
    """Validate cardinality for known parameters; preserve unknown API params."""
    meta = describe_qt(query_type)
    if meta is None:
        return
    if not meta.supports_drilldown:
        filters = [k for k in params if k in DRILLDOWN_PARAMS
                   or k.startswith("custom-dimension-")]
        if filters:
            raise ValidationError(f"{query_type} does not support drilldown filters: {', '.join(filters)}.")
    _validate_percentile(params.get("percentile"))
    known = set(meta.all_params())
    for key, value in params.items():
        if not isinstance(value, (list, tuple)):
            continue
        repeated = (key in DRILLDOWN_PARAMS or key.startswith("custom-dimension-")
                    or query_type == "timers-metrics" and key in ("timer", "metric"))
        if repeated:
            if not value or not all(isinstance(v, str) for v in value):
                raise ValidationError(f"'{key}' must be a non-empty list of strings.")
        elif key in known:
            raise ValidationError(f"'{key}' accepts a single value for {query_type}.")


def _collect_drilldowns(kwargs: dict[str, Any]) -> dict[str, Any]:
    wire: dict[str, Any] = {}
    for arg, value in kwargs.items():
        if value is None:
            continue
        wire_name = _DRILLDOWN_ARG_TO_WIRE.get(arg)
        if wire_name:
            wire[wire_name] = value
    return wire


def _check_history_mode(mode: str) -> None:
    """Reject an unknown history_mode instead of silently downsampling."""
    if mode not in HISTORY_MODES:
        raise ValidationError(
            f"Invalid history_mode {mode!r}.",
            hint=f"Allowed values: {', '.join(HISTORY_MODES)}.",
        )


def _prepare_metrics_by_dimension(query_type: str, wire: dict[str, Any]) -> None:
    """Guard the metrics-by-dimension metric parameter before the API call.

    mPulse takes the PLURAL, comma-separated ``metrics``. The singular ``metric``
    is silently ignored and returns the app's default columns, so it is rejected.
    A list is joined into the documented comma-separated form, because repeated
    ``metrics=`` keys are not the documented mechanism.
    """
    if query_type != "metrics-by-dimension":
        return
    if "metric" in wire:
        raise ValidationError(
            "metrics-by-dimension takes the PLURAL parameter 'metrics' "
            "(comma-separated). The singular 'metric' is silently ignored by "
            "mPulse and returns the app's default columns.",
            hint="Use metrics='beacons,largest_contentful_paint'.",
        )
    metrics = wire.get("metrics")
    if isinstance(metrics, (list, tuple)):
        wire["metrics"] = ",".join(str(m) for m in metrics)


def _enrich_invalid_metric_error(err: MpulseError, wire: dict[str, Any]) -> None:
    """Attach did-you-mean suggestions to mPulse's 'Invalid metric' 400.

    metrics-by-dimension validates its (endpoint-specific, snake_case) metric
    names itself, but the bare 400 names no alternative, sending the model back
    to a full describe_query. Resolve each requested name against the catalog
    enum and put close candidates in the hint. Validation stays with mPulse —
    the server never pre-rejects, so catalog gaps can't block a valid name.
    """
    if "invalid metric" not in err.message.lower():
        return
    from .catalog import resolve_value

    suggestions: list[str] = []
    for name in str(wire.get("metrics", "")).split(","):
        name = name.strip()
        if not name:
            continue
        status, payload = resolve_value("metric", name, "metrics-by-dimension")
        if status == "unknown" and payload:
            suggestions.append(f"{name!r} -> did you mean {' or '.join(map(repr, payload))}?")
    if suggestions:
        err.hint = (
            "metrics-by-dimension uses its OWN snake_case metric names "
            "(not the timers-metrics CamelCase space). " + " ".join(suggestions)
        )


async def _run(
    *,
    app: str | None,
    query_type: str,
    value_params: dict[str, Any],
    date: str | None,
    date_comparator: str | None,
    timezone: str | None,
    drilldowns: dict[str, Any],
    raw: bool,
    history_mode: str = "downsample",
    probe: bool = False,
    date_start: str | None = None,
    date_end: str | None = None,
    trailing_seconds: int | float | None = None,
    custom_dimension_filters: dict[str, FilterValue] | None = None,
) -> dict[str, Any]:
    """Shared execution path for the explicit tools.

    The envelope's ``aggregation`` (how to read the returned numbers) comes
    from :class:`QueryTypeMeta` — single source, no per-call-site literals.
    Validation and upstream failures are raised as :class:`MpulseError`. FastMCP
    reports them to the client as MCP tool errors (``isError: true``), and the
    message includes the hint. Partial failures (per-cell in ``get_aggregate``)
    are returned as data instead.
    """
    from .formatting import normalize  # local import keeps import graph light

    client = _get_client()
    app_name = app or client.registry.default_app
    corrections: list[str] = []
    _check_history_mode(history_mode)
    time_params = _validate_and_build_time(date, date_comparator, timezone,
                                         date_start, date_end, trailing_seconds)
    params: dict[str, Any] = {}
    params.update({k: v for k, v in value_params.items() if v is not None})
    params.update(time_params)
    params.update(drilldowns)
    if custom_dimension_filters:
        params.update({f"custom-dimension-{k}": v for k, v in custom_dimension_filters.items()})
    dimension_notes = _normalize_custom_dimension_params(
        query_type, params, client.registry.get(app)
    )
    _validate_parameter_shapes(query_type, params)
    corrections = _resolve_value_names(query_type, params)
    data = await client.query(app=app, query_type=query_type, params=params)

    # Re-add format for metadata parity (client injects it on the wire).
    params.setdefault("format", "json")
    meta = describe_qt(query_type)
    result = normalize(
        app=app_name,
        query_type=query_type,
        request_params=params,
        aggregation=meta.aggregation if meta else None,
        data=data,
        raw=raw,
        history_mode=history_mode,
    )
    if corrections:
        result["corrections"] = corrections
    if dimension_notes:
        result["dimension_notes"] = dimension_notes
    if probe:
        await _probe_empty_reason(client, app, query_type, params, result)
    return result


# High-cardinality query-types are capped by default so a value dump can't blow
# the token budget; callers can override with an explicit `limit`.
_HIGH_CARDINALITY_QUERY_TYPES = frozenset(
    {
        "dimension-values",
        "dimension-over-time",
        "dimensions-over-time",
        "metrics-by-dimension",
        "geography",
        "page-groups",
        "browsers",
        "ab-tests",
        "bandwidth",
    }
)
_DEFAULT_HIGH_CARDINALITY_LIMIT = 100

# Query-types where mPulse itself decides how many rows come back (a native
# `limit`): (API maximum, value sent when the caller gives none). Without it the
# API silently returns only its default top 10. Verified live 2026-10-29.
_SERVER_LIMIT_QUERY_TYPES: dict[str, tuple[int, int]] = {
    "metrics-by-dimension": (100, 100),
    "app-error-summary": (100, 100),
    "dimension-over-time": (10, 10),
    "dimensions-over-time": (10, 10),
}


def _apply_server_limit(
    query_type: str, wire: dict[str, Any], limit: int | None
) -> tuple[int | None, str | None]:
    """Put a native `limit` on the wire for query-types where mPulse caps rows.

    A `limit` already in ``params`` wins; otherwise the tool's top-level `limit`
    is used, else the API maximum. A value above the API maximum is lowered to
    it (mPulse would answer 400), with a correction note, because a large limit
    means "as many as possible". Values below 1 or non-integers are rejected.
    Returns ``(limit_sent, note)``; ``(None, None)`` for query-types without a
    native limit (those are capped client-side only).
    """
    spec = _SERVER_LIMIT_QUERY_TYPES.get(query_type)
    if spec is None:
        return None, None
    max_limit, default = spec
    value = wire.get("limit", limit if limit is not None else default)
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"limit must be an integer, got {value!r}.") from None
    if n < 1:
        raise ValidationError(
            f"limit {n} is out of range for {query_type}.",
            hint=f"mPulse accepts 1-{max_limit} here.",
        )
    note = None
    if n > max_limit:
        note = f"limit {n} lowered to {max_limit}, the maximum mPulse accepts for {query_type}"
        n = max_limit
    wire["limit"] = n
    return n, note


def _server_row_count(data: dict[str, Any]) -> int | None:
    """Rows returned by a native-limit endpoint ({data:[...]} or {data:{series}})."""
    rows = data.get("data")
    if isinstance(rows, list):
        return len(rows)
    if isinstance(rows, dict) and isinstance(rows.get("series"), list):
        return len(rows["series"])
    return None


async def _probe_empty_reason(
    client: MpulseClient,
    app: str | None,
    query_type: str,
    params: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Compare an empty result with one additional drilldown-free query.

    Opt-in (a network call). Only runs when ``result`` is a non-raw empty
    envelope that had drilldowns. A populated baseline cannot distinguish a
    valid filter with no matches from an unsupported combination. Failures
    are reported, never raised.
    """
    from .formatting import _is_empty

    if not isinstance(result, dict) or not result.get("empty"):
        return
    if not result.get("drilldowns"):
        return

    base = {
        k: v
        for k, v in params.items()
        if k not in DRILLDOWN_PARAMS and not k.startswith("custom-dimension-")
    }
    try:
        base_data = await client.query(app=app, query_type=query_type, params=base)
    except MpulseError as exc:
        result["probe"] = {"ran": False, "error": exc.user_message()}
        return

    base_empty = _is_empty(query_type, base_data)
    result["empty_reason"] = "no_data" if base_empty else "filtered_empty"
    result["probe"] = {"ran": True, "base_empty": base_empty}
    result["note"] = (
        "Probe: same query without drilldowns is "
        + ("also empty → no data returned for the baseline."
           if base_empty
           else "non-empty → filters have no matches or may be unsupported; the probe cannot distinguish them.")
    )


def _resolve_value_names(query_type: str, params: dict[str, Any]) -> list[str]:
    """Auto-correct known ``timer``/``metric`` names in-place, or reject bad ones.

    Uses the catalog to (a) canonicalize a casing/separator-only mismatch
    silently — avoiding a silent PageLoad fallback at zero token cost — and
    (b) raise a :class:`ValidationError` with close suggestions when a value is
    genuinely unknown, so a wrong name never reaches mPulse. Custom timers and
    values are left untouched; if the catalog is unavailable this is a no-op.
    Returns human-readable notes for any auto-corrections made.
    """
    from .catalog import resolve_value

    notes: list[str] = []
    for kind in ("timer", "metric"):
        value = params.get(kind)
        values = value if isinstance(value, list) else [value]
        resolved = []
        for item in values:
            status, payload = resolve_value(kind, item, query_type)
            if status == "unknown":
                hint = (f"Did you mean: {', '.join(payload)}?" if payload
                        else f"Call describe_query('{query_type}') for valid {kind} values.")
                raise ValidationError(f"Unknown {kind} {item!r} for query-type '{query_type}'.", hint=hint)
            canonical = payload if status == "ok" else item
            if status == "ok" and canonical != item:
                notes.append(f"{kind} {item!r} auto-corrected to {canonical!r}")
            if canonical not in resolved:
                resolved.append(canonical)
        if kind in params:
            params[kind] = resolved if isinstance(value, list) else resolved[0]
    return notes


# ===========================================================================
# Explicit tools
# ===========================================================================
@mcp.tool(structured_output=False)
async def get_summary(
    app: str | None = None,
    timer: str | None = None,
    custom_timer: str | None = None,
    percentile: int | None = None,
    date: str | None = None,
    date_comparator: str | None = None,
    timezone: str | None = None,
    page_group: FilterValue | None = None,
    browser: FilterValue | None = None,
    browser_family: FilterValue | None = None,
    ab_test: FilterValue | None = None,
    country: FilterValue | None = None,
    region: FilterValue | None = None,
    device_type: FilterValue | None = None,
    os: FilterValue | None = None,
    connection_type: FilterValue | None = None,
    beacon_type: FilterValue | None = None,
    raw: bool = False,
    probe: bool = False,
    device_model: FilterValue | None = None,
    device_manufacturer: FilterValue | None = None,
    os_family: FilterValue | None = None,
    isp: FilterValue | None = None,
    bandwidth_block: FilterValue | None = None,
    site_version: FilterValue | None = None,
    network_error_code: FilterValue | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    trailing_seconds: int | float | None = None,
    custom_dimension_filters: dict[str, FilterValue] | None = None,
) -> dict[str, Any]:
    """Aggregate summary stats for one timer (median, margin-of-error, count, p95, p98).

    mPulse query-type: `summary`. Returns period statistics in the selected
    timezone (default UTC).

    Time selection (exactly one required):
      * `date` — a single calendar day, `YYYY-MM-DD`.
      * `date_comparator` — a relative window, e.g. `Last24Hours`, `LastHour`.
    For a range use Between with date_start/date_end (exclusive end);
    Last requires trailing_seconds. Filters accept strings or non-empty lists;
    custom_dimension_filters maps labels to filter values.

    Timer: `timer` (built-in, default `PageLoad`) OR `custom_timer` (a custom
    timer name). `percentile` is 1–99 (default 50).

    Drilldowns filter the data (e.g. `page_group`, `browser`, `country`,
    `device_type`, `ab_test`). Unsupported combinations return empty data with a
    note rather than an error; an `empty_reason` heuristic labels the likely
    cause. `probe=True` runs one extra drilldown-free query. A populated
    baseline means filtered_empty, not proof of an unsupported combination.

    Set `raw=True` to get mPulse's untouched JSON. Numeric values are never
    rounded or summarized.
    """
    return await _run(
        app=app,
        query_type="summary",
        value_params={
            "timer": timer,
            "custom-timer": custom_timer,
            "percentile": percentile,
        },
        date_start=date_start,
        date_end=date_end,
        trailing_seconds=trailing_seconds,
        custom_dimension_filters=custom_dimension_filters,
        date=date,
        date_comparator=date_comparator,
        timezone=timezone,
        drilldowns=_collect_drilldowns(
            {
                "page_group": page_group,
                "browser": browser,
                "browser_family": browser_family,
                "ab_test": ab_test,
                "country": country,
                "region": region,
                "device_type": device_type,
                "device_model": device_model,
                "device_manufacturer": device_manufacturer,
                "os": os,
                "os_family": os_family,
                "connection_type": connection_type,
                "isp": isp,
                "bandwidth_block": bandwidth_block,
                "beacon_type": beacon_type,
                "site_version": site_version,
                "network_error_code": network_error_code
            }
        ),
        raw=raw,
        probe=probe,
    )


@mcp.tool(structured_output=False)
async def get_histogram(
    app: str | None = None,
    timer: str | None = None,
    custom_timer: str | None = None,
    percentile: int | None = None,
    date: str | None = None,
    date_comparator: str | None = None,
    timezone: str | None = None,
    page_group: FilterValue | None = None,
    browser: FilterValue | None = None,
    ab_test: FilterValue | None = None,
    country: FilterValue | None = None,
    device_type: FilterValue | None = None,
    beacon_type: FilterValue | None = None,
    raw: bool = False,
    probe: bool = False,
    browser_family: FilterValue | None = None,
    region: FilterValue | None = None,
    device_model: FilterValue | None = None,
    device_manufacturer: FilterValue | None = None,
    os: FilterValue | None = None,
    os_family: FilterValue | None = None,
    connection_type: FilterValue | None = None,
    isp: FilterValue | None = None,
    bandwidth_block: FilterValue | None = None,
    site_version: FilterValue | None = None,
    network_error_code: FilterValue | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    trailing_seconds: int | float | None = None,
    custom_dimension_filters: dict[str, FilterValue] | None = None,
) -> dict[str, Any]:
    """Distribution (histogram buckets) for one timer over the period.

    mPulse query-type: `histogram`. Returns per-bucket counts plus median/p95/
    p98. Time selection and drilldowns behave exactly as in `get_summary`
    (single calendar day OR relative `date_comparator`).

    Use `raw=True` for untouched mPulse JSON. Bucket counts are preserved
    losslessly.
    """
    return await _run(
        app=app,
        query_type="histogram",
        value_params={
            "timer": timer,
            "custom-timer": custom_timer,
            "percentile": percentile,
        },
        date_start=date_start,
        date_end=date_end,
        trailing_seconds=trailing_seconds,
        custom_dimension_filters=custom_dimension_filters,
        date=date,
        date_comparator=date_comparator,
        timezone=timezone,
        drilldowns=_collect_drilldowns(
            {
                "page_group": page_group,
                "browser": browser,
                "browser_family": browser_family,
                "ab_test": ab_test,
                "country": country,
                "region": region,
                "device_type": device_type,
                "device_model": device_model,
                "device_manufacturer": device_manufacturer,
                "os": os,
                "os_family": os_family,
                "connection_type": connection_type,
                "isp": isp,
                "bandwidth_block": bandwidth_block,
                "beacon_type": beacon_type,
                "site_version": site_version,
                "network_error_code": network_error_code
            }
        ),
        raw=raw,
        probe=probe,
    )


@mcp.tool(structured_output=False)
async def get_timers(
    app: str | None = None,
    timer: str | None = None,
    custom_timer: str | None = None,
    percentile: int | None = None,
    date: str | None = None,
    date_comparator: str | None = None,
    timezone: str | None = None,
    page_group: FilterValue | None = None,
    browser: FilterValue | None = None,
    ab_test: FilterValue | None = None,
    country: FilterValue | None = None,
    device_type: FilterValue | None = None,
    beacon_type: FilterValue | None = None,
    raw: bool = False,
    history_mode: HistoryMode = "downsample",
    probe: bool = False,
    browser_family: FilterValue | None = None,
    region: FilterValue | None = None,
    device_model: FilterValue | None = None,
    device_manufacturer: FilterValue | None = None,
    os: FilterValue | None = None,
    os_family: FilterValue | None = None,
    connection_type: FilterValue | None = None,
    isp: FilterValue | None = None,
    bandwidth_block: FilterValue | None = None,
    site_version: FilterValue | None = None,
    network_error_code: FilterValue | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    trailing_seconds: int | float | None = None,
    custom_dimension_filters: dict[str, FilterValue] | None = None,
) -> dict[str, Any]:
    """A single timer's value **by minute over time** (time series).

    mPulse query-type: `by-minute`. Best for one timer (built-in `timer`,
    default `PageLoad`, or a `custom_timer`). For *multiple* timers/metrics at
    once, use `get_metrics` (query-type `timers-metrics`).

    Returns a per-minute series for the selected calendar day (or
    `date_comparator` window). `history_mode` controls series volume:
    `downsample` (default, ≤60 `{x,y}` points under `aPoints_downsampled` +
    `peak` + `statistics`), `none` (drop the series, keep
    endpoints/`peak`/`statistics`), or `full` (every minute, loss-free,
    including per-point MoE). The `peak` carries a human-readable `time` in the
    request timezone — read it for "when did it spike?". Use `full` or
    `raw=True` when you need exact per-minute values.
    """
    return await _run(
        app=app,
        query_type="by-minute",
        value_params={
            "timer": timer,
            "custom-timer": custom_timer,
            "percentile": percentile,
        },
        date_start=date_start,
        date_end=date_end,
        trailing_seconds=trailing_seconds,
        custom_dimension_filters=custom_dimension_filters,
        date=date,
        date_comparator=date_comparator,
        timezone=timezone,
        drilldowns=_collect_drilldowns(
            {
                "page_group": page_group,
                "browser": browser,
                "browser_family": browser_family,
                "ab_test": ab_test,
                "country": country,
                "region": region,
                "device_type": device_type,
                "device_model": device_model,
                "device_manufacturer": device_manufacturer,
                "os": os,
                "os_family": os_family,
                "connection_type": connection_type,
                "isp": isp,
                "bandwidth_block": bandwidth_block,
                "beacon_type": beacon_type,
                "site_version": site_version,
                "network_error_code": network_error_code
            }
        ),
        raw=raw,
        history_mode=history_mode,
        probe=probe,
    )


@mcp.tool(structured_output=False)
async def get_metrics(
    app: str | None = None,
    metric: FilterValue | None = None,
    timer: FilterValue | None = None,
    custom_timer: str | None = None,
    percentile: int | None = None,
    date: str | None = None,
    date_comparator: str | None = None,
    timezone: str | None = None,
    page_group: FilterValue | None = None,
    browser: FilterValue | None = None,
    ab_test: FilterValue | None = None,
    country: FilterValue | None = None,
    device_type: FilterValue | None = None,
    beacon_type: FilterValue | None = None,
    raw: bool = False,
    history_mode: HistoryMode = "downsample",
    probe: bool = False,
    browser_family: FilterValue | None = None,
    region: FilterValue | None = None,
    device_model: FilterValue | None = None,
    device_manufacturer: FilterValue | None = None,
    os: FilterValue | None = None,
    os_family: FilterValue | None = None,
    connection_type: FilterValue | None = None,
    isp: FilterValue | None = None,
    bandwidth_block: FilterValue | None = None,
    site_version: FilterValue | None = None,
    network_error_code: FilterValue | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    trailing_seconds: int | float | None = None,
    custom_dimension_filters: dict[str, FilterValue] | None = None,
) -> dict[str, Any]:
    """Multiple timers/metrics **by minute over time**.

    `metric` and `timer` each accept a string or non-empty list; names are
    validated and canonicalized individually. Shared filters also accept lists.
    mPulse query-type: `timers-metrics`. `metric` defaults to `Beacons`, `timer`
    to `PageLoad`. Returns `series:[{id, latest, ...}]` plus the data timezone;
    `latest` is the period-wide aggregate (e.g. monthly p75 — read this, do NOT
    average the per-minute history).

    `history_mode` controls per-minute volume: `downsample` (default, `latest` +
    ≤60-point `history_downsampled` + `peak`), `none` (just `latest`, endpoints,
    `peak`, `n_points`), or `full` (every minute, loss-free). `latest` is always
    preserved. Use `full` or `raw=True` for exact per-minute values.
    """
    return await _run(
        app=app,
        query_type="timers-metrics",
        value_params={
            "metric": metric,
            "timer": timer,
            "custom-timer": custom_timer,
            "percentile": percentile,
        },
        date_start=date_start,
        date_end=date_end,
        trailing_seconds=trailing_seconds,
        custom_dimension_filters=custom_dimension_filters,
        date=date,
        date_comparator=date_comparator,
        timezone=timezone,
        drilldowns=_collect_drilldowns(
            {
                "page_group": page_group,
                "browser": browser,
                "browser_family": browser_family,
                "ab_test": ab_test,
                "country": country,
                "region": region,
                "device_type": device_type,
                "device_model": device_model,
                "device_manufacturer": device_manufacturer,
                "os": os,
                "os_family": os_family,
                "connection_type": connection_type,
                "isp": isp,
                "bandwidth_block": bandwidth_block,
                "beacon_type": beacon_type,
                "site_version": site_version,
                "network_error_code": network_error_code
            }
        ),
        raw=raw,
        history_mode=history_mode,
        probe=probe,
    )


# ===========================================================================
# Dedicated metrics-by-dimension tool
# ===========================================================================
@mcp.tool(structured_output=False)
async def get_breakdown(
    dimension: str,
    metrics: list[str] | None = None,
    app: str | None = None,
    percentile: int | None = None,
    date: str | None = None,
    date_comparator: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    trailing_seconds: int | None = None,
    timezone: str | None = None,
    page_group: FilterValue | None = None,
    browser: FilterValue | None = None,
    browser_family: FilterValue | None = None,
    ab_test: FilterValue | None = None,
    country: FilterValue | None = None,
    region: FilterValue | None = None,
    device_type: FilterValue | None = None,
    device_model: FilterValue | None = None,
    device_manufacturer: FilterValue | None = None,
    os: FilterValue | None = None,
    os_family: FilterValue | None = None,
    connection_type: FilterValue | None = None,
    isp: FilterValue | None = None,
    bandwidth_block: FilterValue | None = None,
    beacon_type: FilterValue | None = None,
    site_version: FilterValue | None = None,
    custom_dimension_filters: dict[str, FilterValue] | None = None,
    limit: int | None = None,
    sortby: str | None = None,
    raw: bool = False,
    probe: bool = False,
    network_error_code: FilterValue | None = None,
    custom_timer: str | None = None,
) -> dict[str, Any]:
    """Metric values broken down by a dimension (mPulse `metrics-by-dimension`).

    Examples: beacons by country, LCP p75 by `page_group`, or any metric by a
    custom dimension such as `branch`.

    `metrics` uses this endpoint's own snake_case names, e.g. `beacons`,
    `largest_contentful_paint`, and `image_transfer_size` — not the
    `timers-metrics` CamelCase metric names. Names are checked by mPulse; an
    invalid name returns a 400 with did-you-mean suggestions.

    `dimension` is a built-in dimension from `describe_query`, or
    a custom dimension label from `list_custom_dimensions`. `percentile` is
    1–99 (default 50) and applies to timer-like metrics. `apperror_*` dimensions
    ignore `metrics` and return their own columns.

    mPulse returns at most 100 rows. This tool sends `limit=100` by default;
    there is no offset or paging. `truncated.by == "server"` means the result
    filled the limit. To see different rows, change `sortby` (top rows in
    descending order by that column) or narrow the filters.

    Time selection accepts a single `date` or a `date_comparator`. `Between`
    uses `date_start` and exclusive `date_end`; `Last` uses `trailing_seconds`.
    These extra period arguments require their corresponding comparator.
    Omitting the time selection uses mPulse's current-day default.
    Drilldown filters and `custom_dimension_filters` further filter the result.
    Use `raw=True` for untouched mPulse JSON. `probe=True` checks an empty
    drilled-down result with one additional query.
    """
    _validate_breakdown_period(
        date=date,
        date_comparator=date_comparator,
        date_start=date_start,
        date_end=date_end,
        trailing_seconds=trailing_seconds,
    )
    wire: dict[str, Any] = {
        "dimension": dimension,
        "custom-timer": custom_timer,
        "metrics": metrics if metrics is not None else ["beacons"],
        "percentile": percentile if percentile is not None else 50,
    }
    time_values = {
        "date": date,
        "date-comparator": date_comparator,
        "date-start": date_start,
        "date-end": date_end,
        "trailing-seconds": trailing_seconds,
        "timezone": timezone,
    }
    wire.update({key: value for key, value in time_values.items() if value is not None})
    wire.update(
        _collect_drilldowns(
            {
                "page_group": page_group,
                "browser": browser,
                "browser_family": browser_family,
                "ab_test": ab_test,
                "country": country,
                "region": region,
                "device_type": device_type,
                "device_model": device_model,
                "device_manufacturer": device_manufacturer,
                "os": os,
                "os_family": os_family,
                "connection_type": connection_type,
                "isp": isp,
                "bandwidth_block": bandwidth_block,
                "beacon_type": beacon_type,
                "site_version": site_version,
                "network_error_code": network_error_code
            }
        )
    )
    if custom_dimension_filters:
        wire.update(
            {
                f"custom-dimension-{label}": value
                for label, value in custom_dimension_filters.items()
            }
        )
    if sortby is not None:
        wire["sortby"] = sortby

    return await _run_wire(
        app=app,
        query_type="metrics-by-dimension",
        wire=wire,
        raw=raw,
        limit=limit,
        probe=probe,
        include_dimension_corrections=True,
    )


# ===========================================================================
# Generic tool
# ===========================================================================
@mcp.tool(structured_output=False)
async def query(
    query_type: str,
    app: str | None = None,
    params: dict[str, Any] | None = None,
    raw: bool = False,
    history_mode: HistoryMode = "downsample",
    limit: int | None = None,
    probe: bool = False,
) -> dict[str, Any]:
    """Call an arbitrary mPulse query-type with arbitrary parameters.

    Use this for query-types not covered by the explicit tools (see
    `list_query_types`), e.g. `dimension-values`, `geography`, `page-groups`,
    `app-error-summary`. For `metrics-by-dimension`, prefer the dedicated
    `get_breakdown` tool.

    `params` is a dict of mPulse *wire* parameter names (hyphenated), e.g.
    `{"date-comparator": "Last24Hours", "page-group": "Home", "timer":
    "PageLoad"}`. `format=json` is added automatically. `date` selects one
    day; Between requires date-start/date-end, Last requires trailing-seconds.
    Known periods, percentiles (1-99), and parameter cardinality are checked.
    Unknown parameter names remain available for API extensions.

    `history_mode` (`downsample`/`none`/`full`) only affects the temporal
    query-types `timers-metrics`, `by-minute` and `dimension-over-time`; other
    query-types ignore it.

    `limit` controls how many rows come back:
    - `app-error-summary` (1-100) and `dimension-over-time` (1-10): mPulse
      itself caps the rows, so `limit` is sent to mPulse. Without it the server
      sends the maximum; mPulse's own default would be only the top 10. A larger
      value is lowered to the maximum (noted in `corrections`). If the result
      fills the limit, `truncated.by == "server"`.
    - Other high-cardinality types (`dimension-values`, `geography`,
      `page-groups`, `browsers`, …) return everything; the server trims the rows
      to `limit` (default 100) and reports `truncated.by == "client"`.

    `probe=True`: if the result is empty *and* had drilldowns, run one extra
    query without the drilldowns. `filtered_empty` means the baseline has data;
    `no_data` means neither result has data. Neither proves unsupported filters.

    `raw=True` returns mPulse's untouched JSON.
    """
    return await _run_wire(
        app=app,
        query_type=query_type,
        wire=dict(params or {}),
        raw=raw,
        history_mode=history_mode,
        limit=limit,
        probe=probe,
    )


async def _run_wire(
    *,
    app: str | None,
    query_type: str,
    wire: dict[str, Any],
    raw: bool,
    history_mode: str = "downsample",
    limit: int | None = None,
    probe: bool = False,
    include_dimension_corrections: bool = False,
) -> dict[str, Any]:
    """Validate, execute, and normalize a request built from mPulse wire params."""
    from .formatting import normalize

    _check_history_mode(history_mode)
    _prepare_metrics_by_dimension(query_type, wire)
    _validate_parameter_shapes(query_type, wire)
    meta = describe_qt(query_type)
    if meta is not None and meta.supports_time:
        _validate_breakdown_period(
            date=wire.get("date"), date_comparator=wire.get("date-comparator"),
            date_start=wire.get("date-start"), date_end=wire.get("date-end"),
            trailing_seconds=wire.get("trailing-seconds"),
        )
    # Enforce the single-day rule when a literal date is supplied.
    if isinstance(wire.get("date"), str):
        _check_single_day(wire["date"])
    if "date" in wire and "date-comparator" in wire:
        raise ValidationError("Provide either 'date' or 'date-comparator', not both.")
    if isinstance(wire.get("date-comparator"), str):
        _check_date_comparator(wire["date-comparator"])

    # Reject a custom dimension on the endpoints that don't accept one
    # (dimension-values / dimension-over-time), before spending a wasted call;
    # auto-correct a built-in's casing. metrics-by-dimension accepts custom names.
    dim_correction = _resolve_dimension_param(query_type, wire)  # raises when invalid
    dim_notes: list[str] = [dim_correction.note] if dim_correction else []

    # Normalize custom-dimension labels/values against the app's declared set
    # (hints only — never rejected, since the declared list may be incomplete).
    try:
        app_cfg = _get_registry().get(app)
    except MpulseError:
        app_cfg = None
    dim_notes += _normalize_custom_dimension_params(query_type, wire, app_cfg)

    server_limit, limit_note = _apply_server_limit(query_type, wire, limit)

    effective_limit = limit
    if effective_limit is None and query_type in _HIGH_CARDINALITY_QUERY_TYPES:
        effective_limit = _DEFAULT_HIGH_CARDINALITY_LIMIT

    client = _get_client()
    app_name = app or client.registry.default_app
    try:
        data = await client.query(app=app, query_type=query_type, params=wire)
    except MpulseError as err:
        if query_type == "metrics-by-dimension":
            _enrich_invalid_metric_error(err, wire)
        raise

    if (
        server_limit is not None
        and effective_limit is not None
        and effective_limit >= server_limit
    ):
        rows = _server_row_count(data)
        if rows is not None and rows <= server_limit:
            # Native limiting already covers this cap. Preserve a smaller
            # top-level cap, and the old fallback if upstream ignores its limit.
            effective_limit = None

    wire.setdefault("format", "json")
    qt_meta = describe_qt(query_type)
    result = normalize(
        app=app_name,
        query_type=query_type,
        request_params=wire,
        # Known query-types state how to read the numbers; unknown (forward-
        # compatible passthrough) ones simply omit the field.
        aggregation=qt_meta.aggregation if qt_meta else None,
        data=data,
        raw=raw,
        history_mode=history_mode,
        limit=effective_limit,
    )
    if dim_notes:
        result["dimension_notes"] = dim_notes
    if limit_note:
        result.setdefault("corrections", []).append(limit_note)
    if include_dimension_corrections and dim_correction:
        result.setdefault("corrections", []).append(dim_correction.note)
    if server_limit is not None and "truncated" not in result:
        rows = _server_row_count(data)
        if rows is not None and rows >= server_limit:
            result["truncated"] = {
                "by": "server",
                "returned": rows,
                "limit": server_limit,
                "hint": "mPulse returned exactly `limit` rows, so more may exist. "
                "Raise `limit` or narrow the filters.",
            }
    if probe:
        await _probe_empty_reason(client, app, query_type, wire, result)
    return result


@dataclass(frozen=True)
class _DimensionCorrection:
    kind: Literal["builtin_dimension"]
    original: str
    normalized: str

    @property
    def note(self) -> str:
        return f"dimension {self.original!r} auto-corrected to {self.normalized!r}"


def _resolve_dimension_param(
    query_type: str, wire: dict[str, Any]
) -> _DimensionCorrection | None:
    """Validate/auto-correct the ``dimension`` wire param for dimension endpoints.

    Raises :class:`ValidationError` for a custom dimension on an endpoint that
    only accepts built-ins. Returns a correction when a built-in's casing
    was fixed, or ``None`` when there is nothing to do.
    """
    from .catalog import resolve_dimension

    value = wire.get("dimension")
    status, payload = resolve_dimension(value, query_type)
    if status == "ok" and payload != value:
        wire["dimension"] = payload
        return _DimensionCorrection("builtin_dimension", value, payload)
    if status == "unknown_no_custom":
        hint = (
            f"'{query_type}' accepts BUILT-IN dimensions only — custom dimensions "
            "(e.g. 'branch') are not supported here. To break data down by a "
            "custom dimension, use metrics-by-dimension with dimension=<name>."
        )
        if payload:
            hint += f" Or did you mean a built-in: {', '.join(payload)}?"
        raise ValidationError(
            f"Unknown dimension {value!r} for query-type '{query_type}'.",
            hint=hint,
        )
    return None


def _normalize_custom_dimension_params(
    query_type: str, wire: dict[str, Any], app_cfg: AppConfig | None
) -> list[str]:
    """Normalize custom-dimension label/value casing and hint on unknown names.

    Hints only — both the built-in catalog and the declared custom list may be
    incomplete. An unlisted split is not necessarily custom: it may be a newer
    system dimension. Normalize and advise without rejecting it. Handles both
    the metrics-by-dimension split value and any
    ``custom-dimension-<label>`` filter key. Returns advisory notes.
    """
    from .catalog import resolve_dimension
    from .config import custom_dimension_wire_label

    known = app_cfg.custom_dimensions if app_cfg else {}
    app_label = f" for app '{app_cfg.name}'" if app_cfg else ""
    notes: list[str] = []

    def _flag_unknown(label: str, kind: str) -> None:
        if known and label not in known:
            if kind == "dimension":
                notes.append(
                    f"dimension '{label}' is not listed in the built-in catalog "
                    f"or declared custom dimensions{app_label}. Proceeding; it "
                    "may be an uncatalogued built-in or an undeclared custom "
                    "dimension. Verify the exact name in the API documentation "
                    "or app settings."
                )
                return
            notes.append(
                f"{kind} '{label}' is not a declared custom dimension{app_label} "
                f"(declared: {', '.join(sorted(known))}). Proceeding; verify the "
                f"exact name via list_custom_dimensions."
            )

    # (a) metrics-by-dimension split value, only when it's a custom (non-builtin) name
    if query_type == "metrics-by-dimension":
        val = wire.get("dimension")
        if isinstance(val, str) and val:
            status, _ = resolve_dimension(val, query_type)
            if status == "ok_custom":
                label = custom_dimension_wire_label(val)
                if label != val:
                    wire["dimension"] = label
                    notes.append(f"dimension {val!r} normalized to {label!r}")
                _flag_unknown(label, "dimension")

    # (b) custom-dimension-<label> filter keys (any query-type)
    for key in list(wire.keys()):
        if not key.startswith("custom-dimension-"):
            continue
        raw_label = key[len("custom-dimension-"):]
        label = custom_dimension_wire_label(raw_label)
        if label != raw_label:
            wire[f"custom-dimension-{label}"] = wire.pop(key)
            notes.append(f"filter label {raw_label!r} normalized to {label!r}")
        _flag_unknown(label, "custom-dimension filter")

    return notes


# ===========================================================================
# Batch aggregate tool
# ===========================================================================
def _norm_name(name: str) -> str:
    return "".join(c for c in str(name).lower() if c.isalnum())


def _build_period(period: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Turn one period spec into a (label, wire-params) pair, validating it.

    A period is ``{date}`` (single calendar day) OR ``{date_comparator, ...}``
    (relative; ``Between`` needs ``date_start``+``date_end``, ``Last`` needs
    ``trailing_seconds``). An optional ``label`` names the column.
    """
    if not isinstance(period, dict):
        raise ValidationError("Each period must be an object.")
    _validate_breakdown_period(
        date=period.get("date"), date_comparator=period.get("date_comparator"),
        date_start=period.get("date_start"), date_end=period.get("date_end"),
        trailing_seconds=period.get("trailing_seconds"),
    )
    dc = period.get("date_comparator")
    date = period.get("date")
    if dc and date:
        raise ValidationError(
            "A period cannot set both 'date' and 'date_comparator'."
        )
    wire: dict[str, Any] = {}
    if dc:
        _check_date_comparator(dc)
        wire["date-comparator"] = dc
        if dc == "Between":
            ds, de = period.get("date_start"), period.get("date_end")
            if not ds or not de:
                raise ValidationError(
                    "date_comparator='Between' requires 'date_start' and "
                    "'date_end' (date_end is exclusive)."
                )
            wire["date-start"], wire["date-end"] = ds, de
            label = period.get("label") or f"{ds}..{de}"
        elif dc == "Last":
            ts = period.get("trailing_seconds")
            if ts is None:
                raise ValidationError(
                    "date_comparator='Last' requires 'trailing_seconds'."
                )
            wire["trailing-seconds"] = ts
            label = period.get("label") or f"Last{ts}s"
        else:
            label = period.get("label") or dc
    elif date:
        _check_single_day(date)
        wire["date"] = date
        label = period.get("label") or date
    else:
        raise ValidationError("Each period needs 'date' or 'date_comparator'.")
    return label, wire


def _extract_latest(data: dict[str, Any], name: str) -> tuple[Any, str | None]:
    """Pull the period aggregate (`latest`) for `name` from a timers-metrics body.

    Returns ``(value, error)``. Requires exactly one matching series with a
    non-null latest. Never substitutes another series for the requested one.
    """
    values = data.get("values")
    if not isinstance(values, list) or not values:
        return None, "empty result (no matching data returned)"
    target = _norm_name(name)
    matches = [v for v in values if isinstance(v, dict)
               and _norm_name(v.get("id", "")) == target]
    if len(matches) != 1:
        return None, f"requested {name!r}: expected one matching series, got {len(matches)}; no value substituted"
    if matches[0].get("latest") is None:
        return None, f"requested {name!r}: matching series has no latest value"
    return matches[0]["latest"], None



@mcp.tool(structured_output=False)
async def get_aggregate(
    metrics: list[str] | None = None,
    timers: list[str] | None = None,
    periods: list[dict[str, Any]] | None = None,
    percentiles: list[int] | None = None,
    app: str | None = None,
    timezone: str | None = None,
    page_group: FilterValue | None = None,
    browser: FilterValue | None = None,
    ab_test: FilterValue | None = None,
    country: FilterValue | None = None,
    device_type: FilterValue | None = None,
    beacon_type: FilterValue | None = None,
    max_combos: int = 24,
    browser_family: FilterValue | None = None,
    region: FilterValue | None = None,
    device_model: FilterValue | None = None,
    device_manufacturer: FilterValue | None = None,
    os: FilterValue | None = None,
    os_family: FilterValue | None = None,
    connection_type: FilterValue | None = None,
    isp: FilterValue | None = None,
    bandwidth_block: FilterValue | None = None,
    site_version: FilterValue | None = None,
    network_error_code: FilterValue | None = None,
    custom_dimension_filters: dict[str, FilterValue] | None = None,
) -> dict[str, Any]:
    """Batch metrics AND timers in one timers-metrics query per period/percentile.

    2 metrics x 2 periods x 2 percentiles returns 8 cells using 4 query calls.
    Adding timers to the same groups does not add calls. Returns only `latest`;
    main history is never included. Inputs retain their existing names.

    `max_combos` caps output cells (targets x periods x percentiles), not calls.
    Duplicate targets/periods/percentiles reuse requests; output order is kept.
    Known names and periods are validated before any query. Missing, ambiguous,
    or null series are errors, never substituted with another series' value.

    Only HTTP 400 on a multi-target batch triggers individual target recovery.
    Auth, permissions, throttling and upstream failures do NOT trigger splitting.
    Failed groups produce errors for each affected cell, while other groups
    return. `requests` reports planned batches, executed query calls (including
    recovery), and recovery calls; excludes auth and HTTP-level retries.
    """
    from .catalog import resolve_value

    client = _get_client()
    app_name = app or client.registry.default_app
    pcts = [50] if percentiles is None else percentiles
    if not pcts:
        raise ValidationError("Provide at least one percentile.")
    for pct in pcts:
        _validate_percentile(pct)
    targets = [("metric", m) for m in (metrics or [])]
    targets += [("timer", t) for t in (timers or [])]
    if not targets:
        raise ValidationError("Provide at least one of 'metrics' or 'timers'.")
    if not periods:
        raise ValidationError("Provide at least one period in 'periods'.")
    if isinstance(max_combos, bool) or not isinstance(max_combos, int) or max_combos < 1:
        raise ValidationError("max_combos must be a positive integer.")
    combos = len(targets) * len(periods) * len(pcts)
    if combos > max_combos:
        raise ValidationError(f"{combos} combinations exceed max_combos={max_combos}.",
                              hint="Reduce targets/periods/percentiles, or raise max_combos.")
    corrections: list[str] = []
    resolved: list[tuple[str, str]] = []
    for kind, name in targets:
        if not isinstance(name, str) or not name:
            raise ValidationError(f"Each {kind} must be a non-empty string.")
        status, payload = resolve_value(kind, name, "timers-metrics")
        if status == "unknown":
            hint = (f"Did you mean: {', '.join(payload)}?" if payload
                    else "Call describe_query('timers-metrics') for valid names.")
            raise ValidationError(f"Unknown {kind} {name!r}.", hint=hint)
        canonical = payload if status == "ok" else name
        if canonical != name:
            corrections.append(f"{kind} {name!r} auto-corrected to {canonical!r}")
        resolved.append((kind, canonical))
    unique_targets = list(dict.fromkeys(resolved))
    built_periods = [_build_period(period) for period in periods]
    drilldowns = _collect_drilldowns(
            {
                "page_group": page_group,
                "browser": browser,
                "browser_family": browser_family,
                "ab_test": ab_test,
                "country": country,
                "region": region,
                "device_type": device_type,
                "device_model": device_model,
                "device_manufacturer": device_manufacturer,
                "os": os,
                "os_family": os_family,
                "connection_type": connection_type,
                "isp": isp,
                "bandwidth_block": bandwidth_block,
                "beacon_type": beacon_type,
                "site_version": site_version,
                "network_error_code": network_error_code
            }
        )
    if custom_dimension_filters:
        drilldowns.update({f"custom-dimension-{k}": v for k, v in custom_dimension_filters.items()})
    dimension_notes = _normalize_custom_dimension_params(
        "timers-metrics", drilldowns, client.registry.get(app)
    )
    _validate_parameter_shapes("timers-metrics", drilldowns)
    executed = 0
    recovery = 0

    async def fetch(group: list[tuple[str, str]], wire: dict[str, Any], pct: int,
                    is_recovery: bool = False) -> dict[tuple[str, str], tuple[Any, str | None]]:
        nonlocal executed, recovery
        params = {**drilldowns, **wire, "percentile": pct}
        for kind in ("metric", "timer"):
            names = [name for target_kind, name in group if target_kind == kind]
            if names:
                params[kind] = names
        if timezone:
            params["timezone"] = timezone
        executed += 1
        recovery += int(is_recovery)
        try:
            data = await client.query(app=app, query_type="timers-metrics", params=params)
        except MpulseError as exc:
            if isinstance(exc, QueryError) and exc.status_code == 400 and len(group) > 1:
                recovered = await asyncio.gather(*[
                    fetch([target], wire, pct, True) for target in group
                ])
                return {key: value for result in recovered for key, value in result.items()}
            return {target: (None, exc.user_message()) for target in group}
        return {target: _extract_latest(data, target[1]) for target in group}

    def group_key(wire: dict[str, Any], pct: int) -> str:
        return json.dumps([wire, pct], sort_keys=True)

    groups = {group_key(wire, pct): (wire, pct)
              for _, wire in built_periods for pct in pcts}
    results = await asyncio.gather(*[fetch(unique_targets, wire, pct)
                                     for wire, pct in groups.values()])
    lookup = dict(zip(groups, results))
    table, errors = [], []
    for kind, name in resolved:
        for label, wire in built_periods:
            for pct in pcts:
                value, error = lookup[group_key(wire, pct)][(kind, name)]
                coordinates = {"target": name, "kind": kind, "period": label, "percentile": pct}
                if error:
                    errors.append({**coordinates, "message": error})
                else:
                    table.append({**coordinates, "value": value})
    out: dict[str, Any] = {
        "app": app_name, "query_type": "aggregate(timers-metrics.latest)",
        "timezone": timezone or "UTC", "drilldowns": drilldowns,
        "percentiles": pcts, "targets": [name for _, name in resolved], "table": table,
        "requests": {"planned": len(groups), "executed": executed, "recovery": recovery},
    }
    if corrections:
        out["corrections"] = corrections
    if dimension_notes:
        out["dimension_notes"] = dimension_notes
    if errors:
        out["errors"] = errors
    return out


# ===========================================================================
# Meta tools (anti-hallucination)
# ===========================================================================
@mcp.tool(structured_output=False)
def list_apps() -> dict[str, Any]:
    """List registered mPulse apps and the default app.

    Use this before other tools so you reference real app names. The `app`
    argument on every tool is optional and falls back to the default.
    """
    reg = _get_registry()
    return {
        "default_app": reg.default_app,
        "apps": [
            {"name": a.name, "tenant": a.tenant} for a in reg.apps.values()
        ],
    }


@mcp.tool(structured_output=False)
def list_custom_dimensions(app: str | None = None) -> dict[str, Any]:
    """List the custom dimensions configured for an app (or the default app).

    mPulse has **no API to discover custom dimensions**, so they are declared in
    the app registry. Each entry's key is the exact **wire label** to use:
    - as a split: `metrics-by-dimension` with `dimension=<label>`
    - as a filter: `custom-dimension-<label>=<value>` on any query

    Use this before querying a custom dimension so you use the exact name.
    Returns `{app, custom_dimensions: [{label, display, description}]}`. An empty
    list means none are configured (not necessarily that none exist).
    """
    reg = _get_registry()
    app_cfg = reg.get(app)
    return {
        "app": app_cfg.name,
        "custom_dimensions": [
            {
                "label": label,
                "display": meta.get("display", label),
                "description": meta.get("description", ""),
                **({"values": meta["values"]} if meta.get("values") else {}),
            }
            for label, meta in app_cfg.custom_dimensions.items()
        ],
    }


@mcp.tool(structured_output=False)
def list_query_types() -> dict[str, Any]:
    """List the mPulse query-types this server knows about, with one-line summaries.

    The explicit tools cover: summary→`get_summary`, histogram→`get_histogram`,
    by-minute→`get_timers`, timers-metrics→`get_metrics`. Everything else is
    reachable via the generic `query` tool.
    """
    return {
        "query_types": [
            {"slug": m.slug, "summary": m.summary} for m in QUERY_TYPES.values()
        ],
        "date_comparators": list(KNOWN_DATE_COMPARATORS),
    }


@mcp.tool(structured_output=False)
def describe_query(
    query_type: str, app: str | None = None, verbose: bool = False
) -> dict[str, Any]:
    """Describe a query-type: parameters, drilldowns, response shape, and caveats.

    Pass a slug from `list_query_types` (e.g. `summary`, `timers-metrics`,
    `geography`). Pass `app` to also include that app's configured **custom
    dimensions** (usable as a `metrics-by-dimension` split or a
    `custom-dimension-<label>` filter).

    By default the output covers only this query-type: general gotchas plus its
    own, without reference tables for other endpoints. Pass `verbose=True` for
    the complete catalog view.
    """
    meta = describe_qt(query_type)
    if meta is None:
        raise ValidationError(
            f"Unknown query-type '{query_type}'.",
            hint=f"Known query-types: {', '.join(QUERY_TYPES)}.",
        )
    result: dict[str, Any] = {
        "slug": meta.slug,
        "summary": meta.summary,
        "parameters": meta.all_params(),
        "supports_time_selection": meta.supports_time,
        "supports_drilldown": meta.supports_drilldown,
        "response_shape": meta.response_shape or "(varies)",
        "date_comparators": list(KNOWN_DATE_COMPARATORS),
        "constraints": QUERY_CONSTRAINTS,
    }
    # Empty lists are noise for the model; omit rather than emit [].
    if meta.value_params:
        result["value_parameters"] = list(meta.value_params)
    if meta.notes:
        result["notes"] = meta.notes
    if meta.aggregation:
        result["aggregation"] = meta.aggregation
    # Enrich with authoritative enum hints (valid metrics/timers/dimensions,
    # gotchas) from catalog.json. Fail-safe: returns {} if catalog unavailable.
    from .catalog import enrich_describe

    result.update(enrich_describe(query_type, compact=not verbose))

    # App-specific custom dimensions (mPulse has no API to discover these).
    # An unknown app is an error, not a silently omitted block.
    if app is not None:
        app_cfg = _get_registry().get(app)
        if app_cfg.custom_dimensions:
            result["app_custom_dimensions"] = [
                {"label": label, "display": m.get("display", label)}
                for label, m in app_cfg.custom_dimensions.items()
            ]
    if not verbose:
        result["verbose_hint"] = "Pass verbose=True for every gotcha and reference table."
    return result


def main() -> None:
    """Console-script entry point. Runs the MCP server over stdio."""
    log.info("Starting mpulse-mcp v%s (stdio transport)", __version__)
    # Load a local .env (if any) before reading credentials. Existing env vars
    # (e.g. Claude Desktop's env block) take precedence.
    from .config import load_env_files

    load_env_files()
    # Validate config early so misconfiguration surfaces on stderr at startup
    # rather than on the first tool call. Failure here is non-fatal: the meta/
    # tool errors will still explain the problem to the client.
    try:
        _get_registry()
    except MpulseError as exc:
        log.error("Configuration problem at startup: %s", exc.user_message())
    mcp.run()


if __name__ == "__main__":
    main()
