"""A stored group survives a bridge restart and a tag's slot never blocks it."""

from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Any

from bambu_bridge.refill import Declaration, RefillController, RefillStore
from bambu_bridge.service.refill_reports import RefillReports

WHITE = {"tray_info_idx": "GFG99", "tray_color": "FFFFFFFF", "nozzle_temp_min": 230}
BROWN = {"tray_info_idx": "GFG01", "tray_color": "8B5A2BFF", "nozzle_temp_min": 220}


def packet(trays: dict[int, dict[str, Any]], *, presence: str = "3", mask: int = 3) -> dict:
    base = {"tray_type": "PETG", "nozzle_temp_max": 260, "tag_uid": "0000000000000000"}
    return {
        "print": {
            "gcode_state": "IDLE",
            "home_flag": 1024,
            "filam_bak": [mask],
            "ams": {
                "ams_exist_bits": "1",
                "tray_exist_bits": presence,
                "ams": [
                    {
                        "id": "0",
                        "sn": "ams-a",
                        "tray": [{"id": str(w), **base, **f} for w, f in trays.items()],
                    }
                ],
            },
        }
    }


class Printer:
    """Raw reports in, writes applied the way the P1S reads them back."""

    def __init__(self, trays: dict[int, dict[str, Any]]) -> None:
        self.trays = copy.deepcopy(trays)
        self.reports = RefillReports()
        self.writes: list[int] = []
        self.report()

    def report(self, **kw: Any) -> None:
        self.reports.observe(packet(self.trays, **kw))

    async def observe(self):
        return self.reports.snapshot(connected=True)

    async def send(self, wire: int, target: Declaration) -> None:
        slot = self.reports.snapshot(connected=True).slots[wire]
        self.reports.expect(wire, slot.source_generation, target)
        self.writes.append(wire)
        fields = asdict(target)
        self.trays[wire] = {
            k: fields[k] for k in ("tray_info_idx", "tray_color", "nozzle_temp_min")
        }
        self.report()


def restart(printer: Printer, store: RefillStore) -> RefillController:
    """A new bridge run: the stored generations are from the old one."""
    group = store.get("p1")
    assert group is not None
    for step in group["steps"]:
        step["source_generation"] = "earlier-run-" + step["source_generation"].split("-")[-1]
    store.save("p1", group, group["revision"])
    printer.reports = RefillReports()
    printer.report()
    return RefillController(store)


async def applied(tmp_path, trays) -> tuple[Printer, RefillStore, RefillController]:
    printer = Printer(trays)
    store = RefillStore(tmp_path)
    controller = RefillController(store)
    controller.plan("p1", await printer.observe(), [0, 1], "FFFFFFFF", revision=0)
    group = await controller.drive("p1", printer.observe, printer.send)
    assert group["state"] == "active", group
    return printer, store, controller


async def test_a_stored_group_is_active_again_after_a_restart_and_removes_cleanly(tmp_path):
    printer, store, _ = await applied(tmp_path, {0: WHITE, 1: BROWN})
    try:
        controller = restart(printer, store)
        report = await printer.observe()
        assert controller.status("p1", report)["state"] == "pending"
        controller.note("p1", report)  # the first fresh report re-adopts the group
        assert controller.status("p1", await printer.observe())["state"] == "active"
        group = controller.disable("p1", revision=store.get("p1")["revision"])
        group = await controller.drive("p1", printer.observe, printer.send)
        assert group["state"] == "disabled"
        assert printer.trays[1] == BROWN  # every field back, not only colour
    finally:
        store.close()


async def test_a_slot_changed_during_this_run_is_not_re_adopted(tmp_path):
    printer, store, _ = await applied(tmp_path, {0: WHITE, 1: BROWN})
    try:
        controller = restart(printer, store)
        printer.report(presence="1")  # slot 2 pulled and the same-looking spool loaded
        printer.report()
        controller.note("p1", await printer.observe())
        assert controller.status("p1", await printer.observe())["state"] == "pending"
    finally:
        store.close()


TAG = {**BROWN, "tag_uid": "1234567890ABCDEF"}


async def test_a_tag_anchor_survives_its_re_read_and_an_empty_tag_slot_removes(tmp_path):
    printer = Printer({0: WHITE, 1: TAG})
    store = RefillStore(tmp_path)
    controller = RefillController(store)
    try:
        controller.plan("p1", await printer.observe(), [0, 1], "8B5A2BFF", revision=0)
        group = await controller.drive("p1", printer.observe, printer.send)
        assert group["state"] == "active" and printer.writes == [0]
        # A tag re-read: the tray goes blank, then reads the same tag again.
        before = (await printer.observe()).slots[1].source_generation
        printer.reports.observe(
            {"print": {"ams": {"ams": [{"id": "0", "tray": [{"id": "1", "tag_uid": ""}]}]}}}
        )
        printer.report()
        report = await printer.observe()
        assert report.slots[1].source_generation != before
        assert controller.status("p1", report)["state"] == "active"
        # The tagged primary ran out: its slot is empty when the backup is removed.
        printer.report(presence="1")
        controller.disable("p1", revision=store.get("p1")["revision"])
        group = await controller.drive("p1", printer.observe, printer.send)
        assert group["state"] == "disabled"
        assert printer.trays[0] == WHITE and printer.writes == [0, 0]
    finally:
        store.close()
