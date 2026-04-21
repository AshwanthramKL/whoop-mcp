"""
Configuration for WHOOP MCP Server
"""

import os

# Direct WHOOP OAuth 2.0 endpoints (user's own dev app)
WHOOP_OAUTH_AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
WHOOP_OAUTH_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
WHOOP_CLIENT_ID = os.getenv("WHOOP_CLIENT_ID", "")
WHOOP_CLIENT_SECRET = os.getenv("WHOOP_CLIENT_SECRET", "")
WHOOP_REDIRECT_URI = os.getenv("WHOOP_REDIRECT_URI", "http://localhost:8000/callback")

# Back-compat aliases (still imported elsewhere, no longer used for the 3rd-party proxy)
OAUTH_AUTH_URL = WHOOP_OAUTH_AUTH_URL
OAUTH_TOKEN_URL = WHOOP_OAUTH_TOKEN_URL
OAUTH_REFRESH_URL = WHOOP_OAUTH_TOKEN_URL

# WHOOP API configuration
WHOOP_API_BASE = "https://api.prod.whoop.com/developer/v2"
WHOOP_SCOPES = ["read:profile", "read:workout", "read:sleep", "read:recovery", "offline"]

# Storage configuration
HOME_DIR = os.path.expanduser("~")
STORAGE_DIR = os.path.join(HOME_DIR, ".whoop-mcp-server")
TOKEN_STORAGE_PATH = os.path.join(STORAGE_DIR, "tokens.json")
CACHE_STORAGE_PATH = os.path.join(STORAGE_DIR, "cache.json")
CACHE_DURATION = 300  # 5 minutes

# Security configuration
ENCRYPTION_KEY_FILE = os.path.join(STORAGE_DIR, ".encryption_key")

# Rate limiting
MAX_REQUESTS_PER_MINUTE = 100
REQUEST_TIMEOUT = 30  # seconds

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", None)  # None means console only
