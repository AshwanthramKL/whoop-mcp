#!/usr/bin/env python3
"""
WHOOP MCP Server — M1 surface.

Exposes every WHOOP v2 read endpoint as an MCP tool. The server is a
pure data layer: tools return the raw JSON payload from the WHOOP API
unchanged. No flattening, no renaming, no unit conversions, no joins —
those come in later milestones.

Tools never raise. On failure they return a structured error envelope:

    {"error": {"code": "<machine_code>", "message": "<human>", "endpoint": "<path>"}}

Codes: AUTH_FAILED, RATE_LIMITED, NOT_FOUND, UPSTREAM_ERROR, VALIDATION_ERROR.
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from whoop_client import (
    AuthError,
    NotFoundError,
    RateLimitError,
    UpstreamError,
    ValidationError,
    WhoopAPIError,
    WhoopClient,
)

SERVER_VERSION = "0.2.0"

logger = logging.getLogger("whoop_mcp_server")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

mcp = FastMCP(
    "whoop",
    instructions=(
        f"WHOOP MCP server v{SERVER_VERSION}. "
        "Read-only access to WHOOP v2 API: profile, body measurement, cycles, "
        "recoveries, sleeps, workouts. All list tools support ISO-8601 date "
        "ranges and auto-paginate. Tools return raw WHOOP JSON; errors are "
        "returned as a structured envelope, never raised."
    ),
)

# ---------- Client accessor (patched in tests) ----------

_whoop_client: Optional[WhoopClient] = None


def _get_client() -> WhoopClient:
    """Lazy singleton WhoopClient. Tests monkeypatch this function."""
    global _whoop_client
    if _whoop_client is None:
        logger.info("initializing WhoopClient")
        _whoop_client = WhoopClient()
    return _whoop_client


# ---------- Error envelope ----------


def _error_payload(code: str, message: str, endpoint: str) -> Dict[str, Any]:
    return {"error": {"code": code, "message": message, "endpoint": endpoint}}


def _map_error(exc: BaseException, endpoint: str) -> Dict[str, Any]:
    """Convert any exception to the tool-facing error envelope."""
    if isinstance(exc, WhoopAPIError):
        return _error_payload(exc.code, exc.message, exc.endpoint or endpoint)
    # Unexpected error: map to UPSTREAM_ERROR with just the message.
    return _error_payload("UPSTREAM_ERROR", str(exc), endpoint)


# ---------- Tools ----------


@mcp.tool()
def get_whoop_auth_status() -> Dict[str, Any]:
    """Report WHOOP OAuth token status. Call this first if other WHOOP tools
    return AUTH_FAILED, to check whether the user needs to re-authorize.
    """
    try:
        return _get_client().get_auth_status()
    except Exception as e:
        return _map_error(e, "/auth/status")


@mcp.tool()
async def get_whoop_profile() -> Dict[str, Any]:
    """Fetch the authenticated user's WHOOP profile (name, email, user_id).
    Use when the caller asks who the WHOOP account belongs to.
    """
    try:
        return await _get_client().get_profile()
    except Exception as e:
        return _map_error(e, "/user/profile/basic")


@mcp.tool()
async def get_whoop_body_measurement() -> Dict[str, Any]:
    """Fetch the user's latest body measurements (height, weight, max HR).
    Use when computing anything that needs baseline body metrics.
    """
    try:
        return await _get_client().get_body_measurement()
    except Exception as e:
        return _map_error(e, "/user/measurement/body")


@mcp.tool()
async def list_whoop_cycles(
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """List WHOOP physiological cycles in a time window.

    Use when the caller asks about strain trends, day-over-day cycles, or
    needs cycle IDs to look up per-cycle sleep/recovery.

    Args:
        start: ISO-8601 inclusive lower bound (e.g. ``"2026-04-01T00:00:00Z"``).
        end: ISO-8601 exclusive upper bound.
        limit: Max total records to return. None means return everything in
            the window. The client auto-paginates internally.
    """
    try:
        records = await _get_client().list_cycles(start=start, end=end, limit=limit)
        return {"records": records}
    except Exception as e:
        return _map_error(e, "/cycle")


@mcp.tool()
async def get_whoop_cycle(cycle_id: int) -> Dict[str, Any]:
    """Fetch a single WHOOP cycle by numeric ID.

    Use when you already have a cycle_id from ``list_whoop_cycles`` and want
    its full payload (strain score, duration, timezone).

    Args:
        cycle_id: The integer cycle ID.
    """
    try:
        return await _get_client().get_cycle(cycle_id)
    except Exception as e:
        return _map_error(e, f"/cycle/{cycle_id}")


@mcp.tool()
async def get_whoop_cycle_sleep(cycle_id: int) -> Dict[str, Any]:
    """Fetch the sleep record associated with a given cycle.

    Use when tying a night's sleep back to the physiological cycle it
    belongs to. Returns the same schema as ``get_whoop_sleep``.

    Args:
        cycle_id: The integer cycle ID.
    """
    try:
        return await _get_client().get_cycle_sleep(cycle_id)
    except Exception as e:
        return _map_error(e, f"/cycle/{cycle_id}/sleep")


@mcp.tool()
async def get_whoop_cycle_recovery(cycle_id: int) -> Dict[str, Any]:
    """Fetch the recovery record associated with a given cycle.

    Use when you want the morning recovery score (HRV, RHR, SpO2) that
    WHOOP computed for a specific cycle.

    Args:
        cycle_id: The integer cycle ID.
    """
    try:
        return await _get_client().get_cycle_recovery(cycle_id)
    except Exception as e:
        return _map_error(e, f"/cycle/{cycle_id}/recovery")


@mcp.tool()
async def list_whoop_recoveries(
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """List WHOOP recovery records in a time window.

    Use when the caller asks about HRV, resting heart rate, or recovery
    score trends.

    Args:
        start: ISO-8601 inclusive lower bound.
        end: ISO-8601 exclusive upper bound.
        limit: Max total records; None means no cap. Auto-paginated.
    """
    try:
        records = await _get_client().list_recoveries(start=start, end=end, limit=limit)
        return {"records": records}
    except Exception as e:
        return _map_error(e, "/recovery")


@mcp.tool()
async def list_whoop_sleeps(
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """List WHOOP sleep activities (including naps) in a time window.

    Use when the caller asks about sleep duration, efficiency, or sleep
    debt across days.

    Args:
        start: ISO-8601 inclusive lower bound.
        end: ISO-8601 exclusive upper bound.
        limit: Max total records; None means no cap. Auto-paginated.
    """
    try:
        records = await _get_client().list_sleeps(start=start, end=end, limit=limit)
        return {"records": records}
    except Exception as e:
        return _map_error(e, "/activity/sleep")


@mcp.tool()
async def get_whoop_sleep(sleep_id: str) -> Dict[str, Any]:
    """Fetch a single sleep activity by UUID.

    Use when you have a ``sleep_id`` (UUID) from a list call and want the
    full sleep payload (stages, disturbances, efficiency).

    Args:
        sleep_id: UUID of the sleep activity.
    """
    try:
        return await _get_client().get_sleep(sleep_id)
    except Exception as e:
        return _map_error(e, f"/activity/sleep/{sleep_id}")


@mcp.tool()
async def list_whoop_workouts(
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """List WHOOP workouts in a time window.

    Use when the caller asks about training load, sport-specific strain,
    or needs workout IDs for deeper lookups.

    Args:
        start: ISO-8601 inclusive lower bound.
        end: ISO-8601 exclusive upper bound.
        limit: Max total records; None means no cap. Auto-paginated.
    """
    try:
        records = await _get_client().list_workouts(start=start, end=end, limit=limit)
        return {"records": records}
    except Exception as e:
        return _map_error(e, "/activity/workout")


@mcp.tool()
async def get_whoop_workout(workout_id: str) -> Dict[str, Any]:
    """Fetch a single workout by UUID.

    Use when you have a ``workout_id`` (UUID) from a list call and want
    the full payload (zones, kilojoules, distance).

    Args:
        workout_id: UUID of the workout.
    """
    try:
        return await _get_client().get_workout(workout_id)
    except Exception as e:
        return _map_error(e, f"/activity/workout/{workout_id}")


# ---------- Entry point ----------


if __name__ == "__main__":
    logger.info("Starting WHOOP MCP server v%s", SERVER_VERSION)
    _get_client()  # eagerly init to surface auth issues on startup
    mcp.run()
