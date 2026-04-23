"""
Tests for the MCP resource surface added in M3.

We don't spin up an MCP transport; instead we go through FastMCP's
resource manager (``get_resource``) which is exactly how a real MCP
client lookup flows internally.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import whoop_mcp_server as server
from whoop_store import WhoopStore


@pytest.fixture
def tmp_store(tmp_path: Path, monkeypatch) -> WhoopStore:
    path = str(tmp_path / "whoop.db")
    monkeypatch.setenv("WHOOP_DB_PATH", path)
    server._store = None
    s = server._get_store()
    yield s
    server._store = None


async def _read_resource(uri: str) -> str:
    r = await server.mcp._resource_manager.get_resource(uri)
    return await r.read()


@pytest.mark.asyncio
async def test_cycles_resource_returns_json_array(tmp_store: WhoopStore):
    raw = {
        "id": 1,
        "start": "2026-04-20T00:00:00Z",
        "end": "2026-04-21T00:00:00Z",
        "updated_at": "2026-04-21T06:00:00Z",
        "score_state": "SCORED",
    }
    flat = {
        "id": 1,
        "start_utc": raw["start"],
        "end_utc": raw["end"],
        "start_local": None,
        "end_local": None,
        "timezone_offset": None,
        "score_state": "SCORED",
    }
    tmp_store.upsert_records("cycles", [(raw, flat)])

    body = await _read_resource("whoop://db/cycles/2026-04-19/2026-04-22")
    data = json.loads(body)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["id"] == 1


@pytest.mark.asyncio
async def test_cycles_resource_bad_dates_returns_error_body(tmp_store: WhoopStore):
    body = await _read_resource("whoop://db/cycles/not-a-date/also-bad")
    data = json.loads(body)
    assert "error" in data
    assert data["error"]["code"] in {"VALIDATION_ERROR", "CACHE_ERROR"}


@pytest.mark.asyncio
async def test_profile_resource_returns_snapshot(tmp_store: WhoopStore):
    tmp_store.upsert_snapshot(
        "profile_snapshots",
        {"email": "a@b.c"},
        {"email": "a@b.c", "first_name": None, "last_name": None},
    )
    body = await _read_resource("whoop://db/profile")
    data = json.loads(body)
    assert data["email"] == "a@b.c"


@pytest.mark.asyncio
async def test_sync_runs_resource(tmp_store: WhoopStore):
    # Insert a couple of sync_run rows
    for i in range(3):
        rid = tmp_store.start_sync_run("cycles")
        tmp_store.finish_sync_run(rid, status="success", records_fetched=i, records_upserted=i)

    body = await _read_resource("whoop://db/sync_runs/2")
    data = json.loads(body)
    assert isinstance(data, list)
    assert len(data) == 2
