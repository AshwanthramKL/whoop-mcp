"""
WHOOP v2 API client — M1 surface.

Pure data layer. No caching, no joins, no derived metrics.

Design notes:
- One shared ``httpx.AsyncClient`` per ``WhoopClient`` instance.
- Retry policy:
    * 429 -> sleep Retry-After (or 1s), retry once, then raise RateLimitError.
    * 5xx -> exponential backoff 1, 2, 4s, max 3 retries, then raise UpstreamError.
    * 4xx (not 429) -> no retry; 401 -> AuthError, 404 -> NotFoundError, else UpstreamError.
- Auto-pagination: list_* methods accept an optional ``limit`` meaning total
  records desired. Internally we request in batches of 25 and follow
  ``next_token`` until either limit reached or token is null.
- Logging: structured JSON lines to stderr via the module logger; never log
  the bearer token or any secret.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

import httpx

from auth_manager import TokenManager
from config import REQUEST_TIMEOUT, WHOOP_API_BASE

__all__ = [
    "WhoopClient",
    "WhoopAPIError",
    "AuthError",
    "RateLimitError",
    "NotFoundError",
    "UpstreamError",
    "ValidationError",
]

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


# ---------- Exceptions ----------


class WhoopAPIError(Exception):
    """Base class for all WhoopClient errors.

    Attributes:
        code: machine-readable error code (e.g. "AUTH_FAILED").
        status: HTTP status code (0 when the error never left the process).
        message: human-readable description.
        endpoint: request path that triggered the error.
    """

    def __init__(self, code: str, status: int, message: str, endpoint: str) -> None:
        super().__init__(f"{code} [{status}] {endpoint}: {message}")
        self.code = code
        self.status = status
        self.message = message
        self.endpoint = endpoint


class AuthError(WhoopAPIError):
    pass


class RateLimitError(WhoopAPIError):
    pass


class NotFoundError(WhoopAPIError):
    pass


class UpstreamError(WhoopAPIError):
    pass


class ValidationError(WhoopAPIError):
    pass


# ---------- Constants ----------

MAX_PAGE_SIZE = 25
MAX_5XX_RETRIES = 3
BACKOFF_SCHEDULE_S = (1, 2, 4)


def _log(event: str, **fields: Any) -> None:
    """Emit a structured log line to stderr."""
    payload = {"event": event, **fields}
    try:
        logger.info(json.dumps(payload, default=str))
    except Exception:
        # Never let logging break the request path.
        pass


def _coerce_datetime(value: Union[str, datetime, None], field: str, endpoint: str) -> Optional[str]:
    """Accept ISO-8601 string or datetime, return an ISO-8601 string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if not isinstance(value, str):
        raise ValidationError(
            "VALIDATION_ERROR", 0, f"{field} must be str or datetime", endpoint
        )
    # Validate it actually parses. Accept trailing Z by swapping to +00:00.
    parseable = value.replace("Z", "+00:00")
    try:
        datetime.fromisoformat(parseable)
    except ValueError as e:
        raise ValidationError(
            "VALIDATION_ERROR",
            0,
            f"{field} is not a valid ISO-8601 datetime: {e}",
            endpoint,
        ) from e
    return value


# ---------- Client ----------


