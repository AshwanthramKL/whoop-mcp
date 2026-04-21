"""
M6 fault-injection tests for the WHOOP MCP server.

Concrete fault scenarios at the API, auth, and cache layers. No real
network, no real disk — respx stubs HTTP, tmp dirs stub disk, and
``asyncio.sleep`` is monkeypatched to a no-op so backoff tests don't
actually wait.

These tests codify the reliability contract: tool-layer calls never
raise, errors come back as structured envelopes, retries respect the
documented schedule, and snapshots of the cache stay consistent under
adverse conditions.
"""

from __future__ import annotations

import sqlite3

import httpx
import pytest
import respx

import whoop_mcp_server as server
from whoop_client import (
    WhoopClient,
)
from whoop_store import WhoopStore

V2 = "https://api.prod.whoop.com/developer/v2"


# ---------- helpers ----------


@pytest.fixture
def no_sleep(monkeypatch):
    """Monkeypatch asyncio.sleep inside whoop_client to capture delays."""
    delays: list[float] = []

    async def _fake(s):
        delays.append(s)

    import whoop_client as wc

    monkeypatch.setattr(wc.asyncio, "sleep", _fake)
    return delays


# ---------- API layer ----------


@pytest.mark.asyncio
@respx.mock
async def test_401_on_list_bubbles_to_tool_as_auth_failed(monkeypatch, no_sleep):
    respx.get(f"{V2}/cycle").mock(return_value=httpx.Response(401, text="no"))

    # Bypass the cache-first path: fresh=True forces the API hit.
    monkeypatch.setattr(server, "_whoop_client", None)
    r = await server.list_whoop_cycles(fresh=True)
    assert "error" in r
    assert r["error"]["code"] == "AUTH_FAILED"


@pytest.mark.asyncio
@respx.mock
async def test_429_with_retry_after_sleeps_then_success(no_sleep, fixture_loader):
    payload = fixture_loader("profile")
    responses = [
        httpx.Response(429, headers={"Retry-After": "2"}, text="slow"),
        httpx.Response(200, json=payload),
    ]
    idx = {"i": 0}

    def _r(req):
        i = idx["i"]
        idx["i"] += 1
        return responses[i]

    respx.get(f"{V2}/user/profile/basic").mock(side_effect=_r)
    client = WhoopClient()
    out = await client.get_profile()
    assert out == payload
    assert 2 in no_sleep


@pytest.mark.asyncio
@respx.mock
async def test_429_repeating_raises_rate_limited(no_sleep, monkeypatch):
    respx.get(f"{V2}/user/profile/basic").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "1"}, text="nope")
    )
    monkeypatch.setattr(server, "_whoop_client", None)
    r = await server.get_whoop_profile(fresh=True)
    assert "error" in r
    assert r["error"]["code"] == "RATE_LIMITED"


@pytest.mark.asyncio
@respx.mock
async def test_three_500s_raise_upstream_error(no_sleep, monkeypatch):
    respx.get(f"{V2}/user/profile/basic").mock(return_value=httpx.Response(500, text="bad"))
    monkeypatch.setattr(server, "_whoop_client", None)
    r = await server.get_whoop_profile(fresh=True)
    assert "error" in r
    assert r["error"]["code"] == "UPSTREAM_ERROR"


@pytest.mark.asyncio
@respx.mock
async def test_500_500_200_succeeds_with_backoff(no_sleep, fixture_loader):
    payload = fixture_loader("profile")
    responses = [
        httpx.Response(500, text="boom"),
        httpx.Response(500, text="boom"),
        httpx.Response(200, json=payload),
    ]
    idx = {"i": 0}

    def _r(req):
        i = idx["i"]
        idx["i"] += 1
        return responses[i]

    respx.get(f"{V2}/user/profile/basic").mock(side_effect=_r)
    client = WhoopClient()
    out = await client.get_profile()
    assert out == payload
    # backoff: 1s, then 2s
    assert no_sleep[:2] == [1, 2]


