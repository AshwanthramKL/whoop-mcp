"""
Tests for cache-first reads in the MCP tool layer.

The list/get tools now accept ``fresh: bool``. By default they consult the
cache first and only fall back to an API call (and then upsert the results)
when the cache is empty for the requested window.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import whoop_mcp_server as server
from whoop_store import WhoopStore


@pytest.fixture
def tmp_store(tmp_path: Path, monkeypatch) -> WhoopStore:
    path = str(tmp_path / "whoop.db")
    monkeypatch.setenv("WHOOP_DB_PATH", path)
    # Reset the server's cached store singleton
    server._store = None
    s = server._get_store()
    yield s
    server._store = None


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
    monkeypatch.setattr(server, "_get_client", lambda: fake)
    return fake


def _cycle_raw(cid: int, start: str, updated_at: str) -> dict:
    return {
        "id": cid,
        "start": start,
        "end": start.replace("T00:", "T23:"),
        "updated_at": updated_at,
        "score_state": "SCORED",
        "score": {
            "strain": 5.0,
            "kilojoule": 4000.0,
            "average_heart_rate": 65,
            "max_heart_rate": 130,
        },
    }


# ---------- list cache-first ----------


@pytest.mark.asyncio
async def test_list_cycles_fresh_false_reads_from_cache_zero_api(tmp_store, stub_client):
    # Populate cache directly
    raw = _cycle_raw(1, "2026-04-20T00:00:00Z", "2026-04-21T06:00:00Z")
    from whoop_models import Cycle

    flat = Cycle.model_validate(raw).flatten()
    tmp_store.upsert_records("cycles", [(raw, flat)])

    out = await server.list_whoop_cycles(fresh=False)
    assert "records" in out
    assert len(out["records"]) == 1
    assert out["records"][0]["id"] == 1
    stub_client.list_cycles.assert_not_called()


@pytest.mark.asyncio
async def test_list_cycles_fresh_true_hits_api_and_upserts(tmp_store, stub_client):
    raw = _cycle_raw(5, "2026-04-20T00:00:00Z", "2026-04-21T06:00:00Z")
    stub_client.list_cycles.return_value = [raw]

    out = await server.list_whoop_cycles(fresh=True)
    assert "records" in out
    assert out["records"][0]["id"] == 5
    stub_client.list_cycles.assert_called_once()

    # Was upserted into cache
    cached = tmp_store.query_range("cycles", start=None, end=None)
    assert len(cached) == 1


@pytest.mark.asyncio
async def test_list_cycles_empty_cache_autosyncs_then_reads_cache(tmp_store, stub_client):
    raw = _cycle_raw(7, "2026-04-20T00:00:00Z", "2026-04-21T06:00:00Z")
    stub_client.list_cycles.return_value = [raw]

    out = await server.list_whoop_cycles(
        start="2026-04-20T00:00:00Z",
        end="2026-04-22T00:00:00Z",
        fresh=False,
    )
    # Auto-sync pulled from API
    stub_client.list_cycles.assert_called_once()
    assert "records" in out
    assert len(out["records"]) == 1


# ---------- get_*: cache-first single record ----------


@pytest.mark.asyncio
async def test_get_profile_fresh_false_reads_snapshot(tmp_store, stub_client):
    tmp_store.upsert_snapshot(
        "profile_snapshots",
        {"email": "x@y.z", "first_name": "Ash"},
        {"email": "x@y.z", "first_name": "Ash", "last_name": None},
    )
    out = await server.get_whoop_profile(fresh=False)
    assert out["email"] == "x@y.z"
    stub_client.get_profile.assert_not_called()


@pytest.mark.asyncio
async def test_get_profile_fresh_true_bypasses_cache(tmp_store, stub_client):
    stub_client.get_profile.return_value = {
        "email": "a@b.c",
        "first_name": "A",
        "last_name": "B",
        "user_id": "<R>",
    }
    out = await server.get_whoop_profile(fresh=True)
    stub_client.get_profile.assert_called_once()
    assert out["email"] == "a@b.c"
    # Upserted
    snap = tmp_store.get_latest_snapshot("profile_snapshots")
    assert snap is not None and snap["email"] == "a@b.c"
