"""Async REST + WebSocket client for one bambu-bridge instance.

Wire shape: `docs/API-CONTRACT.md`. All paths sit under `/api/v1`. Auth is a
bearer token, identical for HTTP, WS, and media (contract §1).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class BridgeError(Exception):
    """Base error for any bridge client failure."""


class BridgeAuthError(BridgeError):
    """Bearer token missing/invalid, or the bridge has no key configured."""


class BridgeConnectionError(BridgeError):
    """The bridge is unreachable or the request timed out."""


async def _safe_json(resp: aiohttp.ClientResponse) -> dict[str, Any]:
    """Best-effort JSON body — returns {} when the body is not JSON."""
    try:
        return await resp.json()
    except (aiohttp.ClientError, ValueError):
        return {}


class BambuBridgeClient:
    """REST + status-WebSocket client for a single bridge."""

    def __init__(
        self, session: aiohttp.ClientSession, base_url: str, api_key: str
    ) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._api_key = api_key

    @property
    def base_url(self) -> str:
        """The configured bridge base URL (no trailing slash)."""
        return self._base

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _url(self, path: str) -> str:
        return f"{self._base}{API_PREFIX}{path}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Issue a JSON request, mapping bridge error codes to exceptions."""
        try:
            async with self._session.request(
                method,
                self._url(path),
                headers=self._headers,
                timeout=REQUEST_TIMEOUT,
                **kwargs,
            ) as resp:
                if resp.status in (401, 403):
                    raise BridgeAuthError(f"{resp.status} on {path}")
                if resp.status == 503:
                    body = await _safe_json(resp)
                    if body.get("error") == "auth_not_configured":
                        raise BridgeAuthError("bridge has no API key configured")
                    raise BridgeConnectionError(f"503 on {path}")
                if resp.status >= 400:
                    body = await _safe_json(resp)
                    raise BridgeError(body.get("message") or f"{resp.status} on {path}")
                if resp.status == 204 or resp.content_type != "application/json":
                    return None
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise BridgeConnectionError(str(err)) from err

    async def get_version(self) -> dict[str, Any]:
        """GET /version — unauthenticated reachability probe."""
        return await self._request("GET", "/version") or {}

    async def list_printers(self) -> list[dict[str, Any]]:
        """GET /printers — list of PrinterSummary objects (contract §4.1)."""
        result = await self._request("GET", "/printers")
        return result if isinstance(result, list) else []

    async def get_printer(self, printer_id: str) -> dict[str, Any]:
        """GET /printers/{id} — the full translated snapshot (contract §6)."""
        return await self._request("GET", f"/printers/{printer_id}") or {}

    async def post_command(
        self, printer_id: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """POST a control endpoint under /printers/{id} (contract §9)."""
        return await self._request(
            "POST", f"/printers/{printer_id}{path}", json=body
        )

    async def snapshot(self, printer_id: str) -> bytes | None:
        """GET /printers/{id}/camera/snapshot.jpg — raw JPEG bytes."""
        url = self._url(f"/printers/{printer_id}/camera/snapshot.jpg")
        try:
            async with self._session.get(
                url, headers=self._headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status in (401, 403):
                    raise BridgeAuthError("camera snapshot auth failed")
                if resp.status != 200:
                    # 503 camera_no_frame is transient — caller shows last image.
                    return None
                return await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise BridgeConnectionError(str(err)) from err

    async def get_events(
        self,
        printer_id: str,
        *,
        since_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """GET /printers/{id}/events — flat per-printer event feed (contract §8.4).

        `since_id` is used as a cursor: the caller passes the highest event id
        seen so far and only events with id > since_id are returned.  When
        `since_id` is None all events up to `limit` are returned (used only
        on first fetch to initialise the cursor).
        """
        path = f"/printers/{printer_id}/events?limit={limit}"
        result = await self._request("GET", path)
        rows: list[dict[str, Any]] = result if isinstance(result, list) else []
        if since_id is not None:
            rows = [r for r in rows if isinstance(r.get("id"), int) and r["id"] > since_id]
        return rows

    @asynccontextmanager
    async def ws_status(
        self, printer_id: str
    ) -> AsyncIterator[aiohttp.ClientWebSocketResponse]:
        """Open WS /printers/{id}/status (contract §5).

        The contract allows the bearer either as a header or as `?token=`;
        aiohttp can set the header on the upgrade request, so we do that.
        """
        url = self._url(f"/printers/{printer_id}/status")
        ws_url = url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        async with self._session.ws_connect(
            ws_url, headers=self._headers, heartbeat=55
        ) as ws:
            yield ws
