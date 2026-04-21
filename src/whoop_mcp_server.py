#!/usr/bin/env python3
"""
WHOOP MCP Server — M3 surface.

M1 gave us a clean data client. M2 flattened responses. **M3 adds a
durable local cache (SQLite) in front of the WHOOP API**:

- ``sync_whoop`` pulls everything new since the last cursor and writes
  it into ``~/.whoop-mcp-server/whoop.db`` (overridable via
  ``WHOOP_DB_PATH``).
- Every list/get tool accepts ``fresh: bool`` (default ``False``). With
  ``fresh=False`` we read from the cache — no WHOOP API call at all. An
  empty cache for the requested window transparently triggers a
  targeted sync.
- The cache is exposed to Claude as MCP **resources** under
  ``whoop://db/...`` so it can browse date slices without invoking a
  tool.

Tools never raise. Errors are the familiar envelope:

    {"error": {"code": "<CODE>", "message": "<human>", "endpoint": "<path>"}}

Codes: AUTH_FAILED, RATE_LIMITED, NOT_FOUND, UPSTREAM_ERROR,
VALIDATION_ERROR, CACHE_ERROR, SYNC_ERROR.

SECURITY: ``whoop.db`` contains your raw fitness data; treat it as
sensitive as ``tokens.json``. The file is created with ``chmod 600``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
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
from whoop_store import WhoopStore
from whoop_sync import run_sync as _run_sync

SERVER_VERSION = "0.4.0"

logger = logging.getLogger("whoop_mcp_server")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

mcp = FastMCP(
    "whoop",
    instructions=(
        f"WHOOP MCP server v{SERVER_VERSION}. Read-only access to WHOOP v2 "
        "with a local SQLite cache. Call ``sync_whoop`` once per session "
        "(or when you know new WHOOP data exists) to refresh the cache; "
        "list/get tools default to ``fresh=False`` and read from the "
        "cache. Pass ``fresh=True`` to force an API call. The cache is "
        "also exposed as MCP resources under whoop://db/... so you can "
        "browse date slices without a tool call. All responses are "
        "flattened (score wrappers lifted, durations seconds, energy "
        "kcal, HR keys avg_hr_bpm/max_hr_bpm). Errors are a structured "
        "envelope, never raised."
    ),
)


# ---------- Singletons (patched in tests) ----------

_whoop_client: Optional[WhoopClient] = None
_store: Optional[WhoopStore] = None


def _default_db_path() -> str:
    env = os.getenv("WHOOP_DB_PATH")
    if env:
        return env
    return os.path.join(
        os.path.expanduser("~"), ".whoop-mcp-server", "whoop.db"
    )


def _get_client() -> WhoopClient:
    """Lazy singleton WhoopClient. Tests monkeypatch this function."""
    global _whoop_client
    if _whoop_client is None:
        logger.info("initializing WhoopClient")
        _whoop_client = WhoopClient()
    return _whoop_client


def _get_store() -> WhoopStore:
    """Lazy singleton WhoopStore. Tests monkeypatch via env var."""
    global _store
    if _store is None:
        path = _default_db_path()
        logger.info("initializing WhoopStore at %s", path)
        _store = WhoopStore(path)
        _store.init_schema()
    return _store


# ---------- Error envelope ----------


def _error_payload(code: str, message: str, endpoint: str) -> Dict[str, Any]:
    return {"error": {"code": code, "message": message, "endpoint": endpoint}}


def _map_error(exc: BaseException, endpoint: str) -> Dict[str, Any]:
    if isinstance(exc, WhoopAPIError):
        return _error_payload(exc.code, exc.message, exc.endpoint or endpoint)
    return _error_payload("UPSTREAM_ERROR", str(exc), endpoint)


# ---------- M3 helpers: cache-first list/get ----------


_LIST_TABLE_BY_RESOURCE = {
    "cycles": "cycles",
    "recoveries": "recoveries",
    "sleeps": "sleeps",
    "workouts": "workouts",
}


async def _cache_first_list(
    *,
    resource: str,
    table: str,
    start: Optional[str],
    end: Optional[str],
    limit: Optional[int],
    fresh: bool,
    endpoint: str,
    client_method: str,
    model_cls: type,
) -> Dict[str, Any]:
    """Shared read path for list tools.

    - ``fresh=True``: hit the API, upsert into cache, return from the API
      results.
    - ``fresh=False``: read the cache for the requested window. If the
      cache is empty for this window, run a targeted sync for ``[start,
      end)`` and re-read.
    """
    try:
        store = _get_store()
    except Exception as e:
        return _error_payload("CACHE_ERROR", f"store init failed: {e}", endpoint)

    client = _get_client()

    if fresh:
        try:
            raw_records = await getattr(client, client_method)(
                start=start, end=end, limit=limit
            )
        except Exception as e:
            return _map_error(e, endpoint)
        try:
            pairs = []
            for r in raw_records:
                try:
                    pairs.append((r, model_cls.model_validate(r).flatten()))
                except Exception:
                    continue
            store.upsert_records(table, pairs)
        except Exception as e:
            logger.warning("cache upsert failed for %s: %s", resource, e)
        return {"records": [p[1] for p in pairs]}

    # Cache-first path.
    try:
        rows = store.query_range(table, start=start, end=end, limit=limit)
    except Exception as e:
        return _error_payload("CACHE_ERROR", str(e), endpoint)

    if rows:
        return {"records": rows}

    # Empty window -> targeted sync, then re-read.
    logger.info(
        "cache miss for %s window [%s, %s); auto-syncing", resource, start, end
    )
    try:
        raw_records = await getattr(client, client_method)(
            start=start, end=end, limit=limit
        )
    except Exception as e:
        return _map_error(e, endpoint)
    try:
        pairs = []
        for r in raw_records:
            try:
                pairs.append((r, model_cls.model_validate(r).flatten()))
            except Exception:
                continue
        store.upsert_records(table, pairs)
        rows = store.query_range(table, start=start, end=end, limit=limit)
    except Exception as e:
        return _error_payload("CACHE_ERROR", str(e), endpoint)

    return {"records": rows}


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
async def sync_whoop(
    full: bool = False,
    since: Optional[str] = None,
    resources: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Refresh the local WHOOP cache.

    Call this once per session (or when you know new WHOOP data exists)
    to populate / update the on-disk cache. All list/get tools read from
    that cache by default, so running ``sync_whoop`` is cheap and is the
    intended way to avoid duplicate API calls.

    Args:
        full: if True, pull from the earliest allowed date (2010-01-01)
            for every selected resource. Ignores stored cursors. Use this
            once on a brand-new DB.
        since: ISO-8601 string overriding the stored cursor for list
            resources. Ignored for profile/body_measurement.
        resources: explicit subset of
            ``["cycles","recoveries","sleeps","workouts",
            "body_measurement","profile"]``. ``None`` means all.

    Returns a status dict::

        {
          "status": "success" | "partial" | "error",
          "started_at": "...",
          "completed_at": "...",
          "resources": {
            "cycles": {"records_fetched": N, "records_upserted": M,
                       "cursor_after": "...", "status": "success"},
            ...
          },
          "warnings": ["..."]   # only present when status != success
        }
    """
    try:
        store = _get_store()
        client = _get_client()
        return await _run_sync(
            store=store, client=client, full=full, since=since, resources=resources
        )
    except Exception as e:
        return _error_payload("SYNC_ERROR", str(e), "/sync")


