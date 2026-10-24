"""A spool that left its slot is never charged: the tag in the slot decides."""

import dataclasses
import time
from types import SimpleNamespace

from bambu_bridge.inventory_sources import declared_identity, spool_key
from bambu_bridge.service import usage_ledger
from bambu_bridge.service.material_inventory import MaterialInventory
from bambu_bridge.service.shadow_accounting import (
    assignment_stands,
    plan_manifest,
    slot_identities,
)
from bambu_bridge.spool_assignments import SpoolAssignment
from tests.test_usage_ledger import at_layer, running, tick

TAG_A = "A1B2C3D4E5F60001"
TAG_B = "A1B2C3D4E5F60002"


def tray(tag: str | None, material: str = "PETG") -> dict:
    return {
        "id": "0",
        "tray_type": material,
        "tray_color": "FF0000FF",
        "tray_info_idx": "GFG00",
        "tag_uid": tag or "0000000000000000",
        "tray_uuid": "",
    }


def device(tag: str | None):
    """A printer whose AMS tray 1 (wire 0) holds the given spool, with no refills."""
    material = MaterialInventory()
    material.observe(
        {
            "ams": {
                "ams": [
                    {
                        "id": "0",
                        "tray": [tray(tag)] + [{"id": str(i), "tray_type": ""} for i in (1, 2, 3)],
                    }
                ],
                "ams_exist_bits": "1",
                "tray_exist_bits": "1",
            }
        }
    )
    return SimpleNamespace(
        serial="fixture",
        material_inventory=material,
        auto_refill=SimpleNamespace(transitions=[]),
        diagnostic_journal=SimpleNamespace(archived_refills=[]),
    )


def identity_of(tag: str | None) -> str:
    return declared_identity(tray(tag))


async def assign(service, spool_id: int, identity: str | None):
    snapshot = await service.assignments.snapshot()
    held = SpoolAssignment(
        "fixture", "ams:fixture:tray:0", spool_id, observed_identity=identity, wire_slot=0
    )
    await service.assignments.replace(
        (held,), provider_id=snapshot.provider_id, expected_revision=snapshot.revision
    )
    return await service.assignments.snapshot()


def test_slot_identities_match_what_the_assignment_flow_stores():
    tagged, untagged = slot_identities(device(TAG_A))["0"], slot_identities(device(None))["0"]
    assert (tagged["identity"], tagged["tagged"]) == (identity_of(TAG_A), True)
    assert (untagged["identity"], untagged["tagged"]) == (identity_of(None), False)
    assert tagged["spool_key"] == spool_key(tray(TAG_A)) and untagged["spool_key"] == "untagged"
    assert tagged["material"] == "PETG"
    assert slot_identities(SimpleNamespace()) == {}


def test_a_stamped_assignment_is_judged_as_the_seating_pass_judges_it():
    held = SpoolAssignment(
        "fixture",
        "ams:fixture:tray:0",
        43,
        observed_identity=identity_of(TAG_A),
        wire_slot=0,
        spool_key=spool_key(tray(TAG_A)),
        tray_type="PETG",
    )
    assert assignment_stands(held, slot_identities(device(TAG_A))["0"])
    assert not assignment_stands(held, slot_identities(device(TAG_B))["0"])
    assert not assignment_stands(held, slot_identities(device(None))["0"])  # its tag left
    untagged = dataclasses.replace(held, spool_key="untagged")
    assert assignment_stands(untagged, slot_identities(device(None))["0"])


