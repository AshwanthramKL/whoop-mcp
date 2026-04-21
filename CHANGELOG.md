# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
