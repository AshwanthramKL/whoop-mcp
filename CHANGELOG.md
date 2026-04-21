# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.3] - 2026-04-22
### Added
- **MCP Registry ownership-proof marker** in `README.md`. The registry validates that a `pypi` package's README contains `mcp-name: <namespace>/<server>` to prevent anyone from registering a namespace that points at a PyPI package they don't own. Added an unobtrusive marker at the bottom of the README so `mcp-publisher publish` actually succeeds.

### Fixed
- **Namespace casing in `server.json`**. Registry's `github-oidc` auth grants permission based on the exact case of the GitHub username (`AshwanthramKL`). First publish attempt returned 403 with `io.github.ashwanthramkl/...` because the name didn't match. Fixed to `io.github.AshwanthramKL/whoop-mcp`.

### Process
- Re-published wheel + sdist to PyPI so the registry can verify the marker is present on the published package.

## [0.8.2] - 2026-04-22
### Added
- **`pypi_update_available` component in `health_check`.** MCP servers never auto-update — users who installed last month are running last month's code until they explicitly refresh. The check queries `https://pypi.org/pypi/whoop-mcp/json` with a 3s timeout, compares the installed `SERVER_VERSION` against the latest release, and reports `ok` (up-to-date), `warn` (newer available — includes both versions in the output), or `skipped` (when `live=False` or `WHOOP_UPDATE_CHECK=false`). Five new tests cover up-to-date, newer-available, disabled-by-env, live=False, and pypi-unreachable paths.
- **README "What's not supported" section.** Spells out what we don't and won't do (claude.ai web, mobile, write-back, multi-user, >10 users per dev app, webhooks, Python <3.10, Windows-specific paths). Preempts ~80% of bad issues.
- **README "Updating" section.** Documents how to pull a new release for each install path (uvx `--refresh`, pipx upgrade, pip --upgrade, git pull). Breaking-change policy stated explicitly.
- **Community files.** `.github/ISSUE_TEMPLATE/bug_report.md`, `feature_request.md`, `question.md`, `config.yml` (disables blank issues, routes WHOOP-API and MCP-protocol questions upstream). `.github/PULL_REQUEST_TEMPLATE.md` with a pre-merge checklist. `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1 verbatim, with contact email filled in).
- **`.github/dependabot.yml`.** Weekly PRs for pip dependencies (grouped by dev vs runtime patches to avoid PR spam) and monthly PRs for GitHub Actions.

### Changed
- `health_check` docstring updated to document the new `pypi_update_available` component and the `WHOOP_UPDATE_CHECK` env var opt-out.

## [0.8.1] - 2026-04-22
### Added
- **ruff** (replaces black + isort + flake8). Config in `pyproject.toml` under `[tool.ruff]` — lean rule set (E/W/F/I/UP/B/SIM/RUF), 100-char lines, no docstring policing. Ran across the full codebase: 514 initial findings → 0.
- **mypy** passing cleanly on `src/` (0 errors across 11 source files). `mypy_path = ["src"]` handles the flat-module layout. `warn_return_any` is intentionally off for now — most of our returned shapes come from `json.load`ed dicts (typed `Any`); enable when we type store return shapes as `TypedDict`.
- **Pre-commit hooks** (`.pre-commit-config.yaml`): ruff, ruff-format, mypy, plus stdlib hygiene (trailing whitespace, EOF newline, YAML/TOML validation, large-file guard, private-key detection, merge-conflict detection). Install once via `.venv/bin/pre-commit install`.
- **CI badge** and **PyPI badge** in README (previously static placeholders).
- `pyproject.toml [project.optional-dependencies].dev` now mirrors `requirements-dev.txt` so `pip install whoop-mcp[dev]` just works.

### Changed
- `.github/workflows/ci.yml` rewritten from scratch. Previous file was upstream-inherited legacy (py38/py39 matrix, black/isort/flake8/bandit/safety, a broken `integration-test` job that ran the MCP server for 10 seconds expecting it to exit). New workflow: ubuntu+macos × py310/11/12 matrix running `ruff check` + `ruff format --check` + `mypy src/` + `pytest -q`, plus a build job that runs `python -m build` and validates with `twine check dist/*`.
- `.env.example` rewritten. Previous file referenced `OAUTH_BASE_URL` pointing at the long-removed third-party OAuth proxy and didn't mention `WHOOP_CLIENT_ID` / `WHOOP_CLIENT_SECRET`. New file documents the actual env surface.
- `src/__init__.py` deleted. It was dead code (module namespace `src.*` was never imported), and its presence confused mypy's module resolution. Package metadata (`__author__`, `__email__`) already lives in `pyproject.toml`.

### Fixed
- mypy surfaced a real type issue on `whoop_mcp_server.py::get_whoop_events`: `until_dt` was reassigned from `datetime` to `datetime | None` across `if`/`else` branches, and the unreachable-statement warning was a symptom of mypy losing narrowing. Now explicitly typed and assigned in both branches.
- Minor: `whoop_store.start_sync_run` asserted `cur.lastrowid is not None` (always true on INSERT; placates mypy).
- Minor: `setup_direct_oauth.py` `result` dict given an explicit `dict[str, str | None]` type; `HTTPServer` host argument properly defaulted to `"localhost"` instead of `str | None`.

## [0.8.0] - 2026-04-22
### Added
- **Packaging for PyPI.** `[project.scripts]` entries create two console scripts: `whoop-mcp` (starts the MCP server) and `whoop-mcp-oauth` (runs the one-shot OAuth flow). Install with `pip install whoop-mcp`, or run ephemerally via `uvx --from whoop-mcp whoop-mcp`. No clone, no venv, no absolute paths required in MCP client registration.
- **`server.json` at repo root** — MCP Registry manifest with `mcpName = "io.github.AshwanthramKL/whoop-mcp"`, PyPI package identifier, stdio transport, and documented environment variables (required: `WHOOP_CLIENT_ID`, `WHOOP_CLIENT_SECRET`; optional: `WHOOP_REDIRECT_URI`, `WHOOP_LOG_LEVEL`, `WHOOP_LOG_FILE`, `WHOOP_LOG_JSON`). Ready for submission when the next intake window opens.
- **`whoop-insights` skill** bundled under `skills/whoop-insights/`. Pulls 30 days from the cache, computes personal baselines (HRV / recovery / RHR / sleep / strain) with 7-day vs 30-day deltas, flags anomalies with evidence, runs two correlations (sleep → next-day recovery, strain → next-day recovery), and optionally generates a self-contained HTML dashboard (`skills/whoop-insights/dashboard_template.html`) with Chart.js visualizations. Explicit anti-fabrication rules: every claim cites a date and a number.
- `skills/README.md` — skills catalogue and installation notes for Claude Code / Cursor / Windsurf.

### Changed
- `setup_direct_oauth.py` moved from repo root to `src/setup_direct_oauth.py` so it's reachable from `[project.scripts]` after `pip install`. The standalone `python setup_direct_oauth.py` invocation continues to work for the from-source install path; new install uses `whoop-mcp-oauth`.
- `whoop_mcp_server.py` gained a `main()` function (the previous `if __name__ == "__main__"` block now delegates to it) so the console script can enter cleanly.
- README install section rewritten: `uvx` one-liner is now the primary path; from-source remains for contributors.

### Packaging notes
- `py-modules` enumerated in `pyproject.toml` under `[tool.setuptools]` so the flat `src/*.py` layout installs correctly (previously `packages.find` found nothing, shipping a metadata-only wheel).
- Wheel verified locally: `whoop_mcp-0.8.0-py3-none-any.whl` contains all 11 modules; both console scripts resolve; `import whoop_mcp_server` succeeds in a fresh venv.

## [0.7.7] - 2026-04-22
### Added
- `AGENTS.md` at repo root — load-bearing conventions for AI agents (Claude Code, Cursor, Windsurf, Zed, Aider) working on this codebase. File layout, error-envelope discipline, TDD loop, "don't" list, release flow.
- `docs/AGENT_INSTALL_PROMPT.md` — a literal prompt end users paste into their MCP-aware agent so the agent clones, venvs, OAuths, and registers the server end-to-end. Covers Claude Code, Claude Desktop, Cursor, and generic fallbacks.
- `docs/ARCHITECTURE.md` — one-page system map: four-layer diagram, read/fresh/sync/event paths, local-disk layout, invariants, "where to add X".
- README badges row (Python 3.10+, MIT, MCP-compatible, 183 tests passing, version).
- README "Install with your agent (recommended)" section surfacing the paste-to-install prompt.
- README "For agents building on this repo" section linking AGENTS.md and ARCHITECTURE.md.

## [0.7.6] - 2026-04-21
### Fixed (documentation and metadata)
- README install commands pointed at the upstream fork repo and referenced a wrong OAuth redirect port (`:8765` vs the actual `:8000`). Updated to the current repo and the correct port.
- README's `claude mcp add` snippet was missing `--env WHOOP_CLIENT_ID=... --env WHOOP_CLIENT_SECRET=...`, which the server needs to refresh the 1-hour access token. Without those flags, refresh silently failed after an hour of use.
- README described the event-feed window as "half-open"; it is exclusive on both ends. Terminology corrected.
- README's test-count reference was stale (~175). Now reads "183 tests".
- PRIVACY.md listed only four read scopes; the server actually requests six (`read:cycles` and `read:body_measurement` were missing from the doc).
- `docs/registry-notes.md` had the old project name (`whoop-mcp-server`), the upstream repo URL, and a scopes list missing `read:cycles`. All corrected.
- `pyproject.toml` declared `requires-python = ">=3.8"` and listed Python 3.8/3.9 classifiers; the code uses `X | Y` union syntax and requires 3.10+. Bumped to `>=3.10` with py310/py311/py312 classifiers.
- `pyproject.toml` had a `whoop-mcp-setup = setup:main` script entry pointing at the removed `setup.py`. Removed the entry so `pip install .` doesn't crash.
- `pyproject.toml` project URLs still pointed at the upstream fork. Rewired to this repo.
- `pyproject.toml` mypy and black configurations targeted Python 3.8. Bumped to 3.10+.
- `pyproject.toml` had a duplicate `[tool.pytest.ini_options]` block that `pytest.ini` was shadowing. Removed the dead block.
- Package name in `pyproject.toml` (`whoop-mcp-server`) was stale given the repo rename. Now `whoop-mcp`.
- CONTRIBUTING.md promised pre-commit hooks, a black/isort/flake8/mypy workflow, GitHub Actions, and issue templates — none exist. Rewritten to describe the actual contributor workflow.
- `SERVER_VERSION` import fallback in `whoop_mcp_server.py` was pinned to `0.7.1` (a defensive no-op in practice, but drift anyway). Updated.

## [0.7.5] - 2026-04-21
### Changed
- `pyproject.toml` authors, `src/__init__.py` `__author__`, and `SECURITY.md` security contact switched to the current maintainer. Added a Credits section in README crediting the upstream fork.
- `LICENSE` retains the original copyright line and adds one for the v0.2.0+ rewrite.
### Removed
- `smithery/`, root-level `package.json`, `package-lock.json`, `tsconfig.json`, `smithery.yaml` — the legacy Node/TypeScript Smithery deployment path from v0.1.x. Never tested in v0.2.0+; keeping it alongside the Python surface misled users into thinking there was a supported alternative.
- `setup.py` — the pre-v0.2.0 interactive OAuth script that pointed at the defunct third-party proxy. `setup_direct_oauth.py` is the sole OAuth bootstrap script now.
- `docs/INSTALLATION.md`, `docs/TROUBLESHOOTING.md`, `examples/` — stale content that contradicted the rewritten README and referenced the removed install path.
- `test_claude_integration.md` — pre-v0.2.0 Russian-language Claude Desktop walkthrough, superseded by README.
### Verified
- `scripts/fresh_install_check.sh` executed end-to-end against the current HEAD (clean scratch venv, all imports load, `SERVER_VERSION` detected correctly).
- Auth refresh live-tested: forced `expires_at` into the past, confirmed `get_valid_access_token` (sync) and `get_whoop_profile` (async) both hit the real WHOOP refresh endpoint and obtained fresh access tokens.
- `git log --all` scanned for credential material — clean across history, blobs, and commit messages.

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
