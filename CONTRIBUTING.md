# Contributing

Bug reports, PRs, and ideas welcome. This document is intentionally
short — there is no CI, no bot, no required ceremony. The project is
MIT-licensed; contributions are licensed the same way.

## Dev setup

Requires Python 3.10+. WHOOP account optional (fixture-driven tests run
without one; live tests need credentials).

```bash
git clone https://github.com/AshwanthramKL/whoop-mcp.git
cd whoop-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

## Running tests

```bash
.venv/bin/pytest -q          # full suite (183 tests, respx-mocked, ~6s)
.venv/bin/pytest tests/test_whoop_store.py -q    # one file
```

Live tests (rare — most fixture-mocked) need:

```bash
export WHOOP_CLIENT_ID="..."
export WHOOP_CLIENT_SECRET="..."
.venv/bin/python tests/record_fixtures.py   # one-time; refresh fixtures
```

## What the tests cover

- Per-endpoint happy path via `respx` fixtures (no network).
- Auto-pagination, 429 / 5xx retry paths, validation, error envelope.
- SQLite cache upsert/range/idempotency, incremental sync.
- Pydantic flattening, daily summary join, export round-trip.
- Events feed cursor semantics, snapshot hash dedupe.
- Fault injection: auth refresh, concurrent refresh lock, corrupted
  tokens, DB corruption, network errors.

New feature → add a failing test first, then code until green.

## Making changes

1. Fork, branch, commit, open a PR against `main`.
2. Keep the error envelope (`_error_payload` / `_map_error`) — tools
   must never raise; they return `{"error": {"code","message", ...}}`.
3. If you add a new tool, update the tool catalog table in `README.md`
   so `tests/test_readme_links.py` and discoverability stay honest.
4. If you bump the user-visible surface, update `src/__version__.py`
   (single source of truth) and add a `CHANGELOG.md` entry in
   Keep-a-Changelog format.

Commit style is loose — descriptive is enough. Prefixes like `fix:` /
`feat:` / `docs:` / `refactor:` are welcome but not enforced.

## Release process

1. Bump `src/__version__.py`.
2. Update `pyproject.toml` version field to match.
3. Add a `## [X.Y.Z] - YYYY-MM-DD` entry at the top of `CHANGELOG.md`.
4. `pytest -q` must be all green.
5. `git tag -a vX.Y.Z -m "..."` on `main`, push `--tags`.
6. `gh release create vX.Y.Z` for the GitHub release page.

`tests/test_version_import.py` guards against the version drifting
between the three locations.

## Security

Report vulnerabilities per [SECURITY.md](./SECURITY.md) — email the
maintainer directly rather than filing a public issue.

## Questions

Open a GitHub issue for bugs, discussions, or design questions.
