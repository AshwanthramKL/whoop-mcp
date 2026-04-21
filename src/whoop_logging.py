"""
Structured logging for the WHOOP MCP server — M6.

Configures the root logger with:

- A stderr handler (always on).
- A rotating file handler at ``~/.whoop-mcp-server/logs/whoop-mcp.log``
  (``RotatingFileHandler(maxBytes=1_000_000, backupCount=5)``), unless
  disabled by setting ``WHOOP_LOG_FILE`` to the empty string.
- A JSON formatter by default. Set ``WHOOP_LOG_JSON=false`` for a
  human-readable format.

Environment variables:

- ``WHOOP_LOG_LEVEL`` (default ``INFO``).
- ``WHOOP_LOG_FILE`` (default ``~/.whoop-mcp-server/logs/whoop-mcp.log``;
  empty string disables file logging).
- ``WHOOP_LOG_JSON`` (default ``true``).

No secrets are emitted — callers are responsible for never putting
tokens, refresh tokens, encryption keys, or raw request bodies into log
messages. The formatter trusts its inputs.

Structured event pattern::

    logger.info("api_request", extra={"event": "api_request",
                                      "endpoint": "/cycle",
                                      "status": 200,
                                      "duration_ms": 42})

The ``extra=`` dict is merged into the JSON line; scalars only.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = ["setup", "JSONFormatter", "DEFAULT_LOG_FILE"]


_DEFAULT_LOG_DIR = os.path.join(
    os.path.expanduser("~"), ".whoop-mcp-server", "logs"
)
DEFAULT_LOG_FILE = os.path.join(_DEFAULT_LOG_DIR, "whoop-mcp.log")


# Attribute names that belong to the stdlib LogRecord itself. Anything
# else on record.__dict__ is treated as structured "extra" context.
_STD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class JSONFormatter(logging.Formatter):
    """One-dict-per-line JSON formatter for structured logging."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        payload: Dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Merge any structured "extra" context passed via logger.x(..., extra={...}).
        for key, value in record.__dict__.items():
            if key in _STD_ATTRS or key.startswith("_"):
                continue
            # Never copy the raw args tuple.
            if key == "message":
                continue
            try:
                json.dumps(value, default=str)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["exc"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, default=str)
        except Exception:
            # Last-resort fallback — never crash a log line.
            return json.dumps({"ts": ts, "level": record.levelname, "msg": record.getMessage()})


class _PlainFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(fmt="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _resolve_log_file() -> Optional[str]:
    """Return the log file path, or None to disable file logging."""
    env = os.getenv("WHOOP_LOG_FILE")
    if env is None:
        return DEFAULT_LOG_FILE
    # Explicit empty string disables.
    if env.strip() == "":
        return None
    return env


_SETUP_MARKER = "_whoop_logging_configured"


def setup(
    *,
    max_bytes: int = 1_000_000,
    backup_count: int = 5,
) -> None:
    """Configure the root logger. Idempotent.

    Call this once on server startup (or in tests that need to exercise
    the handlers). Subsequent calls are no-ops unless the environment
    changed — but even then handler count stays stable because we clear
    previously-configured handlers before re-adding.
    """
    root = logging.getLogger()

    # If we configured previously, tear down our handlers first so that a
    # subsequent setup() call produces the same handler count (and respects
    # any env-var changes between calls).
    for h in list(root.handlers):
        if getattr(h, _SETUP_MARKER, False):
            root.removeHandler(h)

    level_name = os.getenv("WHOOP_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root.setLevel(level)

    json_mode = _env_bool("WHOOP_LOG_JSON", True)
    formatter: logging.Formatter = JSONFormatter() if json_mode else _PlainFormatter()

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.setLevel(level)
    setattr(stderr_handler, _SETUP_MARKER, True)
    root.addHandler(stderr_handler)

    log_file = _resolve_log_file()
    if log_file:
        try:
            Path(os.path.dirname(log_file) or ".").mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.setLevel(level)
            setattr(file_handler, _SETUP_MARKER, True)
            root.addHandler(file_handler)
            try:
                os.chmod(log_file, 0o600)
            except OSError:
                pass
        except Exception:
            # Never let logging setup crash the server — fall back to stderr.
            pass
