import asyncio
import contextlib
import time

import pytest

from bambu_bridge.inventory_journal import InventoryConflict
from bambu_bridge.inventory_sources import source_choices
from bambu_bridge.service import slot_display
from tests.test_inventory_service import Provider, open_service
from tests.test_inventory_sources import printer, recolor


def frame(service) -> dict:
    return service.material_inventory.snapshot()["ams"]


def test_unload_is_recorded_from_a_full_frame_or_a_presence_bit_drop() -> None:
    service = printer()
    material = service.material_inventory
    assert set(material.take_unloaded()) == set()
    material.observe({"ams": {**frame(service), "tray_exist_bits": "0"}})
    assert set(material.take_unloaded()) == {0}
    material.observe({"ams": {**frame(service), "tray_exist_bits": "1"}})
    material.observe({"ams": {"tray_exist_bits": "0"}})  # partial report
    material.observe({"ams": {"tray_exist_bits": "1"}})
    assert set(material.take_unloaded()) == {0}
    assert set(material.take_unloaded()) == set()


def test_humidity_colour_and_disconnects_are_not_unloads() -> None:
    service = printer()
    material = service.material_inventory
    material.observe({"ams": {"humidity": 30}})
    recolor(service, "FF00FFFF")
    material.clear()
    material.observe({"ams": frame(printer())})
    assert set(material.take_unloaded()) == set()


def test_external_spool_unload_is_recorded() -> None:
    service = printer()
    material = service.material_inventory
    material.observe({"vt_tray": {"id": "254", "tray_type": "PC", "tray_color": "161616FF"}})
    material.observe({"vt_tray": {"id": "254", "tray_type": ""}})
    assert set(material.take_unloaded()) == {254}


async def assigned(tmp_path, service):
    inventory = await open_service(tmp_path, Provider())
    snapshot = await inventory.assignments.snapshot()
    choice = source_choices(service)[0]
    await inventory.assign(
        provider_id=snapshot.provider_id,
        expected_revision=snapshot.revision,
        spool_id=43,
        printer_id=service.serial,
        source=choice["source"],
        observed_identity=choice["observed_identity"],
        choices=lambda: source_choices(service),
    )
    return inventory


@pytest.mark.parametrize(
    "change,released",
    [
        ("colour", False),
        ("nothing", False),
        ("unload_reload", True),
        ("empty", True),
        ("material", True),
        # Declarations: the bridge's own writes and printer-screen edits.
        ("sku", False),
        ("temperatures", False),
        ("relabel", False),
        ("rfid", True),
    ],
)
async def test_assignment_follows_what_the_ams_reports(tmp_path, change, released) -> None:
    service = printer()
    inventory = await assigned(tmp_path, service)
    material = service.material_inventory
    try:
        material.take_unloaded()
        if change == "colour":
            recolor(service, "F330F9FF")
        elif change == "unload_reload":
            material.observe({"ams": {"tray_exist_bits": "0"}})
            material.observe({"ams": {**frame(printer()), "tray_exist_bits": "1"}})
        elif change == "empty":
            material.observe({"ams": {**frame(service), "tray_exist_bits": "0"}})
        elif change == "material":
            recolor(service, "000000FF", tray_type="PETG")
        elif change == "sku":
            recolor(service, "000000FF", tray_info_idx="GFB98")
        elif change == "temperatures":
            recolor(service, "000000FF", nozzle_temp_min="250", nozzle_temp_max="270")
        elif change == "relabel":
            # Every field an ams_filament_setting write sets, except chemistry.
            recolor(
                service,
                "FFFFFFFF",
                tray_info_idx="GFB99",
                nozzle_temp_min="230",
                nozzle_temp_max="260",
            )
        elif change == "rfid":
            recolor(service, "000000FF", tag_uid="1234567890ABCDEF")
        dropped = await inventory.release_unseated(
            service.serial, source_choices(service), material.take_unloaded()
        )
        remaining = (await inventory.assignments.snapshot()).assignments
        assert bool(dropped) is released
        assert [a.spool_id for a in remaining] == ([] if released else [43])
    finally:
        await inventory.close()


