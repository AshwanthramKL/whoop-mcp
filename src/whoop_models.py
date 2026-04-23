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
- Timestamps on Cycle/Sleep/Workout are emitted as ``start_utc``/``end_utc``
  (canonical, ends in ``Z``) and ``start_local``/``end_local`` (derived from
  ``timezone_offset``). UTC is authoritative; the local fields exist so LLM
  callers don't misread the ``Z`` suffix as local wall-clock time.
- ``score_state`` always lifted to top level. If not SCORED, score fields
  are ``None``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BodyMeasurement",
    "Cycle",
    "Profile",
    "Recovery",
    "Sleep",
    "Workout",
]


KJ_TO_KCAL = 0.239006


def _ms_to_s(value: int | None) -> float | None:
    """Convert milliseconds to seconds (1 decimal). Returns None for None."""
    if value is None:
        return None
    return round(value / 1000.0, 1)


def _kj_to_cal(kj: float | None) -> int | None:
    """Convert kilojoules to kcal, rounded to an integer."""
    if kj is None:
        return None
    return round(kj * KJ_TO_KCAL)


def _parse_offset(offset: str | None) -> timezone | None:
    """Parse an offset string like '+05:30' or '-04:00' into a tzinfo.

    Returns None if the offset is missing or unparseable.
    """
    if not offset:
        return None
    if offset in ("Z", "z"):
        return timezone.utc
    try:
        sign = 1 if offset[0] == "+" else -1 if offset[0] == "-" else None
        if sign is None:
            return None
        hh, _, mm = offset[1:].partition(":")
        delta = timedelta(hours=int(hh), minutes=int(mm) if mm else 0)
        return timezone(sign * delta)
    except (ValueError, IndexError):
        return None


def _to_local_iso(utc_iso: str | None, offset: str | None) -> str | None:
    """Convert a UTC ISO-8601 string + offset like '+05:30' into the
    local wall-clock ISO string carrying that offset.

    Returns None if either input is missing or unparseable. UTC is the
    authoritative source of truth; this field exists so LLM consumers
    don't misread the ``...Z`` suffix as local time when talking to the
    user about times of day.
    """
    if not utc_iso or not offset:
        return None
    tz = _parse_offset(offset)
    if tz is None:
        return None
    try:
        dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).isoformat()


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    def flatten(self) -> dict[str, Any]:  # pragma: no cover - overridden by subclasses
        """Return the LLM-friendly flat shape. Each concrete subclass implements this."""
        raise NotImplementedError


# ---------- Profile ----------


class Profile(_Base):
    """WHOOP user profile (identity only)."""

    email: str | None = Field(default=None, description="User's email address.")
    first_name: str | None = Field(default=None, description="User's first name.")
    last_name: str | None = Field(default=None, description="User's last name.")

    def flatten(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "first_name": self.first_name,
            "last_name": self.last_name,
        }


# ---------- Body Measurement ----------


class BodyMeasurement(_Base):
    """Latest body measurements."""

    height_meter: float | None = Field(default=None, description="Height in meters.")
    weight_kilogram: float | None = Field(default=None, description="Weight in kg.")
    max_heart_rate: int | None = Field(default=None, description="Measured max heart rate in bpm.")

    def flatten(self) -> dict[str, Any]:
        return {
            "height_meter": self.height_meter,
            "weight_kilogram": self.weight_kilogram,
            "max_hr_bpm": self.max_heart_rate,
        }


# ---------- Cycle ----------


class _CycleScore(_Base):
    strain: float | None = None
    kilojoule: float | None = None
    average_heart_rate: int | None = None
    max_heart_rate: int | None = None


class Cycle(_Base):
    """One WHOOP physiological cycle (roughly a 'day' per WHOOP's definition)."""

    id: int = Field(description="WHOOP cycle ID.")
    start: str | None = Field(default=None, description="ISO-8601 cycle start.")
    end: str | None = Field(
        default=None, description="ISO-8601 cycle end (null for the ongoing cycle)."
    )
    timezone_offset: str | None = Field(default=None, description="Timezone offset like '+05:30'.")
    score_state: str | None = Field(
        default=None, description="SCORED / PENDING_SCORE / UNSCORABLE."
    )
    score: _CycleScore | None = None

    def flatten(self) -> dict[str, Any]:
        score = self.score
        return {
            "id": self.id,
            "start_utc": self.start,
            "end_utc": self.end,
            "start_local": _to_local_iso(self.start, self.timezone_offset),
            "end_local": _to_local_iso(self.end, self.timezone_offset),
            "timezone_offset": self.timezone_offset,
            "score_state": self.score_state,
            "strain": score.strain if score else None,
            "avg_hr_bpm": score.average_heart_rate if score else None,
            "max_hr_bpm": score.max_heart_rate if score else None,
            "calories": _kj_to_cal(score.kilojoule) if score else None,
        }


# ---------- Recovery ----------


