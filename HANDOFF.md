# HANDOFF — whoop-mcp

**Audience:** the next agent (Claude Code, Cursor, any LLM) or human picking
this repo up cold. Read this in full before doing anything else. Estimated
read time: 8 minutes.

**Status as of last update:** v0.8.5 just shipped. Live on PyPI, MCP
Registry, and GitHub Releases. CI green. Branch protection on. First
fully-automated release went through the new tag→PyPI→Registry pipeline.

---

## 1. What this project is

A read-only WHOOP v2 API MCP server, written in Python 3.10+, distributed
via PyPI (`pip install whoop-mcp` or `uvx --from whoop-mcp whoop-mcp`).
Talks stdio to any MCP-aware client (Claude Code, Claude Desktop, Cursor,
Windsurf, Zed, Aider). Mirrors WHOOP data into a local SQLite cache so
multi-month queries are instant and offline. Exports to CSV/JSONL/Parquet.
17 MCP tools, 9 MCP resources, 198 tests, ruff + mypy clean.

**Origin:** forked from `RomanEvstigneev/whoop-mcp-server` and rewritten
starting at v0.2.0. The upstream is dead since July 2025 and used a
broken third-party OAuth proxy. Our version uses direct WHOOP OAuth via
the user's own dev app — no proxy, no shared trust.

**Audience:** WHOOP power users who want Claude as their fitness analyst.
Not a 2-minute toy.

---

## 2. Where things live

### Public (committed)

| Path | What's there |
|---|---|
| `README.md` | User-facing entrypoint. Install via `uvx`, install via agent prompt, tool catalog (17), MCP resources (9), data model, sync model, event feed, exports, operations, security, dev. |
| `AGENTS.md` | **Load-bearing** conventions for any AI editing this codebase. File map, error envelope discipline, TDD loop, "don'ts", release flow. Read before any code change. |
| `docs/ARCHITECTURE.md` | One-page system map: 4-layer diagram, read/write/sync/event paths, 7 invariants. |
| `docs/AGENT_INSTALL_PROMPT.md` | Copy-paste prompt end users give their agent to install whoop-mcp end-to-end. |
| `docs/registry-notes.md` | Notes on the MCP Registry submission process. |
| `PRIVACY.md` | What the server reads/writes/sends/logs. Required reading for any logging change. |
| `SECURITY.md` | Security policy + reporting contact. |
| `CODE_OF_CONDUCT.md` | Contributor Covenant 2.1 verbatim. |
| `CONTRIBUTING.md` | Short, honest contributor flow. |
| `CHANGELOG.md` | Keep-a-Changelog. Always update before tagging. |
| `LICENSE` | MIT, dual copyright (original + rewrite). |
| `server.json` | MCP Registry manifest. Auto-synced on tag push. |
| `src/__version__.py` | Single source of truth for version. |
| `src/whoop_*.py` | Flat-module Python source (no `whoop_mcp` package — `py-modules` in pyproject installs each as a top-level module). |
| `src/setup_direct_oauth.py` | One-shot OAuth bootstrap. Console script `whoop-mcp-oauth`. |
| `src/whoop_install_skills.py` | Installer for the `whoop-insights` Claude Code skill. Console script `whoop-mcp-install-skills`. |
| `tests/` | pytest + respx (HTTP mocked). 198 tests, all hermetic except `tests/record_fixtures.py`. |
| `skills/whoop-insights/` | Claude Code skill (markdown prompt + Chart.js HTML dashboard template). |
| `scripts/fresh_install_check.sh` | Manual smoke test for the install path. |
| `.github/workflows/ci.yml` | CI: ruff + mypy + pytest on ubuntu+macos × py310/11/12, plus build validation, plus `ci-pass` aggregator job. |
| `.github/workflows/publish-pypi.yml` | Tag-push → PyPI (trusted publishing OIDC) → MCP Registry (chained job). |
| `.github/dependabot.yml` | Weekly pip + monthly GHA. |
| `.github/ISSUE_TEMPLATE/` | bug / feature / question + a `config.yml` that disables blank issues. |
| `.github/PULL_REQUEST_TEMPLATE.md` | PR checklist. |
| `pyproject.toml` | Single config file. Versions, deps, console scripts, ruff, mypy. |
| `requirements.txt` / `requirements-dev.txt` | For from-source installs; pyproject is the canonical dep declaration. |

