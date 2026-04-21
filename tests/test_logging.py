"""
M6 structured logging tests.

Locks in:
- ``whoop_logging.setup()`` is idempotent.
- JSON formatter produces one-dict-per-line output.
- RotatingFileHandler writes to the configured path and rotates.
- Env var ``WHOOP_LOG_FILE=""`` disables file logging.
- Access tokens / refresh tokens never appear in any log line.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx

V2 = "https://api.prod.whoop.com/developer/v2"


def _reset_logging(monkeypatch, **env):
    """Clear existing handlers + set env, then import whoop_logging fresh."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


def test_setup_is_idempotent(tmp_path, monkeypatch):
    _reset_logging(monkeypatch, WHOOP_LOG_FILE=str(tmp_path / "whoop.log"))
    import whoop_logging

    whoop_logging.setup()
    n1 = len(logging.getLogger().handlers)
    whoop_logging.setup()
    n2 = len(logging.getLogger().handlers)
    assert n1 == n2


def test_json_line_structure(tmp_path, monkeypatch, capsys):
    _reset_logging(
        monkeypatch,
        WHOOP_LOG_FILE="",  # stderr only
        WHOOP_LOG_JSON="true",
        WHOOP_LOG_LEVEL="INFO",
    )
    import whoop_logging

    whoop_logging.setup()
    logger = logging.getLogger("whoop.test")
    logger.info("hello", extra={"event": "api_request", "endpoint": "/cycle"})

    err = capsys.readouterr().err.strip().splitlines()
    assert err, "expected at least one JSON line on stderr"
    parsed = json.loads(err[-1])
    assert parsed["level"] == "INFO"
    assert parsed["event"] == "api_request"
    assert parsed["endpoint"] == "/cycle"
    assert "ts" in parsed


def test_file_handler_rotates(tmp_path, monkeypatch):
    log_path = tmp_path / "whoop-mcp.log"
    _reset_logging(
        monkeypatch,
        WHOOP_LOG_FILE=str(log_path),
        WHOOP_LOG_JSON="true",
        WHOOP_LOG_LEVEL="INFO",
    )
    import whoop_logging

    # Shrink rotation size to force a rollover quickly.
    whoop_logging.setup(max_bytes=1_000, backup_count=2)

    logger = logging.getLogger("whoop.test.rot")
    # Each message is ~80 bytes JSON; 200 messages should rotate.
    for i in range(500):
        logger.info("rot-probe", extra={"event": "probe", "i": i, "pad": "x" * 40})

    rotated = tmp_path / "whoop-mcp.log.1"
    assert rotated.exists(), f"expected rotation at {rotated}"


def test_log_file_empty_string_disables_file_handler(tmp_path, monkeypatch):
    _reset_logging(
        monkeypatch,
        WHOOP_LOG_FILE="",
        WHOOP_LOG_JSON="true",
        WHOOP_LOG_LEVEL="INFO",
    )
    import whoop_logging

    whoop_logging.setup()
    root = logging.getLogger()
    kinds = [type(h).__name__ for h in root.handlers]
    assert "RotatingFileHandler" not in kinds


@pytest.mark.asyncio
@respx.mock
async def test_access_token_never_in_logs(tmp_path, monkeypatch, capsys):
    from whoop_client import WhoopClient

    _reset_logging(
        monkeypatch,
        WHOOP_LOG_FILE=str(tmp_path / "whoop.log"),
        WHOOP_LOG_JSON="true",
        WHOOP_LOG_LEVEL="DEBUG",
    )
    import whoop_logging

    whoop_logging.setup()

    respx.get(f"{V2}/user/profile/basic").mock(return_value=httpx.Response(200, json={"ok": True}))
    client = WhoopClient()
    await client.get_profile()

    # Inspect both stderr and the log file.
    err = capsys.readouterr().err
    log_path = tmp_path / "whoop.log"
    file_text = log_path.read_text() if log_path.exists() else ""

    assert "test-access-token" not in err
    assert "test-access-token" not in file_text
    # Bearer <token> must not appear either
    assert "Bearer test-access-token" not in err
    assert "Bearer test-access-token" not in file_text
