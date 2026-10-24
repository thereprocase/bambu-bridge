"""An untagged slot follows its spool's colour; tags, refill aliases and the print are left be."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from bambu_bridge.inventory_sources import declared_identity, source_choices
from bambu_bridge.service.material_inventory import MaterialInventory
from bambu_bridge.service.slot_colour import ColourSync, rgb, wires_in_use
from tests.test_inventory_service import open_service
from tests.test_tagged_spools import Spoolman

TRAY = {
    "tray_type": "PETG",
    "tray_info_idx": "GFG99",
    "tray_color": "F330F9FF",
    "nozzle_temp_min": "220",
    "nozzle_temp_max": "270",
}


class Bus:
    def __init__(self, printer: Printer) -> None:
        self.printer = printer

    @contextlib.asynccontextmanager
    async def subscribe(self):
        async def reports():
            while self.printer.pending:
                yield self.printer.pending.pop(0)()

        yield reports()


class Printer:
    """One AMS with slot 1 loaded; a sent setting is applied to the frame when `applies`."""

    def __init__(self, *, tagged: bool = False, applies: bool = True, view=None) -> None:
        tray = {"id": "0", **TRAY}
        if tagged:
            tray["tag_uid"] = "1234567890ABCDEF"
        trays = [tray] + [
            {"id": str(i), "tray_type": "", "tray_color": "000000FF"} for i in (1, 2, 3)
        ]
        self.material_inventory = MaterialInventory()
        self.material_inventory.observe(
            {
                "ams": {
                    "ams": [{"id": "0", "sn": "test-ams", "tray": trays}],
                    "ams_exist_bits": "1",
                    "tray_exist_bits": "1",
                }
            }
        )
        self.serial = "p1"
        self.connected = True
        self.applies = applies
        self.sent: list[dict[str, Any]] = []
        self.pending: list[Any] = []
        self.raw_bus = Bus(self)
        self.view = view or {"gcode_state": "IDLE", "tray_now": "255"}

    def native_snapshot(self) -> dict[str, Any]:
        return {}

    def print_view(self) -> dict[str, Any]:
        return self.view

    async def request_full_report(self) -> None:
        pass

    async def send_raw(self, envelope: dict[str, Any]) -> None:
        self.sent.append(envelope)
        if self.applies:

            def apply() -> None:
                frame = self.material_inventory.snapshot()["ams"]
                body = envelope["print"]
                frame["ams"][0]["tray"][body["tray_id"]].update(
                    tray_color=body["tray_color"],
                    tray_type=body["tray_type"],
                    tray_info_idx=body["tray_info_idx"],
                    nozzle_temp_min=str(body["nozzle_temp_min"]),
                    nozzle_temp_max=str(body["nozzle_temp_max"]),
                )
                self.material_inventory.observe({"ams": frame})

            self.pending.append(apply)


async def seated(tmp_path: Path, **printer_kwargs: Any):
    """A service with spool #7 (1A1A1A) assigned to slot 1 of a fresh printer."""
    spoolman = Spoolman()
    spoolman.tables["vendor"][1] = {"id": 1, "name": "Bambu Lab"}
    spoolman._create(
        "filament",
        {
            "name": "PETG Basic",
            "vendor_id": 1,
            "material": "PETG",
            "color_hex": "1A1A1A",
            "weight": 1000,
            "density": 1.27,
        },
    )
    for _ in range(6):
        spoolman._create("spool", {"filament_id": 1, "remaining_weight": 500})
    spool = spoolman._create("spool", {"filament_id": 1, "remaining_weight": 500})
    assert spool["id"] == 7
    service = await open_service(tmp_path, spoolman)
    prn = Printer(**printer_kwargs)
    rows = source_choices(prn)
    await service.assign(
        spool_id=7,
        printer_id="p1",
        source=rows[0]["source"],
        observed_identity=rows[0]["observed_identity"],
        choices=lambda: source_choices(prn),
    )
    return service, spoolman, prn


def events_sink(events: list[dict[str, Any]]):
    async def record(**event: Any) -> int:
        events.append(event)
        return 1

    return record


async def run_sync(service, prn, colours: ColourSync, **kw):
    return await colours.sync(prn, source_choices(prn), await service.assignments.snapshot(), **kw)