### Private (gitignored, but valuable)

| Path | What's there |
|---|---|
| `STRATEGY.md` | Living competitive/ship-path doc. Phases A–F, anti-strategy, kill criteria, success metrics, dogfood progress log. **Read this immediately after this HANDOFF.** Last updated end of day 2 (2026-04-22). |

---

## 3. Hard invariants (don't break these)

These are documented in `AGENTS.md` § 9 with the underlying "scars"
(real bugs that motivated each rule). Brief recap:

1. **Tools NEVER raise.** They return `{"error": {"code","message","endpoint"}}`. Stable error codes: `AUTH_FAILED`, `RATE_LIMITED`, `NOT_FOUND`, `UPSTREAM_ERROR`, `VALIDATION_ERROR`, `CACHE_ERROR`, `CACHE_EMPTY`, `FILE_EXISTS`, `EXPORT_ERROR`, `SYNC_ERROR`. Don't invent new ones without updating AGENTS.md § 4.
2. **stdout is MCP protocol; stderr is logs.** A stray `print()` on stdout breaks every client. Use the configured logger.
3. **`fresh=False` never hits the network** (except for transparent auto-sync on never-synced windows).
4. **WHOOP v2 list endpoints reject date-only `start`/`end`** — must be full ISO-8601 (`YYYY-MM-DDT00:00:00.000Z`). The client normalizes; don't bypass it. (Scar: v0.7.2 fix.)
5. **Cache range queries use overlap semantics** (`end >= window_start AND start < window_end`), not start-only inclusion. WHOOP cycles span midnight. (Scar: v0.7.3 fix.)
6. **Recovery rows inherit `start`/`end` from the parent cycle at upsert time** — WHOOP v2 recovery payloads have no own timestamps. Without inheritance, date-windowed cache reads silently return empty. (Scar: v0.7.4 fix.)
7. **Snapshot rows (`profile`, `body_measurement`) only bump `updated_at` when `sha256(raw_json)` changes.** Otherwise the event feed turns into noise after every idempotent sync.
8. **Cycles / sleeps / workouts use `start_utc`/`end_utc` (canonical) and `start_local`/`end_local` (derived from `timezone_offset`).** Don't go back to a single `start`/`end` field — LLMs (and humans) misread `Z` as local time. (Scar: v0.8.4 fix, closes issue #1.)

---

## 4. The release flow (now fully automated)

Single human flow to ship a new version:

```bash
# In ~/mcp-servers/whoop-mcp-server, on main:
# 1. Bump src/__version__.py (single source of truth)
# 2. Bump pyproject.toml's version field to match (test_version_import enforces alignment)
# 3. Prepend a CHANGELOG.md entry under ## [X.Y.Z] - YYYY-MM-DD with ### Added/Changed/Fixed
# 4. .venv/bin/pytest -q   # must be green
git add -A && git commit -m "release(X.Y.Z): ..."
git tag -a vX.Y.Z -m "..."
git push origin main
git push origin vX.Y.Z   # ← this is the trigger
```

Tag push fires `.github/workflows/publish-pypi.yml`:

1. **Job `publish-pypi`** — checkout, build sdist+wheel, twine check, upload to PyPI via `pypa/gh-action-pypi-publish` using **GitHub OIDC** (no token in env). PyPI's trusted-publishing config (set up by the human in PyPI's project settings) authorizes this exact workflow file.

2. **Job `publish-registry`** — `needs: publish-pypi`, polls PyPI for up to 2 min until the new version is visible (CDN propagation), syncs `server.json`'s top-level + `packages[0]` versions to match the tag via `jq`, validates against the registry schema, authenticates via GitHub OIDC, publishes to `registry.modelcontextprotocol.io`.

`gh release create vX.Y.Z` for the GitHub release notes is still
manual but optional — the tag itself works.

**No tokens to rotate, no credentials in dotfiles.** If you ever need
to test the pipeline, push a tag and watch
`https://github.com/AshwanthramKL/whoop-mcp/actions`.

---

