"""Per-printer REST event poller for the Bambu Bridge integration.

The bridge exposes a persistent, paged event feed at
``GET /api/v1/printers/{id}/events`` (contract §8.4).  This module polls that
feed on a fixed interval and translates each new row into:

* an HA bus event — ``bambu_bridge_event`` — for automations and scripts.
* a persistent notification (HA UI) for any row where ``severity == "error"``.

**Cursor discipline:** on integration (re)start we fetch once and record the
highest ``id`` seen; that initialises the cursor so historical events are
*never* replayed as fresh notifications.  On every subsequent poll we request
only rows newer than the cursor.

The poller runs as a cancellable ``asyncio.Task`` owned by
``BambuBridgeCoordinator`` — one task per registered printer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from homeassistant.components.persistent_notification import async_create
from homeassistant.core import HomeAssistant

from .api import BambuBridgeClient, BridgeConnectionError, BridgeError
from .const import DOMAIN, EVENT_BRIDGE, EVENT_POLL_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)

# The HA bus event name re-used here (matches the WS path in coordinator.py).
_BUS_EVENT = EVENT_BRIDGE


class PrinterEventPoller:
    """Polls ``GET /printers/{id}/events`` and forwards to the HA bus.

    One instance lives per registered printer, managed by the coordinator.
    Call ``async_start()`` once; call ``async_stop()`` on teardown.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: BambuBridgeClient,
        printer_id: str,
    ) -> None:
        self._hass = hass
        self._client = client
        self._printer_id = printer_id
        self._cursor: int | None = None
        self._task: asyncio.Task[None] | None = None

    # ---------------------------------------------------------------------- #
    # Lifecycle
    # ---------------------------------------------------------------------- #

    async def async_start(self) -> None:
        """Initialise cursor then launch the poll loop."""
        await self._init_cursor()
        self._task = asyncio.ensure_future(self._loop())

    async def async_stop(self) -> None:
        """Cancel the poll loop and wait for it to finish."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    # ---------------------------------------------------------------------- #
    # Cursor initialisation
    # ---------------------------------------------------------------------- #

    async def _init_cursor(self) -> None:
        """Fetch the current event list once and set cursor to the max id seen.

        This means the poller will only surface events that arrive *after* the
        integration starts — historical events are never replayed.

        On network failure the cursor is left as ``None``.  ``_poll_once``
        detects this and retries init instead of dispatching, so a bridge that
        is unreachable at HA startup can never cause historical events to be
        replayed as fresh notifications once it comes back online.
        """
        try:
            rows = await self._client.get_events(self._printer_id, limit=50)
        except BridgeError as err:
            _LOGGER.debug(
                "%s: event cursor init failed (%s) — will retry on next poll tick",
                self._printer_id,
                err,
            )
            # Leave self._cursor as None so _poll_once knows to retry init
            # rather than fetch with since_id=None (which would return all
            # historical rows and replay them as fresh events).
            return

        if rows:
            self._cursor = max(
                r["id"] for r in rows if isinstance(r.get("id"), int)
            )
        else:
            self._cursor = 0

        _LOGGER.debug(
            "%s: event cursor initialised at id=%d (%d rows seen)",
            self._printer_id,
            self._cursor,
            len(rows),
        )

    # ---------------------------------------------------------------------- #
    # Poll loop
    # ---------------------------------------------------------------------- #

    async def _loop(self) -> None:
        """Tick every ``EVENT_POLL_INTERVAL_SECONDS`` and process new events."""
        while True:
            await asyncio.sleep(EVENT_POLL_INTERVAL_SECONDS)
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — never kill the loop silently
                _LOGGER.exception(
                    "%s: unexpected error in event poll loop", self._printer_id
                )

    async def _poll_once(self) -> None:
        """Fetch new events and dispatch each one.

        If the cursor has not yet been established (bridge was unreachable at
        startup), attempt initialisation first and return without dispatching.
        This ensures we never dispatch historical events as fresh ones.
        """
        if self._cursor is None:
            _LOGGER.debug(
                "%s: cursor not yet initialised — retrying init before dispatch",
                self._printer_id,
            )
            await self._init_cursor()
            return

        try:
            rows = await self._client.get_events(
                self._printer_id,
                since_id=self._cursor,
                limit=50,
            )
        except BridgeConnectionError as err:
            _LOGGER.debug("%s: event poll skipped — bridge unreachable: %s", self._printer_id, err)
            return
        except BridgeError as err:
            _LOGGER.warning("%s: event poll error: %s", self._printer_id, err)
            return

        if not rows:
            return

        # Sort ascending so we process events in chronological order.
        rows.sort(key=lambda r: r.get("id") or 0)

        _LOGGER.debug(
            "%s: event poll received %d new event(s) (cursor was %d)",
            self._printer_id,
            len(rows),
            self._cursor or 0,
        )

        for row in rows:
            self._dispatch(row)
            row_id = row.get("id")
            if isinstance(row_id, int) and (self._cursor is None or row_id > self._cursor):
                self._cursor = row_id

    # ---------------------------------------------------------------------- #
    # Dispatch
    # ---------------------------------------------------------------------- #

    def _dispatch(self, row: dict[str, Any]) -> None:
        """Fire an HA bus event and, for errors, a persistent notification."""
        kind: str = row.get("kind") or row.get("event_type") or "unknown"
        severity: str = row.get("severity") or "info"
        title: str = row.get("title") or kind
        body: str = row.get("detail") or row.get("body") or ""
        ts: str = row.get("ts") or ""
        job_id: str | None = row.get("job_id")
        event_id: int | None = row.get("id")

        payload: dict[str, Any] = {
            "printer_id": self._printer_id,
            "kind": kind,
            "severity": severity,
            "title": title,
            "body": body,
            "timestamp": ts,
            "job_id": job_id,
            "event_id": event_id,
            "raw": row,
        }

        self._hass.bus.async_fire(_BUS_EVENT, payload)

        if severity == "error":
            notification_id = f"{DOMAIN}_{self._printer_id}_{event_id}"
            message = body if body else title
            async_create(
                self._hass,
                message=message,
                title=f"Bambu Bridge [{self._printer_id}]: {title}",
                notification_id=notification_id,
            )
            _LOGGER.warning(
                "%s: error event id=%s kind=%s — persistent notification created",
                self._printer_id,
                event_id,
                kind,
            )
