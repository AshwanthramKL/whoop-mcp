"""
Tests for the M1 rewrite of WhoopClient.

Covers the v2 surface, auto-pagination, retry/backoff, and structured errors.
Uses respx to stub httpx without any real network.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from whoop_client import (
    AuthError,
    NotFoundError,
    RateLimitError,
    UpstreamError,
    ValidationError,
    WhoopAPIError,
    WhoopClient,
)

V2 = "https://api.prod.whoop.com/developer/v2"


# ---------- Single-resource endpoints ----------


@pytest.mark.asyncio
@respx.mock
async def test_get_profile_returns_raw_json(fixture_loader):
    payload = fixture_loader("profile")
    respx.get(f"{V2}/user/profile/basic").mock(return_value=httpx.Response(200, json=payload))

    client = WhoopClient()
    result = await client.get_profile()
    assert result == payload


@pytest.mark.asyncio
@respx.mock
async def test_get_body_measurement(fixture_loader):
    payload = fixture_loader("body_measurement")
    respx.get(f"{V2}/user/measurement/body").mock(return_value=httpx.Response(200, json=payload))

    client = WhoopClient()
    result = await client.get_body_measurement()
    assert result == payload


@pytest.mark.asyncio
@respx.mock
async def test_get_cycle(fixture_loader):
    payload = fixture_loader("cycle_single")
    respx.get(f"{V2}/cycle/1446265073").mock(return_value=httpx.Response(200, json=payload))

    client = WhoopClient()
    result = await client.get_cycle(1446265073)
    assert result == payload


@pytest.mark.asyncio
@respx.mock
async def test_get_cycle_sleep(fixture_loader):
    payload = fixture_loader("cycle_sleep")
    respx.get(f"{V2}/cycle/1446265073/sleep").mock(return_value=httpx.Response(200, json=payload))

    client = WhoopClient()
    result = await client.get_cycle_sleep(1446265073)
    assert result == payload


@pytest.mark.asyncio
@respx.mock
async def test_get_cycle_recovery(fixture_loader):
    payload = fixture_loader("cycle_recovery")
    respx.get(f"{V2}/cycle/1446265073/recovery").mock(
        return_value=httpx.Response(200, json=payload)
    )

    client = WhoopClient()
    result = await client.get_cycle_recovery(1446265073)
    assert result == payload


@pytest.mark.asyncio
@respx.mock
async def test_get_sleep_uuid(fixture_loader):
    payload = fixture_loader("sleep_single")
    sid = "bb68db7b-bb56-44ce-ad8a-eb5a7a93b073"
    respx.get(f"{V2}/activity/sleep/{sid}").mock(return_value=httpx.Response(200, json=payload))

    client = WhoopClient()
    result = await client.get_sleep(sid)
    assert result == payload


@pytest.mark.asyncio
@respx.mock
async def test_get_workout_uuid(fixture_loader):
    payload = fixture_loader("workout_single")
    wid = "a3f00067-344d-4d16-811c-b98b71f67b15"
    respx.get(f"{V2}/activity/workout/{wid}").mock(return_value=httpx.Response(200, json=payload))

    client = WhoopClient()
    result = await client.get_workout(wid)
    assert result == payload


# ---------- Pagination ----------


def _page(records: list[dict], next_token: str | None) -> dict:
    return {"records": records, "next_token": next_token}


def _fake_records(n: int, offset: int = 0) -> list[dict]:
    return [{"id": i + offset, "x": i + offset} for i in range(n)]


@pytest.mark.asyncio
@respx.mock
async def test_list_cycles_auto_paginates_three_pages():
    # 25, 25, 10 -> 60 total
    pages = [
        _page(_fake_records(25, 0), "tok1"),
        _page(_fake_records(25, 25), "tok2"),
        _page(_fake_records(10, 50), None),
    ]
    call_idx = {"i": 0}

    def _responder(request: httpx.Request) -> httpx.Response:
        i = call_idx["i"]
        call_idx["i"] += 1
        return httpx.Response(200, json=pages[i])

    respx.get(f"{V2}/cycle").mock(side_effect=_responder)

    client = WhoopClient()
    records = await client.list_cycles()
    assert len(records) == 60
    assert records[0]["id"] == 0
    assert records[-1]["id"] == 59
    assert call_idx["i"] == 3


@pytest.mark.asyncio
@respx.mock
async def test_list_cycles_limit_truncates_across_pages():
    # limit=30 => fetch page 1 (25), page 2 (25), truncate to 30 total
    pages = [
        _page(_fake_records(25, 0), "tok1"),
        _page(_fake_records(25, 25), "tok2"),
    ]
    call_idx = {"i": 0}

    def _responder(request: httpx.Request) -> httpx.Response:
        i = call_idx["i"]
        call_idx["i"] += 1
        return httpx.Response(200, json=pages[i])

    respx.get(f"{V2}/cycle").mock(side_effect=_responder)

    client = WhoopClient()
    records = await client.list_cycles(limit=30)
    assert len(records) == 30
    assert records[0]["id"] == 0
    assert records[-1]["id"] == 29
    # Should stop after the second page since we already have >= 30 records.
    assert call_idx["i"] == 2


@pytest.mark.asyncio
@respx.mock
async def test_list_recoveries_and_sleeps_and_workouts_paginate():
    for path, method in [
        (f"{V2}/recovery", "list_recoveries"),
        (f"{V2}/activity/sleep", "list_sleeps"),
        (f"{V2}/activity/workout", "list_workouts"),
    ]:
        respx.reset()
        pages = [
            _page(_fake_records(25, 0), "t"),
            _page(_fake_records(5, 25), None),
        ]
        calls = {"i": 0}

        def _responder(request: httpx.Request, _pages=pages, _calls=calls) -> httpx.Response:
            i = _calls["i"]
            _calls["i"] += 1
            return httpx.Response(200, json=_pages[i])

        respx.get(path).mock(side_effect=_responder)
        client = WhoopClient()
        records = await getattr(client, method)()
        assert len(records) == 30, f"{method} should return 30 records"


# ---------- Rate limiting and retries ----------


@pytest.mark.asyncio
@respx.mock
async def test_429_retries_after_retry_after_header(monkeypatch, fixture_loader):
    sleep_calls: list[float] = []

    async def _fake_sleep(s):
        sleep_calls.append(s)

    import whoop_client as wc
    monkeypatch.setattr(wc.asyncio, "sleep", _fake_sleep)

    payload = fixture_loader("profile")
    responses = [
        httpx.Response(429, headers={"Retry-After": "2"}, text="slow down"),
        httpx.Response(200, json=payload),
    ]
    call_idx = {"i": 0}

    def _responder(request: httpx.Request) -> httpx.Response:
        i = call_idx["i"]
        call_idx["i"] += 1
        return responses[i]

    respx.get(f"{V2}/user/profile/basic").mock(side_effect=_responder)

    client = WhoopClient()
    result = await client.get_profile()
    assert result == payload
    assert 2 in sleep_calls  # honored Retry-After


@pytest.mark.asyncio
@respx.mock
async def test_429_twice_raises_rate_limit_error(monkeypatch):
    async def _fake_sleep(s):
        return None

    import whoop_client as wc
    monkeypatch.setattr(wc.asyncio, "sleep", _fake_sleep)

    respx.get(f"{V2}/user/profile/basic").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "1"}, text="nope")
    )

    client = WhoopClient()
    with pytest.raises(RateLimitError):
        await client.get_profile()


@pytest.mark.asyncio
@respx.mock
async def test_5xx_exponential_backoff_then_success(monkeypatch, fixture_loader):
    sleeps: list[float] = []

    async def _fake_sleep(s):
        sleeps.append(s)

    import whoop_client as wc
    monkeypatch.setattr(wc.asyncio, "sleep", _fake_sleep)

    payload = fixture_loader("profile")
    responses = [
        httpx.Response(500, text="bad"),
        httpx.Response(502, text="bad"),
        httpx.Response(200, json=payload),
    ]
    call_idx = {"i": 0}

    def _responder(request: httpx.Request) -> httpx.Response:
        i = call_idx["i"]
        call_idx["i"] += 1
        return responses[i]

    respx.get(f"{V2}/user/profile/basic").mock(side_effect=_responder)

    client = WhoopClient()
    result = await client.get_profile()
    assert result == payload
    # Expect exponential backoff sequence before the third try.
    assert sleeps[:2] == [1, 2]


@pytest.mark.asyncio
@respx.mock
async def test_5xx_exhausted_raises_upstream_error(monkeypatch):
    async def _fake_sleep(s):
        return None

    import whoop_client as wc
    monkeypatch.setattr(wc.asyncio, "sleep", _fake_sleep)

    respx.get(f"{V2}/user/profile/basic").mock(return_value=httpx.Response(503, text="down"))

    client = WhoopClient()
    with pytest.raises(UpstreamError):
        await client.get_profile()


@pytest.mark.asyncio
@respx.mock
async def test_404_raises_not_found():
    respx.get(f"{V2}/cycle/99999").mock(return_value=httpx.Response(404, text="nope"))

    client = WhoopClient()
    with pytest.raises(NotFoundError):
        await client.get_cycle(99999)


@pytest.mark.asyncio
@respx.mock
async def test_401_raises_auth_error():
    respx.get(f"{V2}/user/profile/basic").mock(return_value=httpx.Response(401, text="no"))

    client = WhoopClient()
    with pytest.raises(AuthError):
        await client.get_profile()


# ---------- Validation ----------


@pytest.mark.asyncio
async def test_bad_date_raises_validation_error_before_network():
    client = WhoopClient()
    with pytest.raises(ValidationError):
        await client.list_cycles(start="not-a-date")


@pytest.mark.asyncio
async def test_exception_hierarchy():
    # All error subclasses must be WhoopAPIError
    for cls in (AuthError, RateLimitError, NotFoundError, UpstreamError, ValidationError):
        assert issubclass(cls, WhoopAPIError)


# ---------- Query params plumbing ----------


@pytest.mark.asyncio
@respx.mock
async def test_list_cycles_passes_date_params():
    route = respx.get(f"{V2}/cycle").mock(
        return_value=httpx.Response(200, json={"records": [], "next_token": None})
    )
    client = WhoopClient()
    await client.list_cycles(start="2026-04-01T00:00:00Z", end="2026-04-08T00:00:00Z")
    assert route.called
    qp = dict(route.calls.last.request.url.params)
    assert qp["start"] == "2026-04-01T00:00:00Z"
    assert qp["end"] == "2026-04-08T00:00:00Z"
    assert qp["limit"] == "25"


@pytest.mark.asyncio
@respx.mock
async def test_date_only_params_normalized_to_full_iso():
    """Regression: WHOOP v2 /cycle returns 404 for YYYY-MM-DD; client must normalize."""
    route = respx.get(f"{V2}/cycle").mock(
        return_value=httpx.Response(200, json={"records": [], "next_token": None})
    )
    client = WhoopClient()
    await client.list_cycles(start="2026-04-19", end="2026-04-21")
    assert route.called
    qp = dict(route.calls.last.request.url.params)
    assert qp["start"] == "2026-04-19T00:00:00.000Z"
    assert qp["end"] == "2026-04-21T00:00:00.000Z"


@pytest.mark.asyncio
@respx.mock
async def test_list_cycles_authorization_header_sent():
    route = respx.get(f"{V2}/cycle").mock(
        return_value=httpx.Response(200, json={"records": [], "next_token": None})
    )
    client = WhoopClient()
    await client.list_cycles()
    assert route.called
    assert route.calls.last.request.headers["authorization"] == "Bearer test-access-token"