async def test_an_untagged_slot_is_set_to_its_spools_colour_keeping_everything_else(tmp_path):
    service, _, prn = await seated(tmp_path)
    events: list[dict[str, Any]] = []
    try:
        before = source_choices(prn)[0]
        done = await run_sync(service, prn, ColourSync(service, events_sink(events)))
        assert [d["color"] for d in done] == ["1A1A1A"] and done[0]["confirmed"] is True
        assert len(prn.sent) == 1
        body = prn.sent[0]["print"]
        assert body["ams_id"] == 0 and body["tray_id"] == 0
        assert body["tray_color"] == "1A1A1AFF"
        assert body["tray_type"] == "PETG" and body["tray_info_idx"] == "GFG99"
        assert (body["nozzle_temp_min"], body["nozzle_temp_max"]) == (220, 270)
        after = source_choices(prn)[0]
        assert after["setting"]["tray_color"] == "1A1A1AFF"
        # Colour is not part of a slot's identity: the assignment stands.
        assert after["observed_identity"] == before["observed_identity"]
        assert declared_identity({**TRAY, "tray_color": "1A1A1AFF"}) == declared_identity(TRAY)
        snapshot = await service.assignments.snapshot()
        assert [a.spool_id for a in snapshot.assignments] == [7]
        assert events[0]["event_type"] == "slot_colour_set"
        assert events[0]["payload"]["text"] == "AMS A · slot 1 colour set to match #7"
    finally:
        await service.close()


async def test_a_matching_colour_is_left_alone_whatever_its_case_or_alpha(tmp_path):
    service, _, prn = await seated(tmp_path)
    try:
        frame = prn.material_inventory.snapshot()["ams"]
        frame["ams"][0]["tray"][0]["tray_color"] = "1a1a1a00"
        prn.material_inventory.observe({"ams": frame})
        assert await run_sync(service, prn, ColourSync(service)) == []
        assert not prn.sent
        assert rgb("#1a1a1aff") == rgb("1A1A1A") == "1A1A1A" and rgb("nope") is None
    finally:
        await service.close()


async def test_a_tagged_slot_is_never_written(tmp_path):
    service, _, prn = await seated(tmp_path, tagged=True)
    try:
        assert await run_sync(service, prn, ColourSync(service)) == []
        assert not prn.sent
    finally:
        await service.close()


async def test_a_refill_alias_slot_keeps_its_declared_colour(tmp_path):
    service, _, prn = await seated(tmp_path)
    try:
        service.refill.store.get = lambda _printer: {
            "enabled": True,
            "state": "pending",
            "steps": [{"wire": 0, "state": "pending"}, {"wire": 1, "state": "pending"}],
        }
        assert await run_sync(service, prn, ColourSync(service)) == []
        assert not prn.sent
    finally:
        await service.close()


class Inbox:
    """The native inbox's view of the running print: which slots its attempt maps."""

    def __init__(self, tool_wires: dict[str, int] | None = None) -> None:
        self.tool_wires = tool_wires

    def printing_attempt(self, printer: str):
        if self.tool_wires is None:
            return None
        return ({"id": "attempt", "printer": printer}, {"tool_wires": self.tool_wires})


async def test_a_slot_in_use_by_the_print_waits_until_it_is_free(tmp_path):
    service, _, prn = await seated(tmp_path, view={"gcode_state": "RUNNING", "tray_now": "0"})
    try:
        colours = ColourSync(service)
        assert await run_sync(service, prn, colours) == []
        assert not prn.sent
        prn.view = {"gcode_state": "FINISH", "tray_now": "0"}
        done = await run_sync(service, prn, colours)
        assert [d["color"] for d in done] == ["1A1A1A"] and len(prn.sent) == 1
    finally:
        await service.close()


async def test_a_slot_the_print_maps_but_is_not_feeding_now_also_waits(tmp_path):
    service, _, prn = await seated(tmp_path, view={"gcode_state": "RUNNING", "tray_now": "2"})
    try:
        inbox = Inbox({"T0": 2, "T1": 0})  # a two-colour print: slot 3 feeding, slot 1 mapped
        colours = ColourSync(service, inbox=lambda: inbox)
        assert await run_sync(service, prn, colours) == []
        assert not prn.sent
        inbox.tool_wires = None  # the attempt ends; the printer reports the next state
        prn.view = {"gcode_state": "FINISH", "tray_now": "2"}
        done = await run_sync(service, prn, colours)
        assert [d["color"] for d in done] == ["1A1A1A"] and len(prn.sent) == 1
    finally:
        await service.close()


async def test_wires_in_use_is_empty_between_prints() -> None:
    assert await wires_in_use(Printer(), Inbox({"T0": 0})) == set()
    printing = Printer(view={"gcode_state": "PAUSE", "tray_now": "1"})
    assert await wires_in_use(printing, Inbox({"T0": 1, "T1": 3})) == {"1", "3"}
    assert await wires_in_use(printing, None) == {"1"}


