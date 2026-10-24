"""Synthetic raw reports only; no target-firmware or physical refill qualification."""

import copy
import time
from dataclasses import replace

import pytest

from bambu_bridge.refill import Declaration, RefillController, RefillReport, RefillStore, SlotReport


class Printer:
    def __init__(self):
        self.slots = {
            wire: SlotReport(
                Declaration("GFG99", color, 220, 260, "PETG"),
                f"physical-source-{wire}",
                1,
                time.monotonic(),
                True,
            )
            for wire, color in enumerate(("FF0000FF", "00FF00FF"))
        }
        self.idle = True
        self.auto = True
        self.backups = (3,)
        self.writes = []
        self.fail = None
        self.stale = False

    async def observe(self):
        return RefillReport(
            True, self.idle, copy.deepcopy(self.slots), self.auto, self.backups, time.monotonic()
        )

    async def send(self, wire, declaration):
        self.writes.append((wire, declaration))
        if self.fail == "before":
            raise ConnectionError("delivery unknown")
        if not self.stale:
            self.slots[wire] = replace(
                self.slots[wire],
                declaration=declaration,
                receipt=self.slots[wire].receipt + 1,
                reported_at=time.monotonic(),
            )
        if self.fail == "after":
            raise ConnectionError("response lost")


async def fixture(tmp_path):
    store = RefillStore(tmp_path)
    controller = RefillController(store)
    printer = Printer()
    group = controller.plan("fixture", await printer.observe(), [0, 1], "FFFFFFFF", revision=0)
    return store, controller, printer, group


async def test_unqualified_never_writes_and_active_requires_physical_eligibility(tmp_path):
    store, controller, printer, _ = await fixture(tmp_path)
    controller.qualified = False
    try:
        assert (await controller.drive("fixture", printer.observe, printer.send))[
            "state"
        ] == "unsupported"
        assert printer.writes == []
        controller.qualified = True
        printer.auto = False
        group = await controller.drive("fixture", printer.observe, printer.send)
        assert group["state"] == "pending"
        assert len(printer.writes) == 2
        assert all(step["state"] == "owned" for step in group["steps"])
        printer.auto = True
        printer.backups = (1, 2)
        assert (await controller.drive("fixture", printer.observe, printer.send))[
            "state"
        ] == "pending"
        printer.backups = (3,)
        assert (await controller.drive("fixture", printer.observe, printer.send))[
            "state"
        ] == "active"
        printer.idle = False
        assert (await controller.drive("fixture", printer.observe, printer.send))[
            "state"
        ] == "active"
        assert len(printer.writes) == 2
    finally:
        store.close()


async def test_disable_busy_keeps_ownership_then_restores_only_original_values(tmp_path):
    store, controller, printer, _ = await fixture(tmp_path)
    controller.qualified = True
    before = {wire: slot.declaration for wire, slot in printer.slots.items()}
    try:
        group = await controller.drive("fixture", printer.observe, printer.send)
        printer.idle = False
        group = controller.disable("fixture", revision=group["revision"])
        assert group["state"] == "restoration_pending"
        assert (await controller.drive("fixture", printer.observe, printer.send))[
            "state"
        ] == "restoration_pending"
        assert len(printer.writes) == 2
        printer.idle = True
        group = await controller.drive("fixture", printer.observe, printer.send)
        assert group["state"] == "disabled"
        assert {wire: slot.declaration for wire, slot in printer.slots.items()} == before
        assert printer.auto is True  # The ordinary toggle was never touched.
    finally:
        store.close()


async def test_stale_report_does_not_confirm_alias_and_restart_never_resends(tmp_path):
    store, controller, printer, _ = await fixture(tmp_path)
    controller.qualified = True
    printer.stale = True
    group = await controller.drive("fixture", printer.observe, printer.send)
    assert group["state"] == "needs_attention"
    assert len(printer.writes) == 1
    store.close()
    store = RefillStore(tmp_path)
    controller = RefillController(store)
    controller.qualified = True
    try:
        assert (await controller.drive("fixture", printer.observe, printer.send))[
            "state"
        ] == "needs_attention"
        assert len(printer.writes) == 1
        with pytest.raises(ValueError, match="does not prove"):
            controller.reconcile(
                "fixture",
                0,
                await printer.observe(),
                revision=group["revision"],
                applied=False,
                note="Old value is visible",
            )
    finally:
        store.close()


