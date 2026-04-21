"""
Tests for the M2 Pydantic v2 response models and flattening.

Rules under test (see M2 spec):
- Drop v1_id, user_id, nested score_state wrappers, created_at/updated_at on
  single-resource responses.
- Unit normalization: ms -> seconds (rounded 1 decimal), kilojoule -> calories
  (rounded int), renames for HR / HRV / SpO2 / skin-temp keys.
- Sleep stage rename to deep/rem/light/awake/in_bed seconds and
  sleep_cycle_count.
- Null handling: score_state != SCORED -> score fields null, score_state kept
  at top level.
"""
from __future__ import annotations

import copy

import pytest

from whoop_models import (
    BodyMeasurement,
    Cycle,
    Profile,
    Recovery,
    Sleep,
    Workout,
)


# ---------- Profile ----------


def test_profile_flatten_drops_user_id(fixture_loader):
    raw = fixture_loader("profile")
    flat = Profile.model_validate(raw).flatten()

    assert "user_id" not in flat
    # Basic identity fields are kept
    assert "email" in flat
    assert "first_name" in flat
    assert "last_name" in flat


# ---------- BodyMeasurement ----------


def test_body_measurement_flatten_renames(fixture_loader):
    raw = fixture_loader("body_measurement")
    flat = BodyMeasurement.model_validate(raw).flatten()

    # Rename max_heart_rate -> max_hr_bpm
    assert "max_hr_bpm" in flat
    assert flat["max_hr_bpm"] == 188
    assert "max_heart_rate" not in flat

    # height/weight pass through with unit-bearing keys
    assert flat["height_meter"] == 1.74
    assert flat["weight_kilogram"] == 60.0


# ---------- Cycle ----------


def test_cycle_flatten_units_and_metadata(fixture_loader):
    raw = fixture_loader("cycle_single")
    flat = Cycle.model_validate(raw).flatten()

    # IDs + timestamps preserved
    assert flat["id"] == 1446265073
    assert flat["start"] == "2026-04-20T23:33:32.030Z"
    assert flat["end"] is None
    assert flat["timezone_offset"] == "+05:30"

    # Scrubbed keys
    for bad in ("user_id", "created_at", "updated_at", "v1_id", "score"):
        assert bad not in flat, f"{bad} should be dropped from cycle flatten"

    # Score lifted up; renames/unit conversions applied
    assert flat["score_state"] == "SCORED"
    assert flat["strain"] == pytest.approx(4.930359)
    assert flat["avg_hr_bpm"] == 67
    assert flat["max_hr_bpm"] == 138
    # 3055.2334 kJ * 0.239006 ≈ 730 kcal, rounded int
    assert flat["calories"] == 730
    assert "kilojoule" not in flat


def test_cycle_flatten_null_score_state(fixture_loader):
    raw = copy.deepcopy(fixture_loader("cycle_single"))
    raw["score_state"] = "PENDING_SCORE"
    raw["score"] = None
    flat = Cycle.model_validate(raw).flatten()

    assert flat["score_state"] == "PENDING_SCORE"
    assert flat["strain"] is None
    assert flat["avg_hr_bpm"] is None
    assert flat["max_hr_bpm"] is None
    assert flat["calories"] is None


# ---------- Recovery ----------


def test_recovery_flatten(fixture_loader):
    raw = fixture_loader("cycle_recovery")
    flat = Recovery.model_validate(raw).flatten()

    # FKs preserved
    assert flat["cycle_id"] == 1446265073
    assert flat["sleep_id"] == "bb68db7b-bb56-44ce-ad8a-eb5a7a93b073"

    # Scrubbed
    for bad in ("user_id", "created_at", "updated_at", "v1_id", "score"):
        assert bad not in flat

    # Unit-clear keys
    assert flat["score_state"] == "SCORED"
    assert flat["recovery_score"] == 57.0
    assert flat["resting_heart_rate_bpm"] == 58.0
    assert flat["hrv_rmssd_ms"] == pytest.approx(45.37756)
    assert flat["spo2_pct"] == pytest.approx(96.333336)
    assert flat["skin_temp_c"] == pytest.approx(34.161)
    assert flat["user_calibrating"] is False


