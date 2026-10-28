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


async def test_an_unanswered_refresh_is_logged_with_what_went_out(monkeypatch) -> None:
    service = printer(4620)
    service.serial = "P1"
    logged: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        inventory_sources.log, "info", lambda event, **kw: logged.append((event, kw))
    )
    assert await fresh_after_refresh(service, wait_s=0.05) is False
    assert [event for event, _ in logged] == ["inventory.ams_refresh_unanswered"]
    fields = logged[0][1]
    assert fields["printer"] == "P1" and fields["requested"] is True
    assert fields["reports_seen"] == 0 and fields["frame_age_s"] >= 4620


async def test_an_answered_refresh_logs_nothing(monkeypatch) -> None:
    service = printer(120)
    logged: list[str] = []
    monkeypatch.setattr(inventory_sources.log, "info", lambda event, **kw: logged.append(event))

    async def reply() -> bool:
        async def arrive() -> None:
            await asyncio.sleep(0.02)
            service.material_inventory.ams_at = time.time()
            service.raw_bus.publish(Event("snapshot", {"print": {"ams": {}}}))

        asyncio.get_running_loop().create_task(arrive())
        return True

    service.request_full_report.side_effect = reply
    assert await fresh_after_refresh(service) is True
    assert logged == []


class AmsFrame(Frame):
    def __init__(self, units: list[dict]) -> None:
        super().__init__(5)
        self.units = units

    def snapshot(self) -> dict:
        return {**super().snapshot(), "ams": {"ams": self.units}}


def ams_printer(units: list[dict], modules: list[dict] | None = None, *, connected: bool = True):
    info = {"module": modules or []}
    service = SimpleNamespace(
        serial="SN-" + str(id(units)),
        connected=connected,
        material_inventory=AmsFrame(units),
        raw_bus=EventBus(),
        request_full_report=AsyncMock(return_value=True),
        native_snapshot=lambda: {"info": info},
        send_command=AsyncMock(),
    )
    return service, info


async def test_present_ams_without_a_serial_asks_for_version_and_uses_the_reply() -> None:
    """2026-10-28: every AMS slot was unassignable after a power cycle until a manual
    get_version; the refresh now asks when a present AMS has no serial."""
    service, info = ams_printer([{"id": "0"}])

    async def reply(*_args) -> None:
        async def arrive() -> None:
            await asyncio.sleep(0.05)
            info["module"] = [{"name": "ams/0", "sn": "19C06A3C1300000"}]
            service.raw_bus.publish(Event("snapshot", {"info": {"command": "get_version"}}))

        asyncio.get_running_loop().create_task(arrive())

    service.send_command.side_effect = reply
    await inventory_sources.refresh_source_frame(service, wait_s=2)
    service.send_command.assert_awaited_once_with("info", "get_version")
    assert inventory_sources.ams_units_without_serial(service) == []


async def test_serials_known_or_no_ams_send_nothing() -> None:
    with_serial, _ = ams_printer([{"id": "0"}], [{"name": "ams/0", "sn": "X"}])
    unit_serial, _ = ams_printer([{"id": "0", "sn": "Y"}])
    no_ams, _ = ams_printer([])
    for service in (with_serial, unit_serial, no_ams):
        await inventory_sources.refresh_source_frame(service, wait_s=0.1)
        service.send_command.assert_not_awaited()


async def test_missing_serial_is_not_asked_while_disconnected() -> None:
    offline, _ = ams_printer([{"id": "0"}], connected=False)
    await inventory_sources.ensure_ams_serials(offline, wait_s=0.05)
    offline.send_command.assert_not_awaited()


async def test_unanswered_version_request_is_retried_three_times_then_stops() -> None:
    silent, _ = ams_printer([{"id": "0"}])  # the printer never answers
    started = time.monotonic()
    await inventory_sources.ensure_ams_serials(silent, wait_s=0.05)
    assert silent.send_command.await_count == 3
    assert time.monotonic() - started < 2


async def test_measured_reply_time_sets_the_next_wait_to_three_times_it() -> None:
    service, info = ams_printer([{"id": "0"}])

    async def reply(*_args) -> None:
        async def arrive() -> None:
            await asyncio.sleep(0.2)
            info["module"] = [{"name": "ams/0", "sn": "S"}]
            service.raw_bus.publish(Event("snapshot", {"info": {}}))

        asyncio.get_running_loop().create_task(arrive())

    service.send_command.side_effect = reply
    await inventory_sources.ensure_ams_serials(service)
    measured = inventory_sources.version_wait_s(service.serial)
    assert 0.55 <= measured <= 1.5  # 3 x ~0.2 s, with scheduling slack
    assert inventory_sources.version_wait_s("never-seen") == inventory_sources.VERSION_WAIT_S
