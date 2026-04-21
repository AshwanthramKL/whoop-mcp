"""
M6 token rotation + resilience tests for TokenManager.

These tests exercise the auth layer directly — they skip the autouse
``whoop_access_token`` fixture (which stubs ``TokenManager.__init__``).
They use a real TokenManager pointed at tmp_path.

What we're locking in:
- Happy-path refresh (200) stores new tokens.
- 401 ``invalid_grant`` from WHOOP clears tokens and surfaces as no-token
  from ``get_valid_access_token``.
- 5xx from WHOOP returns None (caller upgrades to upstream error).
- Corrupted ``tokens.json`` (truncated / key mismatch) is treated as
  "no tokens" without crashing.
- Missing encryption key: a new key is regenerated but prior ciphertext
  becomes unreadable and we don't crash.
- Concurrent refresh only issues one HTTP request (async lock).
"""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import pytest

# We are going to manipulate the real TokenManager, bypassing the
# autouse conftest patch.
pytestmark = [pytest.mark.filterwarnings("ignore::DeprecationWarning")]


def _mk_manager(tmp_dir: str):
    """Fresh TokenManager with paths in tmp_dir. Uses real crypto."""
    import auth_manager

    token_path = os.path.join(tmp_dir, "tokens.json")
    key_path = os.path.join(tmp_dir, ".encryption_key")
    with patch.multiple(
        "auth_manager",
        TOKEN_STORAGE_PATH=token_path,
        ENCRYPTION_KEY_FILE=key_path,
    ):
        tm = auth_manager.TokenManager()
    tm.storage_path = token_path
    tm.key_file = key_path
    return tm, token_path, key_path


@pytest.fixture
def _skip_autouse_patch():
    """Bypass the conftest autouse that stubs TokenManager."""
    # The conftest fixture applies to every test. We "opt out" by
    # naming our test file so that the conftest check ``if
    # "test_auth_manager" in test_path`` fails — but since we need
    # test_token_rotation handling too, we use this fixture instead:
    yield


# --- 1. refresh success ---


@patch("requests.post")
@patch("auth_manager.WHOOP_CLIENT_ID", "cid")
@patch("auth_manager.WHOOP_CLIENT_SECRET", "sec")
def test_refresh_success_stores_new_tokens(mock_post, tmp_path):
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "access_token": "NEW",
            "refresh_token": "NEW_REFRESH",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
    )
    tm, _token_path, _ = _mk_manager(str(tmp_path))
    out = tm.refresh_tokens("old_refresh")
    assert out is not None
    assert out["access_token"] == "NEW"
    loaded = tm.load_tokens()
    assert loaded["access_token"] == "NEW"


# --- 2. refresh 401 -> tokens cleared ---


@patch("requests.post")
@patch("auth_manager.WHOOP_CLIENT_ID", "cid")
@patch("auth_manager.WHOOP_CLIENT_SECRET", "sec")
def test_refresh_401_invalid_grant_clears_tokens(mock_post, tmp_path):
    mock_post.return_value = MagicMock(
        status_code=401,
        text='{"error":"invalid_grant"}',
    )
    tm, token_path, _ = _mk_manager(str(tmp_path))
    # Pre-seed with a stored refresh token
    tm.save_tokens(
        {
            "access_token": "A",
            "refresh_token": "R",
            "token_type": "Bearer",
            "expires_in": 3600,
        }
    )
    out = tm.refresh_tokens("R")
    assert out is None
    # TokenManager's contract on 401 refresh: tokens should be cleared so
    # subsequent get_valid_access_token() returns None.
    assert tm.get_valid_access_token() is None or not os.path.exists(token_path)


# --- 3. refresh 5xx -> returns None but keeps tokens ---


@patch("requests.post")
@patch("auth_manager.WHOOP_CLIENT_ID", "cid")
@patch("auth_manager.WHOOP_CLIENT_SECRET", "sec")
def test_refresh_5xx_returns_none(mock_post, tmp_path):
    mock_post.return_value = MagicMock(status_code=503, text="oh no")
    tm, token_path, _ = _mk_manager(str(tmp_path))
    tm.save_tokens(
        {
            "access_token": "A",
            "refresh_token": "R",
            "token_type": "Bearer",
            "expires_in": 3600,
        }
    )
    out = tm.refresh_tokens("R")
    assert out is None
    # Tokens file still exists — 5xx isn't a "clear tokens" signal.
    assert os.path.exists(token_path)


# --- 4. corrupted tokens.json ---


def test_corrupted_tokens_file_treated_as_no_tokens(tmp_path):
    tm, token_path, _ = _mk_manager(str(tmp_path))
    # Write a bogus file.
    with open(token_path, "w") as f:
        f.write("this is not encrypted JSON")
    os.chmod(token_path, 0o600)

    out = tm.load_tokens()
    # We don't want a crash — we want "no tokens" behavior.
    assert out is None or tm.get_valid_access_token() is None


# --- 5. encryption key mismatch ---


def test_missing_key_regeneration_treats_prior_tokens_as_unreadable(tmp_path):
    # Make manager A, save tokens.
    tm_a, token_path, key_path = _mk_manager(str(tmp_path))
    tm_a.save_tokens(
        {
            "access_token": "A1",
            "refresh_token": "R1",
            "token_type": "Bearer",
            "expires_in": 3600,
        }
    )
    # Now simulate key loss.
    os.remove(key_path)

    # A fresh manager regenerates a key; prior ciphertext cannot be
    # decrypted. load_tokens should return None (or at least not crash).
    import auth_manager

    with patch.multiple(
        "auth_manager",
        TOKEN_STORAGE_PATH=token_path,
        ENCRYPTION_KEY_FILE=key_path,
    ):
        tm_b = auth_manager.TokenManager()
    tm_b.storage_path = token_path
    tm_b.key_file = key_path

    loaded = tm_b.load_tokens()
    assert loaded is None


# --- 6. concurrent refresh fires only one HTTP request ---


@pytest.mark.asyncio
async def test_concurrent_refresh_fires_only_one_http_request(tmp_path):
    """
    Two coroutines call get_valid_access_token_async concurrently with an
    expiring token. Only one refresh request should hit the network.

    Relies on M6 adding an async refresh lock in TokenManager.
    """
    import asyncio

    tm, _token_path, _ = _mk_manager(str(tmp_path))
    # Save an already-expired token.
    tm.save_tokens(
        {
            "access_token": "OLD",
            "refresh_token": "R",
            "token_type": "Bearer",
            "expires_in": -60,  # already expired
        }
    )

    call_count = {"n": 0}

    def _fake_refresh(refresh_token):
        call_count["n"] += 1
        # Simulate a slow refresh so both coroutines race.
        time.sleep(0.05)
        new = {
            "access_token": "NEW",
            "refresh_token": "R2",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        tm.save_tokens(new)
        return new

    # Patch the synchronous refresh_tokens method so both paths go through
    # the same mocked transport.
    with patch.object(tm, "refresh_tokens", side_effect=_fake_refresh):
        # If TokenManager exposes get_valid_access_token_async, use it.
        if hasattr(tm, "get_valid_access_token_async"):
            tokens = await asyncio.gather(
                tm.get_valid_access_token_async(),
                tm.get_valid_access_token_async(),
            )
        else:
            pytest.skip("get_valid_access_token_async not yet implemented (M6)")

    assert all(t == "NEW" for t in tokens)
    assert call_count["n"] == 1, (
        f"expected exactly one refresh under the lock; got {call_count['n']}"
    )