async def test_accepted_response_loss_adopts_readback_without_second_write(tmp_path):
    store, controller, printer, _ = await fixture(tmp_path)
    controller.qualified = True
    printer.fail = "after"
    try:
        with pytest.raises(ConnectionError):
            await controller.drive("fixture", printer.observe, printer.send)
        group = store.get("fixture")
        assert group["steps"][0]["state"] == "uncertain"
        controller.reconcile(
            "fixture",
            0,
            await printer.observe(),
            revision=group["revision"],
            applied=True,
            note="Physical target confirmed",
        )
        printer.fail = None
        group = await controller.drive("fixture", printer.observe, printer.send)
        assert group["state"] == "active"
        assert len(printer.writes) == 2  # One per slot, never replays slot zero.
    finally:
        store.close()


async def test_external_edit_is_not_overwritten_during_disable(tmp_path):
    store, controller, printer, _ = await fixture(tmp_path)
    controller.qualified = True
    try:
        group = await controller.drive("fixture", printer.observe, printer.send)
        changed = replace(printer.slots[0].declaration, tray_color="123456FF")
        printer.slots[0] = replace(printer.slots[0], declaration=changed, receipt=3)
        controller.disable("fixture", revision=group["revision"])
        await controller.drive("fixture", printer.observe, printer.send)
        await controller.drive("fixture", printer.observe, printer.send)
        assert printer.slots[0].declaration == changed
        assert printer.slots[1].declaration.tray_color == "00FF00FF"
        assert store.get("fixture")["state"] == "needs_attention"
    finally:
        store.close()


async def test_real_tag_incompatible_product_or_stale_identity_is_rejected_before_writes(tmp_path):
    store = RefillStore(tmp_path)
    controller = RefillController(store)
    printer = Printer()
    original = printer.slots[1]
    try:
        for changed in (
            replace(original, editable=False),
            replace(original, reported_at=time.monotonic() - 60),
            replace(original, declaration=replace(original.declaration, tray_type="PLA")),
        ):
            printer.slots[1] = changed
            with pytest.raises(ValueError):
                controller.plan("fixture", await printer.observe(), [0, 1], "FFFFFFFF", revision=0)
        assert printer.writes == []
        assert store.get("fixture") is None
    finally:
        store.close()


async def test_extra_unselected_backup_slot_cannot_be_active(tmp_path):
    store, controller, printer, _ = await fixture(tmp_path)
    controller.qualified = True
    printer.backups = (7,)
    try:
        assert (await controller.drive("fixture", printer.observe, printer.send))[
            "state"
        ] == "pending"
        assert len(printer.writes) == 2
    finally:
        store.close()


async def test_external_change_can_be_acknowledged_without_overwriting_it(tmp_path):
    store, controller, printer, _ = await fixture(tmp_path)
    controller.qualified = True
    try:
        group = await controller.drive("fixture", printer.observe, printer.send)
        changed = replace(printer.slots[0].declaration, tray_color="123456FF")
        printer.slots[0] = replace(printer.slots[0], declaration=changed, receipt=3)
        controller.disable("fixture", revision=group["revision"])
        group = await controller.drive("fixture", printer.observe, printer.send)
        controller.release_external_change(
            "fixture",
            0,
            await printer.observe(),
            revision=group["revision"],
            note="Keep my later declaration",
        )
        group = await controller.drive("fixture", printer.observe, printer.send)
        assert group["state"] == "disabled"
        assert printer.slots[0].declaration == changed
        assert len(printer.writes) == 3  # Two applies and just the other slot's restore.
    finally:
        store.close()


async def test_commit_failure_after_physical_acceptance_is_not_replayed(tmp_path, monkeypatch):
    store, controller, printer, _ = await fixture(tmp_path)
    controller.qualified = True
    original = store.save

    def fail_recording(printer_id, group, revision):
        if group["steps"][0]["state"] == "owned":
            raise OSError("simulated commit failure")
        return original(printer_id, group, revision)

    monkeypatch.setattr(store, "save", fail_recording)
    with pytest.raises(OSError, match="commit failure"):
        await controller.drive("fixture", printer.observe, printer.send)
    assert len(printer.writes) == 1
    store.close()
    store = RefillStore(tmp_path)
    controller = RefillController(store)
    controller.qualified = True
    try:
        group = await controller.drive("fixture", printer.observe, printer.send)
        assert group["state"] == "needs_attention"
        assert len(printer.writes) == 1
        controller.reconcile(
            "fixture",
            0,
            await printer.observe(),
            revision=group["revision"],
            applied=True,
            note="Fresh original-source target observed",
        )
        assert (await controller.drive("fixture", printer.observe, printer.send))[
            "state"
        ] == "active"
        assert len(printer.writes) == 2
    finally:
        store.close()


