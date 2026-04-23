"""
Tests for the M2 get_whoop_daily_summary MCP tool.

Orchestration logic:
- Fetch cycles in [date, date+1d) window via list_cycles.
- Pick the cycle (if any) -> fetch recovery via get_cycle_recovery.
- Fetch sleeps in window via list_sleeps, filter by that cycle_id, drop naps,
  pick the longest remaining as "primary sleep".
- Fetch workouts in window via list_workouts, filter by start on the given day
  (UTC).
- Populate score_states map and warnings list.
- Partial failure -> null field + warning entry.
- All-fail -> error envelope.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import whoop_mcp_server as server
from whoop_client import UpstreamError

# ---------- shared stub ----------


@pytest.fixture
def stub_client(monkeypatch):
    fake = MagicMock()
    for name in [
        "get_profile",
        "get_body_measurement",
        "list_cycles",
        "get_cycle",
        "get_cycle_sleep",
        "get_cycle_recovery",
        "list_recoveries",
        "list_sleeps",
        "get_sleep",
        "list_workouts",
        "get_workout",
    ]:
        setattr(fake, name, AsyncMock())
    fake.get_auth_status = MagicMock(return_value={"status": "valid"})
    monkeypatch.setattr(server, "_get_client", lambda: fake)
    return fake


# ---------- fixtures / helpers ----------


def _cycle(id_: int, start: str, end: str | None, score_state: str = "SCORED") -> dict:
    return {
        "id": id_,
        "user_id": "<REDACTED>",
        "start": start,
        "end": end,
        "timezone_offset": "+00:00",
        "score_state": score_state,
        "score": {
            "strain": 10.0,
            "kilojoule": 4184.0,  # ~1000 kcal
            "average_heart_rate": 70,
            "max_heart_rate": 140,
        }
        if score_state == "SCORED"
        else None,
        "created_at": "2026-04-20T10:00:00.000Z",
        "updated_at": "2026-04-20T10:00:00.000Z",
    }


def _recovery(cycle_id: int, sleep_id: str, score_state: str = "SCORED") -> dict:
    return {
        "cycle_id": cycle_id,
        "sleep_id": sleep_id,
        "user_id": "<REDACTED>",
        "score_state": score_state,
        "score": {
            "recovery_score": 75.0,
            "resting_heart_rate": 60.0,
            "hrv_rmssd_milli": 50.0,
            "spo2_percentage": 97.0,
            "skin_temp_celsius": 34.0,
            "user_calibrating": False,
        }
        if score_state == "SCORED"
        else None,
        "created_at": "2026-04-21T05:00:00.000Z",
        "updated_at": "2026-04-21T05:00:00.000Z",
    }


def _sleep(
    id_: str,
    cycle_id: int,
    start: str,
    end: str,
    nap: bool,
    in_bed_milli: int,
) -> dict:
    return {
        "id": id_,
        "cycle_id": cycle_id,
        "user_id": "<REDACTED>",
        "v1_id": None,
        "start": start,
        "end": end,
        "timezone_offset": "+00:00",
        "nap": nap,
        "score_state": "SCORED",
        "score": {
            "respiratory_rate": 14.0,
            "sleep_consistency_percentage": 80.0,
            "sleep_efficiency_percentage": 90.0,
            "sleep_performance_percentage": 85.0,
            "sleep_needed": {
                "baseline_milli": 28000000,
                "need_from_recent_nap_milli": 0,
                "need_from_recent_strain_milli": 0,
                "need_from_sleep_debt_milli": 0,
            },
            "stage_summary": {
                "disturbance_count": 3,
                "sleep_cycle_count": 4,
                "total_awake_time_milli": 1000000,
                "total_in_bed_time_milli": in_bed_milli,
                "total_light_sleep_time_milli": in_bed_milli // 2,
                "total_no_data_time_milli": 0,
                "total_rem_sleep_time_milli": in_bed_milli // 4,
                "total_slow_wave_sleep_time_milli": in_bed_milli // 4,
            },
        },
        "created_at": "2026-04-21T06:00:00.000Z",
        "updated_at": "2026-04-21T06:00:00.000Z",
    }


def _workout(id_: str, start: str, end: str) -> dict:
    return {
        "id": id_,
        "user_id": "<REDACTED>",
        "v1_id": None,
        "start": start,
        "end": end,
        "timezone_offset": "+00:00",
        "sport_id": 63,
        "sport_name": "walking",
        "score_state": "SCORED",
        "score": {
            "strain": 5.0,
            "kilojoule": 500.0,
            "average_heart_rate": 110,
            "max_heart_rate": 140,
            "percent_recorded": 1.0,
            "distance_meter": None,
            "altitude_change_meter": None,
            "altitude_gain_meter": None,
            "zone_durations": {
                "zone_zero_milli": 0,
                "zone_one_milli": 0,
                "zone_two_milli": 0,
                "zone_three_milli": 0,
                "zone_four_milli": 0,
                "zone_five_milli": 0,
            },
        },
        "created_at": "2026-04-20T12:00:00.000Z",
        "updated_at": "2026-04-20T12:00:00.000Z",
    }


# ---------- happy path ----------


@pytest.mark.asyncio
async def test_daily_summary_happy(stub_client):
    cycle = _cycle(100, "2026-04-20T00:10:00.000Z", "2026-04-21T00:05:00.000Z")
    rec = _recovery(100, "sleep-primary")
    primary = _sleep(
        "sleep-primary",
        100,
        "2026-04-20T22:00:00.000Z",
        "2026-04-21T06:00:00.000Z",
        nap=False,
        in_bed_milli=28000000,
    )
    nap = _sleep(
        "sleep-nap",
        100,
        "2026-04-20T13:00:00.000Z",
        "2026-04-20T13:30:00.000Z",
        nap=True,
        in_bed_milli=1800000,
    )
    short_sleep = _sleep(
        "sleep-short",
        100,
        "2026-04-20T01:00:00.000Z",
        "2026-04-20T02:00:00.000Z",
        nap=False,
        in_bed_milli=3600000,
    )
    wout = _workout("wout-1", "2026-04-20T14:00:00.000Z", "2026-04-20T14:30:00.000Z")
    off_day_wout = _workout("wout-off", "2026-04-19T14:00:00.000Z", "2026-04-19T14:30:00.000Z")

    stub_client.list_cycles.return_value = [cycle]
    stub_client.get_cycle_recovery.return_value = rec
    stub_client.list_sleeps.return_value = [primary, nap, short_sleep]
    stub_client.list_workouts.return_value = [wout, off_day_wout]

    out = await server.get_whoop_daily_summary("2026-04-20")

    assert out["date"] == "2026-04-20"
    assert out["cycle"]["id"] == 100
    # Cycle is flattened (no user_id, score_state top-level)
    assert "user_id" not in out["cycle"]
    assert out["cycle"]["score_state"] == "SCORED"

    assert out["recovery"]["cycle_id"] == 100
    assert out["recovery"]["recovery_score"] == 75.0

    # Primary sleep is the longest non-nap
    assert out["sleep"]["id"] == "sleep-primary"
    assert out["sleep"]["nap"] is False
    # Timestamps are split into utc+local so callers don't misread Z as local.
    # Primary sleep's raw start is 22:00:00Z with tz_offset=+00:00, so local
    # equals utc just carried with the explicit offset.
    assert out["sleep"]["start_utc"] == "2026-04-20T22:00:00.000Z"
    assert out["sleep"]["start_local"] == "2026-04-20T22:00:00+00:00"
    assert out["cycle"]["start_utc"].endswith("Z")
    assert out["cycle"]["start_local"].endswith("+00:00")

    # Workouts: off-day filtered out
    assert len(out["workouts"]) == 1
    assert out["workouts"][0]["id"] == "wout-1"

    assert out["score_states"]["cycle"] == "SCORED"
    assert out["score_states"]["recovery"] == "SCORED"
    assert out["score_states"]["sleep"] == "SCORED"

    # No warnings on happy path
    assert out.get("warnings", []) == []


# ---------- no data for the date ----------


@pytest.mark.asyncio
async def test_daily_summary_no_cycle(stub_client):
    stub_client.list_cycles.return_value = []
    stub_client.list_sleeps.return_value = []
    stub_client.list_workouts.return_value = []

    out = await server.get_whoop_daily_summary("2099-01-01")

    assert out["date"] == "2099-01-01"
    assert out["cycle"] is None
    assert out["recovery"] is None
    assert out["sleep"] is None
    assert out["workouts"] == []
    # score_states has null entries (no data to report state for)
    assert out["score_states"]["cycle"] is None
    assert out["score_states"]["recovery"] is None
    assert out["score_states"]["sleep"] is None
    assert out.get("warnings", []) == []
    # Recovery fetch should not be attempted with no cycle
    stub_client.get_cycle_recovery.assert_not_called()


# ---------- partial failure ----------


@pytest.mark.asyncio
async def test_daily_summary_recovery_fails(stub_client):
    cycle = _cycle(200, "2026-04-20T00:10:00.000Z", "2026-04-21T00:05:00.000Z")
    primary = _sleep(
        "s1",
        200,
        "2026-04-20T22:00:00.000Z",
        "2026-04-21T06:00:00.000Z",
        nap=False,
        in_bed_milli=28000000,
    )

    stub_client.list_cycles.return_value = [cycle]
    stub_client.get_cycle_recovery.side_effect = UpstreamError(
        "UPSTREAM_ERROR", 500, "down", "/cycle/200/recovery"
    )
    stub_client.list_sleeps.return_value = [primary]
    stub_client.list_workouts.return_value = []

    out = await server.get_whoop_daily_summary("2026-04-20")

    assert out["cycle"]["id"] == 200
    assert out["recovery"] is None
    assert out["sleep"]["id"] == "s1"
    assert out["workouts"] == []

    warnings = out.get("warnings", [])
    assert any("recovery" in w.lower() for w in warnings)


# ---------- all-fail => error envelope ----------


@pytest.mark.asyncio
async def test_daily_summary_all_fail_returns_error_envelope(stub_client):
    err = UpstreamError("UPSTREAM_ERROR", 503, "boom", "/cycle")
    stub_client.list_cycles.side_effect = err
    stub_client.list_sleeps.side_effect = err
    stub_client.list_workouts.side_effect = err

    out = await server.get_whoop_daily_summary("2026-04-20")

    assert "error" in out
    assert out["error"]["code"] == "UPSTREAM_ERROR"


# ---------- sleep filter: cycle-id match only ----------


@pytest.mark.asyncio
async def test_daily_summary_ignores_sleep_from_other_cycle(stub_client):
    cycle = _cycle(300, "2026-04-20T00:10:00.000Z", "2026-04-21T00:05:00.000Z")
    other_cycle_sleep = _sleep(
        "other",
        999,
        "2026-04-20T22:00:00.000Z",
        "2026-04-21T06:00:00.000Z",
        nap=False,
        in_bed_milli=30000000,
    )
    own_sleep = _sleep(
        "own",
        300,
        "2026-04-20T22:00:00.000Z",
        "2026-04-21T06:00:00.000Z",
        nap=False,
        in_bed_milli=28000000,
    )

    stub_client.list_cycles.return_value = [cycle]
    stub_client.get_cycle_recovery.return_value = _recovery(300, "own")
    stub_client.list_sleeps.return_value = [other_cycle_sleep, own_sleep]
    stub_client.list_workouts.return_value = []

    out = await server.get_whoop_daily_summary("2026-04-20")

    assert out["sleep"]["id"] == "own"


# ---------- date validation ----------


@pytest.mark.asyncio
async def test_daily_summary_bad_date_returns_validation_error(stub_client):
    out = await server.get_whoop_daily_summary("not-a-date")
    assert "error" in out
    assert out["error"]["code"] == "VALIDATION_ERROR"


# ---------- tool is registered ----------


def test_daily_summary_tool_registered():
    assert "get_whoop_daily_summary" in server.mcp._tool_manager._tools