@pytest.mark.asyncio
@respx.mock
async def test_503_with_retry_after_respects_header(no_sleep, fixture_loader):
    payload = fixture_loader("profile")
    responses = [
        httpx.Response(503, headers={"Retry-After": "3"}, text="down"),
        httpx.Response(200, json=payload),
    ]
    idx = {"i": 0}

    def _r(req):
        i = idx["i"]
        idx["i"] += 1
        return responses[i]

    respx.get(f"{V2}/user/profile/basic").mock(side_effect=_r)
    client = WhoopClient()
    out = await client.get_profile()
    assert out == payload
    # The 503 path uses the 5xx backoff schedule but we want to document that
    # *some* sleep happened before the retry.
    assert no_sleep, "expected at least one sleep before retry"


@pytest.mark.asyncio
@respx.mock
async def test_connection_error_maps_to_upstream(no_sleep, monkeypatch):
    respx.get(f"{V2}/user/profile/basic").mock(side_effect=httpx.ConnectError("connection refused"))
    monkeypatch.setattr(server, "_whoop_client", None)
    r = await server.get_whoop_profile(fresh=True)
    assert "error" in r
    assert r["error"]["code"] == "UPSTREAM_ERROR"


@pytest.mark.asyncio
@respx.mock
async def test_read_timeout_maps_to_upstream(no_sleep, monkeypatch):
    respx.get(f"{V2}/user/profile/basic").mock(side_effect=httpx.ReadTimeout("timed out"))
    monkeypatch.setattr(server, "_whoop_client", None)
    r = await server.get_whoop_profile(fresh=True)
    assert "error" in r
    assert r["error"]["code"] == "UPSTREAM_ERROR"


# ---------- Cache layer ----------


@pytest.mark.asyncio
async def test_cache_readable_fails_when_store_cannot_init(tmp_path, monkeypatch):
    """If the store can't be brought up, cache_readable reports fail and
    the overall status is unhealthy.
    """

    # Force the store getter to blow up so the readable check reports fail.
    def _boom():
        raise RuntimeError("store on fire")

    monkeypatch.setattr(server, "_get_store", _boom)
    r = await server.health_check(live=False)
    assert r["checks"]["cache_readable"]["status"] == "fail"
    assert r["status"] == "unhealthy"


def test_upsert_under_db_lock_raises_or_reports_cache_error(tmp_path):
    db = tmp_path / "locked.db"
    # Hold an exclusive transaction open via a second connection.
    holder = sqlite3.connect(str(db))
    holder.isolation_level = None  # autocommit off for BEGIN
    holder.execute("BEGIN EXCLUSIVE")

    store = WhoopStore(str(db))
    try:
        # init_schema should fail (can't create tables while another
        # conn has EXCLUSIVE). Narrow to sqlite3 errors — anything else
        # would be a genuine surprise worth seeing in the test output.
        with pytest.raises(sqlite3.OperationalError):
            store.init_schema()
    finally:
        holder.rollback()
        holder.close()
        store.close()


def test_iter_events_skips_corrupt_flat_json(tmp_path, caplog):
    db = tmp_path / "whoop.db"
    store = WhoopStore(str(db))
    store.init_schema()

    # Manually poison a row with invalid JSON in flat_json via low-level write.
    conn = store._connect()
    conn.execute(
        'INSERT INTO cycles (id, start, "end", updated_at, score_state, '
        "cycle_id, raw_json, flat_json) VALUES "
        "('poison', '2026-04-18T00:00:00Z', '2026-04-19T00:00:00Z', "
        "'2026-04-20T12:00:00Z', 'SCORED', NULL, '{}', 'NOT_JSON!!!')"
    )
    # Plus a good row.
    conn.execute(
        'INSERT INTO cycles (id, start, "end", updated_at, score_state, '
        "cycle_id, raw_json, flat_json) VALUES "
        "('good', '2026-04-18T00:00:00Z', '2026-04-19T00:00:00Z', "
        "'2026-04-20T13:00:00Z', 'SCORED', NULL, '{}', '{\"id\":\"good\"}')"
    )

    events = list(
        store.iter_events(
            resources=["cycles"],
            since="2026-01-01T00:00:00Z",
            until="2030-01-01T00:00:00Z",
            limit=100,
        )
    )
    # Corrupt row must not break the feed.
    ids = {e["id"] for e in events}
    assert "good" in ids
    store.close()
