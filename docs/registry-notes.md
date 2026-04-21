# MCP Registry submission notes

The MCP Registry spec is still fast-moving as of 2026-04-21. This doc
captures everything a future submitter needs so we don't have to
re-derive it from scratch. The manifest itself is deliberately **not**
committed — when the registry format stabilises, translate the fields
below into whatever shape the registry accepts.

## Project identity

| Field | Value |
|-------|-------|
| Name | `whoop-mcp` |
| Display name | WHOOP MCP Server |
| Description | Read-only WHOOP v2 fitness data (cycles, recoveries, sleeps, workouts, profile, body measurement) for MCP clients, with a local SQLite cache. |
| Repository | https://github.com/AshwanthramKL/whoop-mcp |
| License | MIT (see `LICENSE`) |
| Language | Python (>=3.10) |
| Transport | stdio |

## Entrypoint

```
python src/whoop_mcp_server.py
```

Requires the `src/` directory on `sys.path` (the script adjusts it
automatically via import-site bootstrapping, but a client invoking it
should still point `PYTHONPATH` there if it uses a non-venv Python).

## Install command

```
pip install -r requirements.txt
```

## Runtime prerequisites

- WHOOP developer app (client ID + secret).
- Completed one-shot OAuth flow: `whoop-mcp-oauth` (after `pip install whoop-mcp`) or `python src/setup_direct_oauth.py` (from source).
- Environment variables: `WHOOP_CLIENT_ID`, `WHOOP_CLIENT_SECRET`.

## WHOOP OAuth scopes

- `read:profile`
- `read:body_measurement`
- `read:cycles`
- `read:recovery`
- `read:sleep`
- `read:workout`
- `offline` (refresh tokens)

## Tools (17)

See `README.md` § "Tool catalog". Every tool returns either a flat
record / array or an `{"error": {...}}` envelope; tools never raise.

## MCP resources (9)

See `README.md` § "MCP resources". All return `application/json`.

## Privacy statement

Link: `PRIVACY.md`. Summary: fully local, no third-party services, no
telemetry, tokens encrypted at rest, SQLite cache mode `0o600`.

## When to submit

Wait until one of the canonical registries (modelcontextprotocol.io or
the equivalent Anthropic-maintained catalogue) publishes a stable
manifest schema. Current status of that work is tracked upstream at
https://modelcontextprotocol.io — re-check before submission.

Do **not** auto-submit from CI or a builder environment.
