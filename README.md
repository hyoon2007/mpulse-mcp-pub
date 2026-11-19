# mPulse MCP Server

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![MCP](https://img.shields.io/badge/MCP-stdio-purple)

Ask your AI assistant about real-user web performance. This MCP server connects
Claude, Cursor, VS Code and other MCP clients to the **Akamai mPulse Query API**,
so you can query page load times, Core Web Vitals, percentiles and breakdowns by
country, browser, page group or custom dimension in plain language.

- **Exact numbers.** Aggregates are never rounded or summarized.
- **Token-efficient.** Per-minute time series are downsampled by default, each
  result is sent once as a single JSON block, and a batch tool returns a whole
  metric × period × percentile table in one call.
- **Hard to misuse.** Metric and timer names are validated against the mPulse
  docs, silent mPulse fallbacks are reported as errors or warnings, and every
  result states what was queried (`period.resolved`) and how to read the
  numbers (`aggregation`).

## Prerequisites

- An **mPulse account** with API access (mPulse Lite is not supported).
- A pre-issued **mPulse API token**: in mPulse, click your name (top right) →
  **Account** → **Generate/Revoke API Token**.
- Your **tenant** name and each app's **API key** (shown in the app's
  configuration in mPulse).
- [**uv**](https://docs.astral.sh/uv/getting-started/installation/), which
  provides the `uvx` command used below.
- Git, which `uvx` uses to fetch the server from GitHub.

## Installation

No clone is needed. `uvx` downloads and caches the server on first run. To test
it:

```bash
uvx --from git+https://github.com/hyoon2007/mpulse-mcp-pub.git mpulse-mcp
```

It prints startup logs to stderr and then waits for an MCP client on stdin.
Press Ctrl+C to stop it.

## Configuration

**1. Create an app registry file**, for example `~/mpulse_apps.json`:

```json
{
  "default_app": "my-site",
  "tenant": "your-tenant",
  "apps": {
    "my-site":  { "api_key": "XXXXX-XXXXX-XXXXX-XXXXX-XXXXX" },
    "my-store": { "api_key": "YYYYY-YYYYY-YYYYY-YYYYY-YYYYY" }
  }
}
```

The registry can contain sensitive app and tenant identifiers; keep it private
and do not commit it. When a tool call doesn't name an app, the server uses
`default_app`.

**2. Set two environment variables** in your MCP client's config (examples below):

| Variable | Value |
|---|---|
| `MPULSE_API_TOKEN` | Your pre-issued mPulse API token |
| `MPULSE_APPS_CONFIG` | Absolute path to the registry file |

Optional: if you declare custom dimensions per app (mPulse has no API to list
them), the assistant can use their exact names. See
[`mpulse_apps.example.json`](mpulse_apps.example.json).

## Client setup

Replace `your-token` and `/absolute/path/to/mpulse_apps.json` in each example.

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "mpulse": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/hyoon2007/mpulse-mcp-pub.git", "mpulse-mcp"],
      "env": {
        "MPULSE_API_TOKEN": "your-token",
        "MPULSE_APPS_CONFIG": "/absolute/path/to/mpulse_apps.json"
      }
    }
  }
}
```

Quit Claude Desktop completely (⌘Q on macOS) and reopen it. Closing the window
doesn't restart the server. If the server fails to start, set `command` to the
full path that `which uvx` prints.

### Claude Code

```bash
claude mcp add --env MPULSE_API_TOKEN=your-token \
  --env MPULSE_APPS_CONFIG=/absolute/path/to/mpulse_apps.json \
  --transport stdio --scope user mpulse \
  -- uvx --from git+https://github.com/hyoon2007/mpulse-mcp-pub.git mpulse-mcp
```

### Cursor

Add to `~/.cursor/mcp.json`, or `.cursor/mcp.json` in a project:

```json
{
  "mcpServers": {
    "mpulse": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/hyoon2007/mpulse-mcp-pub.git", "mpulse-mcp"],
      "env": {
        "MPULSE_API_TOKEN": "your-token",
        "MPULSE_APPS_CONFIG": "/absolute/path/to/mpulse_apps.json"
      }
    }
  }
}
```

### VS Code (GitHub Copilot)

Add to `.vscode/mcp.json` in your workspace. VS Code uses `servers` as the
top-level key, not `mcpServers`:

```json
{
  "servers": {
    "mpulse": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/hyoon2007/mpulse-mcp-pub.git", "mpulse-mcp"],
      "env": {
        "MPULSE_API_TOKEN": "your-token",
        "MPULSE_APPS_CONFIG": "/absolute/path/to/mpulse_apps.json"
      }
    }
  }
}
```

Or add it to your user profile from the command line:

```bash
code --add-mcp '{"name":"mpulse","type":"stdio","command":"uvx","args":["--from","git+https://github.com/hyoon2007/mpulse-mcp-pub.git","mpulse-mcp"],"env":{"MPULSE_API_TOKEN":"your-token","MPULSE_APPS_CONFIG":"/absolute/path/to/mpulse_apps.json"}}'
```

If the server fails to start, set `command` to the full path that `which uvx`
prints — VS Code may not have `~/.local/bin` on its `PATH`.

### Windsurf

Add to `~/.codeium/windsurf/mcp_config.json`, using the same `mcpServers` JSON as
[Cursor](#cursor).

### Gemini CLI

Add to `~/.gemini/settings.json`, or `.gemini/settings.json` in a project, using
the same `mcpServers` JSON as [Cursor](#cursor).

### OpenAI Codex CLI

```bash
codex mcp add mpulse \
  --env MPULSE_API_TOKEN=your-token \
  --env MPULSE_APPS_CONFIG=/absolute/path/to/mpulse_apps.json \
  -- uvx --from git+https://github.com/hyoon2007/mpulse-mcp-pub.git mpulse-mcp