@mcp.tool()
async def get_whoop_profile(fresh: bool = False) -> Dict[str, Any]:
    """Fetch the authenticated user's WHOOP profile (name, email).

    By default reads the latest cached snapshot. Pass ``fresh=True`` to
    hit the API and refresh the cache.
    """
    endpoint = "/user/profile/basic"
    try:
        store = _get_store()
    except Exception as e:
        return _error_payload("CACHE_ERROR", str(e), endpoint)

    if not fresh:
        snap = store.get_latest_snapshot("profile_snapshots")
        if snap is not None:
            return snap

    try:
        raw = await _get_client().get_profile()
        flat = Profile.model_validate(raw).flatten()
        try:
            store.upsert_snapshot("profile_snapshots", raw, flat)
        except Exception as e:
            logger.warning("profile snapshot upsert failed: %s", e)
        return flat
    except Exception as e:
        return _map_error(e, endpoint)


@mcp.tool()
async def get_whoop_body_measurement(fresh: bool = False) -> Dict[str, Any]:
    """Fetch the user's latest body measurements (height, weight, max HR).

    Reads from the cache by default; ``fresh=True`` forces an API call.
    """
    endpoint = "/user/measurement/body"
    try:
        store = _get_store()
    except Exception as e:
        return _error_payload("CACHE_ERROR", str(e), endpoint)

    if not fresh:
        snap = store.get_latest_snapshot("body_measurements")
        if snap is not None:
            return snap

    try:
        raw = await _get_client().get_body_measurement()
        flat = BodyMeasurement.model_validate(raw).flatten()
        try:
            store.upsert_snapshot("body_measurements", raw, flat)
        except Exception as e:
            logger.warning("body_measurement snapshot upsert failed: %s", e)
        return flat
    except Exception as e:
        return _map_error(e, endpoint)


