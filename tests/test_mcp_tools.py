"""
Tests for the M1 MCP tool layer.

We patch the underlying WhoopClient to isolate the tool functions from HTTP,
then verify (a) error wrapping, (b) tools never raise, (c) FastMCP schema
generation shows the expected params for two representative tools.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import whoop_mcp_server as server
from whoop_client import (
    AuthError,
    NotFoundError,
    RateLimitError,
    UpstreamError,
    ValidationError,
)

# ----- helpers -----


@pytest.fixture
def stub_client(monkeypatch):
    """Replace the module-level client with a MagicMock that has async methods."""
    fake = MagicMock()
    # All list_* / get_* methods are async
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
    fake.get_auth_status = MagicMock(return_value={"status": "valid"})

    monkeypatch.setattr(server, "_get_client", lambda: fake)
    return fake


# ----- happy path + error wrapping -----


@pytest.mark.asyncio
async def test_get_whoop_profile_happy(stub_client):
    stub_client.get_profile.return_value = {
        "email": "x@y.z",
        "first_name": "Ash",
        "last_name": "K",
        "user_id": "<REDACTED>",
    }
    out = await server.get_whoop_profile()
    # M2: user_id stripped, rest flattened through
    assert "user_id" not in out
    assert out["email"] == "x@y.z"
    assert out["first_name"] == "Ash"


@pytest.mark.asyncio
async def test_get_whoop_cycle_flatten(stub_client, fixture_loader):
    stub_client.get_cycle.return_value = fixture_loader("cycle_single")
    out = await server.get_whoop_cycle(cycle_id=1446265073)
    assert out["id"] == 1446265073
    assert "user_id" not in out
    assert "score" not in out  # lifted up
    assert out["score_state"] == "SCORED"
    assert out["avg_hr_bpm"] == 67
    assert "calories" in out


@pytest.mark.asyncio
async def test_list_whoop_cycles_records_are_flattened(stub_client, fixture_loader):
    page = fixture_loader("cycles_page")
    stub_client.list_cycles.return_value = page["records"]
    out = await server.list_whoop_cycles()
    assert "records" in out
    first = out["records"][0]
    assert "user_id" not in first
    assert "score" not in first
    assert "avg_hr_bpm" in first


@pytest.mark.asyncio
async def test_list_whoop_recoveries_flattened(stub_client, fixture_loader):
    page = fixture_loader("recoveries_page")
    stub_client.list_recoveries.return_value = page["records"]
    out = await server.list_whoop_recoveries()
    first = out["records"][0]
    assert "user_id" not in first
    assert "score" not in first
    assert "hrv_rmssd_ms" in first
    assert "resting_heart_rate_bpm" in first


@pytest.mark.asyncio
async def test_list_whoop_sleeps_flattened(stub_client, fixture_loader):
    page = fixture_loader("sleeps_page")
    stub_client.list_sleeps.return_value = page["records"]
    out = await server.list_whoop_sleeps()
    first = out["records"][0]
    assert "score" not in first
    assert "in_bed_seconds" in first
    assert "deep_sleep_seconds" in first


@pytest.mark.asyncio
async def test_list_whoop_workouts_flattened(stub_client, fixture_loader):
    page = fixture_loader("workouts_page")
    stub_client.list_workouts.return_value = page["records"]
    out = await server.list_whoop_workouts()
    first = out["records"][0]
    assert "user_id" not in first
    assert "score" not in first
    assert "calories" in first


@pytest.mark.asyncio
async def test_tool_auth_failed_error_mapping(stub_client):
    stub_client.get_profile.side_effect = AuthError(
        "AUTH_FAILED", 401, "bad token", "/user/profile/basic"
    )
    out = await server.get_whoop_profile()
    assert out["error"]["code"] == "AUTH_FAILED"
    assert "endpoint" in out["error"]


@pytest.mark.asyncio
async def test_tool_rate_limited_error_mapping(stub_client):
    stub_client.list_cycles.side_effect = RateLimitError("RATE_LIMITED", 429, "slow", "/cycle")
    out = await server.list_whoop_cycles()
    assert out["error"]["code"] == "RATE_LIMITED"


@pytest.mark.asyncio
async def test_tool_not_found_error_mapping(stub_client):
    stub_client.get_cycle.side_effect = NotFoundError("NOT_FOUND", 404, "no", "/cycle/1")
    out = await server.get_whoop_cycle(cycle_id=1)
    assert out["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_tool_upstream_error_mapping(stub_client):
    stub_client.list_workouts.side_effect = UpstreamError(
        "UPSTREAM_ERROR", 503, "down", "/activity/workout"
    )
    out = await server.list_whoop_workouts()
    assert out["error"]["code"] == "UPSTREAM_ERROR"


@pytest.mark.asyncio
async def test_tool_validation_error_mapping(stub_client):
    stub_client.list_sleeps.side_effect = ValidationError(
        "VALIDATION_ERROR", 0, "bad date", "/activity/sleep"
    )
    out = await server.list_whoop_sleeps(start="not-a-date")
    assert out["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_tool_unexpected_exception_maps_to_upstream(stub_client):
    stub_client.get_body_measurement.side_effect = RuntimeError("boom")
    out = await server.get_whoop_body_measurement()
    assert out["error"]["code"] == "UPSTREAM_ERROR"
    # Must not leak a traceback, only the message string
    assert "Traceback" not in out["error"]["message"]


@pytest.mark.asyncio
async def test_all_list_tools_never_raise(stub_client):
    for tool, method in [
        (server.list_whoop_cycles, "list_cycles"),
        (server.list_whoop_recoveries, "list_recoveries"),
        (server.list_whoop_sleeps, "list_sleeps"),
        (server.list_whoop_workouts, "list_workouts"),
    ]:
        getattr(stub_client, method).side_effect = RuntimeError("boom")
        out = await tool()
        assert "error" in out


# ----- FastMCP schema checks -----


def _tool_schema(tool_name: str) -> dict:
    """Pull the FastMCP input schema for a registered tool."""
    tm = server.mcp._tool_manager
    # FastMCP stores tools keyed by name in _tools
    tool = tm._tools[tool_name]
    return tool.parameters


def test_list_cycles_schema_has_start_end_limit():
    schema = _tool_schema("list_whoop_cycles")
    props = schema.get("properties", {})
    assert "start" in props
    assert "end" in props
    assert "limit" in props
    # These should all be optional, so nothing in required
    required = schema.get("required", []) or []
    assert "start" not in required
    assert "end" not in required
    assert "limit" not in required


def test_get_cycle_schema_requires_cycle_id():
    schema = _tool_schema("get_whoop_cycle")
    props = schema.get("properties", {})
    assert "cycle_id" in props
    required = schema.get("required", []) or []
    assert "cycle_id" in required


def test_expected_tools_registered():
    expected = {
        "get_whoop_auth_status",
        "get_whoop_profile",
        "get_whoop_body_measurement",
        "list_whoop_cycles",
        "get_whoop_cycle",
        "get_whoop_cycle_sleep",
        "get_whoop_cycle_recovery",
        "list_whoop_recoveries",
        "list_whoop_sleeps",
        "get_whoop_sleep",
        "list_whoop_workouts",
        "get_whoop_workout",
        "get_whoop_daily_summary",
    }
    registered = set(server.mcp._tool_manager._tools.keys())
    missing = expected - registered
    assert not missing, f"Missing tools: {missing}"
