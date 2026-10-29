"""Putting a spool in a slot that reports another material sets the slot first."""

import contextlib
from types import SimpleNamespace

import pytest

from bambu_bridge.api import inventory as api
from bambu_bridge.inventory_journal import InventoryConflict
from bambu_bridge.protocol import commands
from bambu_bridge.slot_setting import slot_setting


def test_external_holder_envelope_matches_orca():
    envelope = commands.ams_filament_setting(
        ams_id=255, tray_id=254, **slot_setting("pet-gf", "#000000")
    )["print"]
    assert (envelope["ams_id"], envelope["tray_id"], envelope["slot_id"]) == (255, 254, 0)
    assert envelope["tray_type"] == "PET-GF"
    assert (envelope["nozzle_temp_min"], envelope["nozzle_temp_max"]) == (280, 300)
    assert envelope["tray_color"] == "000000FF"
    ams = commands.ams_filament_setting(ams_id=0, tray_id=2, **slot_setting("PETG", None))
    assert "slot_id" not in ams["print"] and ams["print"]["tray_info_idx"] == "GFG99"


def _stock_generic_ids():
    import json
    from pathlib import Path

    import bambu_bridge

    data = json.loads(
        (Path(bambu_bridge.__file__).parent / "data" / "stock_materials_v242.json").read_text()
    )
    found = {}

    def walk(node):
        if isinstance(node, dict):
            name = node.get("name")
            if isinstance(name, str) and name.startswith("Generic ") and node.get("material_id"):
                found[name.removeprefix("Generic ").upper()] = node["material_id"]
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return found


def test_every_slot_material_with_a_stock_generic_preset_gets_its_id():
    # A PCTG slot written with an empty tray_info_idx vanished from Orca's AMS list (2026-10-28).
    from bambu_bridge.slot_setting import GENERIC_TRAY_INFO_IDX, MATERIAL_NOZZLE_C

    stock = _stock_generic_ids()
    expected = {m: stock[m] for m in MATERIAL_NOZZLE_C if m in stock}
    assert expected == GENERIC_TRAY_INFO_IDX
    assert slot_setting("pctg", "F2D7DC")["tray_info_idx"] == "GFG97"


@pytest.mark.parametrize("material", ["", "PEEK", "wood"])
def test_materials_the_printer_cannot_hold_are_refused(material):
    with pytest.raises(ValueError):
        slot_setting(material, "FFFFFF")


# No print in the bridge's inbox: only tray_now marks a slot as in use.
REQUEST = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


class Bus:
    def __init__(self, printer):
        self.printer = printer

    @contextlib.asynccontextmanager
    async def subscribe(self):
        async def reports():
            while self.printer.pending:
                yield self.printer.pending.pop(0)()

        yield reports()


class Printer:
    """A slot that reports the new material once the printer applies the setting."""

    def __init__(self, material="PC", tagged=False, applies=True, view=None):
        self.choice = {
            "source": "external",
            "wire_slot": 254,
            "label": "External spool",
            "material": material,
            "tagged": tagged,
            "issues": [],
        }
        self.applies = applies
        self.sent = []
        self.pending = []
        self.raw_bus = Bus(self)
        self.view = view or {"gcode_state": "FAILED", "tray_now": "255"}
        self.serial = "fixture"

    async def send_raw(self, envelope):
        self.sent.append(envelope)
        if self.applies:
            self.pending.append(lambda: self.choice.update(material=envelope["print"]["tray_type"]))

    async def request_full_report(self):
        pass

    def print_view(self):
        return self.view


def service(material="PET-GF"):
    spool = SimpleNamespace(
        archived=False,
        filament=SimpleNamespace(name=f"{material} Black", material=material, color_hex="000000"),
    )

    async def get_spool(_spool_id):
        return spool

    return SimpleNamespace(provider=SimpleNamespace(get_spool=get_spool))


def body():
    return api.AssignSpool(
        provider_id="9a03afcc-c08f-4203-a400-c22611bbdc28",
        expected_revision=5,
        spool_id=5,
        source="external",
        set_slot=True,
    )


@pytest.fixture
def printer(monkeypatch):
    def make(**kwargs):
        device = Printer(**kwargs)
        monkeypatch.setattr(api, "source_choices", lambda _p: [dict(device.choice)])
        return device

    return make


async def test_an_untagged_slot_is_set_to_the_spool_material(printer):
    device = printer()
    await api._set_slot_for(service(), device, body(), REQUEST)
    assert len(device.sent) == 1
    assert device.sent[0]["print"]["tray_type"] == "PET-GF"
    assert device.choice["material"] == "PET-GF"


async def test_a_matching_slot_is_left_alone(printer):
    device = printer(material="pet-gf")
    await api._set_slot_for(service(), device, body(), REQUEST)
    assert device.sent == []


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"tagged": True}, "RFID spool"),
        ({"view": {"gcode_state": "RUNNING", "tray_now": "254"}}, "part of the current print"),
        ({"applies": False}, "did not confirm"),
    ],
)
async def test_refusals_never_assign(printer, monkeypatch, kwargs, message):
    monkeypatch.setattr("bambu_bridge.slot_setting.REPORT_WAIT_S", 0.05)
    device = printer(**kwargs)
    with pytest.raises(InventoryConflict, match=message):
        await api._set_slot_for(service(), device, body(), REQUEST)
    if "applies" not in kwargs:
        assert device.sent == []


async def test_a_slow_printer_is_asked_again_until_it_reports(printer, monkeypatch):
    monkeypatch.setattr("bambu_bridge.slot_setting.REPORT_REQUEST_EVERY_S", 0.02)
    device = printer(applies=False)
    asks = []

    async def request_full_report():
        asks.append(1)
        if len(asks) == 3:  # applied only by the third full report
            device.choice["material"] = device.sent[-1]["print"]["tray_type"]
            device.pending.append(lambda: None)

    device.request_full_report = request_full_report
    await api._set_slot_for(service(), device, body(), REQUEST)
    assert device.choice["material"] == "PET-GF" and len(asks) == 3


async def test_a_slot_in_a_refill_group_is_not_rewritten(printer):
    device = printer()
    device.choice["wire_slot"] = 2
    group = {"enabled": True, "state": "active", "steps": [{"wire": 2}, {"wire": 3}]}
    inventory = service()
    inventory.refill = SimpleNamespace(store=SimpleNamespace(get=lambda _printer: group))
    with pytest.raises(InventoryConflict, match="refill backup"):
        await api._set_slot_for(inventory, device, body(), REQUEST)
    assert device.sent == []
