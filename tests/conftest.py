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
def _isolate_store(tmp_path, monkeypatch):
    """Give every test a fresh SQLite cache at a temp path.

    M3 adds a module-level ``_store`` singleton in ``whoop_mcp_server``.
    Without isolation, data cached by one test leaks into the next —
    breaking tests that expect an empty cache or expect an API call to
    actually reach the stubbed client.
    """
    db_path = str(tmp_path / "whoop.db")
    monkeypatch.setenv("WHOOP_DB_PATH", db_path)
    try:
        import whoop_mcp_server as server
    except Exception:
        yield
        return
    # Reset the module-level store singleton so _get_store() re-reads
    # WHOOP_DB_PATH for this test.
    server._store = None
    yield
    if server._store is not None:
        try:
            server._store.close()
        except Exception:
            pass
        server._store = None


@pytest.fixture(autouse=True)
def whoop_access_token(request, monkeypatch):
    """Patch TokenManager so WhoopClient instances get a stable fake token.

    Applied automatically to the M1 test files (``test_whoop_client.py``,
    ``test_mcp_tools.py``). Skipped for ``test_auth_manager.py``, which
    exercises the real TokenManager.
    """
    # Let the auth_manager and token_rotation tests use the real class.
    test_path = str(request.node.fspath)
    if "test_auth_manager" in test_path or "test_token_rotation" in test_path:
        yield
        return

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

    def _fake_init(self):
        self.storage_path = "/tmp/whoop-test-tokens.json"
        self.key_file = "/tmp/whoop-test-key"

    monkeypatch.setattr(
        auth_manager.TokenManager, "get_valid_access_token", _fake_get_valid_access_token
    )
    monkeypatch.setattr(
        auth_manager.TokenManager, "get_token_info", _fake_get_token_info
    )
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
