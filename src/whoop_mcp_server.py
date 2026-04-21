#!/usr/bin/env python3
"""
WHOOP MCP Server — M2 surface.

Exposes every WHOOP v2 read endpoint as an MCP tool. In M2 the server is
still a pure data layer, but responses are now **flattened**: raw WHOOP
records are parsed through Pydantic v2 models (see ``whoop_models.py``)
which drop IDs we don't need, lift nested ``score`` wrappers, and
normalize units (ms -> seconds, kilojoule -> calories, etc.) so Claude
can reason about the data without a decoder ring.

M2 also adds ``get_whoop_daily_summary(date)``, a single join tool that
gathers a day's cycle, recovery, primary sleep, and workouts into one
record with a ``score_states`` map and an optional ``warnings`` list
for partial upstream failures.

Tools never raise. On failure they return a structured error envelope:

    {"error": {"code": "<machine_code>", "message": "<human>", "endpoint": "<path>"}}

Codes: AUTH_FAILED, RATE_LIMITED, NOT_FOUND, UPSTREAM_ERROR, VALIDATION_ERROR.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
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
from whoop_models import (
    BodyMeasurement,
    Cycle,
    Profile,
    Recovery,
    Sleep,
    Workout,
    flatten_list,
)

SERVER_VERSION = "0.3.0"

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
        "recoveries, sleeps, workouts, plus a daily_summary join tool. "
        "All list tools support ISO-8601 date ranges and auto-paginate. "
        "Tool responses are flattened: WHOOP score wrappers are lifted, "
        "durations are seconds, energy is calories (kcal), heart rate keys "
        "are ``avg_hr_bpm`` / ``max_hr_bpm``. Errors are returned as a "
        "structured envelope, never raised."
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
    """Fetch the authenticated user's WHOOP profile (name, email).

    Returns a flat dict with ``email``, ``first_name``, ``last_name``.
    """
    try:
        raw = await _get_client().get_profile()
        return Profile.model_validate(raw).flatten()
    except Exception as e:
        return _map_error(e, "/user/profile/basic")


@mcp.tool()
async def get_whoop_body_measurement() -> Dict[str, Any]:
    """Fetch the user's latest body measurements (height, weight, max HR).

    Returns a flat dict with ``height_meter``, ``weight_kilogram``, ``max_hr_bpm``.
    """
    try:
        raw = await _get_client().get_body_measurement()
        return BodyMeasurement.model_validate(raw).flatten()
    except Exception as e:
        return _map_error(e, "/user/measurement/body")


@mcp.tool()
async def list_whoop_cycles(
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """List WHOOP physiological cycles in a time window.

    Returns ``{"records": [flat_cycle, ...]}``. Each cycle has
    ``id``, ``start``, ``end``, ``timezone_offset``, ``strain``,
    ``avg_hr_bpm``, ``max_hr_bpm``, ``calories``, ``score_state``.

    Args:
        start: ISO-8601 inclusive lower bound.
        end: ISO-8601 exclusive upper bound.
        limit: Max total records; None means no cap. Auto-paginated.
    """
    try:
        records = await _get_client().list_cycles(start=start, end=end, limit=limit)
        return {"records": flatten_list(Cycle, records)}
    except Exception as e:
        return _map_error(e, "/cycle")


@mcp.tool()
async def get_whoop_cycle(cycle_id: int) -> Dict[str, Any]:
    """Fetch a single WHOOP cycle by numeric ID.

    Returns a flat cycle dict (see ``list_whoop_cycles``).

    Args:
        cycle_id: The integer cycle ID.
    """
    try:
        raw = await _get_client().get_cycle(cycle_id)
        return Cycle.model_validate(raw).flatten()
    except Exception as e:
        return _map_error(e, f"/cycle/{cycle_id}")


@mcp.tool()
async def get_whoop_cycle_sleep(cycle_id: int) -> Dict[str, Any]:
    """Fetch the sleep record associated with a given cycle.

    Returns a flat sleep dict with ``in_bed_seconds``, ``deep_sleep_seconds``,
    ``rem_sleep_seconds``, ``light_sleep_seconds``, ``awake_seconds``, and
    sleep score percentages.

    Args:
        cycle_id: The integer cycle ID.
    """
    try:
        raw = await _get_client().get_cycle_sleep(cycle_id)
        return Sleep.model_validate(raw).flatten()
    except Exception as e:
        return _map_error(e, f"/cycle/{cycle_id}/sleep")


@mcp.tool()
async def get_whoop_cycle_recovery(cycle_id: int) -> Dict[str, Any]:
    """Fetch the recovery record associated with a given cycle.

    Returns a flat recovery dict with ``recovery_score``,
    ``hrv_rmssd_ms``, ``resting_heart_rate_bpm``, ``spo2_pct``, ``skin_temp_c``.

    Args:
        cycle_id: The integer cycle ID.
    """
    try:
        raw = await _get_client().get_cycle_recovery(cycle_id)
        return Recovery.model_validate(raw).flatten()
    except Exception as e:
        return _map_error(e, f"/cycle/{cycle_id}/recovery")


@mcp.tool()
async def list_whoop_recoveries(
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """List WHOOP recovery records in a time window.

    Returns ``{"records": [flat_recovery, ...]}``. Each record has
    ``cycle_id``, ``sleep_id``, ``recovery_score``, ``hrv_rmssd_ms``,
    ``resting_heart_rate_bpm``, ``spo2_pct``, ``skin_temp_c``, ``score_state``.

    Args:
        start: ISO-8601 inclusive lower bound.
        end: ISO-8601 exclusive upper bound.
        limit: Max total records; None means no cap. Auto-paginated.
    """
    try:
        records = await _get_client().list_recoveries(start=start, end=end, limit=limit)
        return {"records": flatten_list(Recovery, records)}
    except Exception as e:
        return _map_error(e, "/recovery")


@mcp.tool()
async def list_whoop_sleeps(
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """List WHOOP sleep activities (including naps) in a time window.

    Returns ``{"records": [flat_sleep, ...]}``. Stage durations are in
    seconds and renamed to deep/rem/light/awake/in_bed.

    Args:
        start: ISO-8601 inclusive lower bound.
        end: ISO-8601 exclusive upper bound.
        limit: Max total records; None means no cap. Auto-paginated.
    """
    try:
        records = await _get_client().list_sleeps(start=start, end=end, limit=limit)
        return {"records": flatten_list(Sleep, records)}
    except Exception as e:
        return _map_error(e, "/activity/sleep")


@mcp.tool()
async def get_whoop_sleep(sleep_id: str) -> Dict[str, Any]:
    """Fetch a single sleep activity by UUID.

    Returns a flat sleep dict (see ``list_whoop_sleeps``).

    Args:
        sleep_id: UUID of the sleep activity.
    """
    try:
        raw = await _get_client().get_sleep(sleep_id)
        return Sleep.model_validate(raw).flatten()
    except Exception as e:
        return _map_error(e, f"/activity/sleep/{sleep_id}")


@mcp.tool()
async def list_whoop_workouts(
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """List WHOOP workouts in a time window.

    Returns ``{"records": [flat_workout, ...]}``. Each workout has
    ``id``, ``sport_id``, ``sport_name``, ``start``, ``end``, ``strain``,
    ``avg_hr_bpm``, ``max_hr_bpm``, ``calories``, ``distance_meter``,
    ``zone_durations_seconds`` (dict of zero..five).

    Args:
        start: ISO-8601 inclusive lower bound.
        end: ISO-8601 exclusive upper bound.
        limit: Max total records; None means no cap. Auto-paginated.
    """
    try:
        records = await _get_client().list_workouts(start=start, end=end, limit=limit)
        return {"records": flatten_list(Workout, records)}
    except Exception as e:
        return _map_error(e, "/activity/workout")


@mcp.tool()
async def get_whoop_workout(workout_id: str) -> Dict[str, Any]:
    """Fetch a single workout by UUID.

    Returns a flat workout dict (see ``list_whoop_workouts``).

    Args:
        workout_id: UUID of the workout.
    """
    try:
        raw = await _get_client().get_workout(workout_id)
        return Workout.model_validate(raw).flatten()
    except Exception as e:
        return _map_error(e, f"/activity/workout/{workout_id}")


# ---------- Daily summary (M2 join tool) ----------


def _parse_iso_date(s: str) -> datetime:
    """Parse a 'YYYY-MM-DD' string into a UTC-midnight datetime."""
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
    except (TypeError, ValueError) as e:
        raise ValidationError(
            "VALIDATION_ERROR",
            0,
            f"date must be YYYY-MM-DD: {e}",
            "/daily_summary",
        ) from e
    return d.replace(tzinfo=timezone.utc)


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _day_of_utc(ts: Optional[str]) -> Optional[str]:
    """Return the UTC calendar date (YYYY-MM-DD) of an ISO-8601 timestamp."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d")