class WhoopClient:
    """Async WHOOP v2 data client."""

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None) -> None:
        self.base_url = WHOOP_API_BASE.rstrip("/")
        self.token_manager = TokenManager()
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )

    # ----- public single-resource endpoints -----

    async def get_profile(self) -> Dict[str, Any]:
        return await self._request("GET", "/user/profile/basic")

    async def get_body_measurement(self) -> Dict[str, Any]:
        return await self._request("GET", "/user/measurement/body")

    async def get_cycle(self, cycle_id: Union[int, str]) -> Dict[str, Any]:
        return await self._request("GET", f"/cycle/{cycle_id}")

    async def get_cycle_sleep(self, cycle_id: Union[int, str]) -> Dict[str, Any]:
        return await self._request("GET", f"/cycle/{cycle_id}/sleep")

    async def get_cycle_recovery(self, cycle_id: Union[int, str]) -> Dict[str, Any]:
        return await self._request("GET", f"/cycle/{cycle_id}/recovery")

    async def get_sleep(self, sleep_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/activity/sleep/{sleep_id}")

    async def get_workout(self, workout_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/activity/workout/{workout_id}")

    # ----- public list (paginated) endpoints -----

    async def list_cycles(
        self,
        start: Union[str, datetime, None] = None,
        end: Union[str, datetime, None] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        return await self._paginate("/cycle", start=start, end=end, limit=limit)

    async def list_recoveries(
        self,
        start: Union[str, datetime, None] = None,
        end: Union[str, datetime, None] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        return await self._paginate("/recovery", start=start, end=end, limit=limit)

    async def list_sleeps(
        self,
        start: Union[str, datetime, None] = None,
        end: Union[str, datetime, None] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        return await self._paginate("/activity/sleep", start=start, end=end, limit=limit)

    async def list_workouts(
        self,
        start: Union[str, datetime, None] = None,
        end: Union[str, datetime, None] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        return await self._paginate("/activity/workout", start=start, end=end, limit=limit)

    # ----- auth status passthrough (used by MCP auth tool) -----

    def get_auth_status(self) -> Dict[str, Any]:
        return self.token_manager.get_token_info()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ----- internal: pagination -----

    async def _paginate(
        self,
        path: str,
        *,
        start: Union[str, datetime, None],
        end: Union[str, datetime, None],
        limit: Optional[int],
    ) -> List[Dict[str, Any]]:
        start_iso = _coerce_datetime(start, "start", path)
        end_iso = _coerce_datetime(end, "end", path)
        if limit is not None and limit <= 0:
            raise ValidationError("VALIDATION_ERROR", 0, "limit must be > 0", path)

        records: List[Dict[str, Any]] = []
        next_token: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"limit": MAX_PAGE_SIZE}
            if start_iso is not None:
                params["start"] = start_iso
            if end_iso is not None:
                params["end"] = end_iso
            if next_token:
                params["nextToken"] = next_token

            page = await self._request("GET", path, params=params)
            batch = page.get("records") or []
            records.extend(batch)

            next_token = page.get("next_token")
            if limit is not None and len(records) >= limit:
                return records[:limit]
            if not next_token:
                return records

    # ----- internal: HTTP with retries -----

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = self._auth_headers(path)

        attempt_5xx = 0
        attempt_429 = 0
        while True:
            try:
                response = await self._client.request(
                    method, url, headers=headers, params=params
                )
            except httpx.TimeoutException as e:
                raise UpstreamError("UPSTREAM_ERROR", 0, f"timeout: {e}", path) from e
            except httpx.TransportError as e:
                raise UpstreamError("UPSTREAM_ERROR", 0, f"transport: {e}", path) from e

            status = response.status_code

            if 200 <= status < 300:
                try:
                    return response.json()
                except ValueError as e:
                    raise UpstreamError(
                        "UPSTREAM_ERROR", status, f"invalid JSON: {e}", path
                    ) from e

            if status == 401:
                raise AuthError("AUTH_FAILED", 401, "authentication failed", path)
            if status == 404:
                raise NotFoundError("NOT_FOUND", 404, "resource not found", path)

            if status == 429:
                if attempt_429 >= 1:
                    raise RateLimitError(
                        "RATE_LIMITED", 429, "rate limited after retry", path
                    )
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                attempt_429 += 1
                _log(
                    "retry",
                    endpoint=path,
                    status=429,
                    attempt=attempt_429,
                    delay_s=retry_after,
                )
                await asyncio.sleep(retry_after)
                continue

            if 500 <= status < 600:
                if attempt_5xx >= MAX_5XX_RETRIES:
                    raise UpstreamError(
                        "UPSTREAM_ERROR",
                        status,
                        f"exhausted retries ({MAX_5XX_RETRIES})",
                        path,
                    )
                delay = BACKOFF_SCHEDULE_S[min(attempt_5xx, len(BACKOFF_SCHEDULE_S) - 1)]
                attempt_5xx += 1
                _log(
                    "retry",
                    endpoint=path,
                    status=status,
                    attempt=attempt_5xx,
                    delay_s=delay,
                )
                await asyncio.sleep(delay)
                continue

            # Any other 4xx is a client-side error; no retry.
            raise UpstreamError(
                "UPSTREAM_ERROR", status, f"unexpected status {status}", path
            )

    def _auth_headers(self, path: str) -> Dict[str, str]:
        token = self.token_manager.get_valid_access_token()
        if not token:
            raise AuthError("AUTH_FAILED", 0, "no valid access token", path)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "WHOOP-MCP-Server/0.2.0",
        }


def _parse_retry_after(header_value: Optional[str]) -> float:
    """Parse a Retry-After header (seconds). Falls back to 1s."""
    if not header_value:
        return 1.0
    try:
        return float(header_value)
    except (TypeError, ValueError):
        return 1.0
