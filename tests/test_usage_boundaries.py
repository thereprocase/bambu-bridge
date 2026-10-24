"""Where the counting debit crosses a boundary.

Slicer header columns to tool numbers, grams with nothing to scale them by,
the printer's 1-based AMS trays against the manifest's 0-based wires and the
external holder, and the window in which a refill belongs to a print.
"""

import json

from bambu_bridge.gcode_usage import layer_plan
from bambu_bridge.native_inbox import NativeInbox
from bambu_bridge.service.consumption_delivery import refill_switches, window
from bambu_bridge.service.shadow_accounting import plan_manifest, read_plan, tray_materials
from bambu_bridge.usage_split import slot_usage
from tests.test_accounting_attempts import manifest as legacy_manifest
from tests.test_inventory_service import SPOOL
from tests.test_usage_counting import FLAT, deliver, iso, printer, setup

# --- header columns and tool numbers -----------------------------------------


def test_header_columns_are_indexed_by_tool_number_with_unused_filaments_at_zero():
    # Orca lists every project filament in order, 0.00 for the unused ones, so
    # column 2 belongs to T2 even when T0 and T1 never print.
    gcode = """
; filament used [g] = 0.00, 0.00, 3.00
; filament_density = 1.24, 1.24, 1.27
M83
M620 S2A
T2
M73 L1
G1 E10
M73 L2
G1 E20
"""
    plan, _ = layer_plan(gcode.splitlines())
    assert plan == [{"2": 1.0}, {"2": 2.0}]


def test_a_filament_the_slicer_lists_at_zero_grams_is_counted_by_density():
    # A column of 0.00 is no scale; the tool falls through to its density.
    gcode = """
; filament used [g] = 2.00, 0.00
; filament_density = 1.00, 1.00
M83
M73 L1
T0
G1 E100
T1
G1 E100
"""
    plan, _ = layer_plan(gcode.splitlines())
    # 100 mm of 1.75 mm filament at 1 g/cm³: 2.405 mm² · 100 mm / 1000 = 0.2405 g
    assert plan == [{"0": 2.0, "1": 0.2405}]


def test_a_filament_with_neither_header_grams_nor_density_is_named_not_planned():
    # Tool 1's 100 mm have no weight; the plan leaves it out and names it.
    gcode = """
; filament used [g] = 2.00
M83
M73 L1
T0
G1 E100
T1
G1 E100
"""
    plan, header = layer_plan(gcode.splitlines())
    assert plan == [{"0": 2.0}]
    assert header["unweighed_filaments"] == [1.0]


async def frozen_from(service, inbox, attempt, raw: bytes, mapping: list[int], spools):
    """Freeze `raw` the way reserve_shadow / adoption would, as the event's manifest."""
    sha256, plan, header = read_plan(raw, 1)
    snapshot = await service.assignments.snapshot()
    command = {"ams_mapping": mapping, "use_ams": True}
    manifest = plan_manifest(sha256, 1, plan, header, command, snapshot, "fixture")
    manifest["wire_spools"] = spools
    with inbox.connect() as db:
        db.execute(
            "UPDATE accounting_shadow_events SET manifest=? WHERE attempt_id=?",
            (json.dumps(manifest), attempt),
        )
    return manifest


async def test_a_filament_that_cannot_be_weighed_is_noted_not_dropped(tmp_path):
    raw = b"; filament used [g] = 2.00\nM83\nM73 L1\nT0\nG1 E100\nT1\nG1 E100\n"
    provider, service, inbox, attempt = await setup(tmp_path)
    try:
        await frozen_from(service, inbox, attempt, raw, [0, 1], {"0": 43, "1": 44})
        await deliver(service, inbox)
        assert provider.uses == [(43, 2.0)]
        assert inbox.accounting_review(attempt) == "unweighed_filament"
        assert inbox.accounting_event(attempt)["delivery_state"] == "review"
    finally:
        await service.close()


async def test_a_print_without_a_slicer_header_is_noted_not_debited_at_zero(tmp_path):
    raw = b";Cura-style file: no filament figures\nM83\nM73 L1\nT0\nG1 E100\nM73 L2\nG1 E100\n"
    provider, service, inbox, attempt = await setup(tmp_path)
    try:
        manifest = await frozen_from(service, inbox, attempt, raw, [0], {"0": 43})
        assert manifest["planned_grams"] == 0 and manifest["tool_wires"] == {}
        await deliver(service, inbox)
        assert provider.uses == []
        assert inbox.accounting_review(attempt)
    finally:
        await service.close()


# --- trays, wires and the external holder ------------------------------------


async def test_a_stopped_multicolour_print_with_one_refill_crosses_every_slot_numbering(
    tmp_path,
):
    # The printer reports trays 1-based: tray 1 ran out at layer 2 and tray 3
    # took over. Filament 0 is wired to tray 1 as wire 0; filament 1 feeds from
    # the external holder, wire 254. The print stopped on layer 3 of 4.
    provider, service, inbox, attempt = await setup(
        tmp_path,
        outcome="interrupted",
        layer=3,
        layer_grams=[{"0": 1, "1": 2}] * 4,
        tool_wires={"0": 0, "1": 254},
        wire_spools={"0": 43, "2": 44, "254": 45},
    )
    provider.spools[45] = {**SPOOL, "id": 45, "remaining_weight": 800, "used_weight": 0.0}
    at = iso(float(inbox.accounting_event(attempt)["created_at"]))
    switch = {"from_slot": 1, "to_slot": 3, "status": "completed", "layer": 2, "observed_at": at}
    try:
        await deliver(service, inbox, printer([switch]))
        # 2.5 layers × 3 g = 7.5 g: layer 1 and half of layer 2 on the old spool,
        # the other half of layer 2 and half of layer 3 on the refill, filament 1
        # untouched by the switch.
        assert sorted(provider.uses) == [(43, 1.5), (44, 1.0), (45, 5.0)]
        assert inbox.accounting_event(attempt)["delivery_state"] == "delivered"
    finally:
        await service.close()