@mcp.tool()
async def list_whoop_cycles(
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: Optional[int] = None,
    fresh: bool = False,
) -> Dict[str, Any]:
    """List WHOOP physiological cycles in a time window.

    Cache-first. Pass ``fresh=True`` to bypass the cache.

    Returns ``{"records": [flat_cycle, ...]}``.

    Args:
        start: ISO-8601 inclusive lower bound.
        end: ISO-8601 exclusive upper bound.
        limit: Max total records; None means no cap.
        fresh: if True, hit WHOOP directly and upsert results into cache.
    """
    return await _cache_first_list(
        resource="cycles",
        table="cycles",
        start=start,
        end=end,
        limit=limit,
        fresh=fresh,
        endpoint="/cycle",
        client_method="list_cycles",
        model_cls=Cycle,
    )


@mcp.tool()
async def get_whoop_cycle(cycle_id: int, fresh: bool = False) -> Dict[str, Any]:
    """Fetch a single WHOOP cycle by numeric ID. Cache-first."""
    endpoint = f"/cycle/{cycle_id}"
    try:
        store = _get_store()
    except Exception as e:
        return _error_payload("CACHE_ERROR", str(e), endpoint)

    if not fresh:
        hit = store.get_by_id("cycles", id=str(cycle_id))
        if hit is not None:
            return hit
    try:
        raw = await _get_client().get_cycle(cycle_id)
        flat = Cycle.model_validate(raw).flatten()
        try:
            store.upsert_records("cycles", [(raw, flat)])
        except Exception as e:
            logger.warning("cycle upsert failed: %s", e)
        return flat
    except Exception as e:
        return _map_error(e, endpoint)


@mcp.tool()
async def get_whoop_cycle_sleep(cycle_id: int, fresh: bool = False) -> Dict[str, Any]:
    """Fetch the sleep record associated with a given cycle. Cache-first."""
    endpoint = f"/cycle/{cycle_id}/sleep"
    try:
        store = _get_store()
    except Exception as e:
        return _error_payload("CACHE_ERROR", str(e), endpoint)

    if not fresh:
        hits = store.query_by_cycle_id("sleeps", cycle_id=cycle_id)
        # Prefer a non-nap sleep if multiple
        primary = next((h for h in hits if not h.get("nap")), None) or (hits[0] if hits else None)
        if primary is not None:
            return primary
    try:
        raw = await _get_client().get_cycle_sleep(cycle_id)
        flat = Sleep.model_validate(raw).flatten()
        try:
            store.upsert_records("sleeps", [(raw, flat)])
        except Exception as e:
            logger.warning("sleep upsert failed: %s", e)
        return flat
    except Exception as e:
        return _map_error(e, endpoint)


