import time
from types import SimpleNamespace

import pytest

from bambu_bridge.inventory_journal import InventoryConflict
from bambu_bridge.inventory_sources import source_choices
from bambu_bridge.service.material_inventory import MaterialInventory


def printer(*, hardware: str | None = "test-ams", connected: bool = True):
    unit = {
        "id": "0",
        "tray": [
            {"id": str(i), "tray_type": "ASA" if i == 0 else "", "tray_color": "000000FF"}
            for i in range(4)
        ],
    }
    if hardware:
        unit["sn"] = hardware
    material = MaterialInventory()
    material.observe({"ams": {"ams": [unit], "ams_exist_bits": "1", "tray_exist_bits": "1"}})
    return SimpleNamespace(
        material_inventory=material,
        native_snapshot=lambda: {},
        connected=connected,
        serial="p1",
    )


def test_sources_are_hardware_qualified_without_disclosing_serial() -> None:
    choices = source_choices(printer())
    assert len(choices) == 4
    assert choices[0]["assignable"] is True
    assert choices[0]["wire_slot"] == 0
    assert choices[0]["source"].startswith("ams:")
    assert "test-ams" not in choices[0]["source"]
    assert choices[1]["assignable"] is False
    assert source_choices(printer(hardware="replacement"))[0]["source"] != choices[0]["source"]


def test_unidentified_disconnected_and_stale_sources_not_assignable() -> None:
    assert not source_choices(printer(hardware=None))[0]["assignable"]
    assert not source_choices(printer(connected=False))[0]["assignable"]
    stale = printer()
    stale.material_inventory.ams_at = time.time() - 60
    assert source_choices(stale) == []


def recolor(service, color: str, **changes) -> None:
    frame = service.material_inventory.snapshot()["ams"]
    frame["ams"][0]["tray"][0].update(tray_color=color, **changes)
    service.material_inventory.observe({"ams": frame})


def key(service, wire: int = 0) -> str:
    return next(c for c in source_choices(service) if c["wire_slot"] == wire)["observed_identity"]


def test_material_key_ignores_colour_gaps_reconnects_and_reloads() -> None:
    service = printer()
    original = key(service)
    frame = service.material_inventory.snapshot()["ams"]
    recolor(service, "F330F9FF")  # deliberate refill alias colour
    assert key(service) == original
    service.material_inventory.ams_at = time.time() - 600  # quiet printer
    service.material_inventory.observe({"ams": frame})
    assert key(service) == original
    service.material_inventory.clear()  # reconnect
    service.material_inventory.observe({"ams": frame})
    assert key(service) == original


def test_empty_slot_is_not_assignable() -> None:
    service = printer()
    frame = service.material_inventory.snapshot()["ams"]
    service.material_inventory.observe({"ams": {**frame, "tray_exist_bits": "0"}})
    choice = source_choices(service)[0]
    assert not choice["assignable"]
    assert "source_empty" in choice["issues"]


@pytest.mark.parametrize(
    "changes",
    [{"tray_type": "PETG"}, {"tray_info_idx": "GFB98"}, {"tag_uid": "1234567890ABCDEF"}],
)
def test_material_sku_or_tag_change_changes_the_key(changes) -> None:
    service = printer()
    original = key(service)
    recolor(service, "000000FF", **changes)
    assert key(service) != original


def test_external_spool_key_follows_its_material() -> None:
    service = printer()
    material = service.material_inventory
    material.observe({"vt_tray": {"id": "254", "tray_type": "ASA", "tray_color": "000000FF"}})
    before = key(service, 254)
    material.observe({"vt_tray": {"id": "254", "tray_type": "ASA", "tray_color": "FF0000FF"}})
    assert key(service, 254) == before
    material.observe({"vt_tray": {"id": "254", "tray_type": "PC", "tray_color": "FF0000FF"}})
    assert key(service, 254) != before


async def assign(inventory, service, spool_id: int = 43, identity: str | None = None):
    snapshot = await inventory.assignments.snapshot()
    choice = source_choices(service)[0]
    return await inventory.assign(
        provider_id=snapshot.provider_id,
        expected_revision=snapshot.revision,
        spool_id=spool_id,
        printer_id=service.serial,
        source=choice["source"],
        observed_identity=identity,
        choices=lambda: source_choices(service),
    )


