"""
Shared pytest fixtures for the WHOOP MCP test suite.

We never hit real disk or the real OAuth layer during unit tests. The
`whoop_access_token` fixture monkeypatches ``TokenManager.get_valid_access_token``
so client construction + HTTP calls use a stable fake token.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Make src/ importable for tests
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def whoop_access_token(monkeypatch):
    """Patch TokenManager so every WhoopClient gets a stable fake token.

    Autouse so we never accidentally read ~/.whoop-mcp-server/tokens.json
    during tests.
    """
    import auth_manager

    def _fake_get_valid_access_token(self):
        return "test-access-token"

    def _fake_get_token_info(self):
        return {
            "status": "valid",
            "expires_at": "2099-01-01T00:00:00",
            "token_type": "Bearer",
            "has_refresh_token": True,
        }

    monkeypatch.setattr(
        auth_manager.TokenManager, "get_valid_access_token", _fake_get_valid_access_token
    )
    monkeypatch.setattr(
        auth_manager.TokenManager, "get_token_info", _fake_get_token_info
    )
    # Block real disk/auth init from blowing up: override __init__ with a no-op
    # shim that still sets attributes some code paths look for.
    original_init = auth_manager.TokenManager.__init__

    def _fake_init(self):
        self.storage_path = "/tmp/whoop-test-tokens.json"
        self.key_file = "/tmp/whoop-test-key"

    monkeypatch.setattr(auth_manager.TokenManager, "__init__", _fake_init)
    yield


def load_fixture(name: str) -> dict:
    """Load a JSON fixture file from tests/fixtures/."""
    path = FIXTURES_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Fixture {name!r} not found at {path}. "
            "Run tests/record_fixtures.py to regenerate."
        )
    return json.loads(path.read_text())


@pytest.fixture
def fixture_loader():
    return load_fixture
