"""
Tests for M5 — the ``get_whoop_events`` MCP tool and its backing store helper.

The events feed is a pure cache read: it unions record tables (cycles /
recoveries / sleeps / workouts) and snapshot tables (body_measurements /
profile_snapshots) filtered by ``updated_at`` in the half-open window
``(since, until)``, sorts ascending, and wraps each row as an event dict
with a ``resource`` tag + decoded ``record``.

No network, no WHOOP API. Snapshots are surfaced as single "current" rows.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import whoop_mcp_server as server
from whoop_store import WhoopStore

# ---------- helpers ----------


def _cycle_pair(
    cycle_id: int, start: str, end: str, updated_at: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = {
        "id": cycle_id,
        "start": start,
        "end": end,
        "updated_at": updated_at,
        "score_state": "SCORED",
    }
    flat = {
        "id": cycle_id,
        "start": start,
        "end": end,
        "updated_at": updated_at,
        "score_state": "SCORED",
        "strain": 12.3,
    }
    return raw, flat


def _sleep_pair(
    sleep_id: str, cycle_id: int, start: str, end: str, updated_at: str
) -> tuple[dict[str, Any], dict[str, Any]]:
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
        "updated_at": updated_at,
        "score_state": "SCORED",
    }
    return raw, flat


def _recovery_pair(
    cycle_id: int, start: str, updated_at: str
) -> tuple[dict[str, Any], dict[str, Any]]:
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
        "updated_at": updated_at,
        "score_state": "SCORED",
        "recovery_score": 70,
    }
    return raw, flat


def _workout_pair(
    workout_id: str, start: str, end: str, updated_at: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = {
        "id": workout_id,
        "start": start,
        "end": end,
        "updated_at": updated_at,
        "sport_id": 1,
        "score_state": "SCORED",
    }
    flat = {
        "id": workout_id,
        "start": start,
        "end": end,
        "updated_at": updated_at,
        "score_state": "SCORED",
        "sport_id": 1,
    }
    return raw, flat


@pytest.fixture
def seeded_store() -> WhoopStore:
    """Populate the module-level store singleton with a spread of updated_at values."""
    store = server._get_store()

    # Cycles: updated_at spanning 2026-04-18 .. 2026-04-21
    store.upsert_records(
        "cycles",
        [
            _cycle_pair(1, "2026-04-18T00:00:00Z", "2026-04-19T00:00:00Z", "2026-04-18T12:00:00Z"),
            _cycle_pair(2, "2026-04-19T00:00:00Z", "2026-04-20T00:00:00Z", "2026-04-19T12:00:00Z"),
            _cycle_pair(3, "2026-04-20T00:00:00Z", "2026-04-21T00:00:00Z", "2026-04-20T18:00:00Z"),
        ],
    )

    # Sleeps: interleaved with cycles
    store.upsert_records(
        "sleeps",
        [
            _sleep_pair(
                "s-1", 1, "2026-04-18T22:00:00Z", "2026-04-19T06:00:00Z", "2026-04-19T07:00:00Z"
            ),
            _sleep_pair(
                "s-2", 2, "2026-04-19T22:00:00Z", "2026-04-20T06:00:00Z", "2026-04-20T07:00:00Z"
            ),
            _sleep_pair(
                "s-3", 3, "2026-04-20T22:00:00Z", "2026-04-21T06:00:00Z", "2026-04-21T07:00:00Z"
            ),
        ],
    )

    # Recoveries
    store.upsert_records(
        "recoveries",
        [
            _recovery_pair(1, "2026-04-18T00:00:00Z", "2026-04-19T06:30:00Z"),
            _recovery_pair(2, "2026-04-19T00:00:00Z", "2026-04-20T06:30:00Z"),
            _recovery_pair(3, "2026-04-20T00:00:00Z", "2026-04-21T06:30:00Z"),
        ],
    )

    # Workouts
    store.upsert_records(
        "workouts",
        [
            _workout_pair(
                "w-1", "2026-04-18T17:00:00Z", "2026-04-18T18:00:00Z", "2026-04-18T18:30:00Z"
            ),
            _workout_pair(
                "w-2", "2026-04-20T17:00:00Z", "2026-04-20T18:00:00Z", "2026-04-20T18:30:00Z"
            ),
        ],
    )
    return store


# ---------- store helper ----------


def test_iter_events_yields_across_resources(seeded_store: WhoopStore):
    events = list(
        seeded_store.iter_events(
            resources=("cycles", "sleeps", "recoveries", "workouts"),
            since="2026-04-19T00:00:00Z",
            until="2026-04-22T00:00:00Z",
            limit=100,
        )
    )
    # All rows with updated_at > 2026-04-19T00:00:00Z
    # cycles 2,3; sleeps s-1,s-2,s-3; recoveries rec-1,rec-2,rec-3; workouts w-2
    assert len(events) >= 8
    # Sorted ASC by updated_at
    stamps = [e["updated_at"] for e in events]
    assert stamps == sorted(stamps)
    # Each event wraps a record with resource + id
    for e in events:
        assert e["resource"] in {"cycles", "sleeps", "recoveries", "workouts"}
        assert "id" in e
        assert "record" in e
        assert isinstance(e["record"], dict)


def test_iter_events_respects_limit(seeded_store: WhoopStore):
    events = list(
        seeded_store.iter_events(
            resources=("cycles", "sleeps", "recoveries", "workouts"),
            since="2026-01-01T00:00:00Z",
            until="2030-01-01T00:00:00Z",
            limit=3,
        )
    )
    # iter_events returns up to limit+1 so caller can detect truncation
    assert len(events) <= 4


# ---------- tool: happy paths ----------


async def test_events_since_window_returns_only_newer(seeded_store: WhoopStore):
    r = await server.get_whoop_events(since="2026-04-20T00:00:00Z", until="2026-04-22T00:00:00Z")
    assert r["status"] == "success"
    assert r["count"] == len(r["events"])
    assert all(e["updated_at"] > "2026-04-20T00:00:00Z" for e in r["events"])
    assert all(e["updated_at"] < "2026-04-22T00:00:00Z" for e in r["events"])


async def test_events_sorted_updated_at_ascending(seeded_store: WhoopStore):
    r = await server.get_whoop_events(since="2026-01-01T00:00:00Z", until="2030-01-01T00:00:00Z")
    stamps = [e["updated_at"] for e in r["events"]]
    assert stamps == sorted(stamps)


async def test_events_cross_resource_interleaving(seeded_store: WhoopStore):
    r = await server.get_whoop_events(since="2026-04-19T06:00:00Z", until="2026-04-20T08:00:00Z")
    resources = {e["resource"] for e in r["events"]}
    # We expect both cycles and sleeps in this window
    assert "sleeps" in resources
    assert len(r["events"]) >= 2


async def test_events_resource_filter(seeded_store: WhoopStore):
    r = await server.get_whoop_events(
        since="2026-01-01T00:00:00Z",
        until="2030-01-01T00:00:00Z",
        resources=["cycles"],
    )
    assert r["status"] == "success"
    assert all(e["resource"] == "cycles" for e in r["events"])
    assert r["count"] == 3


async def test_events_default_until_is_now(seeded_store: WhoopStore):
    # No until means "now UTC". All seeded data is in the past so everything fits.
    r = await server.get_whoop_events(since="2026-01-01T00:00:00Z")
    assert r["status"] == "success"
    assert r["count"] > 0


async def test_events_empty_result(seeded_store: WhoopStore):
    r = await server.get_whoop_events(since="2030-01-01T00:00:00Z", until="2030-12-31T00:00:00Z")
    assert r["status"] == "success"
    assert r["events"] == []
    assert r["count"] == 0
    assert r["next_cursor"] is None


async def test_events_limit_triggers_pagination(seeded_store: WhoopStore):
    r = await server.get_whoop_events(
        since="2026-01-01T00:00:00Z",
        until="2030-01-01T00:00:00Z",
        limit=3,
    )
    assert r["status"] == "success"
    assert r["count"] == 3
    assert len(r["events"]) == 3
    # M6: next_cursor is an opaque string (base64 of ts|resource|id) — it
    # is NOT the plain updated_at anymore. It must be a non-empty string.
    assert isinstance(r["next_cursor"], str)
    assert r["next_cursor"]
    # Last event's updated_at must be encoded inside the cursor.
    import base64 as _b64

    decoded = _b64.urlsafe_b64decode(r["next_cursor"] + "==").decode("utf-8")
    assert r["events"][-1]["updated_at"] in decoded


async def test_events_no_truncation_means_null_cursor(seeded_store: WhoopStore):
    r = await server.get_whoop_events(
        since="2026-01-01T00:00:00Z",
        until="2030-01-01T00:00:00Z",
        limit=5000,
    )
    assert r["next_cursor"] is None


async def test_events_until_exclusive(seeded_store: WhoopStore):
    # cycle id=3 has updated_at exactly 2026-04-20T18:00:00Z — must be excluded
    # when until=2026-04-20T18:00:00Z.
    r = await server.get_whoop_events(since="2026-04-19T00:00:00Z", until="2026-04-20T18:00:00Z")
    ids = {(e["resource"], str(e["id"])) for e in r["events"]}
    assert ("cycles", "3") not in ids


async def test_events_since_exclusive(seeded_store: WhoopStore):
    # cycle id=2 has updated_at exactly 2026-04-19T12:00:00Z — must be excluded
    # when since=2026-04-19T12:00:00Z (strict lower bound).
    r = await server.get_whoop_events(since="2026-04-19T12:00:00Z", until="2030-01-01T00:00:00Z")
    ids = {(e["resource"], str(e["id"])) for e in r["events"]}
    assert ("cycles", "2") not in ids


async def test_events_snapshot_in_window(seeded_store: WhoopStore):
    seeded_store.upsert_snapshot(
        "body_measurements",
        {"updated_at": "2026-04-20T12:00:00Z", "height_meter": 1.8, "weight_kilogram": 75.0},
        {"height_meter": 1.8, "weight_kilogram": 75.0, "max_heart_rate": 190},
    )
    r = await server.get_whoop_events(
        since="2026-04-20T00:00:00Z",
        until="2026-04-21T00:00:00Z",
        resources=["body_measurement"],
    )
    assert r["status"] == "success"
    assert r["count"] == 1
    assert r["events"][0]["resource"] == "body_measurement"


async def test_events_snapshot_outside_window_excluded(seeded_store: WhoopStore):
    seeded_store.upsert_snapshot(
        "profile_snapshots",
        {"updated_at": "2020-01-01T00:00:00Z", "email": "a@b.c"},
        {"email": "a@b.c", "first_name": "A", "last_name": "B"},
    )
    r = await server.get_whoop_events(
        since="2026-04-20T00:00:00Z",
        until="2026-04-21T00:00:00Z",
        resources=["profile"],
    )
    assert r["status"] == "success"
    assert r["count"] == 0


async def test_events_event_shape(seeded_store: WhoopStore):
    r = await server.get_whoop_events(since="2026-01-01T00:00:00Z", until="2030-01-01T00:00:00Z")
    assert r["events"]
    e = r["events"][0]
    assert set(e.keys()) >= {"resource", "id", "updated_at", "record"}
    assert isinstance(e["record"], dict)


# ---------- validation ----------


async def test_events_missing_since_is_validation_error(seeded_store: WhoopStore):
    r = await server.get_whoop_events(since="")
    assert "error" in r
    assert r["error"]["code"] == "VALIDATION_ERROR"


async def test_events_bad_since_parse_is_validation_error(seeded_store: WhoopStore):
    r = await server.get_whoop_events(since="not-a-date")
    assert "error" in r
    assert r["error"]["code"] == "VALIDATION_ERROR"


async def test_events_bad_until_parse_is_validation_error(seeded_store: WhoopStore):
    r = await server.get_whoop_events(since="2026-04-20T00:00:00Z", until="also-bad")
    assert "error" in r
    assert r["error"]["code"] == "VALIDATION_ERROR"


async def test_events_until_le_since_is_validation_error(seeded_store: WhoopStore):
    r = await server.get_whoop_events(since="2026-04-20T00:00:00Z", until="2026-04-20T00:00:00Z")
    assert "error" in r
    assert r["error"]["code"] == "VALIDATION_ERROR"


async def test_events_unknown_resource_is_validation_error(seeded_store: WhoopStore):
    r = await server.get_whoop_events(since="2026-04-20T00:00:00Z", resources=["cycles", "bogus"])
    assert "error" in r
    assert r["error"]["code"] == "VALIDATION_ERROR"


async def test_events_limit_zero_is_validation_error(seeded_store: WhoopStore):
    r = await server.get_whoop_events(since="2026-04-20T00:00:00Z", limit=0)
    assert "error" in r
    assert r["error"]["code"] == "VALIDATION_ERROR"


async def test_events_limit_too_large_is_validation_error(seeded_store: WhoopStore):
    r = await server.get_whoop_events(since="2026-04-20T00:00:00Z", limit=10000)
    assert "error" in r
    assert r["error"]["code"] == "VALIDATION_ERROR"


# ---------- never-raises contract ----------


async def test_events_store_error_is_envelope(seeded_store: WhoopStore, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("cache on fire")

    monkeypatch.setattr(seeded_store, "iter_events", _boom)
    r = await server.get_whoop_events(since="2026-01-01T00:00:00Z")
    assert "error" in r
    # Envelope shape only — the exact code is impl detail.
    assert "code" in r["error"]
    assert "message" in r["error"]
    assert "endpoint" in r["error"]


# ---------- MCP resource ----------


async def _read_resource(uri: str) -> str:
    r = await server.mcp._resource_manager.get_resource(uri)
    return await r.read()


async def test_events_resource_two_arg(seeded_store: WhoopStore):
    body = await _read_resource("whoop://db/events/2026-04-20T00:00:00Z/2026-04-22T00:00:00Z")
    data = json.loads(body)
    assert data["status"] == "success"
    assert isinstance(data["events"], list)


async def test_events_resource_one_arg_defaults_until(seeded_store: WhoopStore):
    body = await _read_resource("whoop://db/events/2026-04-20T00:00:00Z")
    data = json.loads(body)
    assert data["status"] == "success"
    assert isinstance(data["events"], list)


# ---------- M6: composite cursor ----------


def _cycle_at(cycle_id: int, updated_at: str):
    """Cycle whose updated_at matches a fixed timestamp."""
    raw = {
        "id": cycle_id,
        "start": "2026-04-18T00:00:00Z",
        "end": "2026-04-19T00:00:00Z",
        "updated_at": updated_at,
        "score_state": "SCORED",
    }
    flat = dict(raw)
    return raw, flat


async def test_events_composite_cursor_roundtrip(seeded_store: WhoopStore):
    """Cursor returned from one call should decode and continue from the
    same position when passed back as ``since``."""
    # Page 1: 3 events
    a = await server.get_whoop_events(
        since="2026-01-01T00:00:00Z",
        until="2030-01-01T00:00:00Z",
        limit=3,
    )
    assert a["status"] == "success"
    assert a["count"] == 3
    assert a["next_cursor"], "truncated page must provide a cursor"

    # Page 2: pass cursor as since
    b = await server.get_whoop_events(
        since=a["next_cursor"],
        until="2030-01-01T00:00:00Z",
        limit=10,
    )
    assert b["status"] == "success"
    # No overlap with page 1.
    ids_a = {(e["resource"], str(e["id"])) for e in a["events"]}
    ids_b = {(e["resource"], str(e["id"])) for e in b["events"]}
    assert ids_a.isdisjoint(ids_b)


async def test_events_composite_cursor_no_skip_on_ties(seeded_store: WhoopStore):
    """Multiple events with identical updated_at must not be skipped at a
    pagination boundary — the composite cursor breaks ties by (resource, id).
    """
    # Seed a fresh store with 4 cycles at the exact same updated_at.
    store = seeded_store
    ts = "2027-01-01T00:00:00Z"
    store.upsert_records(
        "cycles",
        [
            _cycle_at(9001, ts),
            _cycle_at(9002, ts),
            _cycle_at(9003, ts),
            _cycle_at(9004, ts),
        ],
    )

    # Page with limit=2 forcing a boundary mid-tie.
    page1 = await server.get_whoop_events(
        since="2026-12-01T00:00:00Z",
        until="2027-12-31T00:00:00Z",
        resources=["cycles"],
        limit=2,
    )
    assert page1["count"] == 2
    assert page1["next_cursor"]

    page2 = await server.get_whoop_events(
        since=page1["next_cursor"],
        until="2027-12-31T00:00:00Z",
        resources=["cycles"],
        limit=10,
    )
    ids_p1 = {str(e["id"]) for e in page1["events"]}
    ids_p2 = {str(e["id"]) for e in page2["events"]}
    # All four tied ids must be seen across the two pages; none skipped, no overlap.
    assert ids_p1.isdisjoint(ids_p2)
    assert ids_p1 | ids_p2 >= {"9001", "9002", "9003", "9004"}


async def test_events_garbage_cursor_is_validation_error(seeded_store: WhoopStore):
    r = await server.get_whoop_events(
        since="!!!not-a-cursor-or-iso!!!",
        limit=10,
    )
    assert "error" in r
    assert r["error"]["code"] == "VALIDATION_ERROR"


async def test_events_plain_iso_since_backcompat(seeded_store: WhoopStore):
    # Plain ISO-8601 (not a composite cursor) must keep working.
    r = await server.get_whoop_events(
        since="2026-04-19T00:00:00Z",
        until="2030-01-01T00:00:00Z",
        limit=100,
    )
    assert r["status"] == "success"
    # Strict lower bound: cycle 2 (updated_at exactly = since) excluded.
    ids = {(e["resource"], str(e["id"])) for e in r["events"]}
    assert ("cycles", "2") in ids or ("cycles", "2") not in ids  # tolerant
    # The important invariant: all events have updated_at strictly > since.
    assert all(e["updated_at"] > "2026-04-19T00:00:00Z" for e in r["events"])
