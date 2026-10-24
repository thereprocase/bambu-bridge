import asyncio
import contextlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bambu_bridge.service.events import Event, EventBus
from bambu_bridge.service.refill_adapter import PhysicalRefillAdapter
from bambu_bridge.service.refill_reports import RefillReports
from tests.test_refill_reports import raw


def fixture(*, qualified=True):
    tracker = RefillReports()
    tracker.observe(raw())
    printer = SimpleNamespace(
        connected=True,
        refill_reports=tracker,
        material_inventory=MagicMock(),
        raw_bus=EventBus(),
        send_raw=AsyncMock(),
    )
    adapter = PhysicalRefillAdapter(printer, qualified=qualified, timeout=0.01)
    slot = tracker.snapshot(connected=True).slots[0]
    return adapter, printer, replace(slot.declaration, tray_color="FFFFFFFF")


async def test_default_gate_sends_nothing():
    adapter, printer, target = fixture(qualified=False)
    with pytest.raises(ValueError, match="not qualified"):
        await adapter.send(0, target)
    printer.send_raw.assert_not_awaited()


async def test_color_only_command_waits_for_new_target_raw_receipt():
    adapter, printer, target = fixture()
    before = (await adapter.observe()).slots[0]

    async def send(command):
        # Unrelated telemetry and a partial color frame cannot confirm the write.
        printer.refill_reports.observe({"print": {"nozzle_temper": 220}})
        printer.refill_reports.observe(raw("FFFFFFFF"))
        printer.raw_bus.publish(Event("snapshot", {}))

    printer.send_raw.side_effect = send
    await adapter.send(0, target)
    command = printer.send_raw.call_args.args[0]["print"]
    assert command["command"] == "ams_filament_setting"
    assert command["tray_color"] == "FFFFFFFF"
    assert command["tray_info_idx"] == before.declaration.tray_info_idx
    assert command["tray_type"] == before.declaration.tray_type
    after = (await adapter.observe()).slots[0]
    assert after.source_generation == before.source_generation and after.receipt > before.receipt
    printer.send_raw.assert_awaited_once()


@pytest.mark.parametrize("cause", ["no_readback", "same_old_report", "disconnect", "replacement"])
async def test_uncertain_send_is_never_retried_by_adapter(cause):
    adapter, printer, target = fixture()

    async def send(command):
        if cause == "same_old_report":
            printer.refill_reports.observe(raw())
        elif cause == "disconnect":
            printer.connected = False
        elif cause == "replacement":
            printer.refill_reports.observe(raw("FFFFFFFF", serial="replacement-ams"))
        printer.raw_bus.publish(Event("snapshot", {}))

    printer.send_raw.side_effect = send
    with pytest.raises((TimeoutError, ConnectionError, ValueError)):
        await adapter.send(0, target)
    printer.send_raw.assert_awaited_once()


async def test_another_material_is_rejected_before_send():
    adapter, printer, target = fixture()
    with pytest.raises(ValueError, match="never changes the declared material"):
        await adapter.send(0, replace(target, tray_type="ASA"))
    printer.send_raw.assert_not_awaited()


async def test_timeout_is_durable_uncertainty_and_controller_never_resends(tmp_path):
    from bambu_bridge.refill import RefillController, RefillStore

    adapter, printer, target = fixture()
    store = RefillStore(tmp_path)
    controller = RefillController(store)
    controller.qualified = True
    try:
        controller.plan("fixture", await adapter.observe(), [0, 1], target.tray_color, revision=0)
        with pytest.raises(TimeoutError):
            await controller.drive("fixture", adapter.observe, adapter.send)
        group = store.get("fixture")
        assert group["state"] == "needs_attention"
        assert group["steps"][0]["state"] == "uncertain"
        await controller.drive("fixture", adapter.observe, adapter.send)
        printer.send_raw.assert_awaited_once()
        store.close()
        store = RefillStore(tmp_path)
        controller = RefillController(store)
        controller.qualified = True
        await controller.drive("fixture", adapter.observe, adapter.send)
        assert store.get("fixture")["steps"][0]["state"] == "uncertain"
        printer.send_raw.assert_awaited_once()
    finally:
        store.close()