async def _fetch_or_none(coro, warnings: List[str], label: str) -> Any:
    """Await ``coro``, stash a warning on failure, return None."""
    try:
        return await coro
    except WhoopAPIError as e:
        warnings.append(f"{label}: {e.code} {e.message}")
        return None
    except Exception as e:  # pragma: no cover - defensive
        warnings.append(f"{label}: {type(e).__name__}: {e}")
        return None


@mcp.tool()
async def get_whoop_daily_summary(date: str) -> Dict[str, Any]:
    """Gather a single day's WHOOP data into one flat record.

    Assembles cycle, morning recovery, primary (longest non-nap) sleep, and
    the day's workouts for the given UTC calendar date. Each sub-field is
    flattened using the same rules as the per-resource tools. Missing data
    (no cycle yet, or a partial upstream failure) appears as ``null`` with
    an entry in ``warnings``. If every upstream fetch fails, a structured
    error envelope is returned instead.

    Response shape::

        {
          "date": "2026-04-20",
          "cycle": {...} | null,
          "recovery": {...} | null,
          "sleep": {...} | null,
          "workouts": [{...}, ...],
          "score_states": {"cycle": "SCORED", "recovery": "SCORED", "sleep": "SCORED"},
          "warnings": ["recovery: UPSTREAM_ERROR ..."]  # optional
        }

    Args:
        date: ISO date ``YYYY-MM-DD``. Interpreted as a UTC calendar day.
    """
    endpoint = "/daily_summary"
    warnings: List[str] = []

    try:
        start_dt = _parse_iso_date(date)
    except ValidationError as e:
        return _map_error(e, endpoint)

    end_dt = start_dt + timedelta(days=1)
    start_iso = _iso_z(start_dt)
    end_iso = _iso_z(end_dt)

    client = _get_client()

    # Fire the three windowed list calls concurrently.
    cycles_task = _fetch_or_none(
        client.list_cycles(start=start_iso, end=end_iso), warnings, "cycles"
    )
    sleeps_task = _fetch_or_none(
        client.list_sleeps(start=start_iso, end=end_iso), warnings, "sleeps"
    )
    workouts_task = _fetch_or_none(
        client.list_workouts(start=start_iso, end=end_iso), warnings, "workouts"
    )

    raw_cycles, raw_sleeps, raw_workouts = await asyncio.gather(
        cycles_task, sleeps_task, workouts_task
    )

    # All three failed -> surface as an error envelope (no usable data).
    if raw_cycles is None and raw_sleeps is None and raw_workouts is None:
        # Prefer to quote the first WhoopAPIError; fall back to generic.
        return _error_payload(
            "UPSTREAM_ERROR",
            "; ".join(warnings) or "all fetches failed",
            endpoint,
        )

    # Pick the cycle that overlaps this day, if any.
    cycle_flat: Optional[Dict[str, Any]] = None
    cycle_id: Optional[int] = None
    if raw_cycles:
        # If several, prefer the one whose start is on this UTC day;
        # otherwise just take the first.
        chosen = None
        for c in raw_cycles:
            if _day_of_utc(c.get("start")) == date:
                chosen = c
                break
        if chosen is None:
            chosen = raw_cycles[0]
        cycle_flat = Cycle.model_validate(chosen).flatten()
        cycle_id = cycle_flat.get("id")

    # Recovery: only fetch if we have a cycle.
    recovery_flat: Optional[Dict[str, Any]] = None
    if cycle_id is not None:
        raw_recovery = await _fetch_or_none(
            client.get_cycle_recovery(cycle_id), warnings, "recovery"
        )
        if raw_recovery is not None:
            recovery_flat = Recovery.model_validate(raw_recovery).flatten()

    # Primary sleep: non-nap, matching cycle_id, longest in_bed_seconds.
    sleep_flat: Optional[Dict[str, Any]] = None
    if raw_sleeps and cycle_id is not None:
        candidates = [
            s for s in raw_sleeps if s.get("cycle_id") == cycle_id and not s.get("nap")
        ]
        if candidates:
            flats = [Sleep.model_validate(s).flatten() for s in candidates]
            flats.sort(key=lambda f: f.get("in_bed_seconds") or 0, reverse=True)
            sleep_flat = flats[0]

    # Workouts on this UTC day.
    if raw_workouts:
        workouts_flat = [
            Workout.model_validate(w).flatten()
            for w in raw_workouts
            if _day_of_utc(w.get("start")) == date
        ]
    else:
        workouts_flat = []

    score_states = {
        "cycle": cycle_flat["score_state"] if cycle_flat else None,
        "recovery": recovery_flat["score_state"] if recovery_flat else None,
        "sleep": sleep_flat["score_state"] if sleep_flat else None,
    }

    response: Dict[str, Any] = {
        "date": date,
        "cycle": cycle_flat,
        "recovery": recovery_flat,
        "sleep": sleep_flat,
        "workouts": workouts_flat,
        "score_states": score_states,
    }
    if warnings:
        response["warnings"] = warnings
    return response


# ---------- Entry point ----------


if __name__ == "__main__":
    logger.info("Starting WHOOP MCP server v%s", SERVER_VERSION)
    _get_client()  # eagerly init to surface auth issues on startup
    mcp.run()
