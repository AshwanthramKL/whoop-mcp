"""
Incremental sync orchestration — M3.

``run_sync`` walks the configured WHOOP resources (cycles, recoveries,
sleeps, workouts, body_measurement, profile), pulls the records that are
new since the last successful cursor, flattens them through the
Pydantic models in :mod:`whoop_models`, and upserts into
:class:`whoop_store.WhoopStore`. One audit row is written to
``sync_runs`` per resource.

Design notes
------------
- Resources are fetched in parallel via ``asyncio.gather`` with
  ``return_exceptions=True``; a failure on one resource is isolated and
  reported in ``warnings`` without aborting the others.
- Cursor strategy: the per-resource cursor is ``MAX(updated_at)`` across
  the stored rows. On a fresh DB we fall back to "90 days ago". An
  explicit ``since=`` argument overrides both. ``full=True`` overrides
  to the earliest date WHOOP accepts ("2010-01-01").
- No network knowledge leaks below the ``client`` parameter — tests pass
  a mock; production passes a real ``WhoopClient``.
- Logs are stderr-only, structured JSON, and never include the raw
  payload bodies (only counts / status / duration).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from whoop_client import WhoopAPIError, WhoopClient
from whoop_models import BodyMeasurement, Cycle, Profile, Recovery, Sleep, Workout, _Base
from whoop_store import WhoopStore

__all__ = ["DEFAULT_RESOURCES", "EARLIEST_DATE", "run_sync"]

DEFAULT_RESOURCES = (
    "cycles",
    "recoveries",
    "sleeps",
    "workouts",
    "body_measurement",
    "profile",
)

# Anything earlier than this is before WHOOP's product launch; safe as an
# "epoch" for full syncs.
EARLIEST_DATE = "2010-01-01T00:00:00Z"

logger = logging.getLogger("whoop_sync")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


def _log(event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    try:
        logger.info(json.dumps(payload, default=str))
    except Exception:  # pragma: no cover
        pass


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _default_since() -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=90)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# Map each "list" resource name to the client method + model class + store table.
_LIST_RESOURCES: dict[str, tuple[str, type[_Base], str]] = {
    "cycles": ("list_cycles", Cycle, "cycles"),
    "recoveries": ("list_recoveries", Recovery, "recoveries"),
    "sleeps": ("list_sleeps", Sleep, "sleeps"),
    "workouts": ("list_workouts", Workout, "workouts"),
}

_SNAPSHOT_RESOURCES: dict[str, tuple[str, type[_Base], str]] = {
    "profile": ("get_profile", Profile, "profile_snapshots"),
    "body_measurement": ("get_body_measurement", BodyMeasurement, "body_measurements"),
}


def _resolve_since(
    store: WhoopStore,
    resource: str,
    *,
    full: bool,
    override_since: str | None,
) -> str | None:
    """Pick the ``start`` query param for this resource's list call."""
    if full:
        return EARLIEST_DATE
    if override_since:
        return override_since
    # Incremental: use the stored max updated_at as the cursor if we have one.
    # Snapshot resources don't use a since cursor.
    if resource in _LIST_RESOURCES:
        table = _LIST_RESOURCES[resource][2]
        cursor = store.get_max_updated_at(table)
        if cursor:
            return cursor
        return _default_since()
    return None


async def _sync_list_resource(
    resource: str,
    *,
    store: WhoopStore,
    client: WhoopClient,
    since: str | None,
) -> dict[str, Any]:
    """Fetch + upsert one list resource. Always returns a dict describing the outcome."""
    t0 = time.monotonic()
    method_name, model_cls, table = _LIST_RESOURCES[resource]
    run_id = store.start_sync_run(resource, since_cursor=since)

    try:
        raw_records = await getattr(client, method_name)(start=since)
        pairs: list = []
        for raw in raw_records:
            try:
                flat = model_cls.model_validate(raw).flatten()
            except Exception as e:
                # Skip malformed records but keep syncing.
                _log("sync_validate_skip", resource=resource, error=str(e))
                continue
            pairs.append((raw, flat))

        upserted = store.upsert_records(table, pairs)
        cursor_after = store.get_max_updated_at(table)
        store.finish_sync_run(
            run_id,
            status="success",
            records_fetched=len(raw_records),
            records_upserted=upserted,
            until_cursor=cursor_after,
        )
        dur_ms = int((time.monotonic() - t0) * 1000)
        _log(
            "sync_resource_done",
            resource=resource,
            records_fetched=len(raw_records),
            records_upserted=upserted,
            cursor_after=cursor_after,
            duration_ms=dur_ms,
            status="success",
        )
        return {
            "records_fetched": len(raw_records),
            "records_upserted": upserted,
            "cursor_after": cursor_after,
            "status": "success",
        }
    except WhoopAPIError as e:
        dur_ms = int((time.monotonic() - t0) * 1000)
        store.finish_sync_run(
            run_id,
            status="error",
            error_message=f"{e.code}: {e.message}",
        )
        _log(
            "sync_resource_done",
            resource=resource,
            status="error",
            error_code=e.code,
            duration_ms=dur_ms,
        )
        return {
            "records_fetched": 0,
            "records_upserted": 0,
            "cursor_after": None,
            "status": "error",
            "error": f"{e.code}: {e.message}",
        }
    except Exception as e:  # defensive — never let a sync worker raise
        dur_ms = int((time.monotonic() - t0) * 1000)
        store.finish_sync_run(run_id, status="error", error_message=str(e))
        _log(
            "sync_resource_done",
            resource=resource,
            status="error",
            error=str(e),
            duration_ms=dur_ms,
        )
        return {
            "records_fetched": 0,
            "records_upserted": 0,
            "cursor_after": None,
            "status": "error",
            "error": str(e),
        }