@mcp.tool()
async def get_whoop_cycle_recovery(cycle_id: int, fresh: bool = False) -> Dict[str, Any]:
    """Fetch the recovery record associated with a given cycle. Cache-first."""
    endpoint = f"/cycle/{cycle_id}/recovery"
    try:
        store = _get_store()
    except Exception as e:
        return _error_payload("CACHE_ERROR", str(e), endpoint)

    if not fresh:
        hits = store.query_by_cycle_id("recoveries", cycle_id=cycle_id)
        if hits:
            return hits[0]
    try:
        raw = await _get_client().get_cycle_recovery(cycle_id)
        flat = Recovery.model_validate(raw).flatten()
        try:
            store.upsert_records("recoveries", [(raw, flat)])
        except Exception as e:
            logger.warning("recovery upsert failed: %s", e)
        return flat
    except Exception as e:
        return _map_error(e, endpoint)


@mcp.tool()
async def list_whoop_recoveries(
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: Optional[int] = None,
    fresh: bool = False,
) -> Dict[str, Any]:
    """List WHOOP recovery records in a time window. Cache-first.

    Note: the WHOOP recovery endpoint does not expose ``start`` directly
    on the record (it lives on the parent cycle). For cache reads the
    window filter is best-effort; for ``fresh=True`` the API call is
    unchanged.
    """
    return await _cache_first_list(
        resource="recoveries",
        table="recoveries",
        start=start,
        end=end,
        limit=limit,
        fresh=fresh,
        endpoint="/recovery",
        client_method="list_recoveries",
        model_cls=Recovery,
    )


@mcp.tool()
async def list_whoop_sleeps(
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: Optional[int] = None,
    fresh: bool = False,
) -> Dict[str, Any]:
    """List WHOOP sleep activities (including naps) in a time window. Cache-first."""
    return await _cache_first_list(
        resource="sleeps",
        table="sleeps",
        start=start,
        end=end,
        limit=limit,
        fresh=fresh,
        endpoint="/activity/sleep",
        client_method="list_sleeps",
        model_cls=Sleep,
    )


@mcp.tool()
async def get_whoop_sleep(sleep_id: str, fresh: bool = False) -> Dict[str, Any]:
    """Fetch a single sleep activity by UUID. Cache-first."""
    endpoint = f"/activity/sleep/{sleep_id}"
    try:
        store = _get_store()
    except Exception as e:
        return _error_payload("CACHE_ERROR", str(e), endpoint)

    if not fresh:
        hit = store.get_by_id("sleeps", id=sleep_id)
        if hit is not None:
            return hit
    try:
        raw = await _get_client().get_sleep(sleep_id)
        flat = Sleep.model_validate(raw).flatten()
        try:
            store.upsert_records("sleeps", [(raw, flat)])
        except Exception as e:
            logger.warning("sleep upsert failed: %s", e)
        return flat
    except Exception as e:
        return _map_error(e, endpoint)


@mcp.tool()
async def list_whoop_workouts(
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: Optional[int] = None,
    fresh: bool = False,
) -> Dict[str, Any]:
    """List WHOOP workouts in a time window. Cache-first."""
    return await _cache_first_list(
        resource="workouts",
        table="workouts",
        start=start,
        end=end,
        limit=limit,
        fresh=fresh,
        endpoint="/activity/workout",
        client_method="list_workouts",
        model_cls=Workout,
    )


@mcp.tool()
async def get_whoop_workout(workout_id: str, fresh: bool = False) -> Dict[str, Any]:
    """Fetch a single workout by UUID. Cache-first."""
    endpoint = f"/activity/workout/{workout_id}"
    try:
        store = _get_store()
    except Exception as e:
        return _error_payload("CACHE_ERROR", str(e), endpoint)

    if not fresh:
        hit = store.get_by_id("workouts", id=workout_id)
        if hit is not None:
            return hit
    try:
        raw = await _get_client().get_workout(workout_id)
        flat = Workout.model_validate(raw).flatten()
        try:
            store.upsert_records("workouts", [(raw, flat)])
        except Exception as e:
            logger.warning("workout upsert failed: %s", e)
        return flat
    except Exception as e:
        return _map_error(e, endpoint)


# ---------- Daily summary (M2 join tool, now fresh-aware) ----------