async def test_stale_editor_and_second_writer_cannot_replace_intent(tmp_path):
    store, controller, printer, group = await fixture(tmp_path)
    try:
        with pytest.raises(BlockingIOError):
            RefillStore(tmp_path)
        with pytest.raises(ValueError, match="changed"):
            controller.disable("fixture", revision=group["revision"] - 1)
        assert store.get("fixture")["enabled"] is True
    finally:
        store.close()


async def test_status_cannot_reuse_persisted_active_after_freshness_or_qualification_is_lost(
    tmp_path,
):
    store, controller, printer, _ = await fixture(tmp_path)
    controller.qualified = True
    try:
        assert (await controller.drive("fixture", printer.observe, printer.send))[
            "state"
        ] == "active"
        report = await printer.observe()
        stale = replace(report, eligibility_reported_at=time.monotonic() - 60)
        confirmed = controller.status("fixture", report)
        assert (confirmed["state"], confirmed["stale"]) == ("active", False)
        # A quiet printer is not current evidence, but the confirmation stands.
        quiet = controller.status("fixture", stale)
        assert (quiet["state"], quiet["stale"]) == ("pending", True)
        assert quiet["confirmed_at"] == confirmed["confirmed_at"] is not None
        assert quiet["last_confirmed_age_seconds"] >= 0
        silent = controller.status(
            "fixture", replace(report, slots={}, eligibility_reported_at=None)
        )
        assert (silent["stale"], silent["confirmed_at"]) == (True, confirmed["confirmed_at"])
        # Never confirmed by this bridge (a restart): nothing to show.
        assert RefillController(store).status("fixture", stale)["confirmed_at"] is None
        # Fresh evidence against it clears the confirmation as it arrives (note()).
        against = replace(report, backup_masks=())
        controller.note("fixture", against)
        against = controller.status("fixture", against)
        assert (against["state"], against["stale"], against["confirmed_at"]) == (
            "pending",
            False,
            None,
        )
        assert controller.status("fixture", stale)["confirmed_at"] is None
        assert controller.status("fixture", report)["state"] == "active"
        controller.qualified = False
        assert controller.status("fixture", report)["state"] == "unsupported"
        assert len(printer.writes) == 2
    finally:
        store.close()


async def test_manually_matching_aliases_need_no_write_and_disable_preserves_them(tmp_path):
    store = RefillStore(tmp_path)
    controller = RefillController(store)
    controller.qualified = True
    printer = Printer()
    for wire, slot in printer.slots.items():
        printer.slots[wire] = replace(
            slot, declaration=replace(slot.declaration, tray_color="FFFFFFFF")
        )
    try:
        controller.plan("fixture", await printer.observe(), [0, 1], "FFFFFFFF", revision=0)
        group = await controller.drive("fixture", printer.observe, printer.send)
        assert group["state"] == "active"
        assert printer.writes == []
        controller.disable("fixture", revision=group["revision"])
        group = await controller.drive("fixture", printer.observe, printer.send)
        assert group["state"] == "disabled"
        assert printer.writes == []
        assert printer.auto is True
        assert controller.status("fixture", await printer.observe())["printer_auto_refill"] is True
    finally:
        store.close()


async def test_a_backup_declares_the_whole_filament_and_restore_puts_it_all_back(tmp_path):
    store = RefillStore(tmp_path)
    controller = RefillController(store)
    printer = Printer()
    # Slot 2 is the same chemistry under another product and temperature range.
    printer.slots[1] = replace(
        printer.slots[1], declaration=Declaration("GFG02", "00FF00FF", 230, 260, "PETG")
    )
    original = printer.slots[1].declaration
    try:
        group = controller.plan("fixture", await printer.observe(), [0, 1], "FF0000FF", revision=0)
        anchor, member = group["steps"]
        # A refill never declares another chemistry on any slot.
        assert all(s["target"]["tray_type"] == s["before"]["tray_type"] for s in group["steps"])
        assert (anchor["anchor"], anchor["writes"], anchor["changes"]) == (True, False, [])
        assert member["writes"] is True and member["anchor"] is False
        assert member["changes"] == [
            {"field": "tray_info_idx", "before": "GFG02", "after": "GFG99"},
            {"field": "nozzle_temp_min", "before": 230, "after": 220},
            {"field": "tray_color", "before": "00FF00FF", "after": "FF0000FF"},
        ]
        group = await controller.drive("fixture", printer.observe, printer.send)
        assert [w for w, _ in printer.writes] == [1]  # the anchor already matches
        assert printer.slots[1].declaration == printer.slots[0].declaration
        assert group["state"] == "active"
        controller.disable("fixture", revision=group["revision"])
        group = await controller.drive("fixture", printer.observe, printer.send)
        assert printer.slots[1].declaration == original
        assert group["state"] == "disabled"
    finally:
        store.close()