def test_a_refill_on_the_layer_the_print_stopped_at_splits_the_half_layer():
    m = {"layer_grams": FLAT, "tool_wires": {"0": 0}}
    assert slot_usage(m, 2, [(0, 2, 2)]) == {0: 1.25, 2: 0.25}


def test_chained_refills_follow_the_filament_in_layer_order():
    m = {"layer_grams": FLAT, "tool_wires": {"0": 0}}
    moves = [(1, 2, 3), (0, 1, 2)]  # given out of order
    assert slot_usage(m, None, moves) == {0: 1.5, 1: 1.0, 2: 1.5}


def test_tray_numbers_become_wires_and_unplaced_switches_are_skipped():
    begun, ended = 1_000_000.0, 1_000_100.0
    placed = {
        "from_slot": 1,
        "to_slot": 3,
        "status": "completed",
        "layer": 5,
        "observed_at": iso(1_000_050),
    }
    # A finished switch whose destination the printer never named, and one
    # still switching when the print ended: the count stays on the old spool.
    unplaced = {**placed, "from_slot": 2, "to_slot": None, "observed_at": iso(1_000_060)}
    unfinished = {**placed, "from_slot": 4, "status": "interrupted", "observed_at": iso(1_000_070)}
    device = printer([placed, unplaced, unfinished])
    assert refill_switches(device, begun, ended) == [(0, 2, 5)]


# --- the refill window ---------------------------------------------------------


def test_the_refill_window_runs_from_dispatch_to_the_end_of_the_end_events_second():
    # The end event is stamped in whole seconds: a refill observed at
    # 1_000_100.7 is still inside the print.
    event = {"created_at": 1_000_100}
    begun, ended = window(event, {"dispatched_at": 1_000_000, "created": 999_000})
    assert (begun, ended) == (1_000_000.0, 1_000_101.0)
    late = {
        "from_slot": 1,
        "to_slot": 3,
        "status": "completed",
        "layer": 5,
        "observed_at": iso(1_000_100.7),
    }
    assert refill_switches(printer([late]), begun, ended) == [(0, 2, 5)]
    # No dispatch time: the row's creation time, never epoch zero.
    assert window(event, {"dispatched_at": None, "created": 999_000}) == (
        999_000.0,
        1_000_101.0,
    )


def test_an_adopted_print_is_dated_from_the_start_the_bridge_saw(tmp_path):
    inbox = NativeInbox(tmp_path / "inbox")
    manifest = legacy_manifest() | {"layer_grams": [], "tool_wires": {}, "wire_spools": {}}
    seen = inbox.adopt_running_print(
        "fixture", "/seen.3mf", {"md5": "0" * 32}, manifest, sha256="0" * 64, started_at=1_000_000
    )
    row = inbox.get(seen)
    assert row["dispatched_at"] == 1_000_000 and row["created"] >= 1_000_000
    # Adopted without a seen start: the adoption time stands in, never NULL.
    with inbox.connect() as db:
        db.execute(
            "UPDATE uploads SET start_state='resolved',code='BBSTART_ENDED' WHERE id=?", (seen,)
        )
    unseen = inbox.adopt_running_print(
        "fixture", "/later.3mf", {"md5": "1" * 32}, manifest, sha256="1" * 64, started_at=None
    )
    row = inbox.get(unseen)
    assert row["dispatched_at"] and row["dispatched_at"] >= row["created"]


def test_a_refill_from_the_previous_print_is_outside_thewindow():
    begun, ended = 1_000_000.0, 1_000_100.0
    earlier = {
        "from_slot": 1,
        "to_slot": 2,
        "status": "completed",
        "layer": 9,
        "observed_at": iso(999_990),
    }
    current = {**earlier, "from_slot": 3, "to_slot": 4, "layer": 2, "observed_at": iso(1_000_010)}
    device = printer([current])
    device.diagnostic_journal.archived_refills = [{"print": "before", "refills": [earlier]}]
    assert refill_switches(device, begun, ended) == [(2, 3, 2)]


# --- what was in each slot -------------------------------------------------------


def test_the_trays_material_and_colour_are_frozen_by_wire():
    from types import SimpleNamespace

    from tests.test_inventory_sources import printer as ams_printer

    # The fixture AMS has ASA in tray 1 only; the other trays report no type and
    # are absent from the presence bits, so only wire 0 is named.
    assert tray_materials(ams_printer()) == {"0": {"material": "ASA", "color": "000000FF"}}
    # No frame at all: nothing is named, nothing raises.
    bare = SimpleNamespace(material_inventory=SimpleNamespace(snapshot=lambda: {}))
    assert tray_materials(bare) == {}
    assert tray_materials(SimpleNamespace()) == {}


async def test_a_frozen_manifest_carries_the_trays(tmp_path):
    provider, service, inbox, attempt = await setup(tmp_path)
    snapshot = await service.assignments.snapshot()
    try:
        trays = {
            "0": {"material": "ASA", "color": "000000FF"},
            "254": {"material": "PETG", "color": None},
        }
        manifest = plan_manifest(
            "a" * 64, 1, [{"0": 1.0}], {}, {"ams_mapping": [0]}, snapshot, "fixture", trays=trays
        )
        assert manifest["tray_materials"] == trays
        inbox.freeze_accounting(
            inbox.claim_external("other", {"print": {"command": "project_file"}}, reserved=True),
            manifest,
        )
    finally:
        await service.close()
