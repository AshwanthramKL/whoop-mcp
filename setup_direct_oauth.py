#!/usr/bin/env python3
"""Direct WHOOP OAuth setup — uses user's own WHOOP dev app client_id/secret."""
import os
import sys
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
from auth_manager import TokenManager
from config import (
    WHOOP_OAUTH_AUTH_URL,
    WHOOP_OAUTH_TOKEN_URL,
    WHOOP_CLIENT_ID,
    WHOOP_CLIENT_SECRET,
    WHOOP_REDIRECT_URI,
)

SCOPES = "read:profile read:workout read:sleep read:recovery read:cycles read:body_measurement offline"

result = {"code": None, "error": None, "state": None}
expected_state = secrets.token_urlsafe(16)


class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        result["code"] = qs.get("code", [None])[0]
        result["error"] = qs.get("error", [None])[0]
        result["state"] = qs.get("state", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        body = (
            b"<h2>Authorization received.</h2><p>You can close this tab.</p>"
            if result["code"] else
            b"<h2>Authorization failed.</h2><pre>" + str(result["error"]).encode() + b"</pre>"
        )
        self.wfile.write(body)


def main():
    if not WHOOP_CLIENT_ID or not WHOOP_CLIENT_SECRET:
        print("ERROR: set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET env vars before running.")
        sys.exit(1)

    parsed = urlparse(WHOOP_REDIRECT_URI)
    host, port = parsed.hostname, parsed.port or 80
    server = HTTPServer((host, port), CallbackHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    auth_url = f"{WHOOP_OAUTH_AUTH_URL}?" + urlencode({
        "response_type": "code",
        "client_id": WHOOP_CLIENT_ID,
        "redirect_uri": WHOOP_REDIRECT_URI,
        "scope": SCOPES,
        "state": expected_state,
    })

    print(f"Opening browser for WHOOP authorization...\nIf it doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    # Wait for callback
    print(f"Listening on {WHOOP_REDIRECT_URI} for the callback...")
    while result["code"] is None and result["error"] is None:
        pass
    server.shutdown()

    if result["error"]:
        print(f"OAuth error: {result['error']}")
        sys.exit(1)
    if result["state"] != expected_state:
        print("State mismatch — aborting.")
        sys.exit(1)

    print("Exchanging code for tokens...")
    resp = requests.post(
        WHOOP_OAUTH_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": WHOOP_REDIRECT_URI,
            "client_id": WHOOP_CLIENT_ID,
            "client_secret": WHOOP_CLIENT_SECRET,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"Token exchange failed: {resp.status_code} {resp.text[:400]}")
        sys.exit(1)

    tokens = resp.json()
    TokenManager().save_tokens(tokens)
    print("Tokens saved to ~/.whoop-mcp-server/tokens.json (encrypted).")
    print(f"Scopes: {tokens.get('scope')}  Expires in: {tokens.get('expires_in')}s")


if __name__ == "__main__":
    main()