async def test_another_material_is_refused_by_name(tmp_path):
    store = RefillStore(tmp_path)
    controller = RefillController(store)
    printer = Printer()
    printer.slots[1] = replace(
        printer.slots[1], declaration=replace(printer.slots[1].declaration, tray_type="PLA")
    )
    try:
        with pytest.raises(ValueError, match="Slot 2 is PLA and slot 1 is PETG"):
            controller.preview(await printer.observe(), [0, 1], "FF0000FF")
    finally:
        store.close()


def tagged_pair(printer, *, reading=False):
    """Slot 1 (wire 0) untagged white PETG; slot 2 (wire 1) a Bambu PETG Translucent tag."""
    printer.slots[0] = replace(
        printer.slots[0], declaration=Declaration("GFG99", "FFFFFFFF", 220, 270, "PETG")
    )
    printer.slots[1] = replace(
        printer.slots[1],
        declaration=Declaration("GFG01", "8B5A2BFF", 230, 260, "PETG"),
        editable=False,
        tagged=True,
        tag_key="tag-4",
    )


async def test_a_tag_anchors_the_group_and_is_never_written(tmp_path):
    store = RefillStore(tmp_path)
    controller = RefillController(store)
    printer = Printer()
    tagged_pair(printer)
    tag = printer.slots[1].declaration
    try:
        # Slot 1 is the primary and slot 2 (the tag) its backup: the tag still anchors.
        group = controller.plan("fixture", await printer.observe(), [0, 1], "8B5A2BFF", revision=0)
        member, anchor = group["steps"]
        assert (anchor["anchor"], anchor["tagged"], anchor["writes"]) == (True, True, False)
        assert (
            member["target"]
            == anchor["before"]
            == {
                "tray_info_idx": "GFG01",
                "tray_color": "8B5A2BFF",
                "nozzle_temp_min": 230,
                "nozzle_temp_max": 260,
                "tray_type": "PETG",
            }
        )
        assert [c["field"] for c in member["changes"]] == [
            "tray_info_idx",
            "nozzle_temp_min",
            "nozzle_temp_max",
            "tray_color",
        ]
        group = await controller.drive("fixture", printer.observe, printer.send)
        assert [wire for wire, _ in printer.writes] == [0]
        assert printer.slots[0].declaration == tag
        assert group["state"] == "active"
        controller.disable("fixture", revision=group["revision"])
        group = await controller.drive("fixture", printer.observe, printer.send)
        assert group["state"] == "disabled"
        assert [wire for wire, _ in printer.writes] == [0, 0]  # never wire 1
        assert printer.slots[1].declaration == tag
    finally:
        store.close()


async def test_a_tag_that_changes_under_its_group_is_flagged_not_written(tmp_path):
    store = RefillStore(tmp_path)
    controller = RefillController(store)
    printer = Printer()
    tagged_pair(printer)
    try:
        controller.plan("fixture", await printer.observe(), [0, 1], "8B5A2BFF", revision=0)
        group = await controller.drive("fixture", printer.observe, printer.send)
        # The AMS now reports another declaration from the tag's slot.
        printer.slots[1] = replace(
            printer.slots[1],
            declaration=replace(printer.slots[1].declaration, tray_info_idx="GFG02"),
            receipt=5,
        )
        controller.disable("fixture", revision=group["revision"])
        await controller.drive("fixture", printer.observe, printer.send)
        assert all(wire != 1 for wire, _ in printer.writes)
    finally:
        store.close()


@pytest.mark.parametrize(
    "case,message",
    [
        ("two_tags", "both hold RFID spools"),
        ("reading", "still reading its tag"),
        ("unreadable", "tag could not be read"),
        ("other_colour", "takes its tag's colour"),
    ],
)
async def test_tagged_groups_that_are_refused(tmp_path, case, message):
    store = RefillStore(tmp_path)
    controller = RefillController(store)
    printer = Printer()
    tagged_pair(printer)
    color = "8B5A2BFF"
    report = await printer.observe()
    if case == "two_tags":
        report.slots[0] = replace(report.slots[0], editable=False, tagged=True)
    elif case == "reading":
        report = replace(report, reading=frozenset({1}))
    elif case == "unreadable":
        report.slots[1] = replace(report.slots[1], tagged=False)
    else:
        color = "FFFFFFFF"
    try:
        with pytest.raises(ValueError, match=message):
            controller.preview(report, [0, 1], color)
    finally:
        store.close()


