"""
Pydantic v2 response models for the WHOOP v2 API — M2.

Each model parses a raw WHOOP v2 record and exposes a ``.flatten()`` method
that returns a flat, LLM-friendly dict with unit-clear keys. See the M2
spec for the full flattening rules.

Quick rules summary:
- Strip ``v1_id``, ``user_id``, ``created_at``, ``updated_at`` on single
  resources. Lift the nested ``score`` dict into top-level keys.
- Durations: WHOOP returns milliseconds. Emit as seconds (float, 1 decimal).
- Energy: ``kilojoule`` -> ``calories`` (kcal, rounded integer).
- Heart rate renames: ``average_heart_rate`` -> ``avg_hr_bpm``,
  ``max_heart_rate`` -> ``max_hr_bpm``.
- HRV: ``hrv_rmssd_milli`` -> ``hrv_rmssd_ms`` (still milliseconds; HRV is
  conventionally reported in ms, just a clearer key).
- SpO2/skin temp: ``spo2_percentage`` -> ``spo2_pct``,
  ``skin_temp_celsius`` -> ``skin_temp_c``.
- Sleep stage renames to deep/rem/light/awake/in_bed seconds.
- ``score_state`` always lifted to top level. If not SCORED, score fields
  are ``None``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Profile",
    "BodyMeasurement",
    "Cycle",
    "Recovery",
    "Sleep",
    "Workout",
]


KJ_TO_KCAL = 0.239006


def _ms_to_s(value: Optional[int]) -> Optional[float]:
    """Convert milliseconds to seconds (1 decimal). Returns None for None."""
    if value is None:
        return None
    return round(value / 1000.0, 1)


def _kj_to_cal(kj: Optional[float]) -> Optional[int]:
    """Convert kilojoules to kcal, rounded to an integer."""
    if kj is None:
        return None
    return int(round(kj * KJ_TO_KCAL))


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


# ---------- Profile ----------


class Profile(_Base):
    """WHOOP user profile (identity only)."""

    email: Optional[str] = Field(default=None, description="User's email address.")
    first_name: Optional[str] = Field(default=None, description="User's first name.")
    last_name: Optional[str] = Field(default=None, description="User's last name.")

    def flatten(self) -> Dict[str, Any]:
        return {
            "email": self.email,
            "first_name": self.first_name,
            "last_name": self.last_name,
        }


# ---------- Body Measurement ----------


class BodyMeasurement(_Base):
    """Latest body measurements."""

    height_meter: Optional[float] = Field(default=None, description="Height in meters.")
    weight_kilogram: Optional[float] = Field(default=None, description="Weight in kg.")
    max_heart_rate: Optional[int] = Field(
        default=None, description="Measured max heart rate in bpm."
    )

    def flatten(self) -> Dict[str, Any]:
        return {
            "height_meter": self.height_meter,
            "weight_kilogram": self.weight_kilogram,
            "max_hr_bpm": self.max_heart_rate,
        }


# ---------- Cycle ----------


class _CycleScore(_Base):
    strain: Optional[float] = None
    kilojoule: Optional[float] = None
    average_heart_rate: Optional[int] = None
    max_heart_rate: Optional[int] = None


class Cycle(_Base):
    """One WHOOP physiological cycle (roughly a 'day' per WHOOP's definition)."""

    id: int = Field(description="WHOOP cycle ID.")
    start: Optional[str] = Field(default=None, description="ISO-8601 cycle start.")
    end: Optional[str] = Field(
        default=None, description="ISO-8601 cycle end (null for the ongoing cycle)."
    )
    timezone_offset: Optional[str] = Field(
        default=None, description="Timezone offset like '+05:30'."
    )
    score_state: Optional[str] = Field(
        default=None, description="SCORED / PENDING_SCORE / UNSCORABLE."
    )
    score: Optional[_CycleScore] = None

    def flatten(self) -> Dict[str, Any]:
        score = self.score
        return {
            "id": self.id,
            "start": self.start,
            "end": self.end,
            "timezone_offset": self.timezone_offset,
            "score_state": self.score_state,
            "strain": score.strain if score else None,
            "avg_hr_bpm": score.average_heart_rate if score else None,
            "max_hr_bpm": score.max_heart_rate if score else None,
            "calories": _kj_to_cal(score.kilojoule) if score else None,
        }


# ---------- Recovery ----------


class _RecoveryScore(_Base):
    recovery_score: Optional[float] = None
    resting_heart_rate: Optional[float] = None
    hrv_rmssd_milli: Optional[float] = None
    spo2_percentage: Optional[float] = None
    skin_temp_celsius: Optional[float] = None
    user_calibrating: Optional[bool] = None


class Recovery(_Base):
    """Morning recovery record attached to a cycle + sleep."""

    cycle_id: Optional[int] = Field(default=None, description="Parent cycle ID.")
    sleep_id: Optional[str] = Field(default=None, description="Source sleep UUID.")
    score_state: Optional[str] = Field(default=None, description="Score state.")
    score: Optional[_RecoveryScore] = None

    def flatten(self) -> Dict[str, Any]:
        s = self.score
        return {
            "cycle_id": self.cycle_id,
            "sleep_id": self.sleep_id,
            "score_state": self.score_state,
            "recovery_score": s.recovery_score if s else None,
            "resting_heart_rate_bpm": s.resting_heart_rate if s else None,
            "hrv_rmssd_ms": s.hrv_rmssd_milli if s else None,
            "spo2_pct": s.spo2_percentage if s else None,
            "skin_temp_c": s.skin_temp_celsius if s else None,
            "user_calibrating": s.user_calibrating if s else None,
        }


# ---------- Sleep ----------


class _SleepStageSummary(_Base):
    disturbance_count: Optional[int] = None
    sleep_cycle_count: Optional[int] = None
    total_awake_time_milli: Optional[int] = None
    total_in_bed_time_milli: Optional[int] = None
    total_light_sleep_time_milli: Optional[int] = None
    total_no_data_time_milli: Optional[int] = None
    total_rem_sleep_time_milli: Optional[int] = None
    total_slow_wave_sleep_time_milli: Optional[int] = None


class _SleepNeeded(_Base):
    baseline_milli: Optional[int] = None
    need_from_recent_nap_milli: Optional[int] = None
    need_from_recent_strain_milli: Optional[int] = None
    need_from_sleep_debt_milli: Optional[int] = None


class _SleepScore(_Base):
    respiratory_rate: Optional[float] = None
    sleep_consistency_percentage: Optional[float] = None
    sleep_efficiency_percentage: Optional[float] = None
    sleep_performance_percentage: Optional[float] = None
    sleep_needed: Optional[_SleepNeeded] = None
    stage_summary: Optional[_SleepStageSummary] = None


class Sleep(_Base):
    """One sleep activity (main sleep or nap)."""

    id: str = Field(description="Sleep UUID.")
    cycle_id: Optional[int] = Field(default=None, description="Parent cycle ID.")
    start: Optional[str] = None
    end: Optional[str] = None
    timezone_offset: Optional[str] = None
    nap: Optional[bool] = Field(default=None, description="True if this is a nap.")
    score_state: Optional[str] = None
    score: Optional[_SleepScore] = None

    def flatten(self) -> Dict[str, Any]:
        s = self.score
        stage = s.stage_summary if s else None
        needed = s.sleep_needed if s else None

        sleep_needed_s = None
        if needed is not None:
            sleep_needed_s = {
                "baseline_seconds": _ms_to_s(needed.baseline_milli),
                "need_from_recent_nap_seconds": _ms_to_s(needed.need_from_recent_nap_milli),
                "need_from_recent_strain_seconds": _ms_to_s(needed.need_from_recent_strain_milli),
                "need_from_sleep_debt_seconds": _ms_to_s(needed.need_from_sleep_debt_milli),
            }

        return {
            "id": self.id,
            "cycle_id": self.cycle_id,
            "start": self.start,
            "end": self.end,
            "timezone_offset": self.timezone_offset,
            "nap": self.nap,
            "score_state": self.score_state,
            "respiratory_rate": s.respiratory_rate if s else None,
            "sleep_consistency_percentage": s.sleep_consistency_percentage if s else None,
            "sleep_efficiency_percentage": s.sleep_efficiency_percentage if s else None,
            "sleep_performance_percentage": s.sleep_performance_percentage if s else None,
            "in_bed_seconds": _ms_to_s(stage.total_in_bed_time_milli) if stage else None,
            "light_sleep_seconds": _ms_to_s(stage.total_light_sleep_time_milli) if stage else None,
            "rem_sleep_seconds": _ms_to_s(stage.total_rem_sleep_time_milli) if stage else None,
            "deep_sleep_seconds": _ms_to_s(stage.total_slow_wave_sleep_time_milli) if stage else None,
            "awake_seconds": _ms_to_s(stage.total_awake_time_milli) if stage else None,
            "no_data_seconds": _ms_to_s(stage.total_no_data_time_milli) if stage else None,
            "sleep_cycle_count": stage.sleep_cycle_count if stage else None,
            "disturbance_count": stage.disturbance_count if stage else None,
            "sleep_needed_seconds": sleep_needed_s,
        }


# ---------- Workout ----------


class _ZoneDurations(_Base):
    zone_zero_milli: Optional[int] = None
    zone_one_milli: Optional[int] = None
    zone_two_milli: Optional[int] = None
    zone_three_milli: Optional[int] = None
    zone_four_milli: Optional[int] = None
    zone_five_milli: Optional[int] = None


class _WorkoutScore(_Base):
    strain: Optional[float] = None
    kilojoule: Optional[float] = None
    average_heart_rate: Optional[int] = None
    max_heart_rate: Optional[int] = None
    percent_recorded: Optional[float] = None
    distance_meter: Optional[float] = None
    altitude_change_meter: Optional[float] = None
    altitude_gain_meter: Optional[float] = None
    zone_durations: Optional[_ZoneDurations] = None


class Workout(_Base):
    """One recorded workout."""

    id: str = Field(description="Workout UUID.")
    start: Optional[str] = None
    end: Optional[str] = None
    timezone_offset: Optional[str] = None
    sport_id: Optional[int] = None
    sport_name: Optional[str] = None
    score_state: Optional[str] = None
    score: Optional[_WorkoutScore] = None

    def flatten(self) -> Dict[str, Any]:
        s = self.score
        zones = s.zone_durations if s else None

        zone_durations_seconds: Optional[Dict[str, Optional[float]]] = None
        if zones is not None:
            zone_durations_seconds = {
                "zone_zero": _ms_to_s(zones.zone_zero_milli),
                "zone_one": _ms_to_s(zones.zone_one_milli),
                "zone_two": _ms_to_s(zones.zone_two_milli),
                "zone_three": _ms_to_s(zones.zone_three_milli),
                "zone_four": _ms_to_s(zones.zone_four_milli),
                "zone_five": _ms_to_s(zones.zone_five_milli),
            }

        return {
            "id": self.id,
            "start": self.start,
            "end": self.end,
            "timezone_offset": self.timezone_offset,
            "sport_id": self.sport_id,
            "sport_name": self.sport_name,
            "score_state": self.score_state,
            "strain": s.strain if s else None,
            "avg_hr_bpm": s.average_heart_rate if s else None,
            "max_hr_bpm": s.max_heart_rate if s else None,
            "calories": _kj_to_cal(s.kilojoule) if s else None,
            "percent_recorded": s.percent_recorded if s else None,
            "distance_meter": s.distance_meter if s else None,
            "altitude_change_meter": s.altitude_change_meter if s else None,
            "altitude_gain_meter": s.altitude_gain_meter if s else None,
            "zone_durations_seconds": zone_durations_seconds,
        }


# ---------- Convenience helpers ----------


def flatten_list(model_cls: type, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten a list of raw records through a given model class."""
    return [model_cls.model_validate(r).flatten() for r in records]
