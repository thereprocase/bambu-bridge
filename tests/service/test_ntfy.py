"""M6: ntfy dispatcher + bus-driven notifications with per-printer prefs."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from bambu_bridge.db.jobs import (
    Database,
    NotificationPrefs,
    NotificationPrefsRepo,
    Printer,
    PrinterRepo,
)
from bambu_bridge.push.ntfy import NotificationService, NtfyDispatcher
from bambu_bridge.service.events import Event
from bambu_bridge.service.printer import PrinterService
from tests.conftest import ACCESS_CODE, SERIAL


async def _seed_printer(db: Database) -> None:
    await PrinterRepo(db).add(
        Printer(
            id=SERIAL,
            friendly_name="Shop",
            ip="127.0.0.1",
            access_code=ACCESS_CODE,
            added_at=1,
        )
    )


def _capturing_client(sink: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sink.append(request)
        return httpx.Response(200)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_dispatcher_posts_to_topic() -> None:
    sink: list[httpx.Request] = []
    d = NtfyDispatcher("https://ntfy.sh", "mytopic", client=_capturing_client(sink))
    ok = await d.notify(title="Done", message="benchy", priority="high", tags="x")
    assert ok is True
    req = sink[0]
    assert str(req.url) == "https://ntfy.sh/mytopic"
    assert req.headers["Title"] == "Done"
    assert req.headers["Priority"] == "high"
    assert req.content == b"benchy"
    await d.aclose()


@pytest.mark.asyncio
async def test_dispatcher_disabled_without_topic() -> None:
    sink: list[httpx.Request] = []
    d = NtfyDispatcher("https://ntfy.sh", "", client=_capturing_client(sink))
    assert d.enabled is False
    assert await d.notify(title="x", message="y") is False
    assert sink == []
    await d.aclose()


def _service() -> PrinterService:
    return PrinterService(
        SERIAL, "127.0.0.1", ACCESS_CODE, friendly_name="Shop", mqtt_port=1
    )


async def _wait(predicate, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_completion_event_pushes_quickly(database: Database) -> None:
    sink: list[httpx.Request] = []
    dispatcher = NtfyDispatcher(
        "https://ntfy.sh", "topic", client=_capturing_client(sink)
    )
    notifier = NotificationService(dispatcher, NotificationPrefsRepo(database))
    service = _service()
    await notifier.attach(service)
    try:
        await _wait(lambda: service.bus.subscriber_count >= 1)
        start = asyncio.get_event_loop().time()
        service.bus.publish(
            Event("event", {"subtask_name": "benchy"}, name="print_completed")
        )
        await _wait(lambda: len(sink) == 1)
        latency = asyncio.get_event_loop().time() - start
        assert latency < 1.0  # spec 13: sub-1s MQTT-event -> phone
        assert sink[0].headers["Title"] == "Print complete - Shop"
        assert sink[0].content == b"benchy"
    finally:
        await notifier.shutdown()


@pytest.mark.asyncio
async def test_non_notify_events_ignored(database: Database) -> None:
    sink: list[httpx.Request] = []
    notifier = NotificationService(
        NtfyDispatcher("https://ntfy.sh", "t", client=_capturing_client(sink)),
        NotificationPrefsRepo(database),
    )
    service = _service()
    await notifier.attach(service)
    try:
        await _wait(lambda: service.bus.subscriber_count >= 1)
        service.bus.publish(Event("event", {}, name="print_started"))
        service.bus.publish(Event("delta", {"mc_percent": 5}))
        await asyncio.sleep(0.2)
        assert sink == []
    finally:
        await notifier.shutdown()


@pytest.mark.asyncio
async def test_per_printer_prefs_disable(database: Database) -> None:
    sink: list[httpx.Request] = []
    await _seed_printer(database)  # notification_prefs FK -> printers
    prefs = NotificationPrefsRepo(database)
    await prefs.set(NotificationPrefs(printer_id=SERIAL, enabled=False))
    notifier = NotificationService(
        NtfyDispatcher("https://ntfy.sh", "t", client=_capturing_client(sink)),
        prefs,
    )
    service = _service()
    await notifier.attach(service)
    try:
        await _wait(lambda: service.bus.subscriber_count >= 1)
        service.bus.publish(Event("event", {}, name="print_completed"))
        await asyncio.sleep(0.2)
        assert sink == []  # disabled for this printer
    finally:
        await notifier.shutdown()