async def test_dispatch_leaves_out_a_tagged_slot_whose_spool_left(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    try:
        snapshot = await assign(service, 43, identity_of(TAG_A))
        frozen = plan_manifest(
            "a" * 64,
            1,
            [{"0": 1.0}],
            {},
            {"ams_mapping": [0]},
            snapshot,
            "fixture",
            slots=slot_identities(device(TAG_A)),
        )
        assert frozen["wire_spools"] == {"0": 43}  # the tagged spool is the one assigned
        assert frozen["slot_identities"] == {"0": identity_of(TAG_A)}
        swapped = plan_manifest(
            "a" * 64,
            1,
            [{"0": 1.0}],
            {},
            {"ams_mapping": [0]},
            snapshot,
            "fixture",
            slots=slot_identities(device(TAG_B)),
        )
        assert swapped["wire_spools"] == {}  # another tag: nothing is frozen for that slot
        untagged = plan_manifest(
            "a" * 64,
            1,
            [{"0": 1.0}],
            {},
            {"ams_mapping": [0]},
            snapshot,
            "fixture",
            slots=slot_identities(device(None)),
        )
        assert untagged["wire_spools"] == {"0": 43}  # no tag: no proof, charged as today
    finally:
        await service.close()


async def test_a_slot_whose_tag_changed_is_asked_about_not_charged(tmp_path, monkeypatch):
    # Dispatched with nothing frozen for slot 1 (the tag had changed); at the end
    # the stored assignment still names the old spool, so the slot is unassigned.
    provider, service, inbox, attempt, clock = await running(
        tmp_path, monkeypatch, layer=10, wire_spools={}
    )
    try:
        await assign(service, 43, identity_of(TAG_A))
        at_layer(inbox, attempt, 10, "completed")
        await tick(service, inbox, device(TAG_B))
        assert provider.uses == []
        assert inbox.accounting_event(attempt)["delivery_state"] == "review"
        assert usage_ledger.shortfalls(inbox, attempt) == [
            {
                "kind": "unassigned_slot",
                "wire_slot": 0,
                "spool_id": None,
                "grams": 10.0,
                "material": None,
            }
        ]
    finally:
        await service.close()


async def test_an_assignment_updated_to_the_new_tag_is_charged(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(
        tmp_path, monkeypatch, layer=10, wire_spools={}
    )
    try:
        await assign(service, 44, identity_of(TAG_B))  # auto-assign saw the new tag
        at_layer(inbox, attempt, 10, "completed")
        await tick(service, inbox, device(TAG_B))
        assert provider.uses == [(44, 10.0)]
        assert inbox.accounting_event(attempt)["delivery_state"] == "delivered"
    finally:
        await service.close()


async def test_an_untagged_slot_is_charged_as_before(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(
        tmp_path, monkeypatch, layer=10, wire_spools={}
    )
    try:
        await assign(service, 43, identity_of(None))
        at_layer(inbox, attempt, 10, "completed")
        await tick(service, inbox, device(None))
        assert provider.uses == [(43, 10.0)]
    finally:
        await service.close()


async def test_a_tag_change_mid_print_keeps_the_dispatched_spool_and_says_so(tmp_path, monkeypatch):
    # Frozen at dispatch with spool 43 behind tag A; by the end the slot shows tag B.
    # The dispatched spool is charged (it was verified then; the swap was the
    # owner's act) and the record says the tag changed, for "Wrong spool?".
    provider, service, inbox, attempt, clock = await running(
        tmp_path, monkeypatch, layer=3, slot_identities={"0": identity_of(TAG_A)}
    )
    try:
        await assign(service, 43, identity_of(TAG_A))
        await tick(service, inbox, device(TAG_A))
        assert provider.uses == [(43, 2.5)]
        at_layer(inbox, attempt, 10, "completed")
        await tick(service, inbox, device(TAG_B))
        assert provider.uses == [(43, 2.5), (43, 7.5)]
        assert inbox.accounting_event(attempt)["delivery_state"] == "delivered"
        assert inbox.accounting_review(attempt) == "tag_changed_slot_0"
    finally:
        await service.close()


def test_the_check_tolerates_a_printer_without_a_frame():
    bare = SimpleNamespace(material_inventory=SimpleNamespace(snapshot=lambda: {}))
    assert slot_identities(bare) == {}
    assert time.time() > 0


def _device_with(tag: str | None, **changes) -> SimpleNamespace:
    service = device(tag)
    frame = service.material_inventory.snapshot()["ams"]
    frame["ams"][0]["tray"][0].update(changes)
    service.material_inventory.observe({"ams": frame})
    return service


def _manifest(snapshot, slots):
    return plan_manifest(
        "a" * 64, 1, [{"0": 1.0}], {}, {"ams_mapping": [0]}, snapshot, "fixture", slots=slots
    )


async def test_a_tagged_slot_relabelled_still_charges_its_spool(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    try:
        held = SpoolAssignment(
            "fixture",
            "ams:fixture:tray:0",
            4,
            observed_identity=identity_of(TAG_A),
            wire_slot=0,
            spool_key=spool_key(tray(TAG_A)),
            tray_type="PETG",
        )
        snapshot = await service.assignments.snapshot()
        await service.assignments.replace(
            (held,), provider_id=snapshot.provider_id, expected_revision=snapshot.revision
        )
        snapshot = await service.assignments.snapshot()
        relabelled = _device_with(
            TAG_A, tray_info_idx="GFG02", nozzle_temp_min=230, tray_color="FFFFFFFF"
        )
        # (a) declaration edited: stands, frozen and charged to #4
        assert _manifest(snapshot, slot_identities(relabelled))["wire_spools"] == {"0": 4}
        # (c) another tag: not standing, nothing frozen for the slot
        assert _manifest(snapshot, slot_identities(device(TAG_B)))["wire_spools"] == {}
    finally:
        await service.close()


async def test_an_unstamped_tagged_assignment_stands_before_the_first_seating(
    tmp_path, monkeypatch
):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    try:
        # (b) as 0c416d2 stored it: the source-v3 identity and no seat
        snapshot = await assign(service, 4, identity_of(TAG_A))
        assert snapshot.assignments[0].spool_key is None
        assert _manifest(snapshot, slot_identities(device(TAG_A)))["wire_spools"] == {"0": 4}
        assert _manifest(snapshot, slot_identities(device(TAG_B)))["wire_spools"] == {}
    finally:
        await service.close()
