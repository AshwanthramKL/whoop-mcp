# Architecture

One page. Read this before `whoop_mcp_server.py`.

## The four layers

```
┌─────────────────────────────────────────────────────────────┐
│ MCP Client (Claude Code / Desktop / Cursor / Zed / …)       │
└───────────────▲──────────────────────────────▲──────────────┘
                │ stdio: JSON-RPC over stdin/stdout │
                ▼                              ▼
┌─────────────────────────────────────────────────────────────┐
│ whoop_mcp_server.py  —  17 tools + 9 resources              │
│  · parse args → call client/store → flatten → envelope      │
│  · error envelope: tools never raise                        │
│  · structured logs to stderr only                           │
└────────┬──────────────────┬───────────────┬─────────────────┘
         │                  │               │
         ▼                  ▼               ▼
┌─────────────────┐ ┌────────────────┐ ┌──────────────────────┐
│ whoop_client.py │ │ whoop_store.py │ │ whoop_export.py      │
│ httpx async     │ │ stdlib sqlite3 │ │ csv / json / pyarrow │
│ pagination      │ │ WAL mode       │ │ reads from store     │
│ retry+backoff   │ │ overlap range  │ └──────────────────────┘
│ typed errors    │ │ sync_runs      │
└────────┬────────┘ └────────▲───────┘
         │                   │
         │                   │ upserts via
         │                   │ whoop_sync.py
         │                   │ (parallel per-resource,
         │                   │  asyncio.gather,
         │                   │  updated_at cursors,
         │                   │  snapshot hash dedupe)
         │                   │
         ▼                   │
┌─────────────────────────────┴──────────────────────────────┐
│ WHOOP v2 API (api.prod.whoop.com/developer/v2)              │
│ OAuth bearer token from auth_manager.py                     │
│ · encrypted tokens.json (Fernet, mode 0600)                 │
│ · async refresh lock (no stampede on expiry)                │
└─────────────────────────────────────────────────────────────┘

Local disk under ~/.whoop-mcp-server/
 · tokens.json          (encrypted)
 · .encryption_key      (0600)
 · whoop.db             (SQLite WAL, 0600)
 · logs/whoop-mcp.log   (rotating, 1MB × 5)
```

## Read path (default: cache-first)

```
Tool call (fresh=False)
   │
   ▼
whoop_store.query_range(table, start, end)  ←  SQL: overlap semantics
   │
   └── returns flat_json rows from SQLite    ← ZERO API calls
```

If the cache has no rows for that window → **auto-sync triggered
transparently** → cache populated → return.

## Fresh read path (fresh=True)

```
Tool call (fresh=True)
   │
   ▼
whoop_client.list_X(start, end)   ← auto-paginate next_token
   │
   ▼
whoop_models.X.model_validate().flatten()
   │
   ▼
whoop_store.upsert_records(...)   ← write-through cache
   │
   ▼
return flat records
```

## Sync path

```
sync_whoop(full=False, since=None, resources=None)
   │
   ▼
whoop_sync.run_sync
   │
   ├── per-resource cursor = max(stored updated_at) or 90d ago or epoch
   │
   ├── asyncio.gather(
   │        _sync_list_resource("cycles", since),
   │        _sync_list_resource("recoveries", since),
   │        _sync_list_resource("sleeps", since),
   │        _sync_list_resource("workouts", since),
   │        _sync_snapshot_resource("profile"),
   │        _sync_snapshot_resource("body_measurement"),
   │   )
   │
   ├── write sync_runs audit row per resource
   │
   ├── backfill_recovery_windows()   ← inherits start/end from parent cycle
   │
   └── return {status, started_at, completed_at, resources, warnings}
```

Idempotent: rerunning with no new upstream data → 0 upserts across
every resource (including snapshots, thanks to SHA-256 content hash).

## Event feed path

