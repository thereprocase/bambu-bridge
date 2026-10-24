"""The raw slot write refuses a slot that holds a spool or a backup unless overridden."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from bambu_bridge.api import advanced
from bambu_bridge.inventory_sources import declared_identity
from bambu_bridge.refill import RefillController, RefillStore
from bambu_bridge.service.material_inventory import MaterialInventory
from bambu_bridge.service.refill_reports import RefillReports
from bambu_bridge.spool_assignments import SpoolAssignment, SpoolAssignmentStore
from tests.test_refill_reports import raw

TRAY = {
    "id": "1",
    "tray_type": "PETG",
    "tray_info_idx": "GFG99",
    "tray_color": "FF0000FF",
    "nozzle_temp_min": "220",
    "nozzle_temp_max": "260",
    "tag_uid": "0000000000000000",
}


def frame():
    material = MaterialInventory()
    trays = [{"id": "0"}, dict(TRAY), {"id": "2"}, {"id": "3"}]
    material.observe(
        {
            "ams": {
                "ams": [{"id": "0", "sn": "ams-a", "tray": trays}],
                "ams_exist_bits": "1",
                "tray_exist_bits": "2",
            }
        }
    )
    return material


async def setup(
    tmp_path: Path,
    *,
    assigned: bool,
    refill: bool,
    tray_type: str | None = "PETG",
    wire: int = 1,
):
    store = await SpoolAssignmentStore.open(tmp_path / "inventory.sqlite")
    if assigned:
        initial = await store.snapshot()
        held = SpoolAssignment(
            "p1",
            f"ams:a:tray:{wire}",
            8,
            assigned_at=1,
            wire_slot=wire,
            observed_identity=declared_identity(TRAY),
            spool_key="untagged" if tray_type else None,
            tray_type=tray_type,
        )
        await store.replace((held,), provider_id=initial.provider_id, expected_revision=0)
    tracker = RefillReports()
    tracker.observe(raw())
    controller = RefillController(RefillStore(tmp_path / "refill"))
    if refill:
        controller.plan("p1", tracker.snapshot(connected=True), [0, 1], "FF0000FF", revision=0)
    printer = SimpleNamespace(
        serial="p1",
        connected=True,
        refill_reports=tracker,
        send_raw=AsyncMock(),
        material_inventory=frame(),
        native_snapshot=lambda: {},
    )
    registry = SimpleNamespace(get=lambda _id: printer)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(inventory=SimpleNamespace(assignments=store, refill=controller))
        )
    )
    return printer, registry, request, store, controller


def body(**changes):
    values = {
        "ams_id": 0,
        "tray_id": 1,
        "tray_info_idx": "GFG02",
        "tray_color": "FFFFFFFF",
        "nozzle_temp_min": 230,
        "nozzle_temp_max": 260,
        "tray_type": "PETG",
    }
    return advanced.AmsFilamentSettingBody(**{**values, **changes})


async def write(printer, registry, request, **changes):
    return await advanced.ams_filament_setting("p1", body(**changes), request, registry)


@pytest.mark.parametrize(
    "assigned,refill,cause",
    [
        (True, False, "slot_assigned"),
        (False, True, "slot_in_refill"),
        (True, True, "slot_assigned_and_in_refill"),
    ],
)
async def test_a_slot_in_use_is_refused_with_nothing_sent(tmp_path, assigned, refill, cause):
    printer, registry, request, store, controller = await setup(
        tmp_path, assigned=assigned, refill=refill
    )
    try:
        with pytest.raises(HTTPException) as refused:
            await write(printer, registry, request)
        detail = refused.value.detail
        assert refused.value.status_code == 409
        assert (detail["error"], detail["likely_cause"]) == ("slot_in_use", cause)
        context = detail["context"]
        assert context["wire_slot"] == 1 and context["override_field"] == "override"
        assert context["spool_id"] == (8 if assigned else None)
        assert (context["refill"] is not None) is refill
        if refill:
            assert (context["refill"]["from_wire"], context["refill"]["to_wire"]) == (0, 1)
        assert context["assignment_kept"] is True  # same chemistry
        printer.send_raw.assert_not_awaited()
    finally:
        await store.close()
        controller.store.close()


async def test_override_writes_and_says_what_happened(tmp_path):
    printer, registry, request, store, controller = await setup(
        tmp_path, assigned=True, refill=True
    )
    try:
        sent = await write(printer, registry, request, override=True)
        printer.send_raw.assert_awaited_once()
        assert "override" not in printer.send_raw.await_args.args[0]["print"]
        assert sent["guard"] == {
            "overridden": True,
            "assignment_kept": True,
            "refill_state_expected": "pending",  # an enabled group: pending until re-read
        }
    finally:
        await store.close()
        controller.store.close()


async def test_a_chemistry_change_says_the_assignment_ends(tmp_path):
    printer, registry, request, store, controller = await setup(
        tmp_path, assigned=True, refill=False
    )
    try:
        with pytest.raises(HTTPException) as refused:
            await write(printer, registry, request, tray_type="PLA")
        assert refused.value.detail["context"]["assignment_kept"] is False
        assert "is unassigned" in refused.value.detail["remediation_hint"]
        sent = await write(printer, registry, request, tray_type="PLA", override=True)
        assert sent["guard"]["assignment_kept"] is False
        assert sent["guard"]["refill_state_expected"] is None
    finally:
        await store.close()
        controller.store.close()


async def test_a_free_slot_is_written_as_before(tmp_path):
    printer, registry, request, store, controller = await setup(
        tmp_path, assigned=False, refill=False
    )
    try:
        sent = await write(printer, registry, request)
        assert "guard" not in sent and sent["confirmation"] == "pending"
        printer.send_raw.assert_awaited_once()
        request.app.state.inventory = None  # no inventory configured
        assert "guard" not in await write(printer, registry, request)
    finally:
        await store.close()
        controller.store.close()


@pytest.mark.parametrize("value", [1, "true", "false", None])
def test_override_takes_only_a_json_boolean(value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        body(override=value)


async def test_an_unstamped_assignment_is_predicted_to_end_on_a_product_change(tmp_path):
    printer, registry, request, store, controller = await setup(
        tmp_path, assigned=True, refill=False, tray_type=None
    )
    try:
        with pytest.raises(HTTPException) as refused:
            await write(printer, registry, request, tray_info_idx="GFG02")
        assert refused.value.detail["context"]["assignment_kept"] is False
        with pytest.raises(HTTPException) as refused:
            await write(printer, registry, request, tray_info_idx="GFG99", tray_color="00FF00FF")
        assert refused.value.detail["context"]["assignment_kept"] is True  # colour only
    finally:
        await store.close()
        controller.store.close()


async def test_a_write_that_changes_nothing_leaves_the_backup_be(tmp_path):
    printer, registry, request, store, controller = await setup(
        tmp_path, assigned=False, refill=True
    )
    try:
        same = {"tray_info_idx": "GFG99", "tray_color": "ff0000ff", "nozzle_temp_min": 220}
        sent = await write(printer, registry, request, override=True, **same)
        assert sent["guard"]["refill_state_expected"] is None
    finally:
        await store.close()
        controller.store.close()


async def test_a_guard_that_cannot_read_the_inventory_refuses(tmp_path):
    printer, registry, request, store, controller = await setup(
        tmp_path, assigned=True, refill=False
    )
    try:
        await store.close()  # the assignment store is gone
        with pytest.raises(HTTPException) as refused:
            await write(printer, registry, request)
        assert refused.value.status_code == 503
        printer.send_raw.assert_not_awaited()
        await write(printer, registry, request, override=True)
        printer.send_raw.assert_awaited_once()
    finally:
        controller.store.close()


async def test_an_assignment_on_another_ams_is_guarded(tmp_path):
    printer, registry, request, store, controller = await setup(
        tmp_path, assigned=True, refill=True, wire=5
    )
    try:
        with pytest.raises(HTTPException) as refused:
            await write(printer, registry, request, ams_id=1, tray_id=1)
        context = refused.value.detail["context"]
        assert (context["wire_slot"], context["spool_id"], context["refill"]) == (5, 8, None)
        # AMS A slot 2 holds no spool here; only its backup is guarded.
        with pytest.raises(HTTPException) as refused:
            await write(printer, registry, request)
        assert refused.value.detail["likely_cause"] == "slot_in_refill"
    finally:
        await store.close()
        controller.store.close()
