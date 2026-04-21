"""
One-shot script to record live WHOOP v2 responses as JSON fixtures.

Usage:
    export WHOOP_CLIENT_ID=...
    export WHOOP_CLIENT_SECRET=...
    .venv/bin/python tests/record_fixtures.py

Requires a saved, valid refresh token in ~/.whoop-mcp-server/tokens.json.
Redacts obvious PII (user_id, email) before writing so fixtures are safe
to commit.

Run once, commit the tests/fixtures/ output, then never run again.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
sys.path.insert(0, str(SRC_DIR))

from auth_manager import TokenManager  # noqa: E402
from config import WHOOP_API_BASE  # noqa: E402


def _redact(obj: Any) -> Any:
    """Recursively redact user_id / email-like fields."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in {"user_id", "email", "first_name", "last_name"}:
                out[k] = "<REDACTED>"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def _get(client: httpx.Client, path: str, params: dict | None = None) -> dict:
    r = client.get(f"{WHOOP_API_BASE}{path}", params=params or {})
    r.raise_for_status()
    return r.json()


def main() -> int:
    tm = TokenManager()
    token = tm.get_valid_access_token()
    if not token:
        print("No valid access token available. Run `whoop-mcp-oauth` (pip/uvx) or `python src/setup_direct_oauth.py` (git clone).", file=sys.stderr)
        return 1

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(headers=headers, timeout=30.0) as client:
        # --- Single-resource endpoints ---
        to_record: list[tuple[str, str, dict | None]] = [
            ("profile", "/user/profile/basic", None),
            ("body_measurement", "/user/measurement/body", None),
        ]

        # Paginated list endpoints — grab a single page of 25 to keep fixtures small.
        list_endpoints = [
            ("cycles_page", "/cycle"),
            ("recoveries_page", "/recovery"),
            ("sleeps_page", "/activity/sleep"),
            ("workouts_page", "/activity/workout"),
        ]
        for name, path in list_endpoints:
            to_record.append((name, path, {"limit": 25}))

        results: dict[str, dict] = {}
        for name, path, params in to_record:
            print(f"GET {path} params={params}", file=sys.stderr)
            try:
                data = _get(client, path, params)
            except httpx.HTTPStatusError as e:
                print(f"  !! {e.response.status_code}: {e.response.text[:200]}", file=sys.stderr)
                continue
            (FIXTURES_DIR / f"{name}.json").write_text(
                json.dumps(_redact(data), indent=2, sort_keys=True) + "\n"
            )
            results[name] = data

        # --- ID-addressed endpoints: pick one cycle / sleep / workout from the lists. ---
        cycles = results.get("cycles_page", {}).get("records", [])
        if cycles:
            cycle_id = cycles[0]["id"]
            print(f"GET /cycle/{cycle_id}", file=sys.stderr)
            try:
                data = _get(client, f"/cycle/{cycle_id}")
                (FIXTURES_DIR / "cycle_single.json").write_text(
                    json.dumps(_redact(data), indent=2, sort_keys=True) + "\n"
                )
            except httpx.HTTPStatusError as e:
                print(f"  !! {e.response.status_code}", file=sys.stderr)
            for sub in ("sleep", "recovery"):
                try:
                    data = _get(client, f"/cycle/{cycle_id}/{sub}")
                    (FIXTURES_DIR / f"cycle_{sub}.json").write_text(
                        json.dumps(_redact(data), indent=2, sort_keys=True) + "\n"
                    )
                except httpx.HTTPStatusError as e:
                    print(f"  !! /cycle/{cycle_id}/{sub} -> {e.response.status_code}", file=sys.stderr)

        sleeps = results.get("sleeps_page", {}).get("records", [])
        if sleeps:
            sleep_id = sleeps[0]["id"]
            print(f"GET /activity/sleep/{sleep_id}", file=sys.stderr)
            try:
                data = _get(client, f"/activity/sleep/{sleep_id}")
                (FIXTURES_DIR / "sleep_single.json").write_text(
                    json.dumps(_redact(data), indent=2, sort_keys=True) + "\n"
                )
            except httpx.HTTPStatusError as e:
                print(f"  !! {e.response.status_code}", file=sys.stderr)

        workouts = results.get("workouts_page", {}).get("records", [])
        if workouts:
            workout_id = workouts[0]["id"]
            print(f"GET /activity/workout/{workout_id}", file=sys.stderr)
            try:
                data = _get(client, f"/activity/workout/{workout_id}")
                (FIXTURES_DIR / "workout_single.json").write_text(
                    json.dumps(_redact(data), indent=2, sort_keys=True) + "\n"
                )
            except httpx.HTTPStatusError as e:
                print(f"  !! {e.response.status_code}", file=sys.stderr)

    print("Done. Fixtures written to tests/fixtures/.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
