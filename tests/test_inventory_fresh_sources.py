import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bambu_bridge import inventory_sources
from bambu_bridge.service.events import Event, EventBus
from bambu_bridge.service.printer import FULL_REPORT_INTERVAL_S, PrinterService


class Frame:
    def __init__(self, age: float | None) -> None:
        self.ams_at = None if age is None else time.time() - age

    def snapshot(self) -> dict:
        return {"ams": {}, "external": None, "ams_at": self.ams_at, "external_at": None}


def printer(age: float | None, *, connected: bool = True) -> SimpleNamespace:
    service = SimpleNamespace(
        connected=connected,
        material_inventory=Frame(age),
        raw_bus=EventBus(),
        request_full_report=AsyncMock(return_value=True),
    )
    return service


async def fresh_after_refresh(service, **kwargs) -> bool:
    await inventory_sources.refresh_source_frame(service, **kwargs)
    return inventory_sources._frame_fresh(service)


async def test_stale_frame_requests_a_full_report_and_uses_the_reply() -> None:
    service = printer(120)

    async def reply() -> bool:
        async def arrive() -> None:
            await asyncio.sleep(0.05)
            service.raw_bus.publish(Event("snapshot", {"print": {"command": "push_status"}}))
            service.material_inventory.ams_at = time.time()
            service.raw_bus.publish(Event("snapshot", {"print": {"ams": {}}}))

        asyncio.get_running_loop().create_task(arrive())
        return True

    service.request_full_report.side_effect = reply
    assert await fresh_after_refresh(service) is True
    service.request_full_report.assert_awaited_once()


async def test_recent_frame_sends_nothing() -> None:
    service = printer(5)
    assert await fresh_after_refresh(service) is True
    service.request_full_report.assert_not_awaited()


async def test_objects_without_a_full_report_request_are_left_alone() -> None:
    await inventory_sources.refresh_source_frame(object())


@pytest.mark.parametrize("age", [None, 120])
async def test_disconnected_printer_is_not_asked(age) -> None:
    service = printer(age, connected=False)
    assert await fresh_after_refresh(service) is False
    service.request_full_report.assert_not_awaited()


async def test_no_reply_returns_the_stale_view_after_the_wait() -> None:
    service = printer(120)
    started = time.monotonic()
    assert await fresh_after_refresh(service, wait_s=0.2) is False
    assert time.monotonic() - started < 2


async def test_full_report_requests_are_spaced_and_need_a_connection(monkeypatch) -> None:
    service = PrinterService.__new__(PrinterService)
    service._full_report_requested = -FULL_REPORT_INTERVAL_S
    service._connected = True
    service.send_raw = AsyncMock()  # type: ignore[method-assign]
    clock = [1000.0]
    monkeypatch.setattr("bambu_bridge.service.printer.time.monotonic", lambda: clock[0])
    assert await service.request_full_report() is True
    sent = service.send_raw.await_args.args[0]
    assert sent["pushing"]["command"] == "pushall"
    clock[0] += FULL_REPORT_INTERVAL_S - 1
    assert await service.request_full_report() is False
    clock[0] += 1
    assert await service.request_full_report() is True
    assert service.send_raw.await_count == 2
    service._connected = False
    clock[0] += FULL_REPORT_INTERVAL_S
    assert await service.request_full_report() is False
    assert service.send_raw.await_count == 2
