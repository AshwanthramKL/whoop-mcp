# WHOOP MCP Server

[![CI](https://github.com/AshwanthramKL/whoop-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/AshwanthramKL/whoop-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/whoop-mcp.svg)](https://pypi.org/project/whoop-mcp/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-6e6eff.svg)](https://modelcontextprotocol.io)
[![Tests](https://img.shields.io/badge/tests-198%20passing-brightgreen.svg)](./tests)

A local Model Context Protocol (MCP) server that gives an LLM **read-only**
access to your WHOOP fitness data. Authentication is direct OAuth against
your own WHOOP developer app — there is no third-party proxy in the path.
All records are mirrored into a local SQLite cache at
`~/.whoop-mcp-server/whoop.db`, and **no data ever leaves your machine**
except for the authenticated calls the server itself makes to the WHOOP
v2 API.

Current version: **0.8.5** — see [CHANGELOG.md](./CHANGELOG.md).

> **Pre-1.0 status.** The API surface (tool names, response shapes, error
> codes) is stabilizing but not frozen. Breaking changes may land in
> `0.x` minor bumps during dogfooding. Patch bumps (`0.8.x`) are
> bugfix-only. 1.0.0 will be cut when the surface has been stable for
> 2+ weeks of real use. Pin the minor version in CI if you're building
> on top of this.

> **Just want to try it?** Copy the prompt in
> [docs/AGENT_INSTALL_PROMPT.md](./docs/AGENT_INSTALL_PROMPT.md), paste
> it into Claude Code / Claude Desktop / Cursor / Windsurf, and your
> agent will do the install end-to-end.
>
> **Building on this repo?** Start with [AGENTS.md](./AGENTS.md) and
> [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) — they're the
> load-bearing conventions and one-page system map.

## Table of contents

- [What this is](#what-this-is)
- [Install (one line via uvx)](#install-one-line-via-uvx)
- [Install with your agent](#install-with-your-agent)
- [Install from source](#install-from-source-for-development)
- [Analysis skill: whoop-insights](#analysis-skill-whoop-insights)
- [Quick start](#quick-start)
- [Tool catalog](#tool-catalog)
- [MCP resources](#mcp-resources)
- [Data model](#data-model)
- [Sync model](#sync-model)
- [Event feed](#event-feed)
- [Exports](#exports)
- [What's not supported](#whats-not-supported)
- [Updating](#updating)
- [Operations](#operations)
- [Security and privacy](#security-and-privacy)
- [Development](#development)
- [For agents building on this repo](#for-agents-building-on-this-repo)
- [Versioning](#versioning)

## What this is

The WHOOP MCP Server is a small Python process that speaks MCP over stdio.
It exposes WHOOP v2 data (profile, body measurement, cycles, recoveries,
sleeps, workouts) to any MCP-capable client — primarily Claude Desktop and
Claude Code. The server is read-only. It authenticates with WHOOP using an
OAuth app you register yourself, so your tokens never travel through a
third-party server. The WHOOP records you fetch are written to a local
SQLite cache (mode `0o600`) so subsequent reads are free and offline, and
the cache file never leaves your machine.

## Install (one line via uvx)

```bash
# 1. Create a WHOOP developer app (one-time, free, self-serve):
#    https://developer-dashboard.whoop.com/apps/create
#    Redirect URI:  http://localhost:8000/callback
#    Scopes:        read:profile read:body_measurement read:recovery
#                   read:cycles read:sleep read:workout offline

# 2. One-shot OAuth — opens browser, catches callback, saves encrypted tokens.
WHOOP_CLIENT_ID="<your client id>" \
WHOOP_CLIENT_SECRET="<your client secret>" \
  uvx --from whoop-mcp whoop-mcp-oauth

# 3. Register with Claude Code. The --env flags are required so the
#    server can refresh tokens when the 1-hour access token expires.
claude mcp add whoop --scope user \
  --env WHOOP_CLIENT_ID="$WHOOP_CLIENT_ID" \
  --env WHOOP_CLIENT_SECRET="$WHOOP_CLIENT_SECRET" \
  -- uvx --from whoop-mcp whoop-mcp
```

No clone, no venv, no absolute paths. Don't have `uv`? Install it once:
`curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv`).

## Install with your agent

Paste the prompt in [docs/AGENT_INSTALL_PROMPT.md](./docs/AGENT_INSTALL_PROMPT.md)
into any MCP-aware agent (Claude Code, Claude Desktop, Cursor,
Windsurf, Zed, Aider). The agent will run the install for you.

## Install from source (for development)

```bash
git clone https://github.com/AshwanthramKL/whoop-mcp.git
cd whoop-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest -q   # 183 tests, ~6s, all respx-mocked
```

Then register with `-- /abs/path/to/whoop-mcp/.venv/bin/python /abs/path/to/whoop-mcp/src/whoop_mcp_server.py`.

For Claude Desktop, add the equivalent entry to
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS),
pointing `command` at the venv Python and `args` at `src/whoop_mcp_server.py`.
Run `./scripts/fresh_install_check.sh` if you want a clean-env smoke
of the install path end-to-end (minus the browser OAuth flow).

## Quick start

Once the server is registered, try these prompts:

1. **Sync the cache.** "Sync my WHOOP data from the last 30 days."
   Claude calls `sync_whoop()` and reports per-resource counts.
2. **Inspect a day.** "Give me yesterday's WHOOP daily summary."
   Claude calls `get_whoop_daily_summary(date="YYYY-MM-DD")` and shows a
   joined cycle + recovery + primary sleep + workouts record.
3. **Export.** "Export all my cached workouts to `~/whoop-workouts.csv`."
   Claude calls `export_whoop(kind="workouts", format="csv", path="...")`.

## Analysis skill: whoop-insights

Bundled with the repo: [skills/whoop-insights](./skills/whoop-insights/SKILL.md).

**Install (one command):**

```bash
whoop-mcp-install-skills            # writes to ~/.claude/skills/
```

That fetches the latest skill from this repo's `main` branch and drops
it where Claude Code (and Cursor / Windsurf / Zed) auto-discovers
user-scope skills. Restart your MCP client session and ask:

> *"How am I doing? Run the whoop-insights skill."*
> *"Generate my weekly WHOOP report."*
> *"Am I overtraining? Check the last 30 days."*

The skill pulls 30 days from the cache, computes personal baselines
(HRV, recovery, sleep, strain), flags anomalies with evidence, runs two
correlations (sleep→next-day-recovery, strain→next-day-recovery), and
optionally generates a self-contained HTML dashboard with Chart.js
visualizations. Every claim cites a specific date and a specific number
— the skill is instructed never to fabricate.

**For repo-native developers** iterating on the skill itself:
`whoop-mcp-install-skills --source ./skills` installs from your local
working tree instead of GitHub.

## Tool catalog

17 tools. All list tools accept `start` / `end` as ISO-8601 and auto-paginate.
Every read tool takes `fresh: bool = False` — pass `True` to bypass the
cache and hit the WHOOP API, write-through to cache, and return the live
response. Errors are returned as a structured `{"error": {...}}` envelope;
tools never raise.

| Tool | What it does | Key parameters |
|------|--------------|----------------|
| `get_whoop_auth_status` | Report OAuth token status. Call first if other tools return `AUTH_FAILED`. | — |
| `get_whoop_profile` | Authenticated user's WHOOP profile (name, email). | `fresh` |
| `get_whoop_body_measurement` | Latest body measurements: height, weight, max HR. | `fresh` |
| `list_whoop_cycles` | Physiological cycles in a time window. | `start`, `end`, `limit`, `fresh` |
| `get_whoop_cycle` | One cycle by integer ID. | `cycle_id`, `fresh` |
| `get_whoop_cycle_sleep` | Sleep record tied to a given cycle. | `cycle_id`, `fresh` |
| `get_whoop_cycle_recovery` | Recovery record tied to a given cycle. | `cycle_id`, `fresh` |
| `list_whoop_recoveries` | Recoveries (HRV / RHR / recovery score) in a window. | `start`, `end`, `limit`, `fresh` |
| `list_whoop_sleeps` | Sleep activities incl. naps in a window. | `start`, `end`, `limit`, `fresh` |
| `get_whoop_sleep` | One sleep activity by UUID. | `sleep_id`, `fresh` |
| `list_whoop_workouts` | Workouts with zone durations (seconds). | `start`, `end`, `limit`, `fresh` |
| `get_whoop_workout` | One workout by UUID. | `workout_id`, `fresh` |
| `get_whoop_daily_summary` | Joined cycle + recovery + primary sleep + workouts for a UTC date. | `date` |
| `sync_whoop` | Refresh cache from WHOOP API. Idempotent, incremental by default. | `full`, `since`, `resources` |
| `get_whoop_events` | Chronological "what's new" feed across cached resources. | `since`, `until`, `resources`, `limit` |
| `export_whoop` | Dump cached records to CSV / JSONL / Parquet. | `kind`, `format`, `path`, `start`, `end`, `overwrite` |
| `health_check` | Composite status (auth, API, cache, schema). Never raises. | `live` |

Error codes: `AUTH_FAILED`, `RATE_LIMITED`, `NOT_FOUND`, `UPSTREAM_ERROR`,
`VALIDATION_ERROR`, `CACHE_ERROR`, `CACHE_EMPTY`, `FILE_EXISTS`,
`EXPORT_ERROR`, `SYNC_ERROR`.

## MCP resources

The cache is also exposed as read-only MCP resources, so the client can
browse date slices without invoking a tool.

| URI | Content |
|-----|---------|
| `whoop://db/cycles/{start}/{end}` | Cached cycles in `[start, end)` (dates `YYYY-MM-DD`). |
| `whoop://db/recoveries/{start}/{end}` | Cached recoveries. |
| `whoop://db/sleeps/{start}/{end}` | Cached sleeps (including naps). |
| `whoop://db/workouts/{start}/{end}` | Cached workouts. |
| `whoop://db/profile` | Latest profile snapshot. |
| `whoop://db/body_measurement` | Latest body_measurement snapshot. |
| `whoop://db/sync_runs/{limit}` | Most recent sync audit rows. |
| `whoop://db/events/{since}` | Event feed since `since`, `until` = now. |
| `whoop://db/events/{since}/{until}` | Event feed for explicit window. |

All resources return `application/json`. Bad inputs return the same
`{"error": {"code","message"}}` envelope used by tools.

## Data model

Responses are **flattened**: the WHOOP `score` wrapper is lifted, milliseconds
become seconds (`*_seconds`, 1 decimal), kilojoules become calories
(`calories`, rounded int), heart-rate keys are renamed to `avg_hr_bpm` /
`max_hr_bpm`, and sleep stages are named `deep_sleep_seconds`,
`rem_sleep_seconds`, `light_sleep_seconds`, `awake_seconds`,
`in_bed_seconds`. Raw `user_id`, `v1_id`, and per-record `created_at` are
dropped.

**`score_state` convention.** WHOOP scores are not always computed. When
`score_state != "SCORED"` (e.g. `PENDING_SCORE`, `UNSCORABLE`) every
derived score field is `null` and the top-level `score_state` is preserved
so callers know why.

One flattened record per resource:

```json
// Cycle
{
  "id": 123456,
  "start": "2026-04-20T04:00:00.000Z",
  "end": "2026-04-21T04:00:00.000Z",
  "timezone_offset": "+00:00",
  "score_state": "SCORED",
  "strain": 12.4,
  "avg_hr_bpm": 62,
  "max_hr_bpm": 168,
  "calories": 2810
}
```

```json
// Recovery
{
  "cycle_id": 123456,
  "sleep_id": "bb68db7b-...",
  "score_state": "SCORED",
  "recovery_score": 74,
  "resting_heart_rate_bpm": 48,
  "hrv_rmssd_ms": 87.3,
  "spo2_pct": 97.4,
  "skin_temp_c": 33.1,
  "user_calibrating": false
}
```

```json
// Sleep
{
  "id": "bb68db7b-...",
  "cycle_id": 123456,
  "start": "2026-04-20T03:10:00.000Z",
  "end": "2026-04-20T10:42:00.000Z",
  "nap": false,
  "score_state": "SCORED",
  "sleep_performance_pct": 88.0,
  "in_bed_seconds": 27120.0,
  "light_sleep_seconds": 12540.0,
  "rem_sleep_seconds": 5280.0,
  "deep_sleep_seconds": 6840.0,
  "awake_seconds": 1080.0
}
```

```json
// Workout
{
  "id": "a91f...",
  "start": "2026-04-20T17:00:00.000Z",
  "end": "2026-04-20T17:48:00.000Z",
  "sport_name": "Running",
  "score_state": "SCORED",
  "strain": 9.3,
  "avg_hr_bpm": 142,
  "max_hr_bpm": 176,
  "calories": 511,
  "distance_meter": 8030.0,
  "zone_durations_seconds": {
    "zone_zero": 0.0, "zone_one": 120.0, "zone_two": 900.0,
    "zone_three": 1440.0, "zone_four": 420.0, "zone_five": 0.0
  }
}
```

## Sync model

- **Cache-first reads.** Every list/get tool reads from SQLite by default.
  An empty window transparently triggers a targeted sync and re-reads.
- **`fresh=True`** bypasses the cache, hits the WHOOP API, write-throughs
  the result into the cache, and returns the live response.
- **Incremental sync** uses `MAX(updated_at)` per resource as the cursor.
  A fresh DB falls back to the last 90 days. `sync_whoop(full=True)` pulls
  from 2010-01-01 for every resource (do this once on a brand-new cache).
- **Idempotent.** Re-running `sync_whoop` with no new upstream data is a
  no-op. Combined with snapshot hash dedupe (below), this means the event
  feed stays quiet.
- **Snapshot hash dedupe.** `profile` and `body_measurement` are singleton
  snapshots. Before writing, the server compares SHA-256 of the canonical
  raw payload against the stored blob. Byte-identical payloads do not
  advance `updated_at`, so no spurious events are generated.

## Event feed

`get_whoop_events(since, until=None, resources=None, limit=500)` is a
chronological feed across all cached resources — pure cache read, no API
calls. The window is **exclusive on both ends**:
`updated_at > since AND updated_at < until`. The strict `since` bound
means you can feed a returned cursor back in as the next `since` without
re-seeing a row.

Each event wraps a flat record:

```json
{"resource": "sleeps",
 "id": "bb68db7b-...",
 "updated_at": "2026-04-20T14:12:33.123Z",
 "record": { "...flat sleep..." }}
```

Response:

```json
{"status": "success", "count": 17,
 "since": "...", "until": "...",
 "events": [...],
 "next_cursor": null | "<opaque-base64>"}
```

`next_cursor` is an **opaque** base64 encoding of
`updated_at|resource|id` for the last returned event. Pass it back as
`since` to continue. The full triple is used as a tie-broken lower bound,
so events sharing an `updated_at` are never skipped at a pagination
boundary. Plain ISO-8601 strings as `since` still work (M5 compat).

Also exposed as MCP resources — see the [MCP resources](#mcp-resources)
table above.

## Exports

`export_whoop(kind, format, path, start=None, end=None, overwrite=False)`
writes flat cached records to disk. Pure data layer — never hits the API.
Run `sync_whoop()` first.

- **`kind`**: `cycles` | `recoveries` | `sleeps` | `workouts` | `all`.
  `"all"` writes one file per resource (`cycles.*`, `recoveries.*`, …)
  into the given directory.
- **`format`**: `csv` (RFC 4180, alphabetically sorted header, nested
  values JSON-encoded), `jsonl` (one record per line, sorted keys), or
  `parquet` (pyarrow, snappy).
- **`overwrite`**: default `False`. If the destination has content,
  returns `FILE_EXISTS`. `True` replaces silently.

An empty window still yields a file (header-only CSV / empty JSONL /
empty Parquet) so downstream tooling sees a consistent artifact.

## What's not supported

Spelled out so nobody wastes an issue.

| Ask | Why not |
|-----|---------|
| `claude.ai` (the web app) | We ship as a **stdio** MCP (local subprocess). `claude.ai` wants a remote HTTPS MCP. A hosted deployment would also break our single-user / local-only threat model. |
| Mobile Claude apps | Same stdio constraint. |
| Writing back to WHOOP (logging activities, updating weight, etc.) | Read-only by design. Adding writes would require reverse-engineered endpoints outside WHOOP's stable v2 API — too brittle and ToS-risky. See [`jd1207/whoop-mcp`](https://github.com/jd1207/whoop-mcp) if you need that. |
| Multi-user / shared server | One user, one WHOOP account, one machine. The cache + encrypted tokens live under `~/.whoop-mcp-server/`; there's no tenancy model. |
| More than 10 users per WHOOP dev app | WHOOP's dev-app cap is 10 users until app-level approval. Each user should create their own dev app — it's free and self-serve. |
| Push notifications / webhooks | Event feed is poll-driven from the cache. For proactive alerts, run `sync_whoop` + `get_whoop_events` on a cron from whatever scheduler you already have. |
| Python < 3.10 | The code uses `X \| Y` union syntax. `pip install` will refuse on older Pythons. |
| Windows-specific install paths | Code works, but OAuth callback (`localhost:8000`) and path handling haven't been dogfooded on Windows. Ubuntu + macOS are the currently-tested surface. |

## Updating

MCP servers don't auto-update. When you installed is when you pinned.
How to pick up a new release depends on how you installed:

| Install path | How to update |
|---|---|
| `uvx --from whoop-mcp whoop-mcp` | `uvx --refresh --from whoop-mcp whoop-mcp`, or `uv cache clean whoop-mcp` and re-invoke |
| `uvx --from whoop-mcp@latest whoop-mcp` | Update happens on next cache miss (no manual step) |
| `pipx install whoop-mcp` | `pipx upgrade whoop-mcp` |
| `pip install whoop-mcp` | `pip install --upgrade whoop-mcp` |
| Git clone + venv | `git pull && pip install -r requirements.txt` |

Run `health_check()` afterwards — its `pypi_update_available` component
will confirm the version your MCP client is now running and whether a
newer one is published.

Breaking-change policy: while the project is `0.x`, minor bumps
(`0.8.x` → `0.9.0`) may include breaking changes — always called out in
the `### Changed` section of [CHANGELOG.md](./CHANGELOG.md). Patch
bumps (`0.8.3` → `0.8.5`) are bugfix-only. Once we cut `1.0.0`,
breaking changes require a major bump. Pin to a known-good `0.x.y` if
you can't absorb churn.

## Operations

- **Logs.** Stderr (always, structured JSON) plus a rotating file at
  `~/.whoop-mcp-server/logs/whoop-mcp.log` (~1 MB per file, 5 backups).
  Env overrides: `WHOOP_LOG_LEVEL` (default `INFO`), `WHOOP_LOG_FILE`
  (path; empty string disables the file handler), `WHOOP_LOG_JSON`
  (default `true`).
- **`health_check(live=True)`** returns a dict with five component
  checks (`auth`, `api_reachable`, `cache_readable`, `cache_writable`,
  `schema_version`) plus an overall verdict `healthy | degraded |
  unhealthy`. `live=False` skips the network probe.
- **Token refresh.** Refresh tokens are used automatically when the
  access token is within 5 minutes of expiry. An async refresh lock
  prevents stampedes when multiple in-flight requests discover the same
  expired token. If refresh fails beyond recovery, re-run
  `whoop-mcp-oauth` (after `pip install`) or
  `.venv/bin/python src/setup_direct_oauth.py` (from source).
- **Rate limiting.** The client respects `Retry-After` on 429s and
  retries with exponential backoff. After the retry budget, the call
  surfaces as `RATE_LIMITED` — tools never raise.

## Security and privacy

Everything runs locally. OAuth tokens are encrypted at rest in
`~/.whoop-mcp-server/tokens.json` with a key at
`~/.whoop-mcp-server/.encryption_key`. The SQLite cache file is created
with mode `0o600`. No third-party services — the only outbound network
calls are directly to `api.prod.whoop.com`. Logs do **not** include
tokens, refresh tokens, the client secret, or raw WHOOP response bodies.
See [PRIVACY.md](./PRIVACY.md) for the complete inventory of what the
server reads, writes, sends, and logs; and how to delete everything
(`rm -rf ~/.whoop-mcp-server`).

## Development

```bash
# Run the full test suite (183 tests).
.venv/bin/pytest -q

# Re-record fixtures (live calls, requires creds in env).
.venv/bin/python tests/record_fixtures.py

# Fresh-install smoke test (clones current HEAD into a tempdir and
# verifies the install path end-to-end, minus the browser OAuth flow).
bash scripts/fresh_install_check.sh
```

**Adding a new tool.** Add the implementation under `src/whoop_mcp_server.py`
with an `@mcp.tool()` decorator, a Pydantic model in `src/whoop_models.py`
if the response has a new shape, a cache table in `src/whoop_store.py` if
the resource is persisted, and tests in `tests/`. Keep the error envelope
(`_error_payload` / `_map_error`) — tools never raise.

**Release flow.** Bump `src/__version__.py` (single source of truth) →
add a new top section to [CHANGELOG.md](./CHANGELOG.md) in
Keep-a-Changelog format → verify `SERVER_VERSION` in
`whoop_mcp_server.py` picks up the new value (the `test_version_import`
test prevents drift) → update `version` in `pyproject.toml` → tag
`vX.Y.Z` on `main`.

## For agents building on this repo

Start here:

- **[AGENTS.md](./AGENTS.md)** — load-bearing conventions. File layout,
  error-envelope discipline, TDD loop, what-not-to-do, release flow.
  Read before editing anything.
- **[docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)** — one-page system
  map. Four-layer diagram, read/write/sync/event paths, invariants.
- **[docs/AGENT_INSTALL_PROMPT.md](./docs/AGENT_INSTALL_PROMPT.md)** —
  the copy-pasteable prompt end users give their agent to install.

Human contributors: [CONTRIBUTING.md](./CONTRIBUTING.md) has the short
version. The test suite runs in ~6s (`.venv/bin/pytest -q`); every HTTP
call is respx-mocked so you can iterate offline.

## Versioning

Current version: **0.8.5** (see `src/__version__.py`). Semantic
versioning. Full history: [CHANGELOG.md](./CHANGELOG.md).

## Credits

Forked from [RomanEvstigneev/whoop-mcp-server](https://github.com/RomanEvstigneev/whoop-mcp-server)
(v0.1.x). Substantially rewritten starting at v0.2.0 to use direct WHOOP
OAuth (no third-party proxy), the WHOOP v2 API, Pydantic-flattened
responses, a local SQLite cache with incremental sync, cache-first
reads, an event feed, exports (CSV/JSONL/Parquet), a `health_check`
tool, structured JSON logging, and 190+ tests (190 as of 0.8.5). Licensed MIT — see
[LICENSE](./LICENSE) for both copyright lines.

---

<!-- MCP Registry ownership proof. Do not remove. -->

mcp-name: io.github.AshwanthramKL/whoop-mcp
