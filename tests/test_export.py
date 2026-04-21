"""
Tests for M4 — the ``export_whoop`` MCP tool and its backing writers.

The export tool writes flat, decoded records from the cache to disk in
CSV / JSONL / Parquet. It is strictly a data-layer tool — no API calls,
no analytics. Running ``sync_whoop`` is a prerequisite; an empty cache
returns a ``CACHE_EMPTY`` error envelope with a hint to run sync first.

These tests exercise the tool end-to-end using an isolated per-test
SQLite cache populated via ``store.upsert_records(...)`` with synthetic
flat dicts. No network.
"""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

import whoop_mcp_server as server
from whoop_store import WhoopStore


# ---------- helpers: seed the cache with synthetic rows ----------


def _cycle_pair(
    cycle_id: int, start: str, end: str, updated_at: str, strain: float = 10.0
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    raw = {
        "id": cycle_id,
        "start": start,
        "end": end,
        "updated_at": updated_at,
        "score_state": "SCORED",
        "score": {
            "strain": strain,
            "average_heart_rate": 70,
            "max_heart_rate": 150,
            "kilojoule": 8000.0,
        },
    }
    flat = {
        "id": cycle_id,
        "start": start,
        "end": end,
        "score_state": "SCORED",
        "strain": strain,
        "avg_hr_bpm": 70,
        "max_hr_bpm": 150,
        "calories": 1912,
    }
    return raw, flat


def _sleep_pair(
    sleep_id: str, cycle_id: int, start: str, end: str, updated_at: str
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    raw = {
        "id": sleep_id,
        "cycle_id": cycle_id,
        "start": start,
        "end": end,
        "nap": False,
        "updated_at": updated_at,
        "score_state": "SCORED",
    }
    flat = {
        "id": sleep_id,
        "cycle_id": cycle_id,
        "start": start,
        "end": end,
        "nap": False,
        "score_state": "SCORED",
        "in_bed_seconds": 28800.0,
        "deep_sleep_seconds": 6000.0,
        "rem_sleep_seconds": 5000.0,
        # nested dict that should be JSON-encoded in CSV
        "stage_detail": {"light": 17800.0, "awake": 200.0},
    }
    return raw, flat


def _recovery_pair(
    cycle_id: int, start: str, updated_at: str, score: int = 75
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    raw = {
        "id": f"rec-{cycle_id}",
        "cycle_id": cycle_id,
        "start": start,
        "end": start,
        "updated_at": updated_at,
        "score_state": "SCORED",
    }
    flat = {
        "id": f"rec-{cycle_id}",
        "cycle_id": cycle_id,
        "start": start,
        "score_state": "SCORED",
        "recovery_score": score,
        "resting_heart_rate": 55,
        "hrv_rmssd_milli": 45.0,
    }
    return raw, flat


def _workout_pair(
    workout_id: str, start: str, end: str, updated_at: str, sport_id: int = 1
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    raw = {
        "id": workout_id,
        "start": start,
        "end": end,
        "updated_at": updated_at,
        "sport_id": sport_id,
        "score_state": "SCORED",
    }
    flat = {
        "id": workout_id,
        "start": start,
        "end": end,
        "score_state": "SCORED",
        "sport_id": sport_id,
        "strain": 11.5,
        "avg_hr_bpm": 130,
        "max_hr_bpm": 170,
        "calories": 500,
    }
    return raw, flat


@pytest.fixture
def seeded_store() -> WhoopStore:
    """Seed the module-level store singleton with a known set of records."""
    store = server._get_store()
    cycles = [
        _cycle_pair(1, "2026-02-10T00:00:00Z", "2026-02-11T00:00:00Z", "2026-02-11T06:00:00Z"),
        _cycle_pair(2, "2026-03-10T00:00:00Z", "2026-03-11T00:00:00Z", "2026-03-11T06:00:00Z"),
        _cycle_pair(3, "2026-04-10T00:00:00Z", "2026-04-11T00:00:00Z", "2026-04-11T06:00:00Z"),
    ]
    store.upsert_records("cycles", cycles)

    sleeps = [
        _sleep_pair("s-1", 1, "2026-02-10T22:00:00Z", "2026-02-11T06:00:00Z", "2026-02-11T07:00:00Z"),
        _sleep_pair("s-2", 2, "2026-03-10T22:00:00Z", "2026-03-11T06:00:00Z", "2026-03-11T07:00:00Z"),
        _sleep_pair("s-3", 3, "2026-04-10T22:00:00Z", "2026-04-11T06:00:00Z", "2026-04-11T07:00:00Z"),
    ]
    store.upsert_records("sleeps", sleeps)

    recoveries = [
        _recovery_pair(1, "2026-02-10T00:00:00Z", "2026-02-11T06:00:00Z", score=70),
        _recovery_pair(2, "2026-03-10T00:00:00Z", "2026-03-11T06:00:00Z", score=75),
        _recovery_pair(3, "2026-04-10T00:00:00Z", "2026-04-11T06:00:00Z", score=80),
    ]
    store.upsert_records("recoveries", recoveries)

    workouts = [
        _workout_pair("w-1", "2026-02-10T17:00:00Z", "2026-02-10T18:00:00Z", "2026-02-10T18:30:00Z"),
        _workout_pair("w-2", "2026-03-10T17:00:00Z", "2026-03-10T18:00:00Z", "2026-03-10T18:30:00Z", sport_id=45),
        _workout_pair("w-3", "2026-04-10T17:00:00Z", "2026-04-10T18:00:00Z", "2026-04-10T18:30:00Z"),
    ]
    store.upsert_records("workouts", workouts)

    # Single-row snapshots
    store.upsert_snapshot(
        "body_measurements",
        {"updated_at": "2026-04-10T00:00:00Z", "height_meter": 1.8, "weight_kilogram": 75.0},
        {"height_meter": 1.8, "weight_kilogram": 75.0, "max_heart_rate": 190},
    )
    store.upsert_snapshot(
        "profile_snapshots",
        {"updated_at": "2026-04-10T00:00:00Z", "email": "a@b.c", "first_name": "Ash"},
        {"email": "a@b.c", "first_name": "Ash", "last_name": "K"},
    )
    return store


# ---------- iter_records store helper ----------


def test_iter_records_yields_flat_dicts_in_window(seeded_store: WhoopStore):
    rows = list(
        seeded_store.iter_records(
            "cycles",
            start="2026-03-01T00:00:00Z",
            end="2026-03-31T23:59:59Z",
        )
    )
    assert len(rows) == 1
    assert rows[0]["id"] == 2


def test_iter_records_orders_by_start_asc(seeded_store: WhoopStore):
    rows = list(seeded_store.iter_records("cycles", start=None, end=None))
    starts = [r["start"] for r in rows]
    assert starts == sorted(starts)


def test_iter_records_snapshots_ignore_window(seeded_store: WhoopStore):
    rows = list(
        seeded_store.iter_records(
            "body_measurements",
            start="1970-01-01T00:00:00Z",
            end="1970-01-02T00:00:00Z",
        )
    )
    assert len(rows) == 1
    assert rows[0]["height_meter"] == 1.8


# ---------- CSV ----------


async def test_export_csv_cycles(tmp_path: Path, seeded_store: WhoopStore):
    out = tmp_path / "cycles.csv"
    result = await server.export_whoop(
        kind="cycles", format="csv", path=str(out)
    )
    assert result["status"] == "success"
    assert out.exists()
    assert result["files"][0]["records"] == 3
    assert result["files"][0]["bytes"] == out.stat().st_size

    with open(out, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
        # Deterministic alphabetical header
        assert header == sorted(header)
        rows = list(reader)
    assert len(rows) == 3
    # Standard flat keys present
    assert "strain" in header
    assert "id" in header


async def test_export_csv_stringifies_nested(tmp_path: Path, seeded_store: WhoopStore):
    out = tmp_path / "sleeps.csv"
    result = await server.export_whoop(kind="sleeps", format="csv", path=str(out))
    assert result["status"] == "success"

    with open(out, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert rows
    # stage_detail was a nested dict; should be JSON-encoded text now
    stage = rows[0]["stage_detail"]
    assert isinstance(stage, str)
    parsed = json.loads(stage)
    assert "light" in parsed


# ---------- JSONL ----------


async def test_export_jsonl_sleeps(tmp_path: Path, seeded_store: WhoopStore):
    out = tmp_path / "sleeps.jsonl"
    result = await server.export_whoop(kind="sleeps", format="jsonl", path=str(out))
    assert result["status"] == "success"
    assert result["files"][0]["records"] == 3

    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    records = [json.loads(line) for line in lines]
    for r in records:
        assert "deep_sleep_seconds" in r
        # Deterministic key ordering
        assert list(r.keys()) == sorted(r.keys())


# ---------- Parquet ----------


async def test_export_parquet_workouts(tmp_path: Path, seeded_store: WhoopStore):
    import pyarrow.parquet as pq

    out = tmp_path / "workouts.parquet"
    result = await server.export_whoop(
        kind="workouts", format="parquet", path=str(out)
    )
    assert result["status"] == "success"
    assert result["files"][0]["records"] == 3

    tbl = pq.read_table(str(out))
    d = tbl.to_pydict()
    assert "sport_id" in d
    assert len(d["sport_id"]) == 3
    # 45 is present (m-3 set it)
    assert 45 in d["sport_id"]


# ---------- kind='all' ----------


async def test_export_all_writes_directory(tmp_path: Path, seeded_store: WhoopStore):
    out_dir = tmp_path / "all_export"
    result = await server.export_whoop(
        kind="all", format="csv", path=str(out_dir)
    )
    assert result["status"] == "success"
    assert out_dir.is_dir()
    resources = {f["resource"] for f in result["files"]}
    # All 4 list resources at minimum
    assert {"cycles", "recoveries", "sleeps", "workouts"}.issubset(resources)
    for f in result["files"]:
        assert Path(f["path"]).exists()
        assert Path(f["path"]).suffix == ".csv"


async def test_export_all_creates_missing_directory(tmp_path: Path, seeded_store: WhoopStore):
    nested = tmp_path / "does-not-exist-yet" / "sub"
    result = await server.export_whoop(
        kind="all", format="jsonl", path=str(nested)
    )
    assert result["status"] == "success"
    assert nested.is_dir()


# ---------- error paths ----------


async def test_export_cache_empty(tmp_path: Path):
    # Fresh isolated store — do NOT seed.
    out = tmp_path / "cycles.csv"
    result = await server.export_whoop(
        kind="cycles", format="csv", path=str(out)
    )
    assert "error" in result
    assert result["error"]["code"] == "CACHE_EMPTY"
    assert "sync_whoop" in result["error"]["message"]
    assert not out.exists()


async def test_export_file_exists_without_overwrite(
    tmp_path: Path, seeded_store: WhoopStore
):
    out = tmp_path / "cycles.csv"
    r1 = await server.export_whoop(kind="cycles", format="csv", path=str(out))
    assert r1["status"] == "success"
    r2 = await server.export_whoop(kind="cycles", format="csv", path=str(out))
    assert "error" in r2
    assert r2["error"]["code"] == "FILE_EXISTS"


async def test_export_overwrite_replaces(
    tmp_path: Path, seeded_store: WhoopStore
):
    out = tmp_path / "cycles.csv"
    await server.export_whoop(kind="cycles", format="csv", path=str(out))
    r2 = await server.export_whoop(
        kind="cycles", format="csv", path=str(out), overwrite=True
    )
    assert r2["status"] == "success"


async def test_export_invalid_format(tmp_path: Path, seeded_store: WhoopStore):
    result = await server.export_whoop(
        kind="cycles", format="xml", path=str(tmp_path / "c.xml")
    )
    assert "error" in result
    assert result["error"]["code"] == "VALIDATION_ERROR"


async def test_export_invalid_kind(tmp_path: Path, seeded_store: WhoopStore):
    result = await server.export_whoop(
        kind="naps", format="csv", path=str(tmp_path / "c.csv")
    )
    assert "error" in result
    assert result["error"]["code"] == "VALIDATION_ERROR"


async def test_export_bad_date(tmp_path: Path, seeded_store: WhoopStore):
    result = await server.export_whoop(
        kind="cycles",
        format="csv",
        path=str(tmp_path / "c.csv"),
        start="tomorrow",
    )
    assert "error" in result
    assert result["error"]["code"] == "VALIDATION_ERROR"


# ---------- Range filter ----------


async def test_export_range_trims_to_middle_month(
    tmp_path: Path, seeded_store: WhoopStore
):
    # Seeded cycles: Feb 10, Mar 10, Apr 10.
    out = tmp_path / "cycles.csv"
    result = await server.export_whoop(
        kind="cycles",
        format="csv",
        path=str(out),
        start="2026-03-01",
        end="2026-03-31",
    )
    assert result["status"] == "success"
    assert result["files"][0]["records"] == 1

    with open(out, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["id"] == "2"


# ---------- Empty window but valid ----------


async def test_export_empty_window_still_writes_csv_header(
    tmp_path: Path, seeded_store: WhoopStore
):
    out = tmp_path / "cycles.csv"
    result = await server.export_whoop(
        kind="cycles",
        format="csv",
        path=str(out),
        start="1990-01-01",
        end="1990-01-31",
    )
    assert result["status"] == "success"
    assert result["files"][0]["records"] == 0
    assert out.exists()


async def test_export_empty_window_jsonl_empty_file(
    tmp_path: Path, seeded_store: WhoopStore
):
    out = tmp_path / "cycles.jsonl"
    result = await server.export_whoop(
        kind="cycles",
        format="jsonl",
        path=str(out),
        start="1990-01-01",
        end="1990-01-31",
    )
    assert result["status"] == "success"
    assert result["files"][0]["records"] == 0
    assert out.exists()
    assert out.read_text(encoding="utf-8") == ""


# ---------- Tool never raises ----------


async def test_export_tool_never_raises(tmp_path: Path, seeded_store: WhoopStore, monkeypatch):
    # Force an unexpected exception inside the export module.
    import whoop_export

    def _boom(*a, **k):
        raise RuntimeError("explode")

    monkeypatch.setattr(whoop_export, "_write_csv", _boom)
    out = tmp_path / "cycles.csv"
    result = await server.export_whoop(kind="cycles", format="csv", path=str(out))
    assert "error" in result
    assert result["error"]["code"] in {"EXPORT_ERROR", "UPSTREAM_ERROR"}


# ---------- version bump ----------


def test_server_version_bumped_to_0_5_0():
    # M4 required >= 0.5.0; later milestones continue the bump.
    assert server.SERVER_VERSION >= "0.5.0"
