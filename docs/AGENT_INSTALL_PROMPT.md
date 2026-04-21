# Install whoop-mcp via your agent

Copy the block below and paste it into Claude Code, Claude Desktop,
Cursor, Windsurf, Zed, or any MCP-aware agent. The agent will do the
whole install for you — clone, venv, OAuth, register. You only have to
authorize in a browser once.

---

## Paste this

````text
Install the WHOOP MCP server for me. The source of truth is
https://github.com/AshwanthramKL/whoop-mcp — read its README and AGENTS.md
before executing. Keep me informed at each step; ask me to intervene when
you hit something only I can do (browser OAuth, creating a WHOOP dev app).

Steps:

1. **WHOOP developer app.** Ask me if I already have a WHOOP developer
   app. If I don't, walk me through creating one at
   https://developer-dashboard.whoop.com/apps/create with:
     - App Name: anything (e.g. "Claude WHOOP")
     - Redirect URI: http://localhost:8000/callback
     - Scopes: read:profile read:body_measurement read:cycles
                read:recovery read:sleep read:workout offline
     - Privacy policy: a GitHub gist URL with one line is fine
   Ask me for the client_id and client_secret it gives me. Don't echo
   them back; store them only in shell env for this session.

2. **Pick an install path.** Prefer in order:
     a. `uvx whoop-mcp` (once PyPI release is live — check
        https://pypi.org/project/whoop-mcp/)
     b. `pipx install whoop-mcp` (same PyPI dependency)
     c. Git clone fallback:
          git clone https://github.com/AshwanthramKL/whoop-mcp.git ~/mcp-servers/whoop-mcp
          cd ~/mcp-servers/whoop-mcp
          python3 -m venv .venv
          .venv/bin/pip install -r requirements.txt
   Confirm Python 3.10 or later. If my default is older, use pyenv or
   Homebrew Python 3.12 — don't downgrade the project.

3. **OAuth handshake.** Run
      WHOOP_CLIENT_ID=... WHOOP_CLIENT_SECRET=... \
      .venv/bin/python setup_direct_oauth.py
   (or the equivalent via the uvx/pipx install). A browser tab will
   open to WHOOP. Tell me to authorize. The script catches the callback
   at http://localhost:8000/callback and saves encrypted tokens to
   ~/.whoop-mcp-server/tokens.json. If the tab doesn't open, print the
   authorize URL and ask me to visit it manually.

4. **Register with my MCP client.** Detect which client I'm using and
   use the matching registration command:

   - **Claude Code CLI:**
       claude mcp add whoop --scope user \
         --env WHOOP_CLIENT_ID="$WHOOP_CLIENT_ID" \
         --env WHOOP_CLIENT_SECRET="$WHOOP_CLIENT_SECRET" \
         -- <python-path> <absolute-path-to-whoop_mcp_server.py>

     where `<python-path>` is either the uvx/pipx-installed
     `whoop-mcp` script OR the cloned repo's `.venv/bin/python`, and
     the args point at `src/whoop_mcp_server.py`.

   - **Claude Desktop:** append to the file at:
       macOS:  ~/Library/Application Support/Claude/claude_desktop_config.json
       Win:    %APPDATA%/Claude/claude_desktop_config.json
     a block shaped like:
       {
         "mcpServers": {
           "whoop": {
             "command": "<python or whoop-mcp entry>",
             "args": ["<absolute path or empty>"],
             "env": {
               "WHOOP_CLIENT_ID": "<id>",
               "WHOOP_CLIENT_SECRET": "<secret>"
             }
           }
         }
       }
     Tell me to restart Claude Desktop after editing.

   - **Cursor:** append to `.cursor/mcp.json` at the repo or user level
     with the same command/args/env triple.

   - **Other client:** use its documented MCP registration path with
     the command, args, and env from above.

5. **Verify.** In a fresh session of my MCP client, call
   `health_check()` and confirm `status == "healthy"` with all five
   checks green. If `api_reachable` is `warn` or `fail`, re-run the
   OAuth flow. Then run `sync_whoop()` and report the per-resource
   record counts so I see the cache populated.

6. **Security reminder (once).** Tell me:
     - My WHOOP client_secret is now in my shell env and the MCP
       client config. Rotate the secret if it ever leaks.
     - Run `rm -rf ~/.whoop-mcp-server` to uninstall cleanly.
     - See PRIVACY.md and SECURITY.md in the repo for the full
       data-handling statement.

Don't skip any step. Don't echo my client_secret back in chat. If any
step fails, stop and diagnose before continuing. Read CHANGELOG.md if
you hit something unexpected — the recent bug history (v0.7.2–v0.7.6)
documents the common failure modes.
````

---

## What this prompt gets right

- **Scopes** match what `setup_direct_oauth.py` actually requests (all six reads + offline).
- **Redirect URI** matches `src/config.py` (`http://localhost:8000/callback`).
- **`--env` flags** are included in the `claude mcp add` snippet — the server needs them for token refresh, and every other WHOOP MCP install guide forgets this.
- **Client detection** — covers Claude Code, Claude Desktop, Cursor, and a generic fallback.
- **Verification** — ends with `health_check` + `sync_whoop`, so the user sees a working end state, not just "I think it worked."
- **Secret hygiene** — tells the agent not to echo your client_secret back into the chat.

## Variants

- **If you're on Windows**, replace the `~/mcp-servers/` path with
  `%USERPROFILE%\mcp-servers\` and the venv activation accordingly.
  The OAuth callback on `localhost:8000` works identically.
- **If you don't have `uv` or `pipx` installed**, the git-clone
  fallback is always safe. The venv path keeps the install
  self-contained.
- **If you want a read-only preview without OAuth**, skip step 3.
  Tools that don't need WHOOP auth (none, currently — `health_check`
  will report `auth: fail` but won't crash) will still work.

## Pitfalls the prompt explicitly preempts

- Users running Python <3.10 (the code uses `X | Y` union syntax)
- Users forgetting the `--env` flags → token refresh silently fails after 1 hour
- Users hitting the `personal-integrations-462307.uc.r.appspot.com` proxy (that was the upstream fork; our version uses direct OAuth)
- Users on cache-empty state → `sync_whoop` at the end guarantees it's populated