## 5. Branch protection

Active on `main`:
- Force-push and deletion blocked
- Linear history required (no merge commits)
- Non-admin pushes need a PR with 1 approval, conversations resolved, rebased onto latest, `ci-pass` green
- Admin (the maintainer) bypasses for solo work — your `git push origin main` will succeed and emit a "Bypassed rule violations" warning

`ci-pass` is an aggregator job in `ci.yml` that fails unless `test`
(matrix of 6) and `build` jobs all succeed. Required-check rule
references this single name so the matrix can evolve without
breaking the rule.

---

## 6. Open work

### Issues (https://github.com/AshwanthramKL/whoop-mcp/issues)

| # | Title | Status |
|---|---|---|
| 1 | Sleep/cycle/workout timestamps return UTC + timezone_offset as separate fields | **Closed** in v0.8.4 |
| 6 | Skill 'whoop-insights' isn't discoverable after install | **Closed** in v0.8.5 |

No open issues at handoff. Next surfaces from continued dogfooding.

### Open PRs

- Upstream PR **modelcontextprotocol/registry#1192** ("docs: signpost
  non-npm publishers to package-types from quickstart"). Open,
  mergeable. Comment posted on related #531 to attract reviewers
  (`domdomegg`, `Avish34`, `pree-dew`). No action needed unless they
  request changes.

### Outstanding strategy items (see STRATEGY.md for full list)

- **Phase E — announce.** Show HN + r/ClaudeAI. **Don't announce yet** until:
  - 3+ days of dogfood with zero new `dogfood` issues.
  - At least 2 strangers have successfully installed via `uvx whoop-mcp` without hand-holding.
  - v1.0.0 cut (defers stability commitment until evidence supports it).
- **README screenshots** — deferred, non-blocking.
- **Response SLA in README.** Aspirational; add when there's actual demand.
- **`UNMAINTAINED.md` stub.** Empty file as a graceful-exit plan; fill if/when you step away.
- **5 `good first issue` labels** on real bugs to seed drive-by PRs.

### Phase status

| Phase | Status |
|---|---|
| A — Distribution parity | ✅ done |
| B — Agent-friendliness (AGENTS.md, install prompt, ARCHITECTURE) | ✅ done |
| B+ — Dev hygiene (ruff, mypy, pre-commit, CI rewrite) | ✅ done |
| C — Dogfood (compressed 2–3 days) | 🔄 active. 2 dogfood issues caught + closed. |
| D — Pre-launch hardening (community files, branch protection, update story) | ✅ done |
| D+ — Auto-publish pipeline (PyPI + Registry via OIDC) | ✅ done in v0.8.5 |
| E — Announce | ⏳ pending |
| F — Maintenance (Dependabot active, registry auto-publish active) | 🔄 partial |

---

## 7. Credentials & secrets state

**Rotated / no longer needed:**
- The PyPI API token used for v0.8.0–v0.8.4 manual uploads is in this session's transcript. Trusted publishing now handles uploads, so the token can be revoked. **Action for the next human session:** go to https://pypi.org/manage/account/token/ and revoke it if not already done.

**Active:**
- WHOOP developer app credentials (`WHOOP_CLIENT_ID`, `WHOOP_CLIENT_SECRET`) are in the maintainer's local env and the registered Claude Code MCP config (`~/.claude.json`). Also in this session's transcript — **rotate at https://developer-dashboard.whoop.com/ when convenient.** Not urgent; the dev app cap is 10 users so blast radius is small.

**Never logged or persisted by the server itself.** PRIVACY.md is accurate.

---

## 8. Local dev environment (the maintainer's machine)

```
~/mcp-servers/whoop-mcp-server/    # the repo, on main
  .venv/                           # Python 3.10 venv with all deps installed
  ~/.whoop-mcp-server/             # encrypted tokens + SQLite cache + logs
~/.claude/skills/whoop-insights/   # the skill, installed via cp -r (now also reachable via whoop-mcp-install-skills)
~/.local/bin/mcp-publisher         # registry CLI (less critical now that auto-publish works)
```

