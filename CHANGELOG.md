# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.4] - 2026-04-21
### Fixed
- Recoveries stored with NULL `start`/`end` because WHOOP v2 recovery payloads carry no own timestamps (recoveries are attached to a cycle). Cache-first reads with a date window returned 0 rows for recoveries even when hundreds existed. Upsert now inherits `start`/`end` from the parent cycle, and a post-sync `backfill_recovery_windows()` pass repairs any rows inserted before their parent cycle existed (race-proof against parallel sync).

## [0.7.3] - 2026-04-21
### Fixed
- Cache-read helpers (`query_range`, `iter_records`) filtered by `start` column inclusion only. WHOOP cycles/sleeps/workouts frequently span across date boundaries (e.g. a cycle starting 21:23 on day N-1 and ending on day N), so cache reads missed records that the live API included. Both helpers now use overlap semantics: a record is returned when its `[start, end]` range intersects the requested window, matching the API. In-progress records (`end IS NULL`) are treated as still ongoing. Cache-first reads (`fresh=False`) and live reads (`fresh=True`) now return the same record IDs for the same window.

## [0.7.2] - 2026-04-21
### Fixed
- `list_whoop_cycles` / `list_whoop_recoveries` / `list_whoop_sleeps` / `list_whoop_workouts` passed date-only (`YYYY-MM-DD`) `start`/`end` through unchanged; WHOOP v2 rejects that form with `404 NOT_FOUND`. Client now normalizes date-only inputs to `YYYY-MM-DDT00:00:00.000Z` before sending. Full ISO-8601 inputs are unchanged.

## [0.7.1] - 2026-04-21
### Changed
- README rewritten as a single cohesive document with a table of contents, a 17-tool catalog, and a data-model section.
- Server `instructions` trimmed to <=300 chars, pointing to README for detail.
- `SERVER_VERSION` now imports from `src/__version__.py` (single source of truth).
- `pyproject.toml` version pinned to `0.7.1`.
### Added
- `CHANGELOG.md` covering M1 through M7.
- `src/__version__.py` — single source of truth for package version.
- `tests/test_version_import.py` — guards against version drift between `__version__.py`, `SERVER_VERSION`, and `pyproject.toml`.
- `tests/test_readme_links.py` — verifies every internal link in README and CHANGELOG resolves.
- `scripts/fresh_install_check.sh` — manual smoke test for the documented install path.
### Removed
- Unused `_LIST_TABLE_BY_RESOURCE` dict from `whoop_mcp_server.py` (verifier flagged).

## [0.7.0] - 2026-04-21
### Added
- `health_check` tool with composite component status and optional live probe.
- Composite events cursor (opaque base64 of `updated_at|resource|id`).
- Snapshot hash dedupe — no spurious events on idempotent syncs.
- JSON structured logging + rotating file handler at `~/.whoop-mcp-server/logs/`.
- Async refresh lock to prevent token-refresh stampedes.
- Fault-injection test suites (API, auth, cache).
- `PRIVACY.md` detailing what the server reads, writes, sends, and logs.
### Changed
- `get_whoop_events.next_cursor` is now opaque (base64), not plain ISO-8601.
### Fixed
- Snapshot `updated_at` no longer bumps on byte-identical payloads.

## [0.6.0] - 2026-04-21
### Added
- `get_whoop_events` cache-only feed tool with `since` / `until` / `resources` / `limit` parameters.
- MCP resources: `whoop://db/events/{since}` and `whoop://db/events/{since}/{until}`.

## [0.5.0] - 2026-04-21
### Added
- `export_whoop` tool supporting CSV, JSONL, and Parquet output.
- `pyarrow` dependency for Parquet writes with snappy compression.

## [0.4.0] - 2026-04-21
### Added
- SQLite local cache at `~/.whoop-mcp-server/whoop.db` with `chmod 600`.
- `sync_whoop` tool with incremental `updated_at` cursor.
- MCP resources: `whoop://db/{cycles,recoveries,sleeps,workouts,profile,body_measurement,sync_runs}`.
- `fresh` parameter on all read tools (cache-first reads by default).

## [0.3.0] - 2026-04-21
### Added
- Pydantic v2 response models for every resource.
- `get_whoop_daily_summary` join tool (cycle + recovery + primary sleep + workouts for a UTC date).
### Changed
- All tool responses now return flattened LLM-friendly shapes (seconds not ms, kcal not kJ, `avg_hr_bpm` / `max_hr_bpm` instead of nested `score.average_heart_rate`).

## [0.2.0] - 2026-04-21
### Changed
- Moved from `/developer/v1` to `/developer/v2` (v1 endpoints deprecated by WHOOP).
- Replaced third-party OAuth proxy with direct WHOOP OAuth using the user's own dev app.
- Added `read:body_measurement` scope.
### Added
- `setup_direct_oauth.py` for local callback OAuth flow.
