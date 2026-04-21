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

import os
from pathlib import Path

import pytest

from whoop_store import SCHEMA_VERSION, WhoopStore


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
        "score": {
            "strain": 10.0,
            "average_heart_rate": 70,
            "max_heart_rate": 150,
            "kilojoule": 8000.0,
        },
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
    for i, (s, e) in enumerate(
        [
            ("2026-04-18T00:00:00Z", "2026-04-19T00:00:00Z"),
            ("2026-04-19T00:00:00Z", "2026-04-20T00:00:00Z"),
            ("2026-04-20T00:00:00Z", "2026-04-21T00:00:00Z"),
        ]
    ):
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


def test_recovery_start_end_inherited_from_cycle(store: WhoopStore):
    """Regression: WHOOP v2 recoveries lack their own start/end. Upsert must
    inherit them from the linked cycle so date-window queries work."""
    # Insert the parent cycle first.
    store.upsert_records(
        "cycles",
        [
            (
                _raw_cycle(
                    4001, "2026-04-18T21:23:00Z", "2026-04-19T07:12:00Z", "2026-04-19T08:00:00Z"
                ),
                _flat_cycle(4001, "2026-04-18T21:23:00Z", "2026-04-19T07:12:00Z"),
            )
        ],
    )
    # Recovery raw payload with NO start/end (as WHOOP actually returns).
    raw_rec = {
        "cycle_id": 4001,
        "sleep_id": "s-4001",
        "updated_at": "2026-04-19T08:30:00Z",
        "score_state": "SCORED",
        "score": {"recovery_score": 72, "hrv_rmssd_milli": 45.0, "resting_heart_rate": 58},
    }
    flat_rec = {
        "cycle_id": 4001,
        "sleep_id": "s-4001",
        "score_state": "SCORED",
        "recovery_score": 72.0,
    }
    store.upsert_records("recoveries", [(raw_rec, flat_rec)])

    # Querying by cycle's date window must return the recovery.
    rows = store.query_range(
        "recoveries",
        start="2026-04-19T00:00:00Z",
        end="2026-04-20T00:00:00Z",
    )
    assert len(rows) == 1, "recovery inherited from cycle's start/end should be in window"
    assert rows[0]["cycle_id"] == 4001


def test_backfill_recovery_windows(store: WhoopStore):
    """Backfill repairs recoveries inserted before their parent cycle existed."""
    # Recovery inserted first (simulating async sync where recoveries raced ahead).
    raw_rec = {
        "cycle_id": 5001,
        "sleep_id": "s-5001",
        "updated_at": "2026-04-19T08:30:00Z",
        "score_state": "SCORED",
        "score": {"recovery_score": 80},
    }
    store.upsert_records("recoveries", [(raw_rec, {"cycle_id": 5001, "score_state": "SCORED"})])
    # Nothing to inherit yet — start/end should be NULL.
    pre = store.query_range("recoveries", start="2026-04-19T00:00:00Z", end="2026-04-20T00:00:00Z")
    assert len(pre) == 0

    # Now cycle arrives.
    store.upsert_records(
        "cycles",
        [
            (
                _raw_cycle(
                    5001, "2026-04-18T22:00:00Z", "2026-04-19T06:00:00Z", "2026-04-19T08:00:00Z"
                ),
                _flat_cycle(5001, "2026-04-18T22:00:00Z", "2026-04-19T06:00:00Z"),
            )
        ],
    )
    # Run backfill.
    n = store.backfill_recovery_windows()
    assert n == 1, "one recovery row should be patched"

    post = store.query_range("recoveries", start="2026-04-19T00:00:00Z", end="2026-04-20T00:00:00Z")
    assert len(post) == 1


def test_range_query_uses_overlap_semantics(store: WhoopStore):
    """Regression: a cycle starting before the window but spanning into it must be returned.

    WHOOP cycles routinely start in the evening of day N-1 and end on day N.
    Filtering by ``start >= window_start`` alone would wrongly exclude them.
    """
    # Cycle starts 21:23 on Apr 18, ends 07:12 on Apr 19 — spans into the window.
    store.upsert_records(
        "cycles",
        [
            (
                _raw_cycle(
                    2001, "2026-04-18T21:23:00Z", "2026-04-19T07:12:00Z", "2026-04-19T08:00:00Z"
                ),
                _flat_cycle(2001, "2026-04-18T21:23:00Z", "2026-04-19T07:12:00Z"),
            )
        ],
    )
    # In-progress cycle: start before window end, end is NULL.
    store.upsert_records(
        "cycles",
        [
            (
                _raw_cycle(2002, "2026-04-20T22:00:00Z", None, "2026-04-20T22:00:00Z"),
                _flat_cycle(2002, "2026-04-20T22:00:00Z", None),
            )
        ],
    )
    # Cycle fully outside the window.
    store.upsert_records(
        "cycles",
        [
            (
                _raw_cycle(
                    2003, "2026-04-10T00:00:00Z", "2026-04-11T00:00:00Z", "2026-04-11T00:00:00Z"
                ),
                _flat_cycle(2003, "2026-04-10T00:00:00Z", "2026-04-11T00:00:00Z"),
            )
        ],
    )

    rows = store.query_range(
        "cycles",
        start="2026-04-19T00:00:00Z",
        end="2026-04-21T00:00:00Z",
    )
    ids = {r["id"] for r in rows}
    assert 2001 in ids, "cycle that spans into window must be included"
    assert 2002 in ids, "in-progress cycle (end IS NULL) must be included"
    assert 2003 not in ids, "cycle fully outside window must be excluded"