def restoration_fixture(tmp_path, monkeypatch, *, state="IDLE", readback=True):
    from bambu_bridge.refill import RefillController, RefillStore
    from bambu_bridge.service import refill_adapter
    from tests.api.test_inventory_refill import colors

    monkeypatch.setattr(refill_adapter, "QUALIFIED", True)
    tracker = RefillReports()
    declared = {0: "FFFFFFFF", 1: "FFFFFFFF"}

    def report():
        body = colors(declared[0], declared[1])
        body["print"]["gcode_state"] = state
        return body

    tracker.observe(colors("FF0000FF", "0000FFFF"))
    controller = RefillController(RefillStore(tmp_path))
    controller.qualified = True
    group = controller.plan("p1", tracker.snapshot(connected=True), [0, 1], "FFFFFFFF", revision=0)
    # The aliases were applied earlier and are owned; the operator then disabled.
    expected = tracker.snapshot(connected=True)
    for wire in (0, 1):
        tracker.expect(
            wire,
            expected.slots[wire].source_generation,
            replace(expected.slots[wire].declaration, tray_color="FFFFFFFF"),
        )
    tracker.observe(report())
    for step in group["steps"]:
        step["state"] = "owned"
    group = controller.store.save("p1", group, group["revision"])
    group = controller.disable("p1", revision=group["revision"])
    printer = SimpleNamespace(
        serial="p1",
        connected=True,
        refill_reports=tracker,
        material_inventory=MagicMock(),
        raw_bus=EventBus(),
        send_raw=AsyncMock(),
    )

    async def send(command):
        if readback:
            body = command["print"]
            declared[body["tray_id"]] = body["tray_color"]
            tracker.observe(report())
        printer.raw_bus.publish(Event("snapshot", {}))

    printer.send_raw.side_effect = send
    return printer, controller, declared


async def run_worker(printer, controller, seconds=0.3):
    from bambu_bridge.service.refill_adapter import restore_when_idle

    task = asyncio.create_task(restore_when_idle(lambda: [printer], controller, interval=0.02))
    await asyncio.sleep(seconds)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_worker_restores_requested_owned_aliases_once_idle(tmp_path, monkeypatch):
    printer, controller, declared = restoration_fixture(tmp_path, monkeypatch)
    await run_worker(printer, controller)
    group = controller.store.get("p1")
    assert [s["state"] for s in group["steps"]] == ["restored", "restored"]
    assert declared == {0: "FF0000FF", 1: "0000FFFF"}
    assert printer.send_raw.await_count == 2
    controller.store.close()


async def test_worker_waits_while_printing(tmp_path, monkeypatch):
    printer, controller, _ = restoration_fixture(tmp_path, monkeypatch, state="RUNNING")
    await run_worker(printer, controller)
    assert controller.store.get("p1")["state"] == "restoration_pending"
    printer.send_raw.assert_not_awaited()
    controller.store.close()


async def test_worker_does_nothing_unqualified(tmp_path, monkeypatch):
    from bambu_bridge.service import refill_adapter

    printer, controller, _ = restoration_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(refill_adapter, "QUALIFIED", False)
    await run_worker(printer, controller)
    printer.send_raw.assert_not_awaited()
    controller.qualified = False
    monkeypatch.setattr(refill_adapter, "QUALIFIED", True)
    await run_worker(printer, controller)
    printer.send_raw.assert_not_awaited()
    controller.store.close()


async def test_worker_leaves_uncertain_restoration_for_review(tmp_path, monkeypatch):
    printer, controller, _ = restoration_fixture(tmp_path, monkeypatch, readback=False)
    from bambu_bridge.service import refill_adapter

    original = refill_adapter.PhysicalRefillAdapter
    monkeypatch.setattr(
        refill_adapter, "PhysicalRefillAdapter", lambda p: original(p, timeout=0.05)
    )
    await run_worker(printer, controller, seconds=0.6)
    group = controller.store.get("p1")
    assert group["state"] == "needs_attention"
    assert group["steps"][0]["state"] == "uncertain"
    # One send for the first slot, then the worker stops acting on this group.
    assert printer.send_raw.await_count == 1
    controller.store.close()


def test_the_readback_wait_allows_three_times_the_slowest_seen_on_hardware():
    # N9 on the P1S: alias readback about 4 s, restore readback about 6 s.
    adapter = PhysicalRefillAdapter(SimpleNamespace())
    assert adapter.timeout >= 3 * 6.0


async def test_a_send_that_keeps_being_refused_is_logged_once(tmp_path, monkeypatch):
    from bambu_bridge.refill import NotSent
    from bambu_bridge.service import refill_adapter

    printer, controller, _ = restoration_fixture(tmp_path, monkeypatch)

    async def refuse(self, wire, target):
        raise NotSent("fresh idle untagged physical source is required")

    monkeypatch.setattr(refill_adapter.PhysicalRefillAdapter, "send", refuse)
    log = MagicMock()
    monkeypatch.setattr(refill_adapter, "log", log)
    await run_worker(printer, controller)  # about fifteen idle passes
    events = [c.args[0] for c in log.mock_calls if c.args and str(c.args[0]).startswith("refill.")]
    assert events == ["refill.restore_waiting"]
    printer.send_raw.assert_not_awaited()
    controller.store.close()
