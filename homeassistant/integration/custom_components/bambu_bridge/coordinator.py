"""Push coordinator — one per bridge, one status WebSocket per printer.

`iot_class` is `local_push`: the coordinator holds a WS to each printer and
calls `async_set_updated_data` on every frame. A slow REST reconcile runs
behind it as a safety net and to pick up printers added after setup.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
from collections.abc import Mapping
from datetime import timedelta
import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    BambuBridgeClient,
    BridgeAuthError,
    BridgeConnectionError,
    BridgeError,
)
from .const import DOMAIN, EVENT_BRIDGE, RECONCILE_INTERVAL_MINUTES, WS_BACKOFF
from .events import PrinterEventPoller

_LOGGER = logging.getLogger(__name__)

type CoordinatorData = dict[str, dict[str, Any]]

_CLOSE_TYPES = (
    aiohttp.WSMsgType.CLOSE,
    aiohttp.WSMsgType.CLOSING,
    aiohttp.WSMsgType.CLOSED,
    aiohttp.WSMsgType.ERROR,
)


def _deep_merge(base: dict[str, Any], delta: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge a WS delta into a cached snapshot (contract §5.3).

    Deltas carry only changed leaves, deep-nested; never render from a delta
    alone. Mapping values recurse, everything else replaces.
    """
    for key, value in delta.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


class BambuBridgeCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Coordinates one bridge: REST reconcile + per-printer status WebSocket."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: BambuBridgeClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(minutes=RECONCILE_INTERVAL_MINUTES),
        )
        self.client = client
        self._ws_tasks: dict[str, asyncio.Task[None]] = {}
        self._event_pollers: dict[str, PrinterEventPoller] = {}

    async def _async_update_data(self) -> CoordinatorData:
        """Slow REST reconcile — discovers printers and reseeds the cache."""
        try:
            summaries = await self.client.list_printers()
        except BridgeAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except BridgeConnectionError as err:
            raise UpdateFailed(f"bridge unreachable: {err}") from err

        data: CoordinatorData = dict(self.data or {})
        seen: set[str] = set()
        for summary in summaries:
            # Older/edge bridge builds key the summary by "serial"; the value
            # is identical to printer_id.  Accept either so the integration is
            # robust across bridge versions.
            pid = summary.get("printer_id") or summary.get("serial")
            if not pid:
                continue
            seen.add(pid)
            try:
                data[pid] = await self.client.get_printer(pid)
            except BridgeError as err:
                _LOGGER.debug("reconcile: get_printer(%s) failed: %s", pid, err)
                data.setdefault(pid, summary)
            self._ensure_ws(pid)

        # Drop printers the bridge no longer reports.
        for pid in list(data):
            if pid not in seen:
                data.pop(pid, None)
                task = self._ws_tasks.pop(pid, None)
                if task is not None:
                    task.cancel()
                poller = self._event_pollers.pop(pid, None)
                if poller is not None:
                    self.hass.async_create_task(poller.async_stop())
        return data

    def _ensure_ws(self, printer_id: str) -> None:
        """Start a status-WS loop and event poller for a printer if not running."""
        task = self._ws_tasks.get(printer_id)
        if task is None or task.done():
            assert self.config_entry is not None
            self._ws_tasks[printer_id] = self.config_entry.async_create_background_task(
                self.hass,
                self._ws_loop(printer_id),
                f"{DOMAIN}_ws_{printer_id}",
            )
        if printer_id not in self._event_pollers:
            poller = PrinterEventPoller(self.hass, self.client, printer_id)
            self._event_pollers[printer_id] = poller
            assert self.config_entry is not None
            self.config_entry.async_create_background_task(
                self.hass,
                poller.async_start(),
                f"{DOMAIN}_evpoll_start_{printer_id}",
            )

    async def async_start(self) -> None:
        """Start WS loops for every printer found by the first refresh."""
        for printer_id in self.data or {}:
            self._ensure_ws(printer_id)

    async def async_stop(self) -> None:
        """Cancel every status-WS loop and event poller. Called on config-entry unload."""
        tasks = list(self._ws_tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._ws_tasks.clear()

        for poller in self._event_pollers.values():
            await poller.async_stop()
        self._event_pollers.clear()

    async def _ws_loop(self, printer_id: str) -> None:
        """Hold the status WebSocket open; reconnect with backoff (§5.4)."""
        attempt = 0
        while True:
            try:
                async with self.client.ws_status(printer_id) as ws:
                    attempt = 0
                    _LOGGER.debug("WS connected: %s", printer_id)
                    await self._consume_ws(printer_id, ws)
            except asyncio.CancelledError:
                raise
            except BridgeAuthError:
                # 1008/unauthorized — re-fetching the key is a config change,
                # not something a reconnect can fix. Stop this stream.
                _LOGGER.error("WS auth failed for %s; stopping stream", printer_id)
                return
            except (BridgeError, OSError, aiohttp.ClientError) as err:
                _LOGGER.debug("WS %s dropped: %s", printer_id, err)
            except Exception:  # noqa: BLE001 - never let the loop die silently
                _LOGGER.exception("WS %s loop error", printer_id)

            delay = WS_BACKOFF[min(attempt, len(WS_BACKOFF) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def _consume_ws(
        self, printer_id: str, ws: aiohttp.ClientWebSocketResponse
    ) -> None:
        """Process frames until the socket closes (contract §5.1)."""
        async for msg in ws:
            if msg.type in _CLOSE_TYPES:
                break
            if msg.type is not aiohttp.WSMsgType.TEXT:
                continue
            try:
                frame = msg.json()
            except ValueError:
                continue
            ftype = frame.get("type")
            if ftype == "snapshot":
                self._apply_snapshot(printer_id, frame.get("data") or {})
            elif ftype == "delta":
                self._apply_delta(printer_id, frame.get("data") or {})
            elif ftype == "event":
                self._apply_event(printer_id, frame)
            elif ftype == "ping":
                with contextlib.suppress(aiohttp.ClientError):
                    await ws.send_json({"type": "pong"})

    def _apply_snapshot(self, printer_id: str, snapshot: dict[str, Any]) -> None:
        """Full state — replaces the cached snapshot and reseeds (§5.3)."""
        data = dict(self.data or {})
        data[printer_id] = snapshot
        self.async_set_updated_data(data)

    def _apply_delta(self, printer_id: str, delta: Mapping[str, Any]) -> None:
        """Changed leaves — deep-merge into the cached snapshot."""
        data = dict(self.data or {})
        current = copy.deepcopy(data.get(printer_id) or {})
        _deep_merge(current, delta)
        data[printer_id] = current
        self.async_set_updated_data(data)

    def _apply_event(self, printer_id: str, frame: dict[str, Any]) -> None:
        """Named transition — fire it on the HA bus for automations (§12)."""
        payload = frame.get("data") or {}
        self.hass.bus.async_fire(
            EVENT_BRIDGE,
            {
                "printer_id": printer_id,
                "event": frame.get("event"),
                "data": payload,
            },
        )
        # Some events also carry print-error state worth reflecting now.
        if isinstance(payload.get("print_error"), dict):
            self._apply_delta(printer_id, {"print_error": payload["print_error"]})