def test_recovery_flatten_pending(fixture_loader):
    raw = copy.deepcopy(fixture_loader("cycle_recovery"))
    raw["score_state"] = "PENDING_SCORE"
    raw["score"] = None
    flat = Recovery.model_validate(raw).flatten()
    assert flat["score_state"] == "PENDING_SCORE"
    assert flat["recovery_score"] is None
    assert flat["hrv_rmssd_ms"] is None
    assert flat["spo2_pct"] is None


# ---------- Sleep ----------


def test_sleep_flatten(fixture_loader):
    raw = fixture_loader("sleep_single")
    flat = Sleep.model_validate(raw).flatten()

    assert flat["id"] == "bb68db7b-bb56-44ce-ad8a-eb5a7a93b073"
    assert flat["cycle_id"] == 1446265073
    assert flat["nap"] is False
    assert flat["start"] == "2026-04-20T23:33:32.030Z"
    assert flat["end"] == "2026-04-21T06:39:42.380Z"

    for bad in ("user_id", "created_at", "updated_at", "v1_id", "score"):
        assert bad not in flat

    # Stage renames + ms->s (deterministic round(ms/1000, 1))
    assert flat["in_bed_seconds"] == 25570.3
    assert flat["light_sleep_seconds"] == 12906.5
    assert flat["rem_sleep_seconds"] == 5642.4
    assert flat["deep_sleep_seconds"] == 5701.4
    assert flat["awake_seconds"] == 1320.1
    assert flat["sleep_cycle_count"] == 5
    assert flat["disturbance_count"] == 9

    # Score percentages kept as-is (with _pct unit-bearing rename)
    assert flat["sleep_efficiency_pct"] == pytest.approx(94.837456)
    assert flat["sleep_performance_pct"] == 83.0
    assert flat["sleep_consistency_pct"] == 78.0
    assert flat["respiratory_rate"] == pytest.approx(13.9453125)
    assert flat["score_state"] == "SCORED"


def test_sleep_flatten_pending(fixture_loader):
    raw = copy.deepcopy(fixture_loader("sleep_single"))
    raw["score_state"] = "PENDING_SCORE"
    raw["score"] = None
    flat = Sleep.model_validate(raw).flatten()
    assert flat["score_state"] == "PENDING_SCORE"
    assert flat["in_bed_seconds"] is None
    assert flat["deep_sleep_seconds"] is None
    assert flat["rem_sleep_seconds"] is None
    assert flat["sleep_cycle_count"] is None


# ---------- Workout ----------


def test_workout_flatten(fixture_loader):
    raw = fixture_loader("workout_single")
    flat = Workout.model_validate(raw).flatten()

    assert flat["id"] == "a3f00067-344d-4d16-811c-b98b71f67b15"
    assert flat["sport_id"] == 63
    assert flat["sport_name"] == "walking"
    assert flat["start"] == "2026-04-19T14:40:30.770Z"
    assert flat["end"] == "2026-04-19T15:11:59.790Z"

    for bad in ("user_id", "created_at", "updated_at", "v1_id", "score"):
        assert bad not in flat

    assert flat["score_state"] == "SCORED"
    assert flat["strain"] == pytest.approx(5.6585774)
    assert flat["avg_hr_bpm"] == 116
    assert flat["max_hr_bpm"] == 145
    # 585.3417 * 0.239006 ≈ 140 kcal
    assert flat["calories"] == 140
    assert "kilojoule" not in flat
    assert flat["distance_meter"] is None
    assert flat["altitude_gain_meter"] is None
    assert flat["altitude_change_meter"] is None
    assert flat["percent_recorded"] == pytest.approx(0.9999894)

    # Zone durations: ms -> seconds (1dp), renamed to snake_case without milli
    zones = flat["zone_durations_seconds"]
    assert zones["zone_one"] == pytest.approx(1541.0, abs=0.2)
    assert zones["zone_two"] == pytest.approx(38.0, abs=0.2)
    assert zones["zone_zero"] == pytest.approx(310.0, abs=0.2)
    assert zones["zone_three"] == 0.0
    assert zones["zone_four"] == 0.0
    assert zones["zone_five"] == 0.0
