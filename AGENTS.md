# AGENTS.md

Conventions for AI agents (Claude Code, Cursor, Windsurf, Zed, Aider, …)
working in this repo. Human contributors: [CONTRIBUTING.md](./CONTRIBUTING.md)
has the short version; this file has the details.

**If you're an agent reading this, these rules are load-bearing. Do not
invent new ones.**

---

## 1. Project shape in one paragraph

Python 3.10+, stdio MCP server, local SQLite cache. Talks to the WHOOP
v2 API, flattens responses through Pydantic v2 models, persists
everything in `~/.whoop-mcp-server/whoop.db`, and exposes 17 tools +
9 MCP resources. Read-only by design. `src/__version__.py` is the
single source of truth for the version number.

## 2. Before you touch anything

```bash
cd /path/to/whoop-mcp
.venv/bin/pytest -q          # must be all green at baseline
.venv/bin/ruff check src/ tests/   # must be clean
.venv/bin/mypy src/                # must be clean
```

If any of the three is not green, stop and report. Don't build on broken ground.

**Install the pre-commit hooks once:** `.venv/bin/pre-commit install`.
After that, ruff + ruff-format + mypy run automatically on `git commit`,
so you'll catch drift at commit time instead of at CI time.

Read, in order, before your first edit:
1. `README.md` § "Tool catalog" and § "Data model"
2. `PRIVACY.md` (what must never appear in logs or errors)
3. `CHANGELOG.md` (understand the recent bug history — you'll recognize the patterns)
4. This file.

## 3. File layout

| Path | What's in it |
|------|--------------|
| `src/__version__.py` | Single source of truth for version |
| `src/whoop_mcp_server.py` | MCP tool declarations, error envelope mapping |
| `src/whoop_client.py` | Async httpx client, retries, pagination, typed exceptions |
| `src/whoop_models.py` | Pydantic v2 models + `.flatten()` methods per resource |
| `src/whoop_store.py` | SQLite persistence, upsert, range/overlap queries, events |
| `src/whoop_sync.py` | Incremental sync orchestration, cursor management |
| `src/whoop_export.py` | CSV/JSONL/Parquet writers |
| `src/whoop_logging.py` | JSON formatter + rotating file handler |
| `src/auth_manager.py` | Encrypted token storage + async refresh lock |
| `src/config.py` | Endpoints, scopes, paths, env vars |
| `src/setup_direct_oauth.py` | One-shot OAuth bootstrap (opens browser, catches callback). Exposed as the `whoop-mcp-oauth` console script after `pip install`. |
| `tests/` | pytest + respx. All HTTP calls mocked; fixtures under `tests/fixtures/` are redacted. |
| `scripts/fresh_install_check.sh` | Manual smoke for fresh-install path |
| `docs/ARCHITECTURE.md` | One-page system map |

Keep tools thin. Business logic lives in `whoop_client` / `whoop_store`
/ `whoop_sync` / `whoop_export`, not in `whoop_mcp_server`.

## 4. The error envelope is non-negotiable

**Tools NEVER raise.** Every tool catches and returns:

```python
{"error": {"code": "<MACHINE_CODE>", "message": "<human message>", "endpoint": "<tool or path>"}}
```

Valid codes (expand the set only when you're adding a genuinely new category):

- `AUTH_FAILED` — OAuth / 401
- `RATE_LIMITED` — 429 exhausted
- `NOT_FOUND` — 404 / missing resource
- `UPSTREAM_ERROR` — 5xx / network / unexpected exception
- `VALIDATION_ERROR` — bad input (dates, enums, ranges) — fail BEFORE the network
- `CACHE_ERROR` — local store failure
- `CACHE_EMPTY` — export requested on a never-synced resource
- `FILE_EXISTS` — export path conflict without `overwrite=True`
- `EXPORT_ERROR` — writer failure
- `SYNC_ERROR` — sync orchestration failure

Never leak a traceback. Never put a token / secret / raw response body
in a message.

## 5. stdout is protocol. stderr is logs.

MCP speaks JSON-RPC over stdin/stdout. **A stray `print()` on stdout
will break every client.** Use the configured logger:

```python
import logging
logger = logging.getLogger(__name__)
logger.info("sync_start", extra={"resource": "cycles"})
```

`whoop_logging.setup()` wires JSON formatting + rotating file handler +
stderr handler. Don't reconfigure logging yourself.

## 6. TDD loop

Adding a feature means this sequence:

1. Write a failing test. Commit it red.
2. Minimum implementation to turn it green.
3. Refactor for clarity.
4. Re-run `.venv/bin/pytest -q`. All green.
5. If you added a tool: update README tool catalog + add a CHANGELOG entry.
6. Only then commit green.

Every HTTP interaction in tests uses `respx` against the mocked routes.
**Never hit the real WHOOP API from a test.** Live probes go in
`scripts/` or one-off Python commands, not in the test suite.

## 7. Flattening and units

WHOOP v2 returns metric-and-millisecond shapes. We flatten to LLM-friendly
shapes:

| WHOOP raw | Our output |
|-----------|------------|
| `total_in_bed_time_milli: 25920000` | `in_bed_seconds: 25920.0` |
| `score.strain: 12.4` | `strain: 12.4` (lifted) |
| `kilojoule: 11768.2` | `calories: 2813` (rounded) |
| `score.average_heart_rate: 62` | `avg_hr_bpm: 62` |
| `score.max_heart_rate: 168` | `max_hr_bpm: 168` |
| `hrv_rmssd_milli: 87.3` | `hrv_rmssd_ms: 87.3` |
| `spo2_percentage: 97.4` | `spo2_pct: 97.4` |
| `skin_temp_celsius: 33.1` | `skin_temp_c: 33.1` |
| `sleep_performance_percentage: 88` | `sleep_performance_pct: 88` |

Rule: a downstream reader should be able to tell the unit from the key
name alone. No WHOOP-style camelCase leaks. No raw `user_id` / `v1_id` /
per-record metadata.

## 8. Release flow

1. Bump `src/__version__.py`
2. Bump `pyproject.toml` `version` field (the `test_version_import`
   test will fail if they drift)
3. Prepend a `## [X.Y.Z] - YYYY-MM-DD` entry in Keep-a-Changelog format
   to `CHANGELOG.md`. Group under `### Added / Changed / Fixed / Removed`.
4. `.venv/bin/pytest -q` must be green.
5. Commit. `git tag -a vX.Y.Z -m "..."`. Push.
6. `gh release create vX.Y.Z` with release notes cribbed from the
   CHANGELOG entry.

Version drift between `__version__.py`, `pyproject.toml`, and
`whoop_mcp_server.py`'s `SERVER_VERSION` is caught by
`tests/test_version_import.py`.

## 9. Don't

| Anti-pattern | Why |
|---|---|
| Raise from a tool | Breaks the "tools never raise" invariant |
| `print()` on stdout | Breaks MCP JSON-RPC |
| Log tokens / secrets / full bodies | Violates PRIVACY.md |
| Commit real user data, emails, `user_id`s | Redact before writing fixtures |
| Hit live WHOOP API from a test | Tests must be hermetic (respx) |
| Add a Node/TS shim | This is Python-only by design |
| Add write tools | Read-only is the trust moat. ToS risk. |
| Invent a new error code without documenting it | Keep the set tight — update § 4 if you truly need a new one |
| Use camelCase in output keys | Snake_case only — see § 7 |
| Modify `src/whoop_client.py` to pass date-only strings | WHOOP v2 requires full ISO-8601; see v0.7.2 for the scar |
| Filter by `start` alone in range queries | Must use overlap semantics; see v0.7.3 for the scar |
| Store recoveries with raw `start`/`end` | Inherit from parent cycle at upsert; see v0.7.4 for the scar |

The "scars" above are real bugs we hit in the wild. The regression
tests that guard against them are in `tests/test_whoop_store.py` and
`tests/test_whoop_client.py`. Respect them.

## 10. When you're unsure

Read `CHANGELOG.md` for the relevant version. Every user-visible change
has a `Fixed`/`Changed`/`Added` note explaining why. If the CHANGELOG
doesn't answer it, open a GitHub issue or ask a human. Don't guess and
commit.
