"""
Tests for the M3 sync orchestration (``whoop_sync.run_sync``).

We stub the underlying WhoopClient with AsyncMock to avoid any HTTP.
The sync function:
 - reads prior cursors from the store,
 - drives one resource at a time (in parallel via asyncio.gather),
 - upserts flattened records,
 - writes a sync_runs audit row per resource.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from whoop_client import UpstreamError
from whoop_store import WhoopStore
from whoop_sync import run_sync

# ---------- fixtures ----------


@pytest.fixture
def store(tmp_path: Path) -> WhoopStore:
    s = WhoopStore(str(tmp_path / "whoop.db"))
    s.init_schema()
    yield s
    s.close()


def _cycle_record(cid: int, start: str, updated_at: str) -> dict:
    return {
        "id": cid,
        "start": start,
        "end": start.replace("T00:", "T23:"),
        "updated_at": updated_at,
        "timezone_offset": "+00:00",
        "score_state": "SCORED",
        "score": {
            "strain": 5.0,
            "kilojoule": 4000.0,
            "average_heart_rate": 65,
            "max_heart_rate": 130,
        },
    }


def _recovery_record(cid: int, updated_at: str) -> dict:
    return {
        "cycle_id": cid,
        "sleep_id": f"sleep-{cid}",
        "updated_at": updated_at,
        "score_state": "SCORED",
        "score": {
            "recovery_score": 70.0,
            "resting_heart_rate": 60.0,
            "hrv_rmssd_milli": 50.0,
            "spo2_percentage": 97.0,
            "skin_temp_celsius": 34.0,
            "user_calibrating": False,
        },
    }


def _sleep_record(sid: str, cid: int, start: str, updated_at: str, nap: bool = False) -> dict:
    return {
        "id": sid,
        "cycle_id": cid,
        "start": start,
        "end": start.replace("T23:", "T07:"),
        "nap": nap,
        "updated_at": updated_at,
        "score_state": "SCORED",
        "score": {
            "respiratory_rate": 14.0,
            "sleep_consistency_percentage": 80.0,
            "sleep_efficiency_percentage": 95.0,
            "sleep_performance_percentage": 85.0,
            "sleep_needed": {"baseline_milli": 28000000},
            "stage_summary": {
                "total_in_bed_time_milli": 28000000,
                "total_light_sleep_time_milli": 14000000,
                "total_rem_sleep_time_milli": 6000000,
                "total_slow_wave_sleep_time_milli": 6000000,
                "total_awake_time_milli": 1500000,
                "total_no_data_time_milli": 0,
                "sleep_cycle_count": 5,
                "disturbance_count": 8,
            },
        },
    }


def _workout_record(wid: str, start: str, updated_at: str) -> dict:
    return {
        "id": wid,
        "start": start,
        "end": start.replace("T14:", "T15:"),
        "sport_id": 63,
        "sport_name": "walking",
        "updated_at": updated_at,
        "score_state": "SCORED",
        "score": {
            "strain": 5.0,
            "kilojoule": 500.0,
            "average_heart_rate": 115,
            "max_heart_rate": 140,
            "percent_recorded": 1.0,
            "distance_meter": None,
            "altitude_change_meter": None,
            "altitude_gain_meter": None,
            "zone_durations": {
                "zone_zero_milli": 100000,
                "zone_one_milli": 1500000,
                "zone_two_milli": 0,
                "zone_three_milli": 0,
                "zone_four_milli": 0,
                "zone_five_milli": 0,
            },
        },
    }


def _profile_record() -> dict:
    return {"email": "a@b.c", "first_name": "Ash", "last_name": "K"}


def _body_record() -> dict:
    return {"height_meter": 1.74, "weight_kilogram": 60.0, "max_heart_rate": 188}


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    # Defaults: empty list + empty snapshots
    client.list_cycles = AsyncMock(return_value=[])
    client.list_recoveries = AsyncMock(return_value=[])
    client.list_sleeps = AsyncMock(return_value=[])
    client.list_workouts = AsyncMock(return_value=[])
    client.get_profile = AsyncMock(return_value=_profile_record())
    client.get_body_measurement = AsyncMock(return_value=_body_record())
    return client


# ---------- full sync ----------


@pytest.mark.asyncio
async def test_full_sync_populates_cache(store: WhoopStore, mock_client: MagicMock):
    cycles = [
        _cycle_record(
            1001 + i, f"2026-04-{10 + i:02d}T00:00:00Z", f"2026-04-{11 + i:02d}T06:00:00Z"
        )
        for i in range(3)
    ]
    mock_client.list_cycles.return_value = cycles
    mock_client.list_recoveries.return_value = [
        _recovery_record(1001 + i, f"2026-04-{11 + i:02d}T06:00:00Z") for i in range(3)
    ]

    result = await run_sync(store=store, client=mock_client, full=True)

    assert result["status"] == "success"
    assert result["resources"]["cycles"]["records_upserted"] == 3
    assert result["resources"]["recoveries"]["records_upserted"] == 3
    assert result["resources"]["profile"]["records_upserted"] == 1
    assert result["resources"]["body_measurement"]["records_upserted"] == 1
    # Cursor after == max updated_at from fetched records
    assert result["resources"]["cycles"]["cursor_after"] == "2026-04-13T06:00:00Z"


# ---------- incremental / idempotent ----------


@pytest.mark.asyncio
async def test_incremental_second_run_no_new_records_is_idempotent(
    store: WhoopStore, mock_client: MagicMock
):
    cycles = [_cycle_record(1, "2026-04-10T00:00:00Z", "2026-04-11T06:00:00Z")]
    mock_client.list_cycles.return_value = cycles

    r1 = await run_sync(store=store, client=mock_client, full=True)
    assert r1["resources"]["cycles"]["records_upserted"] == 1

    # Second run returns same records -> 0 upserts
    r2 = await run_sync(store=store, client=mock_client)
    assert r2["resources"]["cycles"]["records_upserted"] == 0
    assert r2["resources"]["cycles"]["cursor_after"] == r1["resources"]["cycles"]["cursor_after"]


@pytest.mark.asyncio
async def test_incremental_with_new_record_appends_only_new(
    store: WhoopStore, mock_client: MagicMock
):
    # Initial: one record
    mock_client.list_cycles.return_value = [
        _cycle_record(1, "2026-04-10T00:00:00Z", "2026-04-11T06:00:00Z")
    ]
    await run_sync(store=store, client=mock_client, full=True)

    # Next sync: API returns the old + a new record
    mock_client.list_cycles.return_value = [
        _cycle_record(1, "2026-04-10T00:00:00Z", "2026-04-11T06:00:00Z"),
        _cycle_record(2, "2026-04-12T00:00:00Z", "2026-04-13T06:00:00Z"),
    ]
    r = await run_sync(store=store, client=mock_client)
    assert r["resources"]["cycles"]["records_upserted"] == 1


# ---------- partial failure ----------


@pytest.mark.asyncio
async def test_one_resource_fails_other_resources_succeed(
    store: WhoopStore, mock_client: MagicMock
):
    mock_client.list_cycles.return_value = [
        _cycle_record(1, "2026-04-10T00:00:00Z", "2026-04-11T06:00:00Z")
    ]
    mock_client.list_recoveries.side_effect = UpstreamError(
        "UPSTREAM_ERROR", 503, "exhausted retries (3)", "/recovery"
    )

    r = await run_sync(store=store, client=mock_client, full=True)
    assert r["status"] == "partial"
    assert r["resources"]["cycles"]["status"] == "success"
    assert r["resources"]["recoveries"]["status"] == "error"
    assert "warnings" in r
    assert any("recoveries" in w for w in r["warnings"])


@pytest.mark.asyncio
async def test_all_resources_fail_status_is_error(store: WhoopStore, mock_client: MagicMock):
    err = UpstreamError("UPSTREAM_ERROR", 503, "down", "/x")
    mock_client.list_cycles.side_effect = err
    mock_client.list_recoveries.side_effect = err
    mock_client.list_sleeps.side_effect = err
    mock_client.list_workouts.side_effect = err
    mock_client.get_profile.side_effect = err
    mock_client.get_body_measurement.side_effect = err

    r = await run_sync(store=store, client=mock_client, full=True)
    assert r["status"] == "error"


# ---------- selective options ----------


@pytest.mark.asyncio
async def test_since_override_used_as_window_start(store: WhoopStore, mock_client: MagicMock):
    await run_sync(
        store=store,
        client=mock_client,
        since="2026-04-01T00:00:00Z",
        resources=["cycles"],
    )
    # list_cycles should have been called with start=<since>
    call = mock_client.list_cycles.call_args
    assert call.kwargs.get("start") == "2026-04-01T00:00:00Z"


@pytest.mark.asyncio
async def test_resources_filter_only_syncs_selected(store: WhoopStore, mock_client: MagicMock):
    r = await run_sync(store=store, client=mock_client, resources=["cycles"])
    assert set(r["resources"].keys()) == {"cycles"}
    mock_client.list_recoveries.assert_not_called()


@pytest.mark.asyncio
async def test_sync_runs_audit_row_written(store: WhoopStore, mock_client: MagicMock):
    mock_client.list_cycles.return_value = [
        _cycle_record(1, "2026-04-10T00:00:00Z", "2026-04-11T06:00:00Z")
    ]
    await run_sync(store=store, client=mock_client, resources=["cycles"])
    rows = store.list_sync_runs(limit=5)
    assert len(rows) == 1
    assert rows[0]["resource"] == "cycles"
    assert rows[0]["status"] == "success"
