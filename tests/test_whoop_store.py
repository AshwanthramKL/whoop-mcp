"""
Tests for ``whoop_store`` — the SQLite cache layer introduced in M3.

The store is the only place that talks to sqlite. It knows:
- how to init/migrate schema idempotently
- how to upsert records with staleness guards (newer updated_at wins)
- how to range-query by start/end
- how to filter sleeps/recoveries by cycle_id
- how to record sync_runs audit rows

No network, no Pydantic here — the store takes flat JSON dicts from the caller
and stores both raw and flat representations verbatim.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from whoop_store import WhoopStore, SCHEMA_VERSION


@pytest.fixture
def store_path(tmp_path: Path) -> str:
    return str(tmp_path / "whoop.db")


@pytest.fixture
def store(store_path: str) -> WhoopStore:
    s = WhoopStore(store_path)
    s.init_schema()
    yield s
    s.close()


def _raw_cycle(cycle_id: int, start: str, end: str, updated_at: str) -> dict:
    return {
        "id": cycle_id,
        "start": start,
        "end": end,
        "updated_at": updated_at,
        "score_state": "SCORED",
        "score": {"strain": 10.0, "average_heart_rate": 70, "max_heart_rate": 150, "kilojoule": 8000.0},
    }


def _flat_cycle(cycle_id: int, start: str, end: str) -> dict:
    return {
        "id": cycle_id,
        "start": start,
        "end": end,
        "score_state": "SCORED",
        "strain": 10.0,
        "avg_hr_bpm": 70,
        "max_hr_bpm": 150,
        "calories": 1912,
    }


# ---------- schema ----------


def test_init_schema_is_idempotent(store: WhoopStore):
    store.init_schema()  # second call must not raise
    store.init_schema()  # third for good measure
    # user_version is set
    conn = store._connect()
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    assert v == SCHEMA_VERSION


def test_init_schema_creates_all_tables(store: WhoopStore):
    conn = store._connect()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r[0] for r in rows}
    assert {
        "cycles",
        "recoveries",
        "sleeps",
        "workouts",
        "body_measurements",
        "profile_snapshots",
        "sync_runs",
    }.issubset(names)


def test_db_file_is_chmod_600(store_path: str):
    s = WhoopStore(store_path)
    s.init_schema()
    mode = os.stat(store_path).st_mode & 0o777
    assert mode == 0o600, f"expected 0o600 got {oct(mode)}"
    s.close()


# ---------- upsert cycles ----------


def test_upsert_cycle_inserts_new_record(store: WhoopStore):
    raw = _raw_cycle(1, "2026-04-20T00:00:00Z", "2026-04-21T00:00:00Z", "2026-04-21T06:00:00Z")
    flat = _flat_cycle(1, raw["start"], raw["end"])
    n = store.upsert_records("cycles", [(raw, flat)])
    assert n == 1

    rows = store.query_range("cycles", start=None, end=None)
    assert len(rows) == 1
    assert rows[0]["id"] == 1


def test_upsert_cycle_replaces_when_updated_at_newer(store: WhoopStore):
    raw = _raw_cycle(1, "2026-04-20T00:00:00Z", "2026-04-21T00:00:00Z", "2026-04-21T06:00:00Z")
    flat = _flat_cycle(1, raw["start"], raw["end"])
    store.upsert_records("cycles", [(raw, flat)])

    # Newer update: strain jumps to 12.5
    raw2 = dict(raw)
    raw2["updated_at"] = "2026-04-21T08:00:00Z"
    raw2["score"] = dict(raw["score"], strain=12.5)
    flat2 = dict(flat, strain=12.5)
    n = store.upsert_records("cycles", [(raw2, flat2)])
    assert n == 1

    rows = store.query_range("cycles", start=None, end=None)
    assert len(rows) == 1
    assert rows[0]["strain"] == 12.5


def test_upsert_skips_when_updated_at_older(store: WhoopStore):
    raw = _raw_cycle(1, "2026-04-20T00:00:00Z", "2026-04-21T00:00:00Z", "2026-04-21T08:00:00Z")
    flat = _flat_cycle(1, raw["start"], raw["end"])
    store.upsert_records("cycles", [(raw, flat)])

    # Older update should be ignored
    raw_stale = dict(raw, updated_at="2026-04-21T01:00:00Z")
    raw_stale["score"] = dict(raw["score"], strain=0.0)
    flat_stale = dict(flat, strain=0.0)
    n = store.upsert_records("cycles", [(raw_stale, flat_stale)])
    assert n == 0

    rows = store.query_range("cycles", start=None, end=None)
    assert len(rows) == 1
    assert rows[0]["strain"] == 10.0  # original preserved


# ---------- range query ----------


def test_range_query_by_start(store: WhoopStore):
    for i, (s, e) in enumerate([
        ("2026-04-18T00:00:00Z", "2026-04-19T00:00:00Z"),
        ("2026-04-19T00:00:00Z", "2026-04-20T00:00:00Z"),
        ("2026-04-20T00:00:00Z", "2026-04-21T00:00:00Z"),
    ]):
        raw = _raw_cycle(1000 + i, s, e, "2026-04-21T06:00:00Z")
        flat = _flat_cycle(1000 + i, s, e)
        store.upsert_records("cycles", [(raw, flat)])

    rows = store.query_range(
        "cycles",
        start="2026-04-19T00:00:00Z",
        end="2026-04-21T00:00:00Z",
    )
    # Should include the middle two (start in [2026-04-19, 2026-04-21))
    assert len(rows) == 2


def test_query_limit(store: WhoopStore):
    for i in range(5):
        s = f"2026-04-{10+i:02d}T00:00:00Z"
        e = f"2026-04-{11+i:02d}T00:00:00Z"
        raw = _raw_cycle(1000 + i, s, e, "2026-04-21T06:00:00Z")
        flat = _flat_cycle(1000 + i, s, e)
        store.upsert_records("cycles", [(raw, flat)])

    rows = store.query_range("cycles", start=None, end=None, limit=2)
    assert len(rows) == 2


# ---------- cycle_id filter on children ----------


def test_sleeps_filter_by_cycle_id(store: WhoopStore):
    raw = {
        "id": "uuid-sleep-a",
        "cycle_id": 42,
        "start": "2026-04-20T23:00:00Z",
        "end": "2026-04-21T07:00:00Z",
        "nap": False,
        "updated_at": "2026-04-21T07:30:00Z",
        "score_state": "SCORED",
    }
    flat = {
        "id": "uuid-sleep-a",
        "cycle_id": 42,
        "start": raw["start"],
        "end": raw["end"],
        "nap": False,
        "score_state": "SCORED",
        "in_bed_seconds": 28800.0,
    }
    store.upsert_records("sleeps", [(raw, flat)])

    raw_other = dict(raw, id="uuid-sleep-b", cycle_id=43)
    flat_other = dict(flat, id="uuid-sleep-b", cycle_id=43)
    store.upsert_records("sleeps", [(raw_other, flat_other)])

    rows = store.query_by_cycle_id("sleeps", cycle_id=42)
    assert len(rows) == 1
    assert rows[0]["id"] == "uuid-sleep-a"


# ---------- snapshots ----------


def test_profile_snapshot_upsert(store: WhoopStore):
    raw = {"email": "a@b.c", "first_name": "Ash", "last_name": "K"}
    flat = dict(raw)
    store.upsert_snapshot("profile_snapshots", raw, flat)

    latest = store.get_latest_snapshot("profile_snapshots")
    assert latest is not None
    assert latest["email"] == "a@b.c"

    # Update keeps only current
    store.upsert_snapshot("profile_snapshots", dict(raw, first_name="A2"), dict(flat, first_name="A2"))
    latest2 = store.get_latest_snapshot("profile_snapshots")
    assert latest2["first_name"] == "A2"


# ---------- sync_runs audit ----------


def test_sync_runs_round_trip(store: WhoopStore):
    run_id = store.start_sync_run("cycles", since_cursor="2026-04-01T00:00:00Z")
    store.finish_sync_run(
        run_id,
        status="success",
        records_fetched=25,
        records_upserted=5,
        until_cursor="2026-04-21T08:00:00Z",
    )

    rows = store.list_sync_runs(limit=5)
    assert len(rows) == 1
    r = rows[0]
    assert r["resource"] == "cycles"
    assert r["records_fetched"] == 25
    assert r["records_upserted"] == 5
    assert r["status"] == "success"
    assert r["until_cursor"] == "2026-04-21T08:00:00Z"


def test_latest_cursor_returns_max_updated_at(store: WhoopStore):
    # Write two cycle rows with different updated_at
    for uid, ua in [(1, "2026-04-20T06:00:00Z"), (2, "2026-04-21T06:00:00Z")]:
        raw = _raw_cycle(uid, "2026-04-18T00:00:00Z", "2026-04-19T00:00:00Z", ua)
        flat = _flat_cycle(uid, raw["start"], raw["end"])
        store.upsert_records("cycles", [(raw, flat)])

    c = store.get_max_updated_at("cycles")
    assert c == "2026-04-21T06:00:00Z"


def test_no_rows_returns_none_cursor(store: WhoopStore):
    assert store.get_max_updated_at("cycles") is None
