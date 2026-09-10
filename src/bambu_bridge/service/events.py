"""Per-subscriber event bus (spec 3 ``service/events.py``).

One bus per :class:`~bambu_bridge.service.printer.PrinterService`. Each
subscriber gets its own bounded :class:`asyncio.Queue`; a slow subscriber only
loses *its own* backlog, never blocks the printer ingest path.

Status telemetry is a firehose (MQTT QoS 0 — drops are acceptable, spec 5.1):
when a subscriber's queue is full we drop its oldest event rather than block.
``event``-type messages (print_started, error, …) matter more, but a queue of
256 only fills if a client is wedged, at which point it is already broken.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

import structlog

log = structlog.get_logger(__name__)

EventType = Literal["snapshot", "delta", "event"]


@dataclass(slots=True, frozen=True)
class Event:
    """A message bound for status subscribers.

    Mirrors the WebSocket wire schema (spec 7):

    * ``snapshot`` — full state, sent on (re)seed
    * ``delta``    — only the fields that changed
    * ``event``    — a named occurrence; ``name`` is set (print_started, …)
    """

    type: EventType
    data: dict[str, Any]
    name: str | None = None

    def to_wire(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"type": self.type}
        if self.type == "event":
            msg["event"] = self.name
        msg["data"] = self.data
        return msg


def diff_state(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """Changed leaves only, nested — the ``delta`` payload (spec 7 / §5.3).

    Recurses into nested dicts; lists and scalars replace whole. Keys present
    in ``old`` but absent from ``new`` are not emitted — both the raw
    push_status merge and the translated snapshot have structurally stable
    shapes, so a key never legitimately disappears.
    """
    delta: dict[str, Any] = {}
    for key, new_val in new.items():
        old_val = old.get(key)
        if isinstance(old_val, dict) and isinstance(new_val, dict):
            sub = diff_state(old_val, new_val)
            if sub:
                delta[key] = sub
        elif old_val != new_val:
            delta[key] = new_val
    return delta


class Subscription:
    """A live feed. Async-iterate it for events."""

    def __init__(self, queue: asyncio.Queue[Event]) -> None:
        self._queue = queue

    async def get(self) -> Event:
        return await self._queue.get()

    def __aiter__(self) -> AsyncIterator[Event]:
        return self

    async def __anext__(self) -> Event:
        return await self._queue.get()


class EventBus:
    """Fan-out to N subscribers, each with an independent bounded queue."""

    def __init__(self, maxsize: int = 256) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._maxsize = maxsize

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[Subscription]:
        queue: asyncio.Queue[Event] = asyncio.Queue(self._maxsize)
        self._subscribers.add(queue)
        try:
            yield Subscription(queue)
        finally:
            self._subscribers.discard(queue)

    def publish(self, event: Event) -> None:
        """Non-blocking fan-out. Drops the oldest event for full queues."""
        for queue in self._subscribers:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):  # race only
                    queue.get_nowait()
                log.warning("eventbus.subscriber_lagging", dropped=1)
            queue.put_nowait(event)