async def test_a_malformed_mask_or_flag_is_silence_and_a_real_one_is_evidence(tmp_path):
    store, controller, printer, _ = await fixture(tmp_path)
    try:
        await controller.drive("fixture", printer.observe, printer.send)
        report = await printer.observe()
        assert controller.status("fixture", report)["state"] == "active"
        for silent in (replace(report, backup_masks=None), replace(report, auto_refill=None)):
            status = controller.status("fixture", silent)
            assert (status["state"], status["stale"]) == ("pending", True)
            assert status["confirmed_at"] is not None
        for against in (replace(report, auto_refill=False), replace(report, backup_masks=(12,))):
            assert controller.status("fixture", report)["confirmed_at"] is not None
            status = controller.status("fixture", against)
            assert (status["stale"], status["confirmed_at"]) == (False, None)
    finally:
        store.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("tray_type", "PLA"),
        ("tray_info_idx", "GFG02"),
        ("nozzle_temp_min", 200),
        ("nozzle_temp_max", 280),
        ("tray_color", "123456FF"),
    ],
)
async def test_any_field_changed_on_the_printer_is_kept_on_restore(tmp_path, field, value):
    store, controller, printer, _ = await fixture(tmp_path)
    try:
        group = await controller.drive("fixture", printer.observe, printer.send)
        sent = len(printer.writes)
        changed = replace(printer.slots[1].declaration, **{field: value})
        printer.slots[1] = replace(printer.slots[1], declaration=changed, receipt=9)
        controller.disable("fixture", revision=group["revision"])
        group = await controller.drive("fixture", printer.observe, printer.send)
        assert printer.slots[1].declaration == changed
        assert all(wire != 1 for wire, _ in printer.writes[sent:])
        assert next(s for s in group["steps"] if s["wire"] == 1)["state"] == "conflict"
    finally:
        store.close()


async def test_a_step_that_writes_nothing_is_never_sent(tmp_path):
    store = RefillStore(tmp_path)
    controller = RefillController(store)
    printer = Printer()
    try:
        controller.plan("fixture", await printer.observe(), [0, 1], "FF0000FF", revision=0)
        group = await controller.drive("fixture", printer.observe, printer.send)
        assert [wire for wire, _ in printer.writes] == [1]  # the anchor already matches
        # The owner edits the anchor afterwards; restore never writes it back.
        edited = replace(printer.slots[0].declaration, tray_info_idx="GFG02")
        printer.slots[0] = replace(printer.slots[0], declaration=edited, receipt=9)
        controller.disable("fixture", revision=group["revision"])
        await controller.drive("fixture", printer.observe, printer.send)
        assert all(wire != 0 for wire, _ in printer.writes)
        assert printer.slots[0].declaration == edited
    finally:
        store.close()


async def test_a_removed_and_reset_backup_starts_unconfirmed(tmp_path):
    store, controller, printer, _ = await fixture(tmp_path)
    try:
        group = await controller.drive("fixture", printer.observe, printer.send)
        assert group["state"] == "active"
        controller.disable("fixture", revision=group["revision"])
        group = await controller.drive("fixture", printer.observe, printer.send)
        assert group["state"] == "disabled"
        # The same pair again, with the spools never moved: same fingerprint.
        controller.plan(
            "fixture", await printer.observe(), [0, 1], "FFFFFFFF", revision=group["revision"]
        )
        quiet = replace(await printer.observe(), eligibility_reported_at=None)
        assert controller.status("fixture", quiet)["confirmed_at"] is None
    finally:
        store.close()


async def test_the_restoration_worker_notes_a_confirmation_nobody_polled(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from bambu_bridge.service.refill_adapter import restore_when_idle
    from bambu_bridge.service.refill_reports import RefillReports
    from tests.test_refill_reports import raw

    tracker = RefillReports()
    tracker.observe(raw())  # both slots FF0000FF, backup mask 3, Auto Refill on
    controller = RefillController(RefillStore(tmp_path))
    group = controller.plan("p1", tracker.snapshot(connected=True), [0, 1], "FF0000FF", revision=0)
    for step in group["steps"]:
        step["state"] = "owned"
    controller.store.save("p1", group, group["revision"])
    printer = SimpleNamespace(serial="p1", connected=True, refill_reports=tracker)
    task = asyncio.create_task(restore_when_idle(lambda: [printer], controller, interval=0.01))
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if controller._confirmed:
                break
        quiet = replace(tracker.snapshot(connected=True), eligibility_reported_at=None)
        status = controller.status("p1", quiet)
        assert (status["state"], status["stale"]) == ("pending", True)
        assert status["confirmed_at"] is not None
    finally:
        task.cancel()
        controller.store.close()
