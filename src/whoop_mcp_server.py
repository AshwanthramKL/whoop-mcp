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
import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

import whoop_export
import whoop_logging
from whoop_client import (
    AuthError,
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
    _Base,
)
from whoop_store import RECORD_TABLES, SCHEMA_VERSION, SNAPSHOT_TABLES, WhoopStore
from whoop_sync import run_sync as _run_sync

try:
    # Single source of truth: package version.
    from __version__ import __version__ as SERVER_VERSION  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - defensive fallback for odd sys.paths
    SERVER_VERSION = "0.8.1"

# M6: configure structured JSON logging + rotating file handler once at
# import time. Safe to re-call; ``whoop_logging.setup`` is idempotent.
try:
    whoop_logging.setup()
except Exception:
    # Never let logging setup break module import.
    pass
logger = logging.getLogger("whoop_mcp_server")

mcp = FastMCP(
    "whoop",
    instructions=(
        f"WHOOP MCP server v{SERVER_VERSION}. Read-only WHOOP v2 data "
        "via a local SQLite cache. Run sync_whoop once; list/get tools "
        "read cache (fresh=True bypasses). Also: get_whoop_events, "
        "export_whoop, health_check. Flattened responses; errors are an "
        "envelope, never raised. See README.md for the full catalog."
    ),
)


# ---------- Singletons (patched in tests) ----------

_whoop_client: WhoopClient | None = None
_store: WhoopStore | None = None


def _default_db_path() -> str:
    env = os.getenv("WHOOP_DB_PATH")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".whoop-mcp-server", "whoop.db")


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


def _error_payload(code: str, message: str, endpoint: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "endpoint": endpoint}}


def _map_error(exc: BaseException, endpoint: str) -> dict[str, Any]:
    if isinstance(exc, WhoopAPIError):
        return _error_payload(exc.code, exc.message, exc.endpoint or endpoint)
    return _error_payload("UPSTREAM_ERROR", str(exc), endpoint)


# ---------- M3 helpers: cache-first list/get ----------


async def _cache_first_list(
    *,
    resource: str,
    table: str,
    start: str | None,
    end: str | None,
    limit: int | None,
    fresh: bool,
    endpoint: str,
    client_method: str,
    model_cls: type[_Base],
) -> dict[str, Any]:
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
            raw_records = await getattr(client, client_method)(start=start, end=end, limit=limit)
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
    logger.info("cache miss for %s window [%s, %s); auto-syncing", resource, start, end)
    try:
        raw_records = await getattr(client, client_method)(start=start, end=end, limit=limit)
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
def get_whoop_auth_status() -> dict[str, Any]:
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
    since: str | None = None,
    resources: list[str] | None = None,
) -> dict[str, Any]:
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
async def get_whoop_profile(fresh: bool = False) -> dict[str, Any]:
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
async def get_whoop_body_measurement(fresh: bool = False) -> dict[str, Any]:
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
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
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
async def get_whoop_cycle(cycle_id: int, fresh: bool = False) -> dict[str, Any]:
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
async def get_whoop_cycle_sleep(cycle_id: int, fresh: bool = False) -> dict[str, Any]:
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
async def get_whoop_cycle_recovery(cycle_id: int, fresh: bool = False) -> dict[str, Any]:
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
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
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
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
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
async def get_whoop_sleep(sleep_id: str, fresh: bool = False) -> dict[str, Any]:
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
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
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
async def get_whoop_workout(workout_id: str, fresh: bool = False) -> dict[str, Any]:
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