```

Or add to `~/.codex/config.toml`:

```toml
[mcp_servers.mpulse]
command = "uvx"
args = ["--from", "git+https://github.com/hyoon2007/mpulse-mcp-pub.git", "mpulse-mcp"]

[mcp_servers.mpulse.env]
MPULSE_API_TOKEN = "your-token"
MPULSE_APPS_CONFIG = "/absolute/path/to/mpulse_apps.json"
```

## Example interactions

Your assistant now answers questions about real-user performance. Try asking:

| Ask | What the server does |
|---|---|
| "What was yesterday's page load summary for my-site?" | `get_summary`: median, p95, p98, beacon count |
| "Show LCP p75 by country for the last 24 hours." | `get_breakdown` with `metrics=["largest_contentful_paint"]` and `percentile=75` |
| "Compare TotalRequestCount and TotalTransferSize p50 and p75 for June vs July." | `get_aggregate`: the whole table in one call |
| "When did page load spike yesterday?" | `get_timers`: per-minute series, downsampled, with the `peak` |
| "Break down beacons by the `branch` custom dimension." | `get_breakdown` with `dimension="branch"` |
| "Which apps can I query, and what metrics are valid for timers-metrics?" | `list_apps`, `describe_query` |

## Available tools

| Tool | Description |
|---|---|
| `get_summary` | Median, margin of error, count, p95 and p98 for one timer |
| `get_histogram` | Distribution buckets for one timer (use for exact percentiles) |
| `get_timers` | One timer by minute over time |
| `get_metrics` | Multiple timers and metrics by minute over time |
| `get_aggregate` | Batch metrics and timers per period/percentile; compact result table |
| `get_breakdown` | Metric values broken down by a built-in or custom dimension |
| `query` | Any mPulse query-type, such as `metrics-by-dimension`, `geography` or `dimension-values` |
| `list_apps` | Registered apps and the default app |
| `list_custom_dimensions` | An app's declared custom dimensions |
| `list_query_types` | Supported query-types |
| `describe_query` | Valid parameters, enums and constraints for a query-type |

## Good to know

- **One calendar day per `date`.** For longer periods, use
  `date_comparator="Between"` with `date_start` and `date_end` (`date_end` is
  exclusive), or ask for one call per day.
- **Relative windows** are `Last30Minutes`, `LastHour`, `Last3Hours`,
  `Last12Hours`, `Last24Hours`, `Today`, `ThisWeek` and `ThisMonth`. There is no
  `Yesterday` or `Last7Days`: ask for a date or a range instead.
- **`ThisWeek` and `ThisMonth` are trailing windows**, not calendar periods:
  `ThisWeek` runs from 00:00 seven days ago and `ThisMonth` from 00:00 on the
  same day last month, both until now. For a calendar month, ask for that month
  explicitly. Every result shows the exact range queried in `period.resolved`.
- **Timezone** defaults to UTC. Ask for your local zone, for example
  `Asia/Seoul`.
- **Read `latest` for period percentiles.** A monthly p75 is the period
  aggregate, not the average of daily values. Each result's `aggregation` field
  states how to read its numbers (period aggregate, per-minute series, …).
- **Breakdowns return up to 100 rows** with `get_breakdown` (the mPulse maximum;
  10 for `dimension-over-time` via `query`). The server requests the maximum by
  default — mPulse alone would return only the top 10 — and a `truncated` note
  tells you when more rows may exist.
- **Rate limits:** mPulse allows 3 concurrent requests and 100 per minute. The
  server throttles and retries for you.

More detail: [docs/DETAILS.md](docs/DETAILS.md) (tool behavior and output
format) and [CATALOG.md](CATALOG.md) (mPulse names, enums and gotchas).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `403 Forbidden` | The account is mPulse Lite, or the API token lacks query access. |
| `Environment variable 'MPULSE_API_TOKEN' is not set` | Add it to the `env` block of your client config. |
| Server doesn't start (`spawn uvx ENOENT`) | Use the full path from `which uvx` as `command`. |
| `Repository not found` on first run | Check the GitHub repository URL and your access to it. |
| Changes don't take effect | Fully restart the client. |

Claude Desktop logs are in `~/Library/Logs/Claude/mcp-server-mpulse.log`
(macOS). The server writes all logs to stderr and masks secrets.

## Development

```bash
git clone https://github.com/hyoon2007/mpulse-mcp-pub.git
cd mpulse-mcp
uv sync --extra dev
uv run pytest
```

The default branch is `main`.

To run the server from your clone, use
`uv --directory /path/to/mpulse-mcp run mpulse-mcp` as the client command.
[`tests/smoke.py`](tests/smoke.py) runs a summary query against the live API
with your real credentials. It reads `.env` from the project root (see
[`.env.example`](.env.example)).

### Batch and filter improvements

`get_metrics` accepts `metric` and `timer` as a string or a list. Shared filters on dedicated tools also accept strings or lists, with `custom_dimension_filters` for custom labels. `Between` boundaries and `Last` trailing seconds are available across the dedicated tools. Invalid known periods and percentiles are rejected before HTTP.

`get_aggregate` sends all metrics and timers together per unique period/percentile: 2 metrics × 2 periods × 2 percentiles = 4 queries, 8 cells; adding 2 timers still uses 4 queries, 16 cells. A multi-target HTTP 400 can recover through individual queries. Missing series produce cell errors rather than another series' value. Its `requests` counts exclude authentication and HTTP retries. See [implementation details](docs/DETAILS.md#batch-aggregate).
