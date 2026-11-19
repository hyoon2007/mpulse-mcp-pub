# mpulse-mcp — Detailed Reference

Behavior details behind the tools. For setup, see the [README](../README.md); for
mPulse API names, enums and gotchas, see [CATALOG.md](../CATALOG.md).

## Verified API contract

Confirmed against the mPulse API reference at `techdocs.akamai.com/mpulse`.

| Item | Confirmed value |
|---|---|
| Query base host | `https://mpulse.soasta.com` |
| Query URL format | `/concerto/mpulse/api/v2/<api-key>/<query-type>?<params>` |
| Auth header | **`Authentication: <token>`** (not `Authorization`) |
| Common param | `format=json` (the default `legacy` format is deprecated) |
| Token endpoint | **`PUT /concerto/services/rest/RepositoryService/v1/Tokens`** |
| Token request body | `{ "apiToken": "<pre-issued>", "tenant": "<tenant>" }` (API-token/SSO flow; no username/password) |
| Token response | `{ "token": "<security token>" }` |
| Token lifetime | **expires after 5 hours of inactivity** |
| Data characteristics | per-minute aggregation, **one calendar day per query**, timezone-dependent |

**Confirmed query-type slugs → explicit tools**

| Tool | query-type slug | Response core |
|---|---|---|
| `get_summary` | `summary` | `median, moe, n, p95, p98` |
| `get_histogram` | `histogram` | `series.series[].buckets/aPoints, median, p95, p98` |
| `get_timers` | `by-minute` | `series.series[].aPoints[{x,y}], statistics` (single timer) |
| `get_metrics` | `timers-metrics` | `dataTimeZone, values[{id, history[], latest}]` (multiple) |
| `get_breakdown` | `metrics-by-dimension` | `columnNames, data` (metrics by built-in or custom dimension) |

Other confirmed slugs (reachable via the generic `query` tool):
`dimension-values`, `dimension-over-time`,
`metric-per-page-load-time`, `sessions-per-page-load-time`, `app-error-summary`,
`page-groups`, `browsers`, `ab-tests`, `bandwidth`, `geography`.

**Confirmed parameters**: `timer` (default `PageLoad`) / `custom-timer`,
`metric` (default `Beacons`), `percentile` (1–99, default 50), `date`
(`YYYY-MM-DD`), `date-comparator` (`Last30Minutes`, `LastHour`, `Last24Hours`,
`ThisWeek`, `Last` + `trailing-seconds`, `Between` + `date-start`/`date-end`),
`timezone` (Java TZ id, default UTC).

