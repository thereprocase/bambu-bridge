"""Refill ownership across printer reconnects and sends refused before reaching it."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bambu_bridge.refill import NotSent, RefillController, RefillStore
from bambu_bridge.service.events import EventBus
from bambu_bridge.service.refill_adapter import PhysicalRefillAdapter
from bambu_bridge.service.refill_reports import RefillReports


def payload(colors, serial="ams-A"):
    trays = [
        {
            "id": str(wire),
            "tray_info_idx": "GFG99",
            "tray_type": "PETG",
            "tray_color": colors[wire],
            "nozzle_temp_min": 220,
            "nozzle_temp_max": 260,
            "tag_uid": "0",
        }
        for wire in (0, 1)
    ]
    unit = {"id": "0", "sn": serial, "tray": trays}
    ams = {"ams_exist_bits": "1", "tray_exist_bits": "3", "ams": [unit]}
    return {"print": {"gcode_state": "IDLE", "home_flag": 1024, "filam_bak": [3], "ams": ams}}


def make_printer(colors):
    tracker = RefillReports()
    tracker.observe(payload(colors))
    printer = SimpleNamespace(
        connected=True, refill_reports=tracker, raw_bus=EventBus(), send_raw=AsyncMock()
    )

    async def lands(envelope):
        command = envelope["print"]
        colors[int(command["tray_id"])] = command["tray_color"]
        tracker.observe(payload(colors))

    printer.send_raw.side_effect = lands
    return printer


@pytest.fixture
def controller(tmp_path):
    store = RefillStore(tmp_path)
    yield RefillController(store)
    store.close()


async def test_a_reconnect_to_the_same_trays_keeps_owned_aliases_restorable(controller):
    colors = {0: "FF0000FF", 1: "00FF00FF"}
    printer = make_printer(colors)
    adapter = PhysicalRefillAdapter(printer, timeout=1)
    report = printer.refill_reports.snapshot(connected=True)
    controller.plan("p", report, [0, 1], "FFFFFFFF", revision=0)
    group = await controller.drive("p", adapter.observe, adapter.send)
    assert colors == {0: "FFFFFFFF", 1: "FFFFFFFF"}
    group = controller.disable("p", revision=group["revision"])
    receipt = printer.refill_reports.snapshot(connected=True).slots[0].receipt
    printer.refill_reports.clear()  # session lost, then reconnected
    printer.refill_reports.observe(payload(colors))
    assert printer.refill_reports.snapshot(connected=True).slots[0].receipt > receipt
    group = await controller.drive("p", adapter.observe, adapter.send)
    assert [s["state"] for s in group["steps"]] == ["restored", "restored"]
    assert group["state"] == "disabled" and colors == {0: "FF0000FF", 1: "00FF00FF"}


def test_a_tray_that_changed_while_disconnected_is_a_new_source():
    tracker = RefillReports()
    tracker.observe(payload({0: "FF0000FF", 1: "00FF00FF"}))
    before = tracker.snapshot(connected=True).slots
    tracker.clear()
    tracker.observe(payload({0: "FF0000FF", 1: "0000FFFF"}))
    after = tracker.snapshot(connected=True).slots
    assert after[0].source_generation == before[0].source_generation
    assert after[1].source_generation != before[1].source_generation
    tracker.clear()
    tracker.observe(payload({0: "FF0000FF", 1: "0000FFFF"}, serial="another-ams"))
    assert tracker.snapshot(connected=True).slots[0].source_generation != (
        after[0].source_generation
    )


async def test_a_send_refused_before_reaching_the_printer_can_run_again(controller):
    colors = {0: "FF0000FF", 1: "00FF00FF"}
    printer = make_printer(colors)
    adapter = PhysicalRefillAdapter(printer, timeout=1)
    report = printer.refill_reports.snapshot(connected=True)
    controller.plan("p", report, [0, 1], "FFFFFFFF", revision=0)

    async def refuse(_wire, _target):
        raise NotSent("fresh idle untagged physical source is required")

    with pytest.raises(NotSent):
        await controller.drive("p", adapter.observe, refuse)
    printer.send_raw.assert_not_awaited()
    group = controller.store.get("p")
    assert group["steps"][0]["state"] == "pending" and group["state"] == "pending"
    group = await controller.drive("p", adapter.observe, adapter.send)
    assert [s["state"] for s in group["steps"]] == ["owned", "owned"]


async def test_the_adapter_refuses_a_busy_printer_without_sending():
    colors = {0: "FF0000FF", 1: "00FF00FF"}
    printer = make_printer(colors)
    busy = payload(colors)
    busy["print"]["gcode_state"] = "RUNNING"
    printer.refill_reports.observe(busy)
    target = printer.refill_reports.snapshot(connected=True).slots[1].declaration
    with pytest.raises(NotSent):
        await PhysicalRefillAdapter(printer, timeout=1).send(1, target)
    printer.send_raw.assert_not_awaited()