```
get_whoop_events(since, until, resources, limit)
   │
   ▼
whoop_store.iter_events(...)
   │
   ├── UNION ALL across selected resource tables
   ├── WHERE updated_at > since AND updated_at < until
   ├── ORDER BY updated_at ASC, resource ASC
   ├── LIMIT limit+1  (peek for truncation detection)
   │
   ▼
compose (resource, id, updated_at, record)
   │
   ▼
if truncated → next_cursor = base64(last.updated_at|resource|id)
             (opaque composite cursor, tie-broken, no skip/dup at boundary)
```

## Error flow

Every tool is structurally:

```python
@mcp.tool()
async def thing(...):
    try:
        ... real work ...
        return flat_result
    except WhoopAPIError as e:
        return _map_error(e)   # typed → error envelope
    except Exception as e:       # belt and suspenders
        return {"error": {"code": "UPSTREAM_ERROR", ...}}
```

`_map_error` maps the exception subclass to one of the documented
error codes (see AGENTS.md § 4). No tracebacks leak. No secrets.

## Logging

`whoop_logging.setup()` is called once at module import (idempotent).

- stderr handler (always)
- rotating file handler at `~/.whoop-mcp-server/logs/whoop-mcp.log`
  (disable with `WHOOP_LOG_FILE=""`)
- JSON formatter (toggle with `WHOOP_LOG_JSON=false`)
- Level from `WHOOP_LOG_LEVEL` (default INFO)

Structured events emitted: `api_request`, `api_retry`, `api_error`,
`sync_start`, `sync_resource_done`, `sync_complete`, `cache_upsert`,
`export_done`, `event_feed_returned`, `auth_refresh`, `health_check`,
`recovery_windows_backfilled`.

**Never logged:** access tokens, refresh tokens, encryption keys,
client secret, raw WHOOP response bodies, full URLs containing tokens,
user emails as structured fields. See PRIVACY.md.

## Invariants

These are load-bearing. Changing them is a breaking change.

1. **Tools never raise.** Every error path returns the envelope.
2. **stdout is protocol only.** `print()` is forbidden in `src/`.
3. **Fresh=False never hits the network.** (Except for transparent auto-sync on first-use of a never-synced window.)
4. **`updated_at`-based cursors.** Both sync (stored max) and events
   (opaque composite) use `updated_at`.
5. **Cache overlap semantics.** Range queries filter by
   `(end >= start AND start < end)`, matching the WHOOP API's return
   semantics, so cache reads ≡ API reads for the same window.
6. **Recovery rows inherit start/end from parent cycle.** Raw WHOOP
   recovery payloads have no own timestamps. Without the inheritance,
   date-window queries silently return empty.
7. **Snapshot hash dedupe.** Profile + body_measurement rows only bump
   `updated_at` on real content change. Event feed stays quiet on
   idempotent syncs.

Break one of these and user-visible behavior changes; add the change
to `CHANGELOG.md` under `### Changed` or `### Fixed`.

## Where to add X

| Want to add | Where it goes |
|------|------|
| A new WHOOP endpoint | `whoop_client.py` + `whoop_models.py` + a new tool in `whoop_mcp_server.py` |
| A new derived/joined tool | `whoop_mcp_server.py` only; compose existing client/store calls |
| A new output format | `whoop_export.py` + dispatch entry |
| A new MCP resource URI | `whoop_mcp_server.py` `@mcp.resource(...)` block |
| A migration / schema change | `whoop_store.py` `init_schema`, bump `PRAGMA user_version` |
| A new env var | `config.py` and a README "Operations" entry |

## Not on the diagram (intentional)

- No cloud. No Redis. No external queue.
- No webhooks. (Event feed is poll-driven from the cache.)
- No write-back to WHOOP. Read-only by design — it keeps the trust
  surface small (an exfil vector can't become a mutation vector) and
  avoids dependency on reverse-engineered write endpoints, which live
  outside WHOOP's stable v2 API.
- No Node / TypeScript layer.
- No telemetry / analytics of any kind.
- No multi-tenant support. One user, one WHOOP account, one machine.
