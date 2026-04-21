# 🏃 WHOOP MCP Server

> Connect your WHOOP fitness data to Claude Desktop through the Model Context Protocol (MCP)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)
[![smithery badge](https://smithery.ai/badge/@RomanEvstigneev/whoop-mcp-server)](https://smithery.ai/server/@RomanEvstigneev/whoop-mcp-server)

Transform your WHOOP fitness data into actionable insights through natural language queries in Claude Desktop. Ask questions about your workouts, recovery, sleep patterns, and more - all while keeping your data secure and private.

> 🚀 **NEW**: Try the [Smithery hosted version](./smithery/) for zero-setup deployment!

## ✨ Features

🔐 **Secure OAuth Integration** - Safe WHOOP account connection with encrypted local storage  
🏃 **Complete Data Access** - Workouts, recovery, sleep, cycles, and profile information  
🤖 **Natural Language Queries** - Ask Claude about your fitness data in plain English  
⚡ **Smart Caching** - Optimized performance with intelligent data caching  
🛡️ **Privacy First** - All data stays on your machine, never sent to third parties  
🔄 **Auto Token Refresh** - Seamless experience with automatic authentication renewal

## 🚀 Quick Start

### 🎯 Choose Your Deployment Method

**Option A: Smithery Hosted (Recommended for beginners)**
- ✅ Zero installation complexity
- ✅ Automatic updates and maintenance
- ✅ Enterprise-grade hosting
- ➡️ **[Get started with Smithery](./smithery/README.md)**

**Option B: Local Installation (Advanced users)**
- ✅ Full control and privacy
- ✅ No external dependencies
- ✅ Customize and extend
- ➡️ **Continue with local setup below**

---

## 📦 Local Installation

### 1. Prerequisites
- Python 3.8+
- Claude Desktop
- Active WHOOP account

### 2. Installation

```bash
git clone https://github.com/romanevstigneev/whoop-mcp-server.git
cd whoop-mcp-server
pip install -r requirements.txt
```

### 3. Setup

#### Option A: Interactive Setup (Recommended)

Run the interactive setup:
```bash
python setup.py
```

This will:
- Open your browser for WHOOP OAuth authorization
- Securely save your tokens locally
- Provide Claude Desktop configuration

#### Option B: Manual WHOOP OAuth Setup

If the interactive setup doesn't work, you can manually get your WHOOP tokens:

1. **Open WHOOP OAuth Page**: 
   👉 **[Click here to authorize WHOOP access](https://personal-integrations-462307.uc.r.appspot.com/)**

2. **Authorize Your Account**:
   - Log in with your WHOOP credentials
   - Grant permissions for the requested scopes:
     - `read:profile` - Access to your profile information
     - `read:workout` - Access to workout data
     - `read:recovery` - Access to recovery data
     - `read:sleep` - Access to sleep data
     - `offline` - Refresh token for continued access

3. **Copy Authorization Code**:
   - After authorization, you'll see a success page
   - **Copy the entire authorization code** (long string starting with letters/numbers)
   - It looks like: `ABC123...XYZ789` (much longer)

4. **Exchange Code for Tokens**:
   ```bash
   python -c "
   import sys
   sys.path.insert(0, './src')
   from auth_manager import TokenManager
   import requests
   
   # Paste your authorization code here
   auth_code = 'YOUR_AUTHORIZATION_CODE_HERE'
   
   # Exchange for tokens
   url = f'https://personal-integrations-462307.uc.r.appspot.com/api/get-tokens/{auth_code}'
   response = requests.get(url, timeout=30)
   
   if response.status_code == 200:
       token_data = response.json()
       if token_data.get('success'):
           # Save tokens
           token_manager = TokenManager()
           token_manager.save_tokens(token_data)
           print('✅ Tokens saved successfully!')
       else:
           print('❌ Token exchange failed')
   else:
       print(f'❌ HTTP Error: {response.status_code}')
   "
   ```

5. **Verify Setup**:
   ```bash
   python -c "
   import sys
   sys.path.insert(0, './src')
   from whoop_client import WhoopClient
   client = WhoopClient()
   print(f'✅ Auth status: {client.get_auth_status()}')
   "
   ```

### 4. Configure Claude Desktop

Add to your Claude Desktop settings:

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows**: `%APPDATA%\\Claude\\claude_desktop_config.json`
**Linux**: `~/.config/claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "whoop": {
      "command": "/opt/miniconda3/bin/python",
      "args": ["/path/to/whoop-mcp-server/src/whoop_mcp_server.py"],
      "env": {
        "PYTHONPATH": "/path/to/whoop-mcp-server/src"
      }
    }
  }
}
```

**⚠️ Important**: Use the full Python path (find yours with `which python3`)

### 5. Restart Claude Desktop

After adding the configuration, restart Claude Desktop to load the WHOOP server.

## 💡 Usage Examples

Once configured, you can ask Claude:

- **"Show my WHOOP profile"**
- **"What were my workouts this week?"**
- **"How is my recovery trending?"**
- **"Show my sleep data for the last 7 days"**
- **"What's my HRV looking like?"**
- **"Compare my recovery to last month"**

## 🛠️ Available Tools

### `get_whoop_profile`
Get your WHOOP user profile information.

### `get_whoop_workouts`
Get workout data with optional filters:
- `start_date` (YYYY-MM-DD)
- `end_date` (YYYY-MM-DD)
- `limit` (number of results)

### `get_whoop_recovery`
Get recovery data with optional filters:
- `start_date` (YYYY-MM-DD)
- `end_date` (YYYY-MM-DD)
- `limit` (number of results)

### `get_whoop_sleep`
Get sleep data with optional filters:
- `start_date` (YYYY-MM-DD)
- `end_date` (YYYY-MM-DD)
- `limit` (number of results)

### `get_whoop_cycles`
Get physiological cycles (daily data) with optional filters:
- `start_date` (YYYY-MM-DD)
- `end_date` (YYYY-MM-DD)
- `limit` (number of results)

### `get_whoop_auth_status`
Check authentication status and token information.

### `clear_whoop_cache`
Clear cached data to force fresh API calls.

## 🔐 Security

- **Token Encryption**: All tokens are encrypted at rest using AES encryption
- **Local Storage**: Tokens are stored locally on your machine, never sent to third parties
- **Secure Permissions**: Token files have restricted permissions (600)
- **Auto-Refresh**: Tokens are automatically refreshed when expired

## 📊 Data Caching

- **Smart Caching**: API responses are cached for 5 minutes to improve performance
- **Rate Limiting**: Built-in rate limiting to respect WHOOP API limits
- **Cache Control**: Manual cache clearing available

## 🔧 Configuration

Environment variables (optional):
- `LOG_LEVEL`: Logging level (DEBUG, INFO, WARNING, ERROR)
- `LOG_FILE`: Log file path (default: console only)

## 🆚 Deployment Comparison

| Feature | [Smithery Hosted](./smithery/) | Local Installation |
|---------|-----------------|-------------------|
| **Setup Time** | ⚡ 2 minutes | ⏱️ 10-15 minutes |
| **Complexity** | 🟢 Beginner-friendly | 🟡 Technical setup required |
| **Maintenance** | ✅ Zero (auto-updates) | 🔧 Manual updates needed |
| **Performance** | 🚀 Optimized hosting | 💻 Depends on local setup |
| **Privacy** | 🌐 Hosted platform | 🔒 Fully local |
| **Dependencies** | ❌ None | 🐍 Python, packages, OAuth |
| **Troubleshooting** | 📞 Platform support | 🛠️ Self-service |

## 📁 File Structure

```
whoop-mcp-server/
├── src/                       # Python local installation
│   ├── whoop_mcp_server.py    # Main MCP server
│   ├── whoop_client.py        # WHOOP API client
│   ├── auth_manager.py        # Token management
│   └── config.py              # Configuration
├── smithery/                  # TypeScript source files
│   └── src/
│       ├── index.ts           # Smithery MCP server
│       ├── whoop-client.ts    # TypeScript WHOOP client
│       └── types.ts           # Type definitions
├── storage/                   # Local installation only
│   ├── tokens.json            # Encrypted tokens (auto-generated)
│   └── .encryption_key        # Encryption key (auto-generated)
├── package.json               # Node.js dependencies (Smithery)
├── smithery.yaml              # Smithery configuration (root required)
├── tsconfig.json              # TypeScript configuration
├── setup.py                   # Interactive setup script
└── requirements.txt           # Python dependencies
```

## 🐛 Troubleshooting

### "No valid access token available"
- Run `python setup.py` to re-authorize
- Check that your WHOOP account is active

### "Authentication failed"
- Your tokens may have expired beyond refresh
- Run `python setup.py` to get new tokens

### "Rate limit exceeded"
- Wait a minute before making more requests
- Consider using cached data or reducing request frequency

### Claude Desktop doesn't see the server
- **Use full Python path**: Change `"command": "python"` to `"command": "/opt/miniconda3/bin/python"` (use `which python3` to find yours)
- **Check correct config file**: Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (not `.claude.json`)
- **Use absolute paths**: Full paths like `/Users/username/whoop-mcp-server/src/whoop_mcp_server.py`
- **Check logs**: `tail -f ~/Library/Logs/Claude/mcp-server-whoop.log`
- Restart Claude Desktop after configuration changes

## 🔄 Token Refresh

The server automatically refreshes expired tokens using the refresh token. If this fails, you'll need to re-authorize:

```bash
python setup.py
```

## 📝 Logging

Logs are written to console by default. To log to a file:

```bash
export LOG_FILE="/path/to/whoop-mcp.log"
export LOG_LEVEL="INFO"
```

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

## ⚠️ Disclaimer

This is an unofficial integration with WHOOP. It uses the official WHOOP API but is not endorsed by WHOOP.

## 📞 Support

- Check the troubleshooting section above
- Open an issue on GitHub
- Review WHOOP API documentation at https://developer.whoop.com/

## 🎯 Roadmap

- [ ] Historical data analysis
- [ ] Custom date range queries
- [ ] Data export functionality
- [ ] Webhook support for real-time updates
- [ ] Advanced analytics and insights

## M2 Tool Catalog (v0.3.0)

The server exposes the WHOOP v2 read surface plus one daily-join tool. All
list tools accept ISO-8601 `start` / `end` and auto-paginate internally.

Responses in M2 are **flattened**: raw WHOOP records are parsed through
Pydantic v2 models (`src/whoop_models.py`). The server drops `user_id`,
`v1_id`, and per-record `created_at`/`updated_at`; lifts the nested
`score` wrapper; converts milliseconds to seconds (1 decimal); converts
kilojoules to calories (kcal, rounded int); renames heart-rate keys to
`avg_hr_bpm` / `max_hr_bpm`; renames HRV / SpO2 / skin-temp keys; and
renames sleep stages to `deep_sleep_seconds`, `rem_sleep_seconds`,
`light_sleep_seconds`, `awake_seconds`, `in_bed_seconds`. When
`score_state != "SCORED"`, score fields are `null` and `score_state`
is preserved at the top level so callers know why.

| Tool | Purpose |
|------|---------|
| `get_whoop_auth_status` | Report OAuth token status (call first if other tools return `AUTH_FAILED`). |
| `get_whoop_profile` | Authenticated user's WHOOP profile (name, email). |
| `get_whoop_body_measurement` | Latest body measurements: `height_meter`, `weight_kilogram`, `max_hr_bpm`. |
| `list_whoop_cycles` | Flat physiological cycles in a time window; auto-paginated. |
| `get_whoop_cycle` | Fetch a single cycle by integer ID. |
| `get_whoop_cycle_sleep` | Flat sleep record tied to a given cycle. |
| `get_whoop_cycle_recovery` | Flat recovery record tied to a given cycle. |
| `list_whoop_recoveries` | Flat recoveries (HRV / RHR / recovery score) in a window. |
| `list_whoop_sleeps` | Flat sleep activities (incl. naps) in a window. |
| `get_whoop_sleep` | Fetch a single sleep activity by UUID. |
| `list_whoop_workouts` | Flat workouts with zone durations in seconds. |
| `get_whoop_workout` | Fetch a single workout by UUID. |
| `get_whoop_daily_summary` | Join: cycle + recovery + primary sleep + workouts for one UTC date. |

`get_whoop_daily_summary(date="YYYY-MM-DD")` returns a single record:

```json
{
  "date": "2026-04-20",
  "cycle": {...} | null,
  "recovery": {...} | null,
  "sleep": {...} | null,
  "workouts": [{...}],
  "score_states": {"cycle": "SCORED", "recovery": "SCORED", "sleep": "SCORED"},
  "warnings": ["recovery: UPSTREAM_ERROR ..."]
}
```

Primary sleep is the longest non-nap sleep attached to the day's cycle.
Partial upstream failures populate `warnings` and leave the failing field
as `null`. Only when every fetch fails does the tool return an
`UPSTREAM_ERROR` envelope. Timezone handling for M2 is UTC.

Errors are returned as `{"error": {"code", "message", "endpoint"}}` —
tools never raise. Codes: `AUTH_FAILED`, `RATE_LIMITED`, `NOT_FOUND`,
`UPSTREAM_ERROR`, `VALIDATION_ERROR`, `CACHE_ERROR`, `SYNC_ERROR`
(M3).

## M3 Local Cache & Sync (v0.4.0)

M3 adds a durable SQLite cache at `~/.whoop-mcp-server/whoop.db`
(override with `WHOOP_DB_PATH`). One table per WHOOP resource plus a
`sync_runs` audit table. The file is created with `chmod 600` — treat
it as sensitive as `tokens.json`; it contains your raw physiological
records.

**Sync model.** Call `sync_whoop()` once per session (or whenever you
know new WHOOP data exists) to refresh the cache. By default it runs
incrementally: the per-resource cursor is the `MAX(updated_at)` of the
stored rows; a fresh DB falls back to the last 90 days.

```
sync_whoop(full=False, since=None, resources=None)
```

- `full=True` → pull from 2010-01-01 for every resource.
- `since="2026-01-01"` → override the cursor for list resources.
- `resources=["cycles","recoveries"]` → subset.

Returns a per-resource status dict with `records_fetched`,
`records_upserted`, and `cursor_after`. Overall `status` is
`success` / `partial` / `error`; `partial` means some resources
synced and others failed.

**Cache-first reads.** Every list/get tool takes a `fresh: bool`
argument (default `False`). With `fresh=False`, the tool reads
directly from SQLite — no HTTP call. An empty-window miss transparently
triggers a targeted sync, upserts, and re-reads. Pass `fresh=True` to
always hit WHOOP and write-through to the cache.

**MCP resources.** The cache is also exposed as read-only MCP
resources so Claude can browse date slices without invoking a tool:

| URI | Content |
|-----|---------|
| `whoop://db/cycles/{start}/{end}` | JSON array of cached cycles in `[start, end)` (dates are `YYYY-MM-DD`). |
| `whoop://db/recoveries/{start}/{end}` | Cached recoveries. |
| `whoop://db/sleeps/{start}/{end}` | Cached sleeps (including naps). |
| `whoop://db/workouts/{start}/{end}` | Cached workouts. |
| `whoop://db/profile` | Latest profile snapshot. |
| `whoop://db/body_measurement` | Latest body_measurement snapshot. |
| `whoop://db/sync_runs/{limit}` | Most recent audit rows (up to `limit`). |

All resources return `application/json`. Bad inputs produce an error
body with the same `{"error": {"code","message"}}` shape used by
tools.

## M4 Exports (v0.5.0)

M4 adds `export_whoop`, a pure data-layer tool that dumps flat cached
records to disk. It never hits the WHOOP API — run `sync_whoop()` first
to populate the cache. Supported formats are CSV (RFC 4180, header row
alphabetically sorted, nested values JSON-encoded), JSONL (one record
per line, sorted keys for determinism), and Parquet (pyarrow, `snappy`
compression).

```
export_whoop(kind, format, path, start=None, end=None, overwrite=False)
```

- `kind`: `cycles` | `recoveries` | `sleeps` | `workouts` | `all`
- `format`: `csv` | `jsonl` | `parquet`
- `path`: output file, or output directory for `kind='all'`. Parent is
  created if missing.
- `start` / `end`: inclusive `YYYY-MM-DD` bounds on the primary date.
  Defaults span the whole cache.
- `overwrite`: if `False` (default) and the destination has content,
  returns `FILE_EXISTS`. `True` replaces silently.

For `kind='all'` the tool writes `cycles.*`, `recoveries.*`, `sleeps.*`,
`workouts.*`, `body_measurements.*`, and `profile_snapshots.*` into the
given directory. An empty date window still yields a file (header-only
CSV / empty JSONL / empty Parquet) so downstream tooling sees a
consistent artifact.

Error codes: `VALIDATION_ERROR`, `CACHE_EMPTY` (run `sync_whoop` first),
`FILE_EXISTS`, `EXPORT_ERROR`.

Exports contain your raw fitness data in flat JSON shape — no tokens,
no raw API responses. Write them to a secure location.

## M5 Event feed (v0.6.0)

M5 adds `get_whoop_events`, a chronological "what's new" feed across all
cached resources. Pure cache read — no WHOOP API calls from this path.
Run `sync_whoop()` first to pick up upstream changes.

```
get_whoop_events(since, until=None, resources=None, limit=500)
```

- `since`: ISO-8601 timestamp, strict lower bound on `updated_at`.
- `until`: ISO-8601 timestamp, strict upper bound. Defaults to now UTC.
- `resources`: subset of `["cycles","recoveries","sleeps","workouts",
  "body_measurement","profile"]`. `None` means all.
- `limit`: cap on total events returned. Must be in `[1, 5000]`.

The window is half-open — `updated_at > since AND updated_at < until`.
The "since" bound is strict so callers can feed a returned `next_cursor`
back in as the next `since` without re-seeing that row. Events are
sorted by `updated_at` ascending with `resource` as the tiebreaker.

Each event wraps a flat record with a type tag and change timestamp:

```json
{"resource": "sleeps",
 "id": "bb68db7b-...",
 "updated_at": "2026-04-20T14:12:33.123Z",
 "record": { <full flat_json> }}
```

Return shape:

```json
{"status": "success", "count": 17,
 "since": "...", "until": "...",
 "events": [...],
 "next_cursor": null | "<iso>"}
```

If more events exist than `limit`, `next_cursor` is set to the
`updated_at` of the last returned event so the caller can paginate by
passing that value back as `since`. Otherwise `next_cursor` is `null`.

Snapshot resources (`body_measurement`, `profile`) contribute their
single "current" row when its stored `updated_at` falls in the window.

Error codes: `VALIDATION_ERROR` (bad `since`/`until`, unknown resource,
`limit` out of range), `CACHE_ERROR` (store failure). Tool never raises.

Also available as MCP resources:

- `whoop://db/events/{since}` — until defaults to now UTC
- `whoop://db/events/{since}/{until}` — explicit window