**Confirmed drilldown params**: `page-group`, `browser`, `browser-family`,
`ab-test`, `country` (ISO alpha-2), `region`, `device-type`, `device-model`,
`device-manufacturer`, `os`, `os-family`, `connection-type`, `isp`,
`bandwith-block` (mPulse's own spelling), `beacon-type`, `site-version`,
`custom-dimension-{label}`.
**Unsupported drilldown combinations return empty data, not an error** — the
server flags this in the result `note`.


## Output format

Tool results are sent as a single JSON text block (`structured_output=False`):
with a `dict[str, Any]` return type, FastMCP would otherwise send every result
twice (text + `structuredContent`, both fed to the model by LLM hosts — ~2x
tokens) while the auto-derived schema validates nothing. Consuming this server
from scripts/automation instead of an LLM? Re-register the tools with
`structured_output=True` in [server.py](../src/mpulse_mcp/server.py) to get
machine-readable `structuredContent` back.

- `raw=false` (default): a flat envelope — `app`, `query_type`, `period`,
  `timezone`, `aggregation` (how to read the numbers, e.g. "period aggregate
  per dimension value" vs "per-minute"; omitted for unknown query-types),
  `drilldowns`, `empty`, `history_mode`, and `body`
  (the envelope-stripped data). Empty results are flagged, with a note when an
  unsupported drilldown combination is the likely cause.
- `raw=true`: mPulse's untouched JSON under `data`.

**`history_mode` — per-minute time-series volume (loss-free on demand).** The
temporal query-types `timers-metrics`, `by-minute` and `dimension-over-time` return ~1,440 points/day,
of which the common workload uses only the period aggregate. `history_mode`
(on `get_timers`, `get_metrics`, and `query`) controls this:

| mode | time series | always kept | ~size |
|---|---|---|---|
| `downsample` (**default**) | ≤60 even-spaced `{x,y}` points + `peak` | `latest`/`statistics`, `n_points` | ~1/40 |
| `none` | dropped (endpoints only) | `latest`/`statistics`, `n_points`, `peak` | ~1/100 |
| `full` | every minute (loss-free) | everything | full |

The complete series is always recoverable via `history_mode="full"` or
`raw=true`. **Aggregates are never reduced**: `latest` (period value — read this
for monthly p75, don't average the history), mPulse `statistics`, `histogram`
buckets, and `summary` are kept in full regardless of mode. A `peak`
(`{index/x, value/y}`) is added so "when did it spike?" survives downsampling;
for `by-minute` series it also carries a human-readable `time` in the request
timezone. In the lossy modes, chart chrome is dropped too: per-point tooltip
fields (`label`, `userdata` with its HTML template and per-point MoE) and the
`meta` chart titles. Per-point MoE is available via `full` or `raw=true`.

**`timer`/`metric` name resolution (input side).** mPulse silently answers an
unrecognized `timer`/`metric` with a *default* (PageLoad) series instead of
erroring, so the explicit tools resolve these names against the catalog before
calling the API:

- a casing/separator-only mismatch (`largest_contentful_paint` →
  `LargestContentfulPaint`, endpoint-specific) is **auto-corrected** silently
  (zero extra round-trips); the fix is reported in a `corrections` field;
- a genuinely unknown name is **rejected** with close suggestions
  (`Did you mean: …?`) — the wrong name never reaches mPulse;
- custom timers/dimensions and the generic `query` tool are left permissive
  (forward-compatible). The packaged catalog is now required for query metadata; missing or invalid required data raises a clear configuration error at startup.

**Custom `dimension` support is endpoint-specific.** A custom dimension (e.g.
`branch`) is accepted **only** by `metrics-by-dimension` (as its `dimension`
split value). `dimension-values` and `dimension-over-time` accept **built-in
dimensions only** — a custom name there returns empty/400 (a wasted call). The
`query` tool enforces this: a custom `dimension` on those two query-types is
**rejected up front** with a redirect to `metrics-by-dimension`, while a
built-in's casing is auto-corrected. `describe_query` reports
`custom_dimension_supported` per query-type so the model knows before calling.

For an app's *declared* custom dimensions (see Configure → Custom dimensions),
Dedicated tools and the `query` tool normalize the label to its wire form
(`mobile speed` → `mobile_speed`) for both the split value and
`custom-dimension-<label>` filters, and adds a soft `dimension_notes` advisory
when a name isn't in the declared set — but always passes it through (the
declared list may be incomplete). A split name missing from both the built-in
catalog and app settings is described as unclassified: it may be a newer
system dimension, rather than necessarily a custom dimension. Explicit
`custom-dimension-*` filters still receive custom-label guidance.
The verified built-in dimensions include `device_memory`, `page_domain`,
`page_cdn_cache_status`, `bfcache_navigation`, and `bfcache_not_restored_reason`
on all three dimension endpoints. These do not require custom registrations.
`list_custom_dimensions(app)` and
`describe_query(query_type, app)` expose the declared names so the model uses
the exact label instead of guessing.

**Passing multiple values (mechanism differs per parameter).** Verified against
every endpoint's reference doc:

- **Comma-separated, one param** — only `metrics` on `metrics-by-dimension`
  (**plural** param name; the singular `metric` is silently ignored). Pass a
  pre-joined string: `"metrics": "beacons,largest_contentful_paint"`.
- **Repeated key** — `metric`/`timer` on `timers-metrics` and **every drilldown
  filter** (`country`, `browser`, `ab-test`, …) and `custom-dimension-<label>`.
  Pass a **JSON list** and the client expands it to repeated query keys:
  `"custom-dimension-branch": ["uk","us","sec"]` →
  `custom-dimension-branch=uk&…=us&…=sec` (verified live). A Python-list-repr no
  longer leaks onto the wire.
- **Single value** — `dimension`, `percentile`, `metric` on
  `metric-per-page-load-time` (its own enum: `BounceRate` + `CustomMetric0-9`),
  etc.

`describe_query` surfaces a `parameter_mechanics` block (which params are
comma-separated vs repeat-key) plus an `endpoint_params` block — the
**per-endpoint** contract compiled from every reference page: value params with
defaults, special params (`limit`, `sortby`, `interval`, `series-format`) with
ranges, whether drilldown filters apply, and constraints. So the model knows,
before calling, e.g. that `metrics-by-dimension` takes native `sortby`/`limit`
(1-100; the `query` tool sends its `limit`, default 100), computes
percentiles, and returns a `{columnNames, data:[[…]]}` table; that
`dimension-values` accepts **no** filters; that `dimension-over-time` has its own
dimension enum plus `interval`/`limit` (1-10); that `metric-per-page-load-time`
uses a distinct single `metric`; and that the report endpoints (`geography`,
`page-groups`, `browsers`, `ab-tests`, `bandwidth`) have **no** native
`limit`/`sortby` (use the client-side `limit`).

**Silent-fallback `warning` (output side).** As a backstop for cases input
resolution can't catch, when the series id/name echoed in the response does not
match the requested `timer`/`metric`, the result carries a `warning` so the
caller knows the data is **not** for the requested name. Casing/separator-only
differences are treated as a match and never warn. The `warning` is additive
metadata and appears in `raw` mode too (data stays untouched).

**Payload instrumentation.** Every successful query logs
`payload app=… query=… bytes=… points=…` to **stderr**, for measuring response
size / token cost (e.g. before/after future history-reduction work).

**`limit` — high-cardinality caps.** `dimension-values`, `geography`,
`page-groups`, `browsers`, … can return hundreds–thousands of rows (verified
live: ~215 countries for `geography`, ~1,400 values for `dimension-values`
browser). On the `query` tool these query-types are trimmed to 100 rows by
default (pass an explicit `limit` to change it); when trimmed, the result carries
`truncated: {by: "client", key, total, returned[, sorted_by]}` so nothing
silently disappears.

`metrics-by-dimension`, `app-error-summary` and `dimension-over-time` are
different: mPulse itself caps their rows and returns only its default top 10
without a native `limit`. `get_breakdown` and the `query` tool send `limit` to
mPulse for metrics-by-dimension (default 100); `query` does the same for the
other two (default 100, or 10 for dimension-over-time; a `limit` inside
`params` wins). Values outside the supported range are rejected or lowered
before calling. When the response fills the limit, the result carries
`truncated: {by: "server", returned, limit, hint}`, because more rows may exist.

**`get_breakdown` — metric names and row selection.** This dedicated tool wraps
`metrics-by-dimension`: `metrics` is a list of the endpoint's own snake_case
names (for example `largest_contentful_paint`, not the `timers-metrics`
CamelCase enum), and invalid names are passed to mPulse for validation with
did-you-mean suggestions attached to 400 errors. It requests up to 100 rows by
default; the endpoint has no offset or paging, so use `sortby` to select the
top rows by another column or narrow the result with filters. A full page is
marked `truncated.by == "server"`. `percentile` defaults to 50 and applies to
timer-like metrics; `apperror_*` dimensions return their own columns and ignore
`metrics`.
When the rows carry a count field (e.g. geography's `timerN`), they are **sorted
by volume descending before trimming** (`sorted_by`), so the top markets survive
rather than an alphabetical slice; rows without a count (dimension-values'
strings) keep their order. `raw=true` is never trimmed.

**`empty_reason` + `probe` — why is it empty?** An unsupported drilldown
combination and genuine no-traffic both come back empty. Every empty result now
carries an `empty_reason` heuristic (`no_data` / `likely_no_traffic` /
`possibly_unsupported_combo`) at zero cost. Pass `probe=true` (on the explicit
tools or `query`) to spend **one** extra drilldown-free query and get a
comparison reported in a `probe` block: `filtered_empty` means the unfiltered baseline has data, while `no_data` means both are empty. This cannot distinguish a valid filter with no matches from an unsupported combination. It is diagnostic evidence, not proof of unsupported filters.

## Batch aggregate

`get_aggregate` groups all requested metrics **and timers** into one repeated-key
`timers-metrics` request per unique period/percentile and returns only each
cell's period aggregate (`latest`) — the value you'd read for a monthly p75.

```jsonc
get_aggregate(
  metrics=["TotalRequestCount", "TotalTransferSize"],
  periods=[
    {"date_comparator": "Between", "date_start": "2026-06-01",
     "date_end": "2026-07-01", "label": "June"},   // date_end exclusive
    {"date_comparator": "Between", "date_start": "2026-07-01",
     "date_end": "2026-08-01", "label": "July"}
  ],
  percentiles=[50, 75]
)
// -> { "table": [ {"target":"TotalRequestCount","period":"June",
//                  "percentile":75,"value":142}, … 8 rows … ],
//      "percentiles":[50,75], "targets":[…] }
```

Period objects are `{date: "YYYY-MM-DD"}` or `{date_comparator: …}` (`Between`
needs `date_start`+`date_end`; `Last` needs `trailing_seconds`); `label` names
the column. Names are validated/auto-corrected up front (one error, not N).
Two metrics × two periods × two percentiles produce 8 output cells with **4 query calls**, rather than 8. Adding two timers produces 16 cells with the same 4 calls. Duplicate canonical targets and identical period/percentile groups reuse work.

Only a multi-target HTTP 400 triggers per-target recovery. Other group failures produce an error for every affected cell without splitting; other groups still return. Missing, duplicate, or null `latest` values are errors; a different series is never substituted. Zero is a valid value. `requests` reports `planned`, `executed`, and `recovery` query calls; auth and HTTP retries are excluded. `max_combos` (default 24) still guards output cells, `targets × periods × percentiles`, rather than request count.

## Constraints surfaced to the model

The `date` argument selects one calendar day. Use `Between` with `date_start` and exclusive `date_end` for a range, or `Last` with finite numeric `trailing_seconds`. All dedicated tools and known generic endpoints validate these required fields before HTTP. Percentiles must be integers from 1 through 99. The API controls time-series intervals; do not assume every endpoint always uses minute buckets. Timezone, row coverage, and filter compatibility affect interpretation.

## Errors

`401` → one automatic token re-issue + replay, then an auth error. `403` →
mPulse Lite / permission guidance (the Query API is not available on Lite).
`429` → exponential backoff + jitter (up to 5 retries), then a rate-limit error.
Network/timeout/5xx → retried, then a friendly upstream error. Invalid
app/query-type/params → validation errors with hints. All of these are
returned as MCP tool errors (`isError: true`), not as success payloads, so the
client can tell a failure from data. No error message ever
contains a secret.


## Project layout

```
src/mpulse_mcp/
  server.py       FastMCP instance + tool registration (entry point)
  config.py       app registry + credential loading
  auth.py         token mint/cache/refresh (single-flight)
  client.py       HTTP client + rate limiting + retries
  query_types.py  typed loader for catalog query metadata
  formatting.py   loss-free normalization + raw passthrough
  errors.py       typed exceptions + status mapping
tests/            unit tests + smoke.py
```


## Capability alignment (November 2026)

`get_metrics(metric=..., timer=...)` accepts a string or a list for either field, validates every element, canonicalizes known names, and sends repeated keys. `get_summary`, `get_histogram`, and `get_timers` continue to accept one timer. All dedicated tools expose the documented shared filters as strings or lists, including `network_error_code`, plus `custom_dimension_filters={"branch": ["uk", "us"]}`. Empty filter lists are rejected, rather than silently dropping the filter. `get_breakdown` also exposes `custom_timer`. Existing scalar calls remain supported.

Discovery parameter lists use the endpoint catalog: breakdown uses plural `metrics`; `dimension-values` advertises and accepts no drilldown filters. Known parameter cardinality is checked, while unknown generic parameters remain available for API extensions. Generic timer/metric lists receive a warning if returned IDs omit requested names.

## Response reduction and measurement

Response reduction happens **after** the upstream body is received. Downsampling selects up to 60 original, evenly spaced points; it does not sum beacon counts into 60 buckets. A separate peak preserves the maximum. `none` removes the main history for aggregate-only questions while retaining `latest`, endpoints, count, and peak where present. `latest` is the upstream period value, not the final history point; its aggregation semantics depend on the metric. A percentile must not be recomputed by averaging daily percentiles.

Other mechanisms reduce agent context: one JSON text output without a duplicate structured payload, compact on-demand discovery, row limits (native upstream limits where supported, local trimming otherwise), aggregate-only output, cached local catalogs, and early validation that avoids error/retry conversations. Token counts depend on the serialized text and tokenizer; response counts exclude prompts, tool schemas, transport framing, and host additions unless explicitly measured. Broader tool schemas have an input-token cost. Batching reduces query calls and HTTP overhead; it does not inherently reduce the total series points returned by the API.


## API metadata ownership

Static query metadata is maintained in `catalog.json`: endpoint definitions, parameter names, time/filter support, summaries, response descriptions, and aggregation meaning. `query_types.py` validates the required reference structure and exposes typed Python views. Both `parameters` and `value_parameters` in discovery come from the same endpoint contract. No second hard-coded Python API table or silent fallback is retained. Query handlers, HTTP control, and formatting remain separate implementation code. A valid installed catalog preserves existing query behavior; if the package loses that required JSON, startup reports a configuration error.