async def test_assignment_is_decided_by_the_bridge_at_commit_time(tmp_path) -> None:
    from tests.test_inventory_service import Provider, open_service

    inventory = await open_service(tmp_path, Provider())
    service = printer()
    stale = key(service)
    try:
        # The phone's list is minutes old and the colour has changed since:
        # the bridge reads the slot itself and accepts.
        recolor(service, "F330F9FF")
        await assign(inventory, service, identity=stale)
        stored = (await inventory.assignments.snapshot()).assignments
        assert [(a.spool_id, a.observed_identity) for a in stored] == [(43, key(service))]
    finally:
        await inventory.close()


@pytest.mark.parametrize(
    "change,message",
    [
        ("empty", "as empty"),
        ("material", "reports PETG"),
        ("disconnected", "isn't connected to the printer. Check that the printer is on"),
    ],
)
async def test_assignment_refusals_say_what_to_do(tmp_path, change, message) -> None:
    from tests.test_inventory_service import Provider, open_service

    inventory = await open_service(tmp_path, Provider())
    service = printer()
    frame = service.material_inventory.snapshot()["ams"]
    if change == "empty":
        service.material_inventory.observe({"ams": {**frame, "tray_exist_bits": "0"}})
    elif change == "material":
        recolor(service, "000000FF", tray_type="PETG")
    else:
        service.connected = False
    snapshot = await inventory.assignments.snapshot()
    try:
        with pytest.raises(InventoryConflict, match=message):
            await assign(inventory, service)
        assert await inventory.assignments.snapshot() == snapshot
    finally:
        await inventory.close()


def test_the_last_known_slots_are_listed_stale_and_never_assignable() -> None:
    stale = printer()
    stale.material_inventory.ams_at = time.time() - 60
    assert source_choices(stale) == []  # anything that acts on a slot sees none
    rows = source_choices(stale, stale_ok=True)
    assert len(rows) == 4 and all(row["stale"] for row in rows)
    assert all("ams_stale" in row["issues"] and not row["assignable"] for row in rows)
    assert 59 <= rows[0]["age_seconds"] <= 61
    fresh = source_choices(printer(), stale_ok=True)
    assert not fresh[0]["stale"] and fresh[0]["assignable"]


def test_a_new_spool_never_inherits_the_last_one_in_its_slot() -> None:
    material = MaterialInventory()
    trays = [{"id": str(i)} for i in range(4)]
    trays[1] = {
        "id": "1",
        "tray_type": "PETG",
        "tray_color": "FF0000FF",
        "tray_info_idx": "GFG99",
        "tag_uid": "A1B2C3D4E5F60708",
        "tray_uuid": "spool-a",
    }
    unit = {"id": "0", "sn": "test-ams", "tray": trays}
    material.observe({"ams": {"ams": [unit], "ams_exist_bits": "1", "tray_exist_bits": "2"}})
    material.observe({"ams": {"tray_exist_bits": "0"}})  # spool A taken out of slot 2
    inserted = {"ams": [{"id": "0", "tray": [{"id": "1", "tray_type": "PLA"}]}]}
    material.observe({"ams": {**inserted, "tray_exist_bits": "2"}})
    slot = material.snapshot()["ams"]["ams"][0]["tray"][1]
    assert slot == {"id": "1", "tray_type": "PLA"}


def tag_a_frame(**extra: str) -> dict:
    trays = [{"id": str(i)} for i in range(4)]
    trays[1] = {
        "id": "1",
        "tray_type": "PETG",
        "tray_color": "FF0000FF",
        "tray_info_idx": "GFG99",
        "tag_uid": "A1B2C3D4E5F60708",
        "tray_uuid": "spool-a",
    }
    unit = {"id": "0", "sn": "test-ams", "tray": trays}
    return {"ams": {"ams": [unit], "ams_exist_bits": "1", "tray_exist_bits": "2", **extra}}


def test_a_reported_tray_replaces_the_stored_one_whole() -> None:
    material = MaterialInventory()
    material.observe(tag_a_frame())
    again = {"id": "1", "tray_type": "PETG", "tray_info_idx": "GFG99"}  # no colour, no tag
    material.observe({"ams": {"ams": [{"id": "0", "tray": [again]}]}})
    trays = material.snapshot()["ams"]["ams"][0]["tray"]
    assert trays[1] == again  # nothing of tag A blended in
    assert [t["id"] for t in trays] == ["0", "1", "2", "3"]  # the others stay


def test_the_rfid_read_bits_stay_at_the_top_of_the_ams_block() -> None:
    material = MaterialInventory()
    material.observe(tag_a_frame(tray_reading_bits="2", tray_read_done_bits="0"))
    material.observe({"ams": {"tray_read_done_bits": "2"}})
    ams = material.snapshot()["ams"]
    assert (ams["tray_reading_bits"], ams["tray_read_done_bits"]) == ("2", "2")