def _day_of_utc(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d")


async def _fetch_or_none(coro, warnings: list[str], label: str) -> Any:
    try:
        return await coro
    except WhoopAPIError as e:
        warnings.append(f"{label}: {e.code} {e.message}")
        return None
    except Exception as e:  # pragma: no cover
        warnings.append(f"{label}: {type(e).__name__}: {e}")
        return None


@mcp.tool()
async def get_whoop_daily_summary(date: str, fresh: bool = False) -> dict[str, Any]:
    """Gather a single day's WHOOP data into one flat record.

    Assembles cycle, morning recovery, primary (longest non-nap) sleep,
    and the day's workouts. Subtools read from the cache by default;
    pass ``fresh=True`` to force API calls.

    Args:
        date: ISO date ``YYYY-MM-DD`` (UTC calendar day).
        fresh: propagated to each underlying fetch.
    """
    endpoint = "/daily_summary"
    warnings: list[str] = []

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
                r
                for r in store.query_range("cycles", start=start_iso, end=end_iso)
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
        response: dict[str, Any] = {
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
        candidates = [s for s in raw_sleeps if s.get("cycle_id") == cycle_id and not s.get("nap")]
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


def _validate_iso_date(s: str) -> str | None:
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
        return _resource_error("VALIDATION_ERROR", "start/end must be YYYY-MM-DD")
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


# ---------- Events feed (M5) ----------


# Public resource names accepted by get_whoop_events. Includes both
# record tables and snapshot aliases.
_EVENT_RESOURCES = (
    "cycles",
    "recoveries",
    "sleeps",
    "workouts",
    "body_measurement",
    "profile",
)

_EVENT_LIMIT_MAX = 5000


def _parse_iso_ts(s: str) -> datetime | None:
    """Best-effort ISO-8601 parse -> aware UTC datetime. ``None`` on failure."""
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------- composite events cursor ----------
#
# Cursor encodes the last-returned event as urlsafe-base64 of the literal
# string ``<updated_at>|<resource>|<id>``. Opaque to callers; cheap to
# round-trip. A malformed cursor is rejected at tool-boundary as
# VALIDATION_ERROR. Plain ISO-8601 ``since`` values still work for
# back-compat: the encoder only produces cursors; the decoder treats
# anything that fails base64 + split as "not a cursor".

_CURSOR_SEP = "|"


def _encode_events_cursor(updated_at: str, resource: str, id_: str) -> str:
    raw = f"{updated_at}{_CURSOR_SEP}{resource}{_CURSOR_SEP}{id_}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_events_cursor(s: str) -> tuple[str, str, str] | None:
    """Return ``(updated_at, resource, id)`` or ``None`` if ``s`` is not a cursor.

    "Not a cursor" includes: not base64, decoded string doesn't contain
    two separators, or decoded updated_at doesn't parse as ISO-8601.
    """
    if not isinstance(s, str) or not s:
        return None
    # Heuristic: a valid ISO string contains ``:`` which urlsafe-base64
    # does not. If it starts with a digit and has a ``-`` / ``:`` pattern,
    # treat as ISO and don't try base64.
    if ":" in s or s.endswith("Z"):
        return None
    try:
        decoded = base64.urlsafe_b64decode(s.encode("ascii") + b"==").decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    parts = decoded.split(_CURSOR_SEP)
    if len(parts) != 3:
        return None
    updated_at, resource, id_ = parts
    if _parse_iso_ts(updated_at) is None:
        return None
    if not resource or not id_:
        return None
    return updated_at, resource, id_


def _events_core(
    *,
    since: str,
    until: str | None,
    resources: list[str] | None,
    limit: int,
) -> dict[str, Any]:
    """Shared implementation for the events tool and resource.

    Validates args, queries the store via ``iter_events``, applies limit +
    pagination cursor, and returns either the success payload or an error
    envelope. Never raises.
    """
    endpoint = "get_whoop_events"

    # Validate since. Accepts either:
    #  - plain ISO-8601 timestamp (back-compat, strict >)
    #  - opaque cursor from a previous next_cursor (composite tiebreaker)
    since_cursor = _decode_events_cursor(since) if since else None
    if since_cursor is not None:
        since_for_sql = since_cursor[0]
        since_dt = _parse_iso_ts(since_for_sql)
    else:
        since_dt = _parse_iso_ts(since) if since else None
        since_for_sql = since

    if since_dt is None:
        # Empty / garbage / anything else.
        return _error_payload(
            "VALIDATION_ERROR",
            "'since' must be a non-empty ISO-8601 timestamp or opaque events cursor",
            endpoint,
        )

    # Validate / default until.
    until_dt: datetime
    if until is None or until == "":
        until_dt = datetime.now(timezone.utc)
        until = until_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    else:
        parsed_until = _parse_iso_ts(until)
        if parsed_until is None:
            return _error_payload(
                "VALIDATION_ERROR",
                "'until' must be an ISO-8601 timestamp",
                endpoint,
            )
        until_dt = parsed_until

    if until_dt <= since_dt:
        return _error_payload(
            "VALIDATION_ERROR",
            "'until' must be strictly greater than 'since'",
            endpoint,
        )

    # Validate limit.
    try:
        limit_i = int(limit)
    except (TypeError, ValueError):
        return _error_payload(
            "VALIDATION_ERROR", "'limit' must be an integer in [1, 5000]", endpoint
        )
    if limit_i < 1 or limit_i > _EVENT_LIMIT_MAX:
        return _error_payload(
            "VALIDATION_ERROR",
            f"'limit' must be in [1, {_EVENT_LIMIT_MAX}]",
            endpoint,
        )

    # Validate + normalize resources.
    if resources is None:
        resource_list = list(_EVENT_RESOURCES)
    else:
        if not isinstance(resources, (list, tuple)) or not resources:
            return _error_payload(
                "VALIDATION_ERROR",
                "'resources' must be a non-empty list when provided",
                endpoint,
            )
        for r in resources:
            if r not in _EVENT_RESOURCES:
                return _error_payload(
                    "VALIDATION_ERROR",
                    f"unknown resource {r!r}; allowed: {list(_EVENT_RESOURCES)}",
                    endpoint,
                )
        resource_list = list(resources)

    # Read.
    try:
        store = _get_store()
    except Exception as e:
        return _error_payload("CACHE_ERROR", f"store init failed: {e}", endpoint)

    try:
        rows = list(
            store.iter_events(
                resources=resource_list,
                since=since_for_sql,
                until=until,
                limit=limit_i,
                since_cursor=since_cursor,
            )
        )
    except Exception as e:
        logger.exception("get_whoop_events: iter_events failed")
        return _error_payload("CACHE_ERROR", f"{type(e).__name__}: {e}", endpoint)

    # Truncation: iter_events returns up to limit+1 rows. If we got the
    # extra row, trim to limit and set next_cursor to the composite cursor
    # of the last kept event (so a later call with since=cursor continues
    # correctly across ties).
    if len(rows) > limit_i:
        events = rows[:limit_i]
        if events:
            last = events[-1]
            next_cursor = _encode_events_cursor(
                str(last["updated_at"]), str(last["resource"]), str(last["id"])
            )
        else:
            next_cursor = None
    else:
        events = rows
        next_cursor = None

    return {
        "status": "success",
        "count": len(events),
        "since": since,
        "until": until,
        "events": events,
        "next_cursor": next_cursor,
    }


@mcp.tool()
async def get_whoop_events(
    since: str,
    until: str | None = None,
    resources: list[str] | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Returns WHOOP records that changed since a given timestamp, across
    all cached resources.

    Use for 'what's new' checks or to build activity feeds. Reads from the
    local cache only — call ``sync_whoop()`` first to pick up upstream
    changes. No WHOOP API calls are made from this path.

    The window is half-open: ``updated_at > since AND updated_at < until``
    (strict on both sides). This lets you paginate by passing the returned
    ``next_cursor`` back as the next ``since`` without re-seeing the
    cursor row.

    Args:
        since: ISO-8601 timestamp; strict lower bound on ``updated_at``.
            Required.
        until: ISO-8601 timestamp; strict upper bound. Defaults to
            ``now`` UTC.
        resources: subset of
            ``["cycles","recoveries","sleeps","workouts","body_measurement","profile"]``.
            ``None`` means all.
        limit: cap on total events returned. Must be in ``[1, 5000]``.
            Default 500.

    Returns::

        {"status": "success", "count": N, "since": "...", "until": "...",
         "events": [{"resource": "...", "id": "...",
                     "updated_at": "...", "record": {...}}, ...],
         "next_cursor": null | "<iso>"}

    If more events exist than ``limit``, ``next_cursor`` is an **opaque
    composite cursor** encoded as urlsafe base64 of
    ``<updated_at>|<resource>|<id>`` — the triple of the last returned
    event. Pass it as ``since`` on the next call to continue; the feed
    uses it as a tiebroken lower bound so no two events with the same
    ``updated_at`` are skipped at a pagination boundary.

    Back-compat: ``since`` still accepts a plain ISO-8601 string (strict
    ``>`` lower bound). Garbage strings are rejected as VALIDATION_ERROR.
    On validation failure or cache error, returns the standard
    ``{"error": {...}}`` envelope. Tool never raises.
    """
    return _events_core(since=since, until=until, resources=resources, limit=limit)


# ---------- Events feed resources (M5) ----------

# FastMCP 1.27 path-template params are required, so we expose two
# resources — one that defaults ``until`` to "now" and one that takes
# both bounds explicitly. Path segments are ISO-8601 timestamps (Z form
# recommended).


@mcp.resource(
    "whoop://db/events/{since}",
    mime_type="application/json",
    description=(
        "Cached events with updated_at in (since, now). 'since' is an "
        "ISO-8601 timestamp. Returns the same shape as get_whoop_events."
    ),
)
def resource_events_since(since: str) -> str:
    payload = _events_core(since=since, until=None, resources=None, limit=500)
    return json.dumps(payload, default=str)


@mcp.resource(
    "whoop://db/events/{since}/{until}",
    mime_type="application/json",
    description=(
        "Cached events with updated_at in (since, until). Both bounds are "
        "ISO-8601 timestamps. Returns the same shape as get_whoop_events."
    ),
)
def resource_events_window(since: str, until: str) -> str:
    payload = _events_core(since=since, until=until, resources=None, limit=500)
    return json.dumps(payload, default=str)


# ---------- Export tool (M4) ----------


@mcp.tool()
async def export_whoop(
    kind: str,
    format: str,
    path: str,
    start: str | None = None,
    end: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Export cached WHOOP records to disk.

    Requires ``sync_whoop()`` to have been run first — exports read from
    the local cache and never hit the WHOOP API. Supports CSV, JSONL, and
    Parquet. For ``kind='all'``, writes one file per resource into the
    given directory (created if missing). For an empty date window the
    export still writes a file (header-only CSV, empty JSONL, empty
    Parquet table) so downstream tooling sees a consistent artifact.

    Tip: write exports to a secure location. The file contains your raw
    fitness data in flat JSON shape (no OAuth secrets, no raw API
    payloads — just the LLM-friendly flattened rows).

    Args:
        kind: one of ``cycles``, ``recoveries``, ``sleeps``, ``workouts``,
            ``all``.
        format: one of ``csv``, ``jsonl``, ``parquet``.
        path: output file (for a single kind) or directory (for
            ``kind='all'``). Parent directory is created if needed.
        start: inclusive lower-bound date ``YYYY-MM-DD`` (UTC). Defaults
            to the cache epoch.
        end: inclusive upper-bound date ``YYYY-MM-DD`` (UTC). Defaults
            to today.
        overwrite: if False (default) and the destination has content,
            returns ``FILE_EXISTS``. If True, replaces silently.

    Returns a dict with shape::

        {"status": "success", "kind": "cycles", "format": "csv",
         "files": [{"resource": "cycles", "path": "...",
                    "records": 91, "bytes": 12345}],
         "range": {"start": "...", "end": "..."}, "warnings": []}

    Errors are returned as ``{"error": {"code": ..., "message": ...,
    "endpoint": "export_whoop"}}`` with codes ``VALIDATION_ERROR``,
    ``CACHE_EMPTY``, ``FILE_EXISTS``, or ``EXPORT_ERROR``.
    """
    try:
        store = _get_store()
    except Exception as e:
        return _error_payload("CACHE_ERROR", f"store init failed: {e}", "export_whoop")

    try:
        return whoop_export.export_whoop(
            store=store,
            kind=kind,
            format=format,
            path=path,
            start=start,
            end=end,
            overwrite=overwrite,
        )
    except Exception as e:
        logger.exception("export_whoop tool failed")
        return _error_payload("EXPORT_ERROR", f"{type(e).__name__}: {e}", "export_whoop")


# ---------- Health check (M6) ----------


def _component(status: str, detail: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"status": status, "detail": detail}
    out.update(extra)
    return out


def _rank(status: str) -> int:
    # Higher = worse.
    return {"ok": 0, "skipped": 0, "warn": 1, "fail": 2}.get(status, 2)


async def _check_auth() -> dict[str, Any]:
    import time as _time

    t0 = _time.perf_counter()
    try:
        info = _get_client().get_auth_status()
    except Exception as e:
        return _component("fail", f"get_auth_status raised: {type(e).__name__}")
    latency_ms = int((_time.perf_counter() - t0) * 1000)
    status_str = info.get("status") if isinstance(info, dict) else None
    if status_str == "valid":
        # Intentionally omit the raw timestamp (PII-adjacent); summarize.
        return _component(
            "ok",
            "token valid",
            latency_ms=latency_ms,
            has_refresh_token=bool(info.get("has_refresh_token")),
        )
    if status_str == "expired":
        if info.get("has_refresh_token"):
            return _component(
                "warn",
                "token expired; refresh token available",
                latency_ms=latency_ms,
            )
        return _component("fail", "token expired; no refresh token", latency_ms=latency_ms)
    if status_str == "no_tokens":
        return _component("fail", "no tokens on file", latency_ms=latency_ms)
    return _component("warn", f"unknown auth status: {status_str!r}", latency_ms=latency_ms)


async def _check_api(live: bool) -> dict[str, Any]:
    if not live:
        return _component("skipped", "live=False; skipped network call")
    import time as _time

    import httpx as _httpx

    t0 = _time.perf_counter()
    try:
        client = _get_client()
        # Short timeout: the goal is liveness, not data.
        await asyncio.wait_for(client.get_profile(), timeout=5.0)
    except asyncio.TimeoutError:
        latency_ms = int((_time.perf_counter() - t0) * 1000)
        return _component("fail", "timeout after 5s", latency_ms=latency_ms)
    except AuthError:
        latency_ms = int((_time.perf_counter() - t0) * 1000)
        return _component("fail", "auth failed (401)", latency_ms=latency_ms)
    except WhoopAPIError as e:
        latency_ms = int((_time.perf_counter() - t0) * 1000)
        return _component("fail", f"{e.code}: {e.message[:120]}", latency_ms=latency_ms)
    except _httpx.HTTPError as e:
        latency_ms = int((_time.perf_counter() - t0) * 1000)
        return _component("fail", f"transport: {type(e).__name__}", latency_ms=latency_ms)
    except Exception as e:
        latency_ms = int((_time.perf_counter() - t0) * 1000)
        return _component("fail", f"{type(e).__name__}", latency_ms=latency_ms)
    latency_ms = int((_time.perf_counter() - t0) * 1000)
    return _component("ok", "reachable", latency_ms=latency_ms)


def _check_cache_readable() -> dict[str, Any]:
    try:
        store = _get_store()
    except Exception as e:
        return _component("fail", f"store init failed: {type(e).__name__}", rows_total=0)
    total = 0
    try:
        for table in list(RECORD_TABLES) + list(SNAPSHOT_TABLES):
            total += store.count(table)
    except Exception as e:
        return _component("fail", f"count failed: {type(e).__name__}", rows_total=0)
    return _component(
        "ok", f"cache has {total} rows across record + snapshot tables", rows_total=total
    )


def _check_cache_writable() -> dict[str, Any]:
    try:
        store = _get_store()
        conn = store._connect()
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _health ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL)"
            )
            cur = conn.execute(
                "INSERT INTO _health (ts) VALUES (?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            rowid = cur.lastrowid
            conn.execute("DELETE FROM _health WHERE id = ?", (rowid,))
    except Exception as e:
        return _component("fail", f"{type(e).__name__}: {e}")
    return _component("ok", "sentinel write + delete succeeded")


def _check_schema_version() -> dict[str, Any]:
    try:
        store = _get_store()
        conn = store._connect()
        actual = conn.execute("PRAGMA user_version").fetchone()[0]
    except Exception as e:
        return _component("fail", f"{type(e).__name__}")
    if actual == SCHEMA_VERSION:
        return _component("ok", f"user_version={actual} expected={SCHEMA_VERSION}")
    return _component(
        "warn",
        f"user_version={actual} expected={SCHEMA_VERSION}; migration may be needed",
    )


@mcp.tool()
async def health_check(live: bool = True) -> dict[str, Any]:
    """Run server health checks and return a structured status dict.

    Run before long operations or when diagnosing issues; fast local-only
    mode available via ``live=False`` (skips the WHOOP API round-trip).

    Returns::

        {
          "status": "healthy" | "degraded" | "unhealthy",
          "checks": {
            "auth":           {"status": "ok|warn|fail", "detail": "...", ...},
            "api_reachable":  {"status": "ok|warn|fail|skipped", "detail": "...", ...},
            "cache_readable": {"status": "ok|warn|fail", "detail": "...", "rows_total": N},
            "cache_writable": {"status": "ok|warn|fail", "detail": "..."},
            "schema_version": {"status": "ok|warn|fail", "detail": "user_version=..."}
          },
          "server_version": "...",
          "timestamp": "<utc iso>"
        }

    Never raises. Never emits tokens or PII.
    """
    checks: dict[str, dict[str, Any]] = {}
    try:
        checks["auth"] = await _check_auth()
    except Exception as e:  # pragma: no cover
        checks["auth"] = _component("fail", f"{type(e).__name__}")
    try:
        checks["api_reachable"] = await _check_api(live=live)
    except Exception as e:  # pragma: no cover
        checks["api_reachable"] = _component("fail", f"{type(e).__name__}")
    try:
        checks["cache_readable"] = _check_cache_readable()
    except Exception as e:
        checks["cache_readable"] = _component("fail", f"{type(e).__name__}")
    try:
        checks["cache_writable"] = _check_cache_writable()
    except Exception as e:
        checks["cache_writable"] = _component("fail", f"{type(e).__name__}")
    try:
        checks["schema_version"] = _check_schema_version()
    except Exception as e:
        checks["schema_version"] = _component("fail", f"{type(e).__name__}")

    worst = max(_rank(c["status"]) for c in checks.values())
    if worst == 0:
        overall = "healthy"
    elif worst == 1:
        overall = "degraded"
    else:
        overall = "unhealthy"

    result = {
        "status": overall,
        "checks": checks,
        "server_version": SERVER_VERSION,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    try:
        logger.info(
            "health_check",
            extra={
                "event": "health_check",
                "overall": overall,
                "checks": {k: v["status"] for k, v in checks.items()},
            },
        )
    except Exception:
        pass
    return result


# ---------- Entry point ----------


def main() -> None:
    """Console-script entry point. Registered as ``whoop-mcp`` via pyproject."""
    logger.info("Starting WHOOP MCP server v%s", SERVER_VERSION)
    _get_client()
    _get_store()
    mcp.run()


if __name__ == "__main__":
    main()