async def test_other_printers_keep_their_assignments(tmp_path) -> None:
    service = printer()
    inventory = await assigned(tmp_path, service)
    try:
        dropped = await inventory.release_unseated("another-printer", [], {0: time.time()})
        assert dropped == []
        assert len((await inventory.assignments.snapshot()).assignments) == 1
    finally:
        await inventory.close()


async def test_worker_releases_an_unloaded_slot(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(slot_display, "SEATING_INTERVAL_S", 0.01)
    service = printer()
    inventory = await assigned(tmp_path, service)
    try:
        async with slot_display.seating_worker(lambda: [service], inventory):
            service.material_inventory.observe({"ams": {"tray_exist_bits": "0"}})
            reload = {**frame(printer()), "tray_exist_bits": "1"}
            service.material_inventory.observe({"ams": reload})
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(2):
                    while (await inventory.assignments.snapshot()).assignments:
                        await asyncio.sleep(0.01)
        assert (await inventory.assignments.snapshot()).assignments == ()
    finally:
        await inventory.close()


def test_malformed_frame_with_the_external_spool_visible_is_not_an_unload() -> None:
    service = printer()
    material = service.material_inventory
    material.observe({"vt_tray": {"id": "254", "tray_type": "PC", "tray_color": "161616FF"}})
    material.take_unloaded()
    broken = frame(service)
    broken["ams"][0]["tray"] = broken["ams"][0]["tray"][:1]  # "Incomplete AMS unit"
    material.observe({"ams": broken})
    assert material.take_unloaded() == {}


async def test_a_reassignment_after_a_pending_unload_stays(tmp_path) -> None:
    service = printer()
    material = service.material_inventory
    material.observe({"ams": {"tray_exist_bits": "0"}})
    material.observe({"ams": {**frame(printer()), "tray_exist_bits": "1"}})
    pending = material.take_unloaded()  # the worker drained it, then waited
    inventory = await assigned(tmp_path, service)  # the owner reassigned meanwhile
    try:
        dropped = await inventory.release_unseated(service.serial, source_choices(service), pending)
        assert dropped == []
        assert len((await inventory.assignments.snapshot()).assignments) == 1
    finally:
        await inventory.close()


def _with_read_bits(service, done: str = "1", reading: str = "0") -> dict:
    full = {**frame(service), "tray_read_done_bits": done, "tray_reading_bits": reading}
    service.material_inventory.observe({"ams": full})
    return full


async def _seat(inventory, service) -> list:
    material = service.material_inventory
    return await inventory.release_unseated(
        service.serial, source_choices(service), material.take_unloaded()
    )


async def test_a_tag_reread_keeps_the_assignment_of_an_untagged_slot(tmp_path) -> None:
    # The P1S sequence seen live: the tray goes blank at +1.5 s with read-done
    # cleared, the reading bit is set from +7.5 s, and the tray is back at +50 s.
    service = printer()
    full = _with_read_bits(service)
    inventory = await assigned(tmp_path, service)
    material = service.material_inventory
    unit = full["ams"][0]
    blank_unit = {**unit, "tray": [{"id": "0"}, *unit["tray"][1:]]}
    mid_read_full = {**full, "ams": [blank_unit], "tray_read_done_bits": "0"}
    back = {"ams": [{"id": "0", "tray": [unit["tray"][0]]}], "tray_read_done_bits": "1"}
    steps = [
        {"ams": [{"id": "0", "tray": [{"id": "0"}]}], "tray_read_done_bits": "0"},  # +1.5 s
        {"tray_reading_bits": "1"},  # +7.5 s
        {**mid_read_full, "tray_reading_bits": "1"},  # a full report mid-read
        {**back, "tray_reading_bits": "0"},  # +50 s
    ]
    try:
        for step in steps:
            material.observe({"ams": step})
            assert await _seat(inventory, service) == []
            assert source_choices(service)[0]["material"] == "ASA"
        assert len((await inventory.assignments.snapshot()).assignments) == 1
    finally:
        await inventory.close()


async def test_a_reread_that_ends_on_another_material_still_releases(tmp_path) -> None:
    service = printer()
    full = _with_read_bits(service)
    inventory = await assigned(tmp_path, service)
    material = service.material_inventory
    other = {**full["ams"][0]["tray"][0], "tray_type": "PLA"}
    try:
        material.observe({"ams": {"tray_read_done_bits": "0", "tray_reading_bits": "1"}})
        assert await _seat(inventory, service) == []
        done = {"ams": [{"id": "0", "tray": [other]}], "tray_read_done_bits": "1"}
        material.observe({"ams": {**done, "tray_reading_bits": "0"}})
        assert len(await _seat(inventory, service)) == 1
    finally:
        await inventory.close()


def test_a_tray_never_marked_read_still_updates() -> None:
    service = printer()
    _with_read_bits(service, done="0")  # this AMS never marks the tray read
    recolor(service, "FF00FFFF")
    assert source_choices(service)[0]["color"] == "FF00FFFF"


async def test_a_blank_tray_without_read_bits_keeps_the_assignment(tmp_path) -> None:
    # Firmware that does not report the read bits still blanks the tray while it
    # reads; a blank tray says nothing about the spool.
    service = printer()
    inventory = await assigned(tmp_path, service)
    material = service.material_inventory
    try:
        material.observe({"ams": {"ams": [{"id": "0", "tray": [{"id": "0"}]}]}})
        assert source_choices(service)[0]["material"] == ""
        assert await _seat(inventory, service) == []
        recolor(service, "000000FF", tray_type="ASA")  # the tray comes back
        assert await _seat(inventory, service) == []
        assert len((await inventory.assignments.snapshot()).assignments) == 1
    finally:
        await inventory.close()


async def test_a_reread_that_ends_on_a_tag_releases(tmp_path) -> None:
    service = printer()
    full = _with_read_bits(service)
    inventory = await assigned(tmp_path, service)
    material = service.material_inventory
    tagged = {**full["ams"][0]["tray"][0], "tag_uid": "1234567890ABCDEF", "tray_uuid": "AB" * 16}
    try:
        material.observe({"ams": {"tray_read_done_bits": "0", "tray_reading_bits": "1"}})
        assert await _seat(inventory, service) == []
        done = {"ams": [{"id": "0", "tray": [tagged]}], "tray_read_done_bits": "1"}
        material.observe({"ams": {**done, "tray_reading_bits": "0"}})
        assert len(await _seat(inventory, service)) == 1
    finally:
        await inventory.close()


async def _tagged(tmp_path, service, uid: str = "1234567890ABCDEF"):
    recolor(service, "000000FF", tag_uid=uid, tray_uuid="AB" * 16)
    return await assigned(tmp_path, service)


@pytest.mark.parametrize(
    "change,released",
    [
        ("same_tag_relabelled", False),
        ("another_tag", True),
        ("tag_gone", True),
    ],
)
async def test_a_tagged_assignment_follows_its_tag(tmp_path, change, released) -> None:
    service = printer()
    inventory = await _tagged(tmp_path, service)
    try:
        if change == "same_tag_relabelled":
            recolor(service, "FFFFFFFF", tray_info_idx="GFB99", nozzle_temp_min="230")
        elif change == "another_tag":
            recolor(service, "000000FF", tag_uid="FEDCBA0987654321", tray_uuid="CD" * 16)
        else:
            recolor(service, "000000FF", tag_uid="0000000000000000", tray_uuid="0" * 32)
        assert bool(await _seat(inventory, service)) is released
    finally:
        await inventory.close()


@pytest.mark.parametrize(
    "swap,released",
    [
        # Seen: one report with the presence bit low between two seating passes.
        ("seen", True),
        # Unseen (bridge offline, MQTT gap): an identical untagged spool cannot be
        # told apart; the owner reassigns, or uses "Wrong spool?" after a print.
        ("unseen_same_chemistry", False),
        ("unseen_other_chemistry", True),
        ("unseen_to_tagged", True),
    ],
)
async def test_a_swap_inside_one_poll_gap(tmp_path, swap, released) -> None:
    service = printer()
    inventory = await assigned(tmp_path, service)
    material = service.material_inventory
    try:
        if swap == "seen":
            material.observe({"ams": {"tray_exist_bits": "0"}})
            material.observe({"ams": {**frame(printer()), "tray_exist_bits": "1"}})
        else:
            material.clear()  # a gap: nothing is known about what happened in it
            reload = frame(printer())
            tray = reload["ams"][0]["tray"][0]
            tray.update(tray_color="FF0000FF", tray_info_idx="GFL99")
            if swap == "unseen_other_chemistry":
                tray["tray_type"] = "PLA"
            elif swap == "unseen_to_tagged":
                tray.update(tag_uid="1234567890ABCDEF", tray_uuid="AB" * 16)
            material.observe({"ams": reload})
        assert bool(await _seat(inventory, service)) is released
    finally:
        await inventory.close()


async def _legacy(inventory) -> None:
    """Make the stored assignment look as 0c416d2 left it: no seat."""
    await inventory.assignments._conn.execute("DELETE FROM assignment_seats")
    await inventory.assignments._conn.commit()
    (assignment,) = (await inventory.assignments.snapshot()).assignments
    assert assignment.spool_key is None


@pytest.mark.parametrize(
    "change,released",
    [
        ("nothing", False),  # the deploy itself drops nothing
        ("colour", False),
        ("sku", True),  # changed while no seat recorded it: as the old bridge did
        ("rfid", True),
    ],
)
async def test_a_stored_source_v3_assignment_is_stamped(tmp_path, change, released) -> None:
    service = printer()
    inventory = await assigned(tmp_path, service)
    try:
        await _legacy(inventory)
        if change == "colour":
            recolor(service, "F330F9FF")
        elif change == "sku":
            recolor(service, "000000FF", tray_info_idx="GFB98")
        elif change == "rfid":
            recolor(service, "000000FF", tag_uid="1234567890ABCDEF")
        assert bool(await _seat(inventory, service)) is released
        if not released:
            (assignment,) = (await inventory.assignments.snapshot()).assignments
            assert (assignment.spool_key, assignment.tray_type) == ("untagged", "ASA")
            # Stamped once: from now on a relabel keeps it.
            recolor(service, "000000FF", tray_info_idx="GFB98")
            assert await _seat(inventory, service) == []
    finally:
        await inventory.close()


async def test_a_legacy_assignment_waits_out_a_blank_tray(tmp_path) -> None:
    service = printer()
    inventory = await assigned(tmp_path, service)
    try:
        await _legacy(inventory)
        revision = (await inventory.assignments.snapshot()).revision
        service.material_inventory.observe({"ams": {"ams": [{"id": "0", "tray": [{"id": "0"}]}]}})
        assert await _seat(inventory, service) == []
        snapshot = await inventory.assignments.snapshot()
        assert snapshot.revision == revision and snapshot.assignments[0].spool_key is None
    finally:
        await inventory.close()


async def test_a_tray_uuid_filled_in_a_frame_after_the_tag_keeps_the_assignment(tmp_path) -> None:
    service = printer()
    recolor(service, "000000FF", tag_uid="1234567890abcdef ", tray_uuid="0" * 32)
    inventory = await assigned(tmp_path, service)
    try:
        recolor(service, "000000FF", tag_uid="1234567890ABCDEF", tray_uuid="AB" * 16)
        assert await _seat(inventory, service) == []
    finally:
        await inventory.close()


async def test_an_unload_stamped_with_the_assignment_time_came_before_it(tmp_path) -> None:
    service = printer()
    inventory = await assigned(tmp_path, service)
    try:
        (assignment,) = (await inventory.assignments.snapshot()).assignments
        unloaded = {0: assignment.assigned_at}
        assert (
            await inventory.release_unseated(service.serial, source_choices(service), unloaded)
            == []
        )
        later = {0: assignment.assigned_at + 0.001}
        assert len(await inventory.release_unseated(service.serial, [], later)) == 1
    finally:
        await inventory.close()


async def test_a_reassignment_is_always_later_than_the_one_it_replaces(
    tmp_path, monkeypatch
) -> None:
    service = printer()
    inventory = await assigned(tmp_path, service)
    try:
        (first,) = (await inventory.assignments.snapshot()).assignments
        monkeypatch.setattr("bambu_bridge.service.inventory.time.time", lambda: first.assigned_at)
        choice = source_choices(service)[0]
        await inventory.assign(
            spool_id=43,
            printer_id=service.serial,
            source=choice["source"],
            observed_identity=None,
            choices=lambda: source_choices(service),
        )
        (second,) = (await inventory.assignments.snapshot()).assignments
        assert second.assigned_at > first.assigned_at
    finally:
        await inventory.close()


@pytest.mark.parametrize(
    "tray,released",
    [
        ({"tray_type": "ASA", "tray_color": "FFFFFFFF", "tray_info_idx": "GFB98"}, False),
        ({"tray_type": "PETG", "tray_color": "161616FF"}, True),  # another chemistry
        ({"tray_type": ""}, True),  # no presence bit: an empty holder is the unload
    ],
)
async def test_the_external_holder_follows_the_same_rule(tmp_path, tray, released) -> None:
    service = printer()
    material = service.material_inventory
    material.observe({"vt_tray": {"id": "254", "tray_type": "ASA", "tray_color": "161616FF"}})
    inventory = await open_service(tmp_path, Provider())
    try:
        external = next(c for c in source_choices(service) if c["wire_slot"] == 254)
        await inventory.assign(
            spool_id=43,
            printer_id=service.serial,
            source=external["source"],
            observed_identity=None,
            choices=lambda: source_choices(service),
        )
        material.take_unloaded()
        material.observe({"vt_tray": {"id": "254", **tray}})
        assert bool(await _seat(inventory, service)) is released
    finally:
        await inventory.close()


def test_a_choice_without_material_is_blank_not_an_error() -> None:
    from bambu_bridge.inventory_sources import same_spool
    from bambu_bridge.spool_assignments import SpoolAssignment

    held = SpoolAssignment("p1", "s", 43, spool_key="untagged", tray_type="PLA")
    row = {"material": None, "spool_key": "untagged", "observed_identity": "x"}
    assert same_spool(held, row) is None


async def test_a_tag_shown_in_pieces_after_its_read_keeps_the_assignment(tmp_path) -> None:
    # The reading bit drops before read-done returns; meanwhile the tray can show
    # untagged or in pieces. Nothing about the tag is judged until done.
    service = printer()
    recolor(service, "000000FF", tag_uid="1234567890ABCDEF", tray_uuid="AB" * 16)
    full = _with_read_bits(service)
    inventory = await assigned(tmp_path, service)
    material = service.material_inventory
    tray = full["ams"][0]["tray"][0]
    pieces = {**tray, "tag_uid": "0000000000000000", "tray_uuid": "0" * 32}
    try:
        (before,) = (await inventory.assignments.snapshot()).assignments
        steps = [
            {"tray_read_done_bits": "0", "tray_reading_bits": "1"},
            {"ams": [{"id": "0", "tray": [pieces]}], "tray_reading_bits": "0"},
            {"ams": [{"id": "0", "tray": [tray]}], "tray_read_done_bits": "1"},
        ]
        for step in steps:
            material.observe({"ams": step})
            assert await _seat(inventory, service) == []
        (after,) = (await inventory.assignments.snapshot()).assignments
        assert after == before
        # Accounting judges the slot the same way.
        from bambu_bridge.service.shadow_accounting import assignment_stands, slot_identities

        material.observe({"ams": {**steps[1], "tray_read_done_bits": "0"}})
        assert assignment_stands(after, slot_identities(service)["0"])
    finally:
        await inventory.close()


async def test_a_slot_with_no_declared_material_is_not_assigned(tmp_path) -> None:
    service = printer()
    inventory = await open_service(tmp_path, Provider())
    service.material_inventory.observe({"ams": {"ams": [{"id": "0", "tray": [{"id": "0"}]}]}})
    choice = source_choices(service)[0]
    try:
        with pytest.raises(InventoryConflict, match="has not declared a material"):
            await inventory.assign(
                spool_id=43,
                printer_id=service.serial,
                source=choice["source"],
                observed_identity=None,
                choices=lambda: source_choices(service),
            )
    finally:
        await inventory.close()
