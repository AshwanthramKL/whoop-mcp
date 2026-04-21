"""
Token management for WHOOP MCP Server
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any

from cryptography.fernet import Fernet

from config import (
    ENCRYPTION_KEY_FILE,
    OAUTH_REFRESH_URL,
    REQUEST_TIMEOUT,
    TOKEN_STORAGE_PATH,
    WHOOP_CLIENT_ID,
    WHOOP_CLIENT_SECRET,
)

logger = logging.getLogger(__name__)


class TokenManager:
    """Manages WHOOP OAuth tokens with encryption"""

    def __init__(self):
        self.storage_path = TOKEN_STORAGE_PATH
        self.key_file = ENCRYPTION_KEY_FILE
        self.encryption_key = self._get_or_create_key()
        self.fernet = Fernet(self.encryption_key)
        self.cipher_suite = self.fernet  # Alias for compatibility

        # Ensure storage directory exists
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)

        # Async refresh lock (M6): ensures concurrent callers to
        # ``get_valid_access_token_async`` only issue a single refresh.
        self._refresh_lock: asyncio.Lock | None = None

    def _get_or_create_key(self) -> bytes:
        """Get or create encryption key"""
        if os.path.exists(self.key_file):
            with open(self.key_file, "rb") as f:
                return f.read()
        else:
            # Create new key
            key = Fernet.generate_key()
            os.makedirs(os.path.dirname(self.key_file), exist_ok=True)
            with open(self.key_file, "wb") as f:
                f.write(key)
            os.chmod(self.key_file, 0o600)  # Restrict permissions
            return key

    def _encrypt_data(self, data: str) -> str:
        """Encrypt sensitive data"""
        return self.fernet.encrypt(data.encode()).decode()

    def _decrypt_data(self, encrypted_data: str) -> str:
        """Decrypt sensitive data"""
        return self.fernet.decrypt(encrypted_data.encode()).decode()

    def save_tokens(self, tokens: dict[str, Any]) -> None:
        """Save tokens to encrypted storage"""
        try:
            # Calculate expiration time
            expires_in = tokens.get("expires_in", 3600)  # Default 1 hour
            expires_at = datetime.now() + timedelta(seconds=expires_in)

            # Prepare data for storage
            token_data = {
                "access_token": self._encrypt_data(tokens["access_token"]),
                "refresh_token": self._encrypt_data(tokens.get("refresh_token", "")),
                "token_type": tokens.get("token_type", "Bearer"),
                "expires_at": expires_at.isoformat(),
                "created_at": datetime.now().isoformat(),
            }

            # Save to file
            with open(self.storage_path, "w") as f:
                json.dump(token_data, f, indent=2)

            # Restrict file permissions
            os.chmod(self.storage_path, 0o600)

            logger.info("Tokens saved successfully")

        except Exception as e:
            logger.error(f"Failed to save tokens: {e}")
            raise

    def load_tokens(self) -> dict[str, Any] | None:
        """Load and decrypt tokens from storage.

        Returns ``None`` for any failure — missing file, corrupted
        ciphertext, key mismatch, malformed JSON — treating them all as
        "no tokens" so the caller can prompt for re-auth without crashing.
        """
        if not os.path.exists(self.storage_path):
            logger.warning("No tokens found")
            return None

        try:
            with open(self.storage_path, "rb") as f:
                file_content = f.read()
        except OSError as e:
            logger.error(f"Failed to read tokens file: {e}")
            return None

        # New (binary) Fernet format.
        if file_content.startswith(b"gAAAAA"):
            try:
                decrypted_data = self.cipher_suite.decrypt(file_content)
                return json.loads(decrypted_data.decode())
            except Exception as e:
                logger.error(f"Failed to decrypt/parse tokens file: {e}")
                return None

        # Legacy JSON format with encrypted field values.
        try:
            encrypted_data = json.loads(file_content.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError) as e:
            logger.error(f"Corrupt tokens file (not JSON): {e}")
            return None

        try:
            return {
                "access_token": self._decrypt_data(encrypted_data["access_token"]),
                "refresh_token": self._decrypt_data(encrypted_data.get("refresh_token", "")),
                "token_type": encrypted_data.get("token_type", "Bearer"),
                "expires_at": encrypted_data.get("expires_at"),
                "created_at": encrypted_data.get("created_at"),
            }
        except Exception as e:
            logger.error(f"Failed to decrypt legacy tokens: {e}")
            return None

    def is_token_expired(self, tokens: dict[str, Any]) -> bool:
        """Check if access token is expired"""
        try:
            expires_at = datetime.fromisoformat(tokens["expires_at"])
            # Consider token expired 5 minutes before actual expiration
            buffer_time = timedelta(minutes=5)
            return datetime.now() + buffer_time >= expires_at
        except Exception:
            return True

    def get_valid_access_token(self) -> str | None:
        """Get valid access token, refreshing if necessary"""
        tokens = self.load_tokens()
        if not tokens:
            logger.warning("No tokens available")
            return None

        # Check if token is expired
        if not self.is_token_expired(tokens):
            return tokens["access_token"]

        # Try to refresh token
        logger.info("Access token expired, attempting refresh")
        refreshed_tokens = self.refresh_tokens(tokens["refresh_token"])

        if refreshed_tokens:
            return refreshed_tokens["access_token"]

        logger.error("Failed to refresh token")
        return None

    def refresh_tokens(self, refresh_token: str) -> dict[str, Any] | None:
        """Refresh access token using refresh token (direct WHOOP OAuth).

        Behavior (M6 hardened):
        - 200 -> new tokens saved, dict returned.
        - 401 / 400 (``invalid_grant``) -> stored tokens cleared and ``None``.
        - 5xx / network error -> ``None`` without clearing stored tokens.
        """
        try:
            import requests

            if not WHOOP_CLIENT_ID or not WHOOP_CLIENT_SECRET:
                logger.error("WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET not set")
                return None

            response = requests.post(
                OAUTH_REFRESH_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": WHOOP_CLIENT_ID,
                    "client_secret": WHOOP_CLIENT_SECRET,
                    "scope": "offline",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=REQUEST_TIMEOUT,
            )

            status = getattr(response, "status_code", None)
            if status == 200:
                token_data = response.json()
                self.save_tokens(token_data)
                return token_data

            # 401 / 400 → refresh token is dead; wipe stored creds so the
            # caller prompts for re-auth instead of looping.
            if status in (400, 401):
                logger.error(f"Refresh denied ({status}); clearing stored tokens")
                try:
                    self.clear_tokens()
                except Exception:
                    pass
                return None

            logger.error(f"Token refresh failed: {status} {getattr(response, 'text', '')[:200]}")
            return None

        except Exception as e:
            logger.error(f"Error refreshing tokens: {e}")
            return None

    async def get_valid_access_token_async(self) -> str | None:
        """Async variant with a per-instance refresh lock (M6).

        Ensures concurrent coroutines don't each trigger a redundant
        refresh when the stored token is expiring.
        """
        # Fast path: already valid, no lock needed.
        tokens = self.load_tokens()
        if tokens and not self.is_token_expired(tokens):
            return tokens.get("access_token")

        # Lazy init of the lock on the active event loop.
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            # Re-check after acquiring the lock — another coroutine may
            # have refreshed in the meantime.
            tokens = self.load_tokens()
            if tokens and not self.is_token_expired(tokens):
                return tokens.get("access_token")
            if not tokens:
                return None
            refresh_token = tokens.get("refresh_token") or ""
            # Run the sync refresh in a thread so we don't block the loop.
            try:
                refreshed = await asyncio.to_thread(self.refresh_tokens, refresh_token)
            except Exception as e:
                logger.error(f"Async refresh failed: {e}")
                return None
            if refreshed:
                return refreshed.get("access_token")
            return None

    def clear_tokens(self) -> None:
        """Clear stored tokens"""
        try:
            if os.path.exists(self.storage_path):
                os.remove(self.storage_path)
            logger.info("Tokens cleared")
        except Exception as e:
            logger.error(f"Failed to clear tokens: {e}")

    def get_token_info(self) -> dict[str, Any]:
        """Get token information without sensitive data"""
        tokens = self.load_tokens()
        if not tokens:
            return {"status": "no_tokens"}

        # Handle different token formats
        if "expires_at" in tokens:
            expires_at = datetime.fromisoformat(tokens["expires_at"])
        elif "timestamp" in tokens and "expires_in" in tokens:
            # New format with timestamp and expires_in
            created_at = datetime.fromtimestamp(tokens["timestamp"])
            expires_at = created_at + timedelta(seconds=tokens["expires_in"])
        else:
            # Default to 1 hour from now if no expiry info
            expires_at = datetime.now() + timedelta(hours=1)

        is_expired = datetime.now() > expires_at

        return {
            "status": "expired" if is_expired else "valid",
            "expires_at": expires_at.isoformat(),
            "created_at": tokens.get("created_at", datetime.now().isoformat()),
            "token_type": tokens.get("token_type", "Bearer"),
            "has_refresh_token": bool(tokens.get("refresh_token")),
        }
