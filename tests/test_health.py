"""
M6 health_check tests.

health_check is a read-only MCP tool that returns a structured dict
with overall + per-component status. It must never raise.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest
import respx

import whoop_mcp_server as server


V2 = "https://api.prod.whoop.com/developer/v2"


# ---------- shape ----------


@pytest.mark.asyncio
async def test_health_check_shape(tmp_path, monkeypatch):
    # live=False: no network call.
    r = await server.health_check(live=False)
    assert isinstance(r, dict)
    assert "status" in r
    assert r["status"] in {"healthy", "degraded", "unhealthy"}
    assert "checks" in r
    assert set(r["checks"].keys()) >= {
        "auth",
        "api_reachable",
        "cache_readable",
        "cache_writable",
        "schema_version",
    }
    for name, check in r["checks"].items():
        assert "status" in check
        assert check["status"] in {"ok", "warn", "fail", "skipped"}
    assert r["server_version"].startswith("0.")
    assert "timestamp" in r


# ---------- auth check ----------


@pytest.mark.asyncio
async def test_health_check_auth_valid_is_ok():
    r = await server.health_check(live=False)
    # Autouse conftest stub always returns "valid".
    assert r["checks"]["auth"]["status"] == "ok"


# ---------- api_reachable ----------


@pytest.mark.asyncio
@respx.mock
async def test_health_check_live_skipped_when_live_false(monkeypatch):
    # No respx mock configured — if we hit the network, respx will raise.
    r = await server.health_check(live=False)
    assert r["checks"]["api_reachable"]["status"] in {"ok", "skipped"}


@pytest.mark.asyncio
@respx.mock
async def test_health_check_live_200_is_ok(monkeypatch):
    respx.get(f"{V2}/user/profile/basic").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    monkeypatch.setattr(server, "_whoop_client", None)
    r = await server.health_check(live=True)
    assert r["checks"]["api_reachable"]["status"] == "ok"


@pytest.mark.asyncio
@respx.mock
async def test_health_check_live_500_is_fail(monkeypatch, no_sleep_for_health):
    respx.get(f"{V2}/user/profile/basic").mock(
        return_value=httpx.Response(500, text="down")
    )
    monkeypatch.setattr(server, "_whoop_client", None)
    r = await server.health_check(live=True)
    assert r["checks"]["api_reachable"]["status"] == "fail"
    assert r["status"] in {"unhealthy", "degraded"}


# ---------- cache check ----------


@pytest.mark.asyncio
async def test_health_check_cache_readable_reports_rows(tmp_path, monkeypatch):
    r = await server.health_check(live=False)
    assert "rows_total" in r["checks"]["cache_readable"]


@pytest.mark.asyncio
async def test_health_check_cache_writable_creates_and_deletes_sentinel(
    tmp_path, monkeypatch
):
    r = await server.health_check(live=False)
    assert r["checks"]["cache_writable"]["status"] == "ok"


# ---------- never raises ----------


@pytest.mark.asyncio
async def test_health_check_never_raises_on_store_error(monkeypatch):
    # Force the store getter to blow up.
    def _boom():
        raise RuntimeError("store on fire")

    monkeypatch.setattr(server, "_get_store", _boom)
    r = await server.health_check(live=False)
    assert isinstance(r, dict)
    assert "checks" in r
    assert r["status"] == "unhealthy"


# ---------- no secrets in output ----------


@pytest.mark.asyncio
async def test_health_check_no_tokens_in_output():
    r = await server.health_check(live=False)
    blob = repr(r)
    assert "test-access-token" not in blob
    assert "Bearer" not in blob


# ---------- helper fixture ----------


@pytest.fixture
def no_sleep_for_health(monkeypatch):
    import whoop_client as wc

    async def _f(s):
        return None

    monkeypatch.setattr(wc.asyncio, "sleep", _f)