class _RecoveryScore(_Base):
    recovery_score: float | None = None
    resting_heart_rate: float | None = None
    hrv_rmssd_milli: float | None = None
    spo2_percentage: float | None = None
    skin_temp_celsius: float | None = None
    user_calibrating: bool | None = None


class Recovery(_Base):
    """Morning recovery record attached to a cycle + sleep."""

    cycle_id: int | None = Field(default=None, description="Parent cycle ID.")
    sleep_id: str | None = Field(default=None, description="Source sleep UUID.")
    score_state: str | None = Field(default=None, description="Score state.")
    score: _RecoveryScore | None = None

    def flatten(self) -> dict[str, Any]:
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
    disturbance_count: int | None = None
    sleep_cycle_count: int | None = None
    total_awake_time_milli: int | None = None
    total_in_bed_time_milli: int | None = None
    total_light_sleep_time_milli: int | None = None
    total_no_data_time_milli: int | None = None
    total_rem_sleep_time_milli: int | None = None
    total_slow_wave_sleep_time_milli: int | None = None


class _SleepNeeded(_Base):
    baseline_milli: int | None = None
    need_from_recent_nap_milli: int | None = None
    need_from_recent_strain_milli: int | None = None
    need_from_sleep_debt_milli: int | None = None


class _SleepScore(_Base):
    respiratory_rate: float | None = None
    sleep_consistency_percentage: float | None = None
    sleep_efficiency_percentage: float | None = None
    sleep_performance_percentage: float | None = None
    sleep_needed: _SleepNeeded | None = None
    stage_summary: _SleepStageSummary | None = None


class Sleep(_Base):
    """One sleep activity (main sleep or nap)."""

    id: str = Field(description="Sleep UUID.")
    cycle_id: int | None = Field(default=None, description="Parent cycle ID.")
    start: str | None = None
    end: str | None = None
    timezone_offset: str | None = None
    nap: bool | None = Field(default=None, description="True if this is a nap.")
    score_state: str | None = None
    score: _SleepScore | None = None

    def flatten(self) -> dict[str, Any]:
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
            "start_utc": self.start,
            "end_utc": self.end,
            "start_local": _to_local_iso(self.start, self.timezone_offset),
            "end_local": _to_local_iso(self.end, self.timezone_offset),
            "timezone_offset": self.timezone_offset,
            "nap": self.nap,
            "score_state": self.score_state,
            "respiratory_rate": s.respiratory_rate if s else None,
            "sleep_consistency_pct": s.sleep_consistency_percentage if s else None,
            "sleep_efficiency_pct": s.sleep_efficiency_percentage if s else None,
            "sleep_performance_pct": s.sleep_performance_percentage if s else None,
            "in_bed_seconds": _ms_to_s(stage.total_in_bed_time_milli) if stage else None,
            "light_sleep_seconds": _ms_to_s(stage.total_light_sleep_time_milli) if stage else None,
            "rem_sleep_seconds": _ms_to_s(stage.total_rem_sleep_time_milli) if stage else None,
            "deep_sleep_seconds": _ms_to_s(stage.total_slow_wave_sleep_time_milli)
            if stage
            else None,
            "awake_seconds": _ms_to_s(stage.total_awake_time_milli) if stage else None,
            "no_data_seconds": _ms_to_s(stage.total_no_data_time_milli) if stage else None,
            "sleep_cycle_count": stage.sleep_cycle_count if stage else None,
            "disturbance_count": stage.disturbance_count if stage else None,
            "sleep_needed_seconds": sleep_needed_s,
        }


# ---------- Workout ----------


class _ZoneDurations(_Base):
    zone_zero_milli: int | None = None
    zone_one_milli: int | None = None
    zone_two_milli: int | None = None
    zone_three_milli: int | None = None
    zone_four_milli: int | None = None
    zone_five_milli: int | None = None


class _WorkoutScore(_Base):
    strain: float | None = None
    kilojoule: float | None = None
    average_heart_rate: int | None = None
    max_heart_rate: int | None = None
    percent_recorded: float | None = None
    distance_meter: float | None = None
    altitude_change_meter: float | None = None
    altitude_gain_meter: float | None = None
    zone_durations: _ZoneDurations | None = None


class Workout(_Base):
    """One recorded workout."""

    id: str = Field(description="Workout UUID.")
    start: str | None = None
    end: str | None = None
    timezone_offset: str | None = None
    sport_id: int | None = None
    sport_name: str | None = None
    score_state: str | None = None
    score: _WorkoutScore | None = None

    def flatten(self) -> dict[str, Any]:
        s = self.score
        zones = s.zone_durations if s else None

        zone_durations_seconds: dict[str, float | None] | None = None
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
            "start_utc": self.start,
            "end_utc": self.end,
            "start_local": _to_local_iso(self.start, self.timezone_offset),
            "end_local": _to_local_iso(self.end, self.timezone_offset),
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


def flatten_list(model_cls: type[_Base], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten a list of raw records through a given model class."""
    return [model_cls.model_validate(r).flatten() for r in records]