def _parse_iso_date(s: str) -> datetime:
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
    except (TypeError, ValueError) as e:
        raise ValidationError(
            "VALIDATION_ERROR", 0, f"date must be YYYY-MM-DD: {e}", "/daily_summary"
        ) from e
    return d.replace(tzinfo=timezone.utc)


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _day_of_utc(ts: Optional[str]) -> Optional[str]:
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
    try:
        return await coro
    except WhoopAPIError as e:
        warnings.append(f"{label}: {e.code} {e.message}")
        return None
    except Exception as e:  # pragma: no cover
        warnings.append(f"{label}: {type(e).__name__}: {e}")
        return None


@mcp.tool()
async def get_whoop_daily_summary(date: str, fresh: bool = False) -> Dict[str, Any]:
    """Gather a single day's WHOOP data into one flat record.

    Assembles cycle, morning recovery, primary (longest non-nap) sleep,
    and the day's workouts. Subtools read from the cache by default;
    pass ``fresh=True`` to force API calls.

    Args:
        date: ISO date ``YYYY-MM-DD`` (UTC calendar day).
        fresh: propagated to each underlying fetch.
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

    # For daily_summary we always go through the client directly (not the
    # cache-first tool) to keep the existing M2 join semantics with
    # warnings. If ``fresh=False`` and the cache has rows for the day,
    # we still fall back to the client on the first miss — the cache
    # layer is transparent through the per-tool path above.
    if fresh:
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
    else:
        try:
            store = _get_store()
            raw_cycles = [
                # query_range returns flat rows; daily_summary downstream
                # works off raw_cycles via Pydantic. For the cache path
                # we already have flat rows so we short-circuit below.
                r for r in store.query_range("cycles", start=start_iso, end=end_iso)
            ]
            # If cache is empty, delegate to the fresh path instead.
            if not raw_cycles:
                return await get_whoop_daily_summary(date=date, fresh=True)
        except Exception:
            return await get_whoop_daily_summary(date=date, fresh=True)
        # Cache hit: we already have flat cycles. Fetch sleep/workout
        # from cache by window.
        store = _get_store()
        flat_cycles = raw_cycles  # already flat
        flat_sleeps = store.query_range("sleeps", start=start_iso, end=end_iso)
        flat_workouts = store.query_range("workouts", start=start_iso, end=end_iso)

        chosen = next(
            (c for c in flat_cycles if _day_of_utc(c.get("start")) == date),
            flat_cycles[0] if flat_cycles else None,
        )
        cycle_flat = chosen
        cycle_id = cycle_flat.get("id") if cycle_flat else None

        recovery_flat = None
        if cycle_id is not None:
            recs = store.query_by_cycle_id("recoveries", cycle_id=cycle_id)
            if recs:
                recovery_flat = recs[0]

        sleep_flat = None
        if cycle_id is not None:
            cands = [s for s in flat_sleeps if s.get("cycle_id") == cycle_id and not s.get("nap")]
            if cands:
                cands.sort(key=lambda f: f.get("in_bed_seconds") or 0, reverse=True)
                sleep_flat = cands[0]

        workouts_flat = [w for w in flat_workouts if _day_of_utc(w.get("start")) == date]

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
        return response

    # fresh=True path (M2 logic, using live fetches)
    if raw_cycles is None and raw_sleeps is None and raw_workouts is None:
        return _error_payload(
            "UPSTREAM_ERROR",
            "; ".join(warnings) or "all fetches failed",
            endpoint,
        )

    cycle_flat = None
    cycle_id = None
    if raw_cycles:
        chosen = next(
            (c for c in raw_cycles if _day_of_utc(c.get("start")) == date),
            raw_cycles[0],
        )
        cycle_flat = Cycle.model_validate(chosen).flatten()
        cycle_id = cycle_flat.get("id")

    recovery_flat = None
    if cycle_id is not None:
        raw_recovery = await _fetch_or_none(
            client.get_cycle_recovery(cycle_id), warnings, "recovery"
        )
        if raw_recovery is not None:
            recovery_flat = Recovery.model_validate(raw_recovery).flatten()

    sleep_flat = None
    if raw_sleeps and cycle_id is not None:
        candidates = [
            s for s in raw_sleeps if s.get("cycle_id") == cycle_id and not s.get("nap")
        ]
        if candidates:
            flats = [Sleep.model_validate(s).flatten() for s in candidates]
            flats.sort(key=lambda f: f.get("in_bed_seconds") or 0, reverse=True)
            sleep_flat = flats[0]

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

    response = {
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


# ---------- MCP Resources ----------

# FastMCP URI templates use path params, so we encode the date window as
# two path segments. This keeps Claude's lookup fully declarative:
#   whoop://db/cycles/2026-04-01/2026-04-08

_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_iso_date(s: str) -> Optional[str]:
    if not _DATE_RE.match(s):
        return None
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None
    return s + "T00:00:00.000Z"


def _resource_error(code: str, message: str) -> str:
    return json.dumps({"error": {"code": code, "message": message}})


def _records_resource(table: str, start: str, end: str) -> str:
    s = _validate_iso_date(start)
    e = _validate_iso_date(end)
    if s is None or e is None:
        return _resource_error(
            "VALIDATION_ERROR", "start/end must be YYYY-MM-DD"
        )
    try:
        rows = _get_store().query_range(table, start=s, end=e)
    except Exception as exc:
        return _resource_error("CACHE_ERROR", str(exc))
    return json.dumps(rows, default=str)


@mcp.resource(
    "whoop://db/cycles/{start}/{end}",
    mime_type="application/json",
    description="Cached cycles in [start, end). Dates are YYYY-MM-DD UTC.",
)
def resource_cycles(start: str, end: str) -> str:
    return _records_resource("cycles", start, end)


@mcp.resource(
    "whoop://db/recoveries/{start}/{end}",
    mime_type="application/json",
    description="Cached recoveries in [start, end). Dates are YYYY-MM-DD UTC.",
)
def resource_recoveries(start: str, end: str) -> str:
    return _records_resource("recoveries", start, end)


@mcp.resource(
    "whoop://db/sleeps/{start}/{end}",
    mime_type="application/json",
    description="Cached sleeps (including naps) in [start, end). Dates YYYY-MM-DD UTC.",
)
def resource_sleeps(start: str, end: str) -> str:
    return _records_resource("sleeps", start, end)


@mcp.resource(
    "whoop://db/workouts/{start}/{end}",
    mime_type="application/json",
    description="Cached workouts in [start, end). Dates are YYYY-MM-DD UTC.",
)
def resource_workouts(start: str, end: str) -> str:
    return _records_resource("workouts", start, end)


@mcp.resource(
    "whoop://db/profile",
    mime_type="application/json",
    description="Latest cached profile snapshot.",
)
def resource_profile() -> str:
    try:
        snap = _get_store().get_latest_snapshot("profile_snapshots")
    except Exception as e:
        return _resource_error("CACHE_ERROR", str(e))
    if snap is None:
        return _resource_error("NOT_FOUND", "no profile snapshot; run sync_whoop first")
    return json.dumps(snap, default=str)


@mcp.resource(
    "whoop://db/body_measurement",
    mime_type="application/json",
    description="Latest cached body_measurement snapshot.",
)
def resource_body_measurement() -> str:
    try:
        snap = _get_store().get_latest_snapshot("body_measurements")
    except Exception as e:
        return _resource_error("CACHE_ERROR", str(e))
    if snap is None:
        return _resource_error("NOT_FOUND", "no body_measurement snapshot; run sync_whoop first")
    return json.dumps(snap, default=str)


@mcp.resource(
    "whoop://db/sync_runs/{limit}",
    mime_type="application/json",
    description="Most recent sync_runs audit rows (limit = integer).",
)
def resource_sync_runs(limit: str) -> str:
    try:
        n = int(limit)
        if n <= 0 or n > 500:
            raise ValueError("limit out of range")
    except ValueError as e:
        return _resource_error("VALIDATION_ERROR", f"bad limit: {e}")
    try:
        rows = _get_store().list_sync_runs(limit=n)
    except Exception as e:
        return _resource_error("CACHE_ERROR", str(e))
    return json.dumps(rows, default=str)


# ---------- Entry point ----------


if __name__ == "__main__":
    logger.info("Starting WHOOP MCP server v%s", SERVER_VERSION)
    _get_client()
    _get_store()
    mcp.run()