def test_iter_records_uses_overlap_semantics(store: WhoopStore):
    """Same overlap regression for ``iter_records`` (exports path)."""
    store.upsert_records(
        "cycles",
        [
            (
                _raw_cycle(
                    3001, "2026-04-18T21:23:00Z", "2026-04-19T07:12:00Z", "2026-04-19T08:00:00Z"
                ),
                _flat_cycle(3001, "2026-04-18T21:23:00Z", "2026-04-19T07:12:00Z"),
            )
        ],
    )
    store.upsert_records(
        "cycles",
        [
            (
                _raw_cycle(
                    3002, "2026-04-10T00:00:00Z", "2026-04-11T00:00:00Z", "2026-04-11T00:00:00Z"
                ),
                _flat_cycle(3002, "2026-04-10T00:00:00Z", "2026-04-11T00:00:00Z"),
            )
        ],
    )
    ids = {
        r["id"]
        for r in store.iter_records(
            "cycles", start="2026-04-19T00:00:00Z", end="2026-04-21T00:00:00Z"
        )
    }
    assert 3001 in ids
    assert 3002 not in ids


def test_query_limit(store: WhoopStore):
    for i in range(5):
        s = f"2026-04-{10 + i:02d}T00:00:00Z"
        e = f"2026-04-{11 + i:02d}T00:00:00Z"
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
    store.upsert_snapshot(
        "profile_snapshots", dict(raw, first_name="A2"), dict(flat, first_name="A2")
    )
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


# ---------- M6: snapshot hash-based change detection ----------


def test_snapshot_unchanged_payload_does_not_bump_updated_at(store: WhoopStore):
    """Repeated upsert of byte-identical snapshot must not advance updated_at."""
    raw = {"email": "a@b.c", "first_name": "A", "last_name": "K"}
    flat = {"email": "a@b.c", "first_name": "A", "last_name": "K"}
    changed_1 = store.upsert_snapshot("profile_snapshots", raw, flat)
    assert changed_1 == 1

    # Capture the stored updated_at.
    conn = store._connect()
    row = conn.execute("SELECT updated_at FROM profile_snapshots WHERE id='current'").fetchone()
    ts_before = row["updated_at"]

    # Repeated upsert, identical payload.
    changed_2 = store.upsert_snapshot("profile_snapshots", raw, flat)
    assert changed_2 == 0, "identical payload must not count as a change"

    row2 = conn.execute("SELECT updated_at FROM profile_snapshots WHERE id='current'").fetchone()
    assert row2["updated_at"] == ts_before, (
        "updated_at must be preserved when raw_json is unchanged"
    )


def test_snapshot_changed_payload_advances_updated_at(store: WhoopStore):
    """When raw_json changes the row's updated_at must advance."""
    raw1 = {"email": "a@b.c", "first_name": "A", "last_name": "K"}
    flat1 = dict(raw1)
    store.upsert_snapshot("profile_snapshots", raw1, flat1)

    conn = store._connect()
    ts_before = conn.execute(
        "SELECT updated_at FROM profile_snapshots WHERE id='current'"
    ).fetchone()["updated_at"]

    raw2 = {"email": "a@b.c", "first_name": "AZ", "last_name": "K"}
    flat2 = dict(raw2)
    changed = store.upsert_snapshot("profile_snapshots", raw2, flat2)
    assert changed == 1

    ts_after = conn.execute(
        "SELECT updated_at FROM profile_snapshots WHERE id='current'"
    ).fetchone()["updated_at"]
    assert ts_after > ts_before, "updated_at must advance on real change"


def test_snapshot_idempotent_sync_produces_no_events(store: WhoopStore):
    """After two back-to-back identical upserts, the events feed for the
    snapshot resource in that window must be empty the second time around.
    """
    raw = {"email": "a@b.c", "first_name": "A", "last_name": "K"}
    flat = dict(raw)
    store.upsert_snapshot("profile_snapshots", raw, flat)

    # Capture the timestamp after the first write.
    conn = store._connect()
    ts = conn.execute("SELECT updated_at FROM profile_snapshots WHERE id='current'").fetchone()[
        "updated_at"
    ]

    # A second identical write should not produce a new event after ``ts``.
    store.upsert_snapshot("profile_snapshots", raw, flat)

    events = list(
        store.iter_events(
            resources=["profile"],
            since=ts,
            until="2099-01-01T00:00:00Z",
            limit=100,
        )
    )
    assert events == []