Claude Code MCP registration (user scope) points at:
```
/Users/ashwanthram/mcp-servers/whoop-mcp-server/.venv/bin/python
/Users/ashwanthram/mcp-servers/whoop-mcp-server/src/whoop_mcp_server.py
```
This means **edits to `src/` take effect on the next Claude Code session restart** — no PyPI publish needed for local iteration. The PyPI install path (`uvx --from whoop-mcp whoop-mcp`) is for everyone else.

---

## 9. Methodology notes (how this project was built)

- Built across 8 milestones (M1–M7), each with a builder-agent / verifier-agent loop. Builder writes failing tests first, then implementation. Verifier (no shared context) reviews against an explicit rubric, returns PASS or punch list. Builder iterates.
- Live-integration regressions (4 of them, scars #4–#7 above) caught real bugs that hermetic tests missed. The "respx-only" test discipline is good for fast feedback but doesn't substitute for live probes after each milestone.
- Both projects in this niche (`AshwanthramKL/whoop-mcp` and the closest competitor `shashankswe2020-ux/whoop-mcp`) are AI-authored. Don't claim "hand-written" anywhere — that's been retracted from the README. The honest differentiator is **methodology + documented bug history**, not provenance.

---

## 10. Quick reference for "I want to..."

| Goal | Command / file |
|---|---|
| Run the test suite | `cd ~/mcp-servers/whoop-mcp-server && .venv/bin/pytest -q` |
| Run lint + types | `.venv/bin/ruff check src/ tests/ && .venv/bin/mypy src/` |
| Re-record HTTP fixtures | `WHOOP_CLIENT_ID=... WHOOP_CLIENT_SECRET=... .venv/bin/python tests/record_fixtures.py` |
| See what changed since last release | `git log --oneline $(git describe --tags --abbrev=0)..HEAD` |
| Smoke-test the install path | `bash scripts/fresh_install_check.sh` |
| Verify a registry entry | `curl -s 'https://registry.modelcontextprotocol.io/v0/servers?search=AshwanthramKL%2Fwhoop-mcp' \| jq` |
| Verify a PyPI release | `curl -s -o /dev/null -w "%{http_code}\n" https://pypi.org/pypi/whoop-mcp/X.Y.Z/json` |
| Trigger CI manually | `gh workflow run ci.yml --repo AshwanthramKL/whoop-mcp` |
| See last CI run status | `gh run list --repo AshwanthramKL/whoop-mcp --limit 5` |
| Add a `dogfood` issue | `gh issue create --label dogfood,bug --title "..." --body "..." --repo AshwanthramKL/whoop-mcp` |
| Check what tools the server exposes right now | `.venv/bin/python -c "import sys; sys.path.insert(0,'src'); from whoop_mcp_server import mcp; print(sorted(mcp._tool_manager._tools))"` |

---

## 11. What I'd do first as the next agent

1. **Read STRATEGY.md** (gitignored, in repo root). 8 minutes. Tells you what's been tried, what's been ruled out, and the kill criteria.
2. **Run health_check** in your fresh Claude Code session to confirm the local MCP is on 0.8.5.
3. **Pick one of:**
   - Continue dogfood (file any new friction as `dogfood` issues; fix as patch releases).
   - Cut v1.0.0 if dogfood is genuinely clean for 2+ weeks.
   - Start Phase E announce work (Show HN draft + r/ClaudeAI post). Only if v1.0.0 is cut.
4. **Don't:**
   - Add write tools (ToS risk; deliberate no).
   - Ship a Node shim (Python-only is the strategy).
   - Announce before stability gates pass.
   - Touch the auto-publish pipeline without a test tag (push v0.8.x-rc1 against the new pipeline if you need to verify).

---

## 12. Open questions worth revisiting

These are tracked in STRATEGY.md § 8 too:

- Timezone-aware `daily_summary` boundaries (currently UTC-only — the v0.8.4 fix only addressed labeling, not the day-cut question).
- Auto-sync on first tool call (would hide the "run sync_whoop first" friction; latency cost unclear).
- Second skill: `whoop-correlate` for multi-variable analysis. Wait until `whoop-insights` shows real use.
- Windows support officially. Code runs; OAuth callback path untested.

---

**End of handoff.** If anything in this document contradicts the code, the code is right and this doc is stale — update it.
