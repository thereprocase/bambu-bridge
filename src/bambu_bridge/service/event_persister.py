"""Persist named bus events to the SQLite events table.

The :class:`~bambu_bridge.service.printer.PrinterService` fans named
events over its :class:`~bambu_bridge.service.events.EventBus`; subscribers
that need them in-memory (the WS layer, the JobRun FSM) take them off the
bus directly. This persister is the third subscriber — one per printer —
that bucket-sorts each named event into the `events` table so the
NotificationsScreen's :code:`GET /api/v1/printers/{id}/events` (contract
§8.4) can list and filter them.

Bookkeeping `state_change` / `job_created` rows are already written by
the JobRun FSM; those stay in the table but are filtered out at the API
layer (they're not user-facing notifications).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import structlog

from bambu_bridge.db.jobs import EventRepo
from bambu_bridge.service.events import Event

if TYPE_CHECKING:
    from bambu_bridge.service.printer import PrinterService

log = structlog.get_logger(__name__)


# Map bus-event name → wire `kind` + default severity bucket. Names not
# in this table are persisted unchanged with severity inferred from the
# payload's `severity` key (set by PrinterService._maybe_emit_error via
# the HMS lookup), falling back to "info".
_KIND_SEVERITY: dict[str, str] = {
    "print_started": "info",
    "print_completed": "info",
    "print_failed": "error",
    "print_progress": "info",
    "filament_runout": "warn",
    "feed_warning": "warn",
    "error": "error",
    "connection_lost": "warn",
    "connection_restored": "info",
    "cert_changed": "error",
    "cert_trusted": "info",
}


class EventPersister:
    """One persister, one EventRepo, many printer subscriptions.

    Plugged into the :class:`~bambu_bridge.service.registry.Registry` as a
    listener at app startup. The registry calls :meth:`attach` for every
    PrinterService that comes up (load + add), and the persister spawns a
    background task subscribing to that printer's bus until it stops.
    """

    def __init__(self, events: EventRepo) -> None:
        self._events = events
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def attach(self, service: PrinterService) -> None:
        prior = self._tasks.pop(service.serial, None)
        if prior is not None:
            prior.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await prior
        self._tasks[service.serial] = asyncio.create_task(
            self._consume(service),
            name=f"events:persist:{service.serial}",
        )

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        for task in self._tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def _consume(self, service: PrinterService) -> None:
        log_ = log.bind(printer_id=service.serial)
        async with service.bus.subscribe() as sub:
            async for ev in sub:
                if ev.type != "event" or ev.name is None:
                    # snapshots/deltas aren't notifications — they're
                    # state-stream traffic.
                    continue
                severity = _severity_for(ev)
                try:
                    await self._events.add(
                        printer_id=service.serial,
                        event_type=ev.name,
                        payload=dict(ev.data),
                        severity=severity,
                    )
                except Exception:  # noqa: BLE001 — never let bus die
                    log_.exception("event_persister.write_failed", name=ev.name)


def _severity_for(ev: Event) -> str:
    """Pick the severity bucket for a named event.

    Priority: the structured `print_error.severity` (HMS-derived, carried
    by `error` / `print_failed` events per §12.2) > a flat `severity` key
    if one is present > the static kind map > "info" catch-all.
    """
    data = ev.data if isinstance(ev.data, dict) else {}
    err = data.get("print_error")
    if isinstance(err, dict):
        sev = err.get("severity")
        if isinstance(sev, str) and sev in ("info", "warn", "error"):
            return sev
    payload_sev = data.get("severity")
    if isinstance(payload_sev, str) and payload_sev in ("info", "warn", "error"):
        return payload_sev
    if ev.name in _KIND_SEVERITY:
        return _KIND_SEVERITY[ev.name]
    return "info"