async def _sync_snapshot_resource(
    resource: str,
    *,
    store: WhoopStore,
    client: WhoopClient,
) -> dict[str, Any]:
    t0 = time.monotonic()
    method_name, model_cls, table = _SNAPSHOT_RESOURCES[resource]
    run_id = store.start_sync_run(resource)
    try:
        raw = await getattr(client, method_name)()
        flat = model_cls.model_validate(raw).flatten()
        changed = store.upsert_snapshot(table, raw, flat)
        store.finish_sync_run(run_id, status="success", records_fetched=1, records_upserted=changed)
        dur_ms = int((time.monotonic() - t0) * 1000)
        _log(
            "sync_resource_done",
            resource=resource,
            records_fetched=1,
            records_upserted=changed,
            duration_ms=dur_ms,
            status="success",
        )
        return {
            "records_fetched": 1,
            "records_upserted": changed,
            "cursor_after": None,
            "status": "success",
        }
    except WhoopAPIError as e:
        dur_ms = int((time.monotonic() - t0) * 1000)
        store.finish_sync_run(run_id, status="error", error_message=f"{e.code}: {e.message}")
        _log(
            "sync_resource_done",
            resource=resource,
            status="error",
            error_code=e.code,
            duration_ms=dur_ms,
        )
        return {
            "records_fetched": 0,
            "records_upserted": 0,
            "cursor_after": None,
            "status": "error",
            "error": f"{e.code}: {e.message}",
        }
    except Exception as e:  # defensive
        dur_ms = int((time.monotonic() - t0) * 1000)
        store.finish_sync_run(run_id, status="error", error_message=str(e))
        _log(
            "sync_resource_done",
            resource=resource,
            status="error",
            error=str(e),
            duration_ms=dur_ms,
        )
        return {
            "records_fetched": 0,
            "records_upserted": 0,
            "cursor_after": None,
            "status": "error",
            "error": str(e),
        }


async def run_sync(
    *,
    store: WhoopStore,
    client: WhoopClient,
    full: bool = False,
    since: str | None = None,
    resources: list[str] | None = None,
) -> dict[str, Any]:
    """Run a sync pass, populate the store, and return a per-resource summary.

    Args:
        store: the :class:`WhoopStore` to write to.
        client: a client that exposes the same async methods as
            :class:`whoop_client.WhoopClient`.
        full: if True, pull from the earliest allowed date for every
            selected resource. Overrides ``since`` and stored cursors.
        since: ISO-8601 string overriding the stored cursor for list
            resources. Ignored for snapshot resources.
        resources: explicit list of resource names. ``None`` means
            :data:`DEFAULT_RESOURCES`.
    """
    store.init_schema()
    targets = list(resources) if resources is not None else list(DEFAULT_RESOURCES)
    unknown = [r for r in targets if r not in _LIST_RESOURCES and r not in _SNAPSHOT_RESOURCES]
    if unknown:
        return {
            "status": "error",
            "started_at": _utcnow_iso(),
            "completed_at": _utcnow_iso(),
            "resources": {},
            "warnings": [f"unknown resource: {name}" for name in unknown],
        }

    started_at = _utcnow_iso()

    async def _one(name: str):
        if name in _LIST_RESOURCES:
            since_for = _resolve_since(store, name, full=full, override_since=since)
            return name, await _sync_list_resource(
                name, store=store, client=client, since=since_for
            )
        return name, await _sync_snapshot_resource(name, store=store, client=client)

    results = await asyncio.gather(*[_one(n) for n in targets])

    per_resource: dict[str, Any] = {}
    warnings: list[str] = []
    ok = 0
    for name, res in results:
        per_resource[name] = res
        if res["status"] == "success":
            ok += 1
        else:
            err_msg = res.get("error") or "unknown"
            warnings.append(f"{name}: {err_msg}")

    if ok == len(targets):
        status = "success"
    elif ok == 0:
        status = "error"
    else:
        status = "partial"

    # Backfill NULL start/end on recoveries from their parent cycle. Runs every sync
    # (idempotent) so records inserted before the parent cycle existed get patched
    # on the next pass.
    try:
        backfilled = store.backfill_recovery_windows()
        if backfilled:
            _log("recovery_windows_backfilled", rows_updated=backfilled)
    except Exception as exc:
        logger.warning("backfill_recovery_windows failed: %s", exc)

    completed_at = _utcnow_iso()
    _log(
        "sync_complete",
        status=status,
        resources_ok=ok,
        resources_total=len(targets),
        started_at=started_at,
        completed_at=completed_at,
    )

    response: dict[str, Any] = {
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "resources": per_resource,
    }
    if warnings:
        response["warnings"] = warnings
    return response
