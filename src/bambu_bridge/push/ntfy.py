"""Push via ntfy (spec 13, M6).

Two pieces:

* :class:`NtfyDispatcher` — POSTs a notification to ``<NTFY_URL>/<topic>``.
  No topic configured => a no-op (push is optional).
* :class:`NotificationService` — attaches to each printer's event bus and
  fires a push on ``print_completed`` / ``print_failed`` /
  ``filament_runout`` (subject to per-printer prefs). Latency is one bus hop
  plus one HTTP POST — well under a second.

The dispatcher takes an injected ``httpx.AsyncClient`` so tests can capture
requests without a network.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import structlog

from bambu_bridge.db.jobs import NotificationPrefsRepo
from bambu_bridge.service.events import Event
from bambu_bridge.service.printer import PrinterService

log = structlog.get_logger(__name__)

# event name -> (title template, ntfy priority, tags)
_EVENT_META: dict[str, tuple[str, str, str]] = {
    "print_completed": ("Print complete", "default", "white_check_mark"),
    "print_failed": ("Print failed", "high", "x"),
    "filament_runout": ("Filament runout", "high", "warning"),
}


def _ascii(value: str) -> str:
    """Drop non-ASCII so the value is safe in an HTTP header."""
    return value.encode("ascii", "ignore").decode("ascii")


class NtfyDispatcher:
    def __init__(
        self,
        base_url: str,
        topic: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._topic = topic
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None

    @property
    def enabled(self) -> bool:
        return bool(self._topic)

    async def notify(
        self,
        *,
        title: str,
        message: str,
        priority: str = "default",
        tags: str | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        # HTTP headers are latin-1; ntfy renders the body as UTF-8. Keep the
        # Title header ASCII-safe and let any unicode live in the message.
        headers = {"Title": _ascii(title), "Priority": priority}
        if tags:
            headers["Tags"] = _ascii(tags)
        try:
            resp = await self._client.post(
                f"{self._base_url}/{self._topic}",
                content=message.encode("utf-8"),
                headers=headers,
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — push failure must not cascade
            log.warning("ntfy.post_failed", error=str(exc))
            return False
        return True

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class NotificationService:
    """Fans printer bus events out to ntfy, honouring per-printer prefs."""

    def __init__(
        self, dispatcher: NtfyDispatcher, prefs: NotificationPrefsRepo
    ) -> None:
        self._dispatcher = dispatcher
        self._prefs = prefs
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def attach(self, service: PrinterService) -> None:
        """Registry listener: start watching this printer's bus."""
        if service.serial in self._tasks:
            return
        self._tasks[service.serial] = asyncio.create_task(self._watch(service))

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        await self._dispatcher.aclose()

    async def _watch(self, service: PrinterService) -> None:
        async with service.bus.subscribe() as sub:
            async for event in sub:
                if event.type == "event" and event.name in _EVENT_META:
                    await self._dispatch(service, event)

    async def _dispatch(self, service: PrinterService, event: Event) -> None:
        assert event.name is not None
        prefs = await self._prefs.get(service.serial)
        if not prefs.wants(event.name):
            return
        title, priority, tags = _EVENT_META[event.name]
        subtask = event.data.get("subtask_name") or service.friendly_name
        await self._dispatcher.notify(
            # ASCII only: ntfy Title is an HTTP header (latin-1).
            title=f"{title} - {service.friendly_name}",
            message=f"{subtask}",
            priority=priority,
            tags=tags,
        )
        log.info(
            "ntfy.dispatched",
            printer_id=service.serial,
            event_name=event.name,
        )