async def test_each_slot_and_colour_is_written_once_per_run(tmp_path):
    service, _, prn = await seated(tmp_path, applies=False)
    try:
        colours = ColourSync(service)
        done = await run_sync(service, prn, colours)
        assert len(done) == 1 and done[0]["confirmed"] is False
        assert await run_sync(service, prn, colours) == []
        assert len(prn.sent) == 1
    finally:
        await service.close()


async def test_a_spool_edited_in_spoolman_moves_the_slot_colour(tmp_path):
    service, spoolman, prn = await seated(tmp_path)
    try:
        colours = ColourSync(service)
        await run_sync(service, prn, colours)
        spoolman.tables["filament"][1]["color_hex"] = "2255AA"  # the owner edits it in Spoolman
        colours.forget(7)
        done = await run_sync(service, prn, colours)
        assert [d["color"] for d in done] == ["2255AA"]
        assert prn.sent[-1]["print"]["tray_color"] == "2255AAFF"
    finally:
        await service.close()


async def test_the_external_holder_is_written_with_its_own_ids(tmp_path):
    spoolman = Spoolman()
    spoolman.tables["vendor"][1] = {"id": 1, "name": "Bambu Lab"}
    spoolman._create(
        "filament",
        {
            "name": "PLA",
            "vendor_id": 1,
            "material": "PLA",
            "color_hex": "FF0000",
            "weight": 1000,
            "density": 1.24,
        },
    )
    spoolman._create("spool", {"filament_id": 1, "remaining_weight": 500})
    service = await open_service(tmp_path, spoolman)
    prn = Printer()
    try:
        prn.material_inventory.observe(
            {
                "vt_tray": {
                    "id": "254",
                    "tray_type": "PLA",
                    "tray_info_idx": "GFL99",
                    "tray_color": "000000FF",
                    "nozzle_temp_min": "180",
                    "nozzle_temp_max": "240",
                }
            }
        )

        def apply_external() -> None:
            body = prn.sent[-1]["print"]
            prn.material_inventory.observe(
                {
                    "vt_tray": {
                        "id": "254",
                        "tray_type": "PLA",
                        "tray_info_idx": "GFL99",
                        "tray_color": body["tray_color"],
                        "nozzle_temp_min": "180",
                        "nozzle_temp_max": "240",
                    }
                }
            )

        async def send_raw(envelope):
            prn.sent.append(envelope)
            prn.pending.append(apply_external)

        prn.send_raw = send_raw  # type: ignore[method-assign]
        external = next(r for r in source_choices(prn) if r["wire_slot"] == 254)
        await service.assign(
            spool_id=1,
            printer_id="p1",
            source="external",
            observed_identity=external["observed_identity"],
            choices=lambda: source_choices(prn),
        )
        done = await run_sync(service, prn, ColourSync(service))
        assert [d["wire_slot"] for d in done] == [254]
        body = prn.sent[-1]["print"]
        assert (body["ams_id"], body["tray_id"]) == (255, 254) and body["tray_color"] == "FF0000FF"
    finally:
        await service.close()


def test_rows_carry_the_slots_current_setting() -> None:
    row = source_choices(Printer())[0]
    assert row["setting"] == {
        "tray_type": "PETG",
        "tray_info_idx": "GFG99",
        "tray_color": "F330F9FF",
        "nozzle_temp_min": 220,
        "nozzle_temp_max": 270,
    }
    assert source_choices(Printer())[1]["setting"] is None  # an empty slot reports no temps


async def test_a_refill_group_keeps_its_slots_until_removed_then_hands_them_back(tmp_path):
    service, _, prn = await seated(tmp_path, applies=False)
    group: dict[str, Any] | None = None
    service.refill.store.get = lambda _printer: group
    try:
        colours = ColourSync(service)
        assert len(await run_sync(service, prn, colours)) == 1  # sent, not confirmed
        # The group declares slot 1 as another product and colour: left alone.
        group = {
            "enabled": True,
            "state": "active",
            "steps": [{"wire": 0, "state": "owned"}, {"wire": 1, "state": "owned"}],
        }
        assert await run_sync(service, prn, colours) == []
        group = {**group, "enabled": False, "state": "restoration_pending"}
        assert await run_sync(service, prn, colours) == []
        # Restored: the slot is colour sync's again, though written once this run.
        group = {**group, "state": "disabled"}
        assert len(await run_sync(service, prn, colours)) == 1
        assert len(prn.sent) == 2
    finally:
        await service.close()
