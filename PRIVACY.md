# Privacy

The WHOOP MCP server runs **entirely on your local machine**. It is not
a hosted service. No telemetry leaves your computer.

## What this server reads

- Your WHOOP OAuth tokens from `~/.whoop-mcp-server/tokens.json`
  (encrypted with a key at `~/.whoop-mcp-server/.encryption_key`).
- Cached WHOOP records from `~/.whoop-mcp-server/whoop.db` (SQLite).
- Environment variables `WHOOP_CLIENT_ID`, `WHOOP_CLIENT_SECRET`,
  `WHOOP_DB_PATH` (optional), and the `WHOOP_LOG_*` variables.

## What this server writes

- New WHOOP tokens (encrypted) on refresh.
- WHOOP record/snapshot rows in the local SQLite cache.
- Sync audit rows in `sync_runs`.
- Health-check sentinel rows in a `_health` table (immediately deleted).
- Log lines on stderr and to
  `~/.whoop-mcp-server/logs/whoop-mcp.log` (rotating, 5 backups of ~1 MB
  each, disable with `WHOOP_LOG_FILE=""`).
- Optional exports, only to paths you explicitly pass to
  `export_whoop`.

## What this server sends over the network

- OAuth refresh POSTs to `https://api.prod.whoop.com/oauth/oauth2/token`.
- Authenticated `GET` requests to the WHOOP v2 API at
  `https://api.prod.whoop.com/developer/v2`.

That's it. There are **no** calls to third-party services, analytics
backends, Anthropic, OpenAI, telemetry collectors, crash reporters, or
advertising networks. There is no webhook endpoint and no inbound
network socket.

## What this server does NOT log

- Access tokens, refresh tokens, OAuth client secret.
- The encryption key or its filename contents.
- Raw WHOOP response bodies.
- Full URLs with query parameters that might contain sensitive tokens.
- User email addresses as structured log fields (they may appear in
  cached data, but not in log lines).

Log lines do include: endpoint paths (`/cycle`, `/activity/sleep`),
HTTP status codes, record counts, timing, IDs of records
upserted / retrieved, and high-level event names
(`sync_start`, `api_retry`, `health_check`, …).

## How to delete everything

```
rm -rf ~/.whoop-mcp-server
```

That removes tokens, encryption key, cache, logs, and all derived
artifacts. Your WHOOP account on whoop.com is unaffected.

## Scope of access

The OAuth scopes requested are the read-only set:

- `read:profile`
- `read:body_measurement`
- `read:cycles`
- `read:recovery`
- `read:sleep`
- `read:workout`
- `offline` (for refresh tokens)

The server has no write access to your WHOOP account and no way to
modify data on WHOOP's side.

## Export behavior

`export_whoop` writes flat record rows (CSV / JSONL / Parquet) to a
path you provide. The exported file contains your raw fitness data and
should be protected like any other sensitive file — the server does
not add extra encryption, set `chmod 600`, or upload the export
anywhere.
