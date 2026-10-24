"""A held print carries one action: charge it, charge nothing, or mark it charged by hand."""

import json
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from bambu_bridge.api.inventory_usage import (
    Charge,
    Reassign,
    Resolution,
    held_print,
    reassign_print,
    resolve_held_print,
)
from bambu_bridge.service import consumption_delivery, usage_ledger, usage_review
from bambu_bridge.spool_assignments import SpoolAssignment
from tests.test_accounting_attempts import manifest
from tests.test_inventory_service import PRODUCT
from tests.test_usage_counting import iso, printer
from tests.test_usage_ledger import at_layer, running, spool, tick

OLD_FORMAT = {
    "layer_feed_mm": [0, 1000, 2000],
    "sliced_settings": {"slicer_mass_g": [83.78, 0.0]},
    "sources": [{"physical_source": "external", "spool_id": 43}],
}


async def held(tmp_path, monkeypatch):
    """The owner's print: frozen by an older release, ended, held with nothing written."""
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    snapshot = await service.assignments.snapshot()
    external = SpoolAssignment("fixture", "external", 43, observed_identity="fixture")
    await service.assignments.replace(
        (external,), provider_id=snapshot.provider_id, expected_revision=snapshot.revision
    )
    at_layer(inbox, attempt, 186, "completed")
    old = ("layer_grams", "tool_wires", "wire_spools")
    legacy = {k: v for k, v in manifest().items() if k not in old}
    legacy.update(OLD_FORMAT)
    with inbox.connect() as db:
        db.execute(
            "UPDATE accounting_shadow_events SET manifest=? WHERE attempt_id=?",
            (json.dumps(legacy), attempt),
        )
    await tick(service, inbox)
    assert inbox.accounting_event(attempt)["delivery_state"] == "review"
    return provider, service, inbox, attempt, clock


def request_for(inbox, device=None, library=None):
    state = SimpleNamespace(
        native_gateway=SimpleNamespace(inbox=inbox),
        registry=SimpleNamespace(get=lambda _id: device or printer()),
        library=library,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


RED_PETG = {**PRODUCT, "id": 9, "name": "PETG Red", "material": "PETG", "color_hex": "FF0000"}


async def mixed(tmp_path, monkeypatch, **fields):
    """Filament 0 from spool 43; filament 1 from AMS tray 4 (wire 3), which has no spool."""
    provider, service, inbox, attempt, clock = await running(
        tmp_path,
        monkeypatch,
        layer_grams=[{"0": 1.0, "1": 2.0}] * 4,
        tool_wires={"0": 0, "1": 3},
        wire_spools={"0": 43},
        tray_materials={"3": {"material": "PETG", "color": "FF0000FF"}},
        **fields,
    )
    provider.spools[46] = {**spool(46), "filament": RED_PETG}
    return provider, service, inbox, attempt, clock


async def test_a_held_print_offers_the_slicer_figure_against_its_spool(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await held(tmp_path, monkeypatch)
    try:
        body = await held_print(attempt, request_for(inbox), service)
        assert body["delivery_state"] == "review"
        assert body["review_reason"] == "manifest_predates_layer_plan"
        assert body["suggested"] == [{"spool_id": 43, "grams": 83.78}]
        assert body["candidates"] == [{"spool_id": 43, "wire_slot": 254}]
        assert body["written"] == [] and body["resolution"] is None
        assert body["shortfalls"] == [
            {
                "kind": "no_layer_plan",
                "wire_slot": None,
                "spool_id": 43,
                "grams": 83.78,
                "material": None,
            }
        ]
        assert body["charged"] == [] and body["title"] == ""
        assert body["created_at"] and body["ended_at"] and body["capture_id"] is None
    finally:
        await service.close()


async def test_charging_a_held_print_writes_once_and_closes_it(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await held(tmp_path, monkeypatch)
    try:
        body = await resolve_held_print(
            attempt,
            Resolution(action="charge", charges=[{"spool_id": 43, "grams": 83.78}]),
            request_for(inbox),
            service,
        )
        assert provider.uses == [(43, 83.78)]
        assert body["delivery_state"] == "delivered"
        assert body["resolution"]["action"] == "charge"
        assert body["resolution"]["charges"] == [{"spool_id": 43, "grams": 83.78}]
        assert body["written"] == [
            {"spool_id": 43, "written_g": 83.78, "state": "done", "reason": None}
        ]
        with pytest.raises(HTTPException) as refused:
            await resolve_held_print(
                attempt, Resolution(action="nothing"), request_for(inbox), service
            )
        assert refused.value.status_code == 409
        await tick(service, inbox)  # the automatic side leaves a settled print alone
        assert provider.uses == [(43, 83.78)]
    finally:
        await service.close()


@pytest.mark.parametrize("action", ["nothing", "charged_elsewhere"])
async def test_a_held_print_can_leave_the_queue_without_a_write(tmp_path, monkeypatch, action):
    provider, service, inbox, attempt, clock = await held(tmp_path, monkeypatch)
    try:
        body = await resolve_held_print(
            attempt, Resolution(action=action), request_for(inbox), service
        )
        assert provider.uses == []
        assert body["delivery_state"] == "delivered"
        assert body["resolution"]["action"] == action
    finally:
        await service.close()


async def test_an_unconfirmed_charge_keeps_the_print_in_the_queue(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await held(tmp_path, monkeypatch)
    provider.fail_get = True
    try:
        with pytest.raises(HTTPException) as refused:
            await resolve_held_print(
                attempt,
                Resolution(action="charge", charges=[{"spool_id": 43, "grams": 83.78}]),
                request_for(inbox),
                service,
            )
        assert refused.value.status_code == 409
        assert inbox.accounting_event(attempt)["delivery_state"] == "review"
        assert provider.uses == []
        provider.fail_get = False
        body = await resolve_held_print(
            attempt,
            Resolution(action="charge", charges=[{"spool_id": 43, "grams": 83.78}]),
            request_for(inbox),
            service,
        )
        assert provider.uses == [(43, 83.78)] and body["delivery_state"] == "delivered"
    finally:
        await service.close()


async def test_a_charge_tops_up_what_the_ledger_already_wrote(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    try:
        await tick(service, inbox)  # layer 3: 2.5 g written while it ran
        at_layer(inbox, attempt, 10, "completed")
        provider.fail_put = "before"
        failures: dict[str, float] = {}
        await tick(service, inbox, failures=failures)
        clock.now += 7 * 3600  # Spoolman silent past the budget
        await tick(service, inbox, failures=failures)
        assert inbox.accounting_event(attempt)["delivery_state"] == "review"
        provider.fail_put = None
        body = await held_print(attempt, request_for(inbox), service)
        assert body["suggested"] == [{"spool_id": 43, "grams": 10.0}]
        assert body["charged"] == [{"spool_id": 43, "grams": 2.5, "layers": [1, 10]}]
        assert body["shortfalls"] == [
            {
                "kind": "delivery_error",
                "wire_slot": 0,
                "spool_id": 43,
                "grams": 7.5,
                "material": None,
            }
        ]
        assert body["written"] == [
            {"spool_id": 43, "written_g": 2.5, "state": "open", "reason": None}
        ]
        await resolve_held_print(
            attempt,
            Resolution(action="charge", charges=[{"spool_id": 43, "grams": 10.0}]),
            request_for(inbox),
            service,
        )
        assert provider.uses == [(43, 2.5), (43, 7.5)]
    finally:
        await service.close()


async def test_unknown_prints_and_bad_charges_are_refused(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await held(tmp_path, monkeypatch)
    try:
        with pytest.raises(HTTPException) as missing:
            await held_print("0" * 32, request_for(inbox), service)
        assert missing.value.status_code == 404
        with pytest.raises(usage_review.ReviewError) as empty:
            await usage_review.resolve(service, inbox, lambda _id: printer(), attempt, "charge", [])
        assert empty.value.status == 422
        assert inbox.accounting_event(attempt)["delivery_state"] == "review"
        assert usage_ledger.rows(inbox, attempt) == {}
    finally:
        await service.close()


async def test_the_listing_can_show_only_the_held_prints(tmp_path, monkeypatch):
    from bambu_bridge.api.inventory import accounting_shadow

    provider, service, inbox, attempt, clock = await held(tmp_path, monkeypatch)
    settled = inbox.claim_external("other", {"print": {"command": "project_file"}}, reserved=True)
    inbox.freeze_accounting(settled, manifest() | {"wire_spools": {"0": 43}})
    at_layer(inbox, settled, 10, "completed")
    try:
        await tick(service, inbox)  # the second print is debited and delivered
        page = await accounting_shadow(request_for(inbox), service, 50, 0, "review")
        assert [row["attempt_id"] for row in page["attempts"]] == [attempt]
        assert page["attempts"][0]["review_reason"] == "manifest_predates_layer_plan"
        everything = await accounting_shadow(request_for(inbox), service, 50, 0, None)
        assert {row["delivery_state"] for row in everything["attempts"]} == {"review", "delivered"}
        with pytest.raises(ValueError):
            inbox.accounting_page(50, 0, "held")
    finally:
        await service.close()


async def test_charging_a_spool_that_cannot_take_it_is_refused_with_the_reason(
    tmp_path, monkeypatch
):
    provider, service, inbox, attempt, clock = await held(tmp_path, monkeypatch)
    provider.spools[43]["archived"] = True
    try:
        with pytest.raises(HTTPException) as refused:
            await resolve_held_print(
                attempt,
                Resolution(action="charge", charges=[{"spool_id": 43, "grams": 83.78}]),
                request_for(inbox),
                service,
            )
        assert refused.value.status_code == 409 and "archived" in refused.value.detail
        assert inbox.accounting_event(attempt)["delivery_state"] == "review"
        assert provider.uses == []
        # Another spool takes it; the refused one stays named in the record.
        body = await resolve_held_print(
            attempt,
            Resolution(action="charge", charges=[{"spool_id": 44, "grams": 83.78}]),
            request_for(inbox),
            service,
        )
        assert provider.uses == [(44, 83.78)] and body["delivery_state"] == "delivered"
        assert {w["spool_id"]: w["reason"] for w in body["written"]} == {43: "archived", 44: None}
    finally:
        await service.close()


async def test_a_slot_used_without_a_spool_holds_the_print_and_asks_what_was_in_it(
    tmp_path, monkeypatch
):
    provider, service, inbox, attempt, clock = await mixed(tmp_path, monkeypatch, layer=4)
    try:
        at_layer(inbox, attempt, 4, "completed")
        await tick(service, inbox)
        assert provider.uses == [(43, 4.0)]  # the assigned spool is settled at once
        assert inbox.accounting_event(attempt)["delivery_state"] == "review"
        assert inbox.accounting_review(attempt) == "unassigned_slot"
        body = await held_print(attempt, request_for(inbox), service)
        assert body["suggested"] == [{"spool_id": 43, "grams": 4.0}]
        assert body["charged"] == [{"spool_id": 43, "grams": 4.0, "layers": [1, 4]}]
        assert body["shortfalls"] == [
            {
                "kind": "unassigned_slot",
                "wire_slot": 3,
                "spool_id": None,
                "grams": 8.0,
                "material": "PETG",
            }
        ]
        body = await resolve_held_print(
            attempt,
            Resolution(action="charge", charges=[{"spool_id": 46, "slot": 3}]),
            request_for(inbox),
            service,
        )
        assert provider.uses == [(43, 4.0), (46, 8.0)]
        assert body["delivery_state"] == "delivered"
        assert body["resolution"]["charges"] == [{"spool_id": 46, "grams": 8.0}]
        assert body["shortfalls"][0]["kind"] == "unassigned_slot"  # kept for the history
        assert body["charged"] == [
            {"spool_id": 43, "grams": 4.0, "layers": [1, 4]},
            {"spool_id": 46, "grams": 8.0, "layers": None},
        ]
    finally:
        await service.close()


async def test_resolving_after_a_partial_charge_never_charges_the_assigned_part_twice(
    tmp_path, monkeypatch
):
    provider, service, inbox, attempt, clock = await mixed(tmp_path, monkeypatch, layer=3)
    try:
        await tick(service, inbox)  # while it ran: 2.5 g to spool 43
        at_layer(inbox, attempt, 4, "completed")
        await tick(service, inbox)  # the end tops 43 up to 4 g and holds the rest
        assert provider.uses == [(43, 2.5), (43, 1.5)]
        assert inbox.accounting_review(attempt) == "unassigned_slot"
        await resolve_held_print(
            attempt,
            Resolution(action="charge", charges=[{"spool_id": 46, "slot": 3}]),
            request_for(inbox),
            service,
        )
        assert provider.uses == [(43, 2.5), (43, 1.5), (46, 8.0)]
    finally:
        await service.close()


async def test_a_refill_into_an_unassigned_slot_holds_only_that_part(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch, layer=10)
    switch = {
        "from_slot": 1,
        "to_slot": 3,
        "status": "completed",
        "layer": 4,
        "observed_at": iso(time.time()),
    }
    device = printer([switch])
    try:
        at_layer(inbox, attempt, 10, "completed")
        await tick(service, inbox, device)
        assert provider.uses == [(43, 3.5)]  # layers 1-3 and half of 4
        assert inbox.accounting_review(attempt) == "unassigned_slot"
        body = await held_print(attempt, request_for(inbox, device), service)
        assert body["shortfalls"] == [
            {
                "kind": "unassigned_slot",
                "wire_slot": 2,
                "spool_id": None,
                "grams": 6.5,
                "material": None,
            }
        ]
        with pytest.raises(HTTPException) as refused:
            await resolve_held_print(
                attempt,
                Resolution(action="charge", charges=[{"spool_id": 44, "slot": 1}]),
                request_for(inbox, device),
                service,
            )
        assert refused.value.status_code == 422
        await resolve_held_print(
            attempt,
            Resolution(action="charge", charges=[{"spool_id": 44, "slot": 2}]),
            request_for(inbox, device),
            service,
        )
        assert provider.uses == [(43, 3.5), (44, 6.5)]
    finally:
        await service.close()


def test_a_charge_names_grams_or_a_slot_never_both():
    assert Charge(spool_id=5, grams=1.5).slot is None
    assert Charge(spool_id=5, slot=254).grams is None
    with pytest.raises(ValueError):
        Charge(spool_id=5)
    with pytest.raises(ValueError):
        Charge(spool_id=5, grams=1.5, slot=0)


async def test_the_listing_carries_what_the_card_needs(tmp_path, monkeypatch):
    from bambu_bridge.api.inventory import accounting_shadow

    provider, service, inbox, attempt, clock = await mixed(tmp_path, monkeypatch, layer=4)
    try:
        at_layer(inbox, attempt, 4, "completed")
        await tick(service, inbox)
        page = await accounting_shadow(request_for(inbox), service, 50, 0, "review")
        (row,) = page["attempts"]
        assert row["attempt_id"] == attempt and row["title"] == ""
        assert row["created_at"] and row["ended_at"] >= row["created_at"]
        assert row["capture_id"] is None
        assert row["charged"] == [{"spool_id": 43, "grams": 4.0, "layers": [1, 4]}]
        assert row["shortfalls"] == [
            {
                "kind": "unassigned_slot",
                "wire_slot": 3,
                "spool_id": None,
                "grams": 8.0,
                "material": "PETG",
            }
        ]
        assert row["resolution"] is None
        assert inbox.review_count() == 1
        await resolve_held_print(attempt, Resolution(action="nothing"), request_for(inbox), service)
        assert inbox.review_count() == 0
        page = await accounting_shadow(request_for(inbox), service, 50, 0, None)
        (row,) = page["attempts"]
        assert row["delivery_state"] == "delivered" and len(row["shortfalls"]) == 1
        assert row["resolution"]["action"] == "nothing" and row["resolution"]["resolved_at"]
    finally:
        await service.close()


async def test_a_spool_that_ran_out_can_be_charged_elsewhere_for_the_rest(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    provider.spools[43]["remaining_weight"] = 1.0
    try:
        at_layer(inbox, attempt, 10, "completed")
        await tick(service, inbox)
        assert provider.uses == [(43, 1.0)]
        body = await held_print(attempt, request_for(inbox), service)
        assert body["shortfalls"] == [
            {"kind": "spool_empty", "wire_slot": 0, "spool_id": 43, "grams": 9.0, "material": None}
        ]
        body = await resolve_held_print(
            attempt,
            Resolution(action="charge", charges=[{"spool_id": 44, "grams": 9.0}]),
            request_for(inbox),
            service,
        )
        assert provider.uses == [(43, 1.0), (44, 9.0)] and body["delivery_state"] == "delivered"
    finally:
        await service.close()


async def test_the_library_capture_is_merged_in_for_the_thumbnail(tmp_path, monkeypatch):
    from bambu_bridge.api.inventory import accounting_shadow

    provider, service, inbox, attempt, clock = await held(tmp_path, monkeypatch)
    library = SimpleNamespace(captures_for=lambda ids: {attempt: "cap-1"} if attempt in ids else {})
    try:
        body = await held_print(attempt, request_for(inbox, library=library), service)
        assert body["capture_id"] == "cap-1"
        page = await accounting_shadow(request_for(inbox, library=library), service, 50, 0, None)
        assert page["attempts"][0]["capture_id"] == "cap-1"
        # No library, or a row the library has not scanned: null, never an error.
        assert (await held_print(attempt, request_for(inbox), service))["capture_id"] is None
        broken = SimpleNamespace(captures_for=lambda ids: 1 / 0)
        assert (await held_print(attempt, request_for(inbox, library=broken), service))[
            "capture_id"
        ] is None
    finally:
        await service.close()


# --- what the bug hunt found -----------------------------------------------------


def total(provider, spool_id):
    return round(sum(grams for s, grams in provider.uses if s == spool_id), 2)


async def test_a_slot_named_twice_is_refused_before_anything_is_written(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await mixed(tmp_path, monkeypatch, layer=4)
    provider.spools[47] = {**spool(47), "filament": RED_PETG}
    try:
        at_layer(inbox, attempt, 4, "completed")
        await tick(service, inbox)
        with pytest.raises(HTTPException) as refused:
            await resolve_held_print(
                attempt,
                Resolution(
                    action="charge",
                    charges=[{"spool_id": 46, "slot": 3}, {"spool_id": 47, "slot": 3}],
                ),
                request_for(inbox),
                service,
            )
        assert refused.value.status_code == 422 and "named twice" in refused.value.detail
        assert total(provider, 46) + total(provider, 47) == 0
        assert inbox.accounting_event(attempt)["delivery_state"] == "review"
    finally:
        await service.close()


async def test_a_slot_charge_adds_to_what_the_spool_already_has_from_this_print(
    tmp_path, monkeypatch
):
    provider, service, inbox, attempt, clock = await mixed(tmp_path, monkeypatch, layer=4)
    try:
        at_layer(inbox, attempt, 4, "completed")
        await tick(service, inbox)
        assert total(provider, 43) == 4.0
        body = await resolve_held_print(
            attempt,
            Resolution(action="charge", charges=[{"spool_id": 43, "slot": 3}]),
            request_for(inbox),
            service,
        )
        assert total(provider, 43) == 12.0
        assert body["charged"] == [{"spool_id": 43, "grams": 12.0, "layers": [1, 4]}]
        assert body["resolution"]["charges"] == [{"spool_id": 43, "grams": 12.0}]
    finally:
        await service.close()


async def test_a_retry_with_a_bigger_total_tops_up_a_spool_already_closed(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await held(tmp_path, monkeypatch)
    try:
        with pytest.raises(HTTPException) as first:
            await resolve_held_print(
                attempt,
                Resolution(
                    action="charge",
                    charges=[{"spool_id": 43, "grams": 83.78}, {"spool_id": 99, "grams": 5.0}],
                ),
                request_for(inbox),
                service,
            )
        assert first.value.status_code == 409
        assert "#99 isn't in Spoolman" in first.value.detail
        assert "already charged #43 83.78 g" in first.value.detail
        assert total(provider, 43) == 83.78
        await resolve_held_print(
            attempt,
            Resolution(
                action="charge",
                charges=[{"spool_id": 43, "grams": 90.0}, {"spool_id": 44, "grams": 5.0}],
            ),
            request_for(inbox),
            service,
        )
        assert total(provider, 43) == 90.0 and total(provider, 44) == 5.0
    finally:
        await service.close()


async def test_closing_without_a_write_records_no_charges(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await held(tmp_path, monkeypatch)
    try:
        body = await resolve_held_print(
            attempt,
            Resolution(action="nothing", charges=[{"spool_id": 43, "grams": 10.0}]),
            request_for(inbox),
            service,
        )
        assert provider.uses == [] and body["resolution"]["charges"] == []
    finally:
        await service.close()


def test_a_charge_is_bounded_at_the_model():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Charge(spool_id=2**63, grams=5.0)
    for bad in (float("nan"), float("inf"), -1.0, 0.0, 10_001.0):
        with pytest.raises(ValidationError):
            Charge(spool_id=43, grams=bad)


async def test_two_resolves_at_once_charge_once(tmp_path, monkeypatch):
    import asyncio

    provider, service, inbox, attempt, clock = await held(tmp_path, monkeypatch)
    try:
        charge = Resolution(action="charge", charges=[{"spool_id": 43, "grams": 83.78}])
        outcomes = await asyncio.gather(
            resolve_held_print(attempt, charge, request_for(inbox), service),
            resolve_held_print(attempt, charge, request_for(inbox), service),
            return_exceptions=True,
        )
        refused = [o for o in outcomes if isinstance(o, HTTPException)]
        assert len(refused) == 1 and refused[0].status_code == 409
        assert provider.uses == [(43, 83.78)]
    finally:
        await service.close()


async def test_a_resolve_during_the_end_pass_is_not_undone_by_it(tmp_path, monkeypatch):
    # The end pass has settled the assigned spool and is about to record the hold
    # when the owner resolves the print; the pass must not flip it back to review.
    import asyncio

    provider, service, inbox, attempt, clock = await mixed(tmp_path, monkeypatch, layer=4)
    original = consumption_delivery.shortfalls_for
    resolved: list = []

    def shortfalls_then_resolve(*args, **kwargs):
        found = original(*args, **kwargs)
        if not resolved:
            # Runs on the loop between the pass's settle and its close.
            resolved.append(
                asyncio.ensure_future(
                    resolve_held_print(
                        attempt,
                        Resolution(action="charge", charges=[{"spool_id": 46, "slot": 3}]),
                        request_for(inbox),
                        service,
                    )
                )
            )
        return found

    monkeypatch.setattr(consumption_delivery, "shortfalls_for", shortfalls_then_resolve)
    try:
        at_layer(inbox, attempt, 4, "completed")
        await tick(service, inbox)
        await resolved[0]
        event = inbox.accounting_event(attempt)
        assert event["delivery_state"] == "delivered"
        assert total(provider, 43) == 4.0 and total(provider, 46) == 8.0
        assert usage_review._resolution(inbox, attempt)["action"] == "charge"
    finally:
        await service.close()


# --- "that benchy didn't come from #5, it came from #12" ---------------------


async def debited(tmp_path, monkeypatch, **fields):
    """A finished print, 10 g charged to spool 43 and delivered."""
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch, **fields)
    at_layer(inbox, attempt, 10, "completed")
    await tick(service, inbox)
    assert provider.uses == [(43, 10.0)]
    assert inbox.accounting_event(attempt)["delivery_state"] == "delivered"
    return provider, service, inbox, attempt, clock


async def move(attempt, inbox, service, from_spool, to_spool, grams=None):
    return await reassign_print(
        attempt,
        Reassign(from_spool_id=from_spool, to_spool_id=to_spool, grams=grams),
        request_for(inbox),
        service,
    )


async def test_a_print_can_be_moved_whole_to_the_spool_it_really_came_from(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await debited(tmp_path, monkeypatch)
    try:
        body = await move(attempt, inbox, service, 43, 44)
        assert provider.uses == [(43, 10.0), (43, -10.0), (44, 10.0)]
        assert provider.spools[43]["used_weight"] == 0.0
        assert provider.spools[44]["used_weight"] == 10.0
        assert body["charged"] == [{"spool_id": 44, "grams": 10.0, "layers": [1, 10]}]
        rows = usage_ledger.rows(inbox, attempt)
        assert (rows[43]["written_g"], rows[43]["counted_g"]) == (0.0, 0.0)
        assert (rows[44]["written_g"], rows[44]["counted_g"], rows[44]["state"]) == (
            10.0,
            10.0,
            "done",
        )
        (moved,) = body["moves"]
        assert (moved["from_spool_id"], moved["to_spool_id"], moved["grams"]) == (43, 44, 10.0)
        assert moved["state"] == "done" and moved["finished_at"]
        assert body["delivery_state"] == "delivered"
    finally:
        await service.close()


async def test_part_of_a_print_can_be_moved(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await debited(tmp_path, monkeypatch)
    try:
        body = await move(attempt, inbox, service, 43, 44, grams=4)
        assert provider.uses == [(43, 10.0), (43, -4.0), (44, 4.0)]
        assert body["charged"] == [
            {"spool_id": 43, "grams": 6.0, "layers": [1, 10]},
            {"spool_id": 44, "grams": 4.0, "layers": [1, 10]},
        ]
        # And the rest can follow later, to a third spool.
        await move(attempt, inbox, service, 43, 45)
        assert provider.spools[43]["used_weight"] == 0.0
        assert provider.spools[45]["used_weight"] == 6.0
    finally:
        await service.close()


async def test_a_move_interrupted_after_the_credit_is_finished_by_sending_it_again(
    tmp_path, monkeypatch
):
    provider, service, inbox, attempt, clock = await debited(tmp_path, monkeypatch)
    provider.fail_put_for = {44}
    try:
        with pytest.raises(HTTPException) as refused:
            await move(attempt, inbox, service, 43, 44)
        assert refused.value.status_code == 409
        assert refused.value.detail == "Gave #43 10 g back; Spoolman didn't answer; try again"
        assert provider.uses == [(43, 10.0), (43, -10.0)]
        rows = usage_ledger.rows(inbox, attempt)
        assert rows[43]["written_g"] == 0.0
        assert (rows[44]["counted_g"], rows[44]["written_g"]) == (10.0, 0.0)  # owed, not lost
        body = await held_print(attempt, request_for(inbox), service)
        assert body["moves"][0]["state"] == "credited"
        # Another pair is refused until this move is finished.
        with pytest.raises(HTTPException) as other:
            await move(attempt, inbox, service, 43, 45)
        assert other.value.detail == "Finish the move from #43 to #44 first"
        provider.fail_put_for = set()
        body = await move(attempt, inbox, service, 43, 44)
        assert provider.uses == [(43, 10.0), (43, -10.0), (44, 10.0)]  # credited once
        assert body["moves"][0]["state"] == "done"
        assert body["charged"] == [{"spool_id": 44, "grams": 10.0, "layers": [1, 10]}]
    finally:
        await service.close()


async def test_a_move_whose_credit_reply_was_lost_is_not_credited_twice(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await debited(tmp_path, monkeypatch)
    provider.lose_get_after_put = True
    try:
        with pytest.raises(HTTPException) as refused:
            await move(attempt, inbox, service, 43, 44)
        assert refused.value.detail == "Spoolman didn't answer; try again"
        assert provider.uses == [(43, 10.0), (43, -10.0)]  # landed, unwitnessed
        provider.fail_get = provider.lose_get_after_put = False
        await move(attempt, inbox, service, 43, 44)
        assert provider.uses == [(43, 10.0), (43, -10.0), (44, 10.0)]
        assert provider.spools[43]["used_weight"] == 0.0
    finally:
        await service.close()


async def test_a_print_closed_by_hand_can_still_be_moved(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await held(tmp_path, monkeypatch)
    try:
        await resolve_held_print(
            attempt,
            Resolution(action="charge", charges=[{"spool_id": 43, "grams": 83.78}]),
            request_for(inbox),
            service,
        )
        body = await move(attempt, inbox, service, 43, 44)
        assert provider.uses == [(43, 83.78), (43, -83.78), (44, 83.78)]
        assert body["charged"] == [{"spool_id": 44, "grams": 83.78, "layers": None}]
        assert body["resolution"]["action"] == "charge"  # the history keeps both
    finally:
        await service.close()


async def test_moves_are_refused_in_plain_words(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await debited(tmp_path, monkeypatch)
    provider.spools[45]["archived"] = True
    try:
        for args, status, message in [
            ((43, 43), 422, "Pick a different spool"),
            ((43, 44, 11), 422, "#43 has only 10 g from this print"),
            ((44, 43), 422, "#44 has nothing from this print"),
            ((43, 45), 409, "#45 is archived in Spoolman"),
            ((43, 99), 409, "#99 isn't in Spoolman any more"),
        ]:
            with pytest.raises(HTTPException) as refused:
                await move(attempt, inbox, service, *args)
            assert (refused.value.status_code, refused.value.detail) == (status, message), args
        with pytest.raises(HTTPException) as missing:
            await move("0" * 32, inbox, service, 43, 44)
        assert missing.value.status_code == 404
        # Spoolman says less was used than this print wrote (someone edited it):
        # the credit would push used_weight below zero, so it is refused.
        provider.spools[43]["used_weight"] = 3.0
        with pytest.raises(HTTPException) as short:
            await move(attempt, inbox, service, 43, 44)
        assert short.value.detail == "#43 has only 3 g used in Spoolman"
        assert provider.uses == [(43, 10.0)]
    finally:
        await service.close()


# --- which layers came from which spool ----------------------------------------


async def history(inbox, service, spool_id, library=None):
    from bambu_bridge.api.inventory_usage import spool_history

    return (await spool_history(spool_id, request_for(inbox, library=library), service, 20))[
        "prints"
    ]


async def test_a_single_spool_print_is_all_of_it(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await debited(tmp_path, monkeypatch)
    try:
        (entry,) = await history(inbox, service, 43)
        assert entry["attempt_id"] == attempt and entry["ended_at"]
        assert (entry["layers"], entry["total_layers"]) == ([1, 10], 10)
        assert (entry["grams"], entry["share_pct"]) == (10.0, 100)
        body = await held_print(attempt, request_for(inbox), service)
        assert body["charged"] == [{"spool_id": 43, "grams": 10.0, "layers": [1, 10]}]
        assert await history(inbox, service, 44) == []
    finally:
        await service.close()


async def test_a_refill_splits_the_layers_between_two_spools(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(
        tmp_path, monkeypatch, layer=10, wire_spools={"0": 43, "2": 44}
    )
    switch = {
        "from_slot": 1,
        "to_slot": 3,
        "status": "completed",
        "layer": 4,
        "observed_at": iso(time.time()),
    }
    device = printer([switch])
    try:
        at_layer(inbox, attempt, 10, "completed")
        await tick(service, inbox, device)
        # 43 fed layers 1-3 and half of 4 (3.5 g); 44 half of 4 and 5-10 (6.5 g).
        (first,) = await history(inbox, service, 43)
        (second,) = await history(inbox, service, 44)
        assert first["layers"] == [1, 4] and second["layers"] == [4, 10]
        assert (first["grams"], second["grams"]) == (3.5, 6.5)
        assert 99 <= first["share_pct"] + second["share_pct"] <= 101
        assert (first["share_pct"], second["share_pct"]) == (35, 65)
    finally:
        await service.close()


async def test_a_move_takes_the_layers_with_the_grams(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await debited(tmp_path, monkeypatch)
    try:
        await move(attempt, inbox, service, 43, 44, grams=4)  # part: both keep the range
        body = await held_print(attempt, request_for(inbox), service)
        assert body["charged"] == [
            {"spool_id": 43, "grams": 6.0, "layers": [1, 10]},
            {"spool_id": 44, "grams": 4.0, "layers": [1, 10]},
        ]
        assert (await history(inbox, service, 44))[0]["share_pct"] == 40
        await move(attempt, inbox, service, 43, 44)  # the rest: the range leaves 43
        body = await held_print(attempt, request_for(inbox), service)
        assert body["charged"] == [{"spool_id": 44, "grams": 10.0, "layers": [1, 10]}]
        assert await history(inbox, service, 43) == []
        (entry,) = await history(inbox, service, 44)
        assert (entry["layers"], entry["share_pct"]) == ([1, 10], 100)
    finally:
        await service.close()


async def test_a_stopped_print_shows_the_layers_it_reached(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch, layer=3)
    try:
        await tick(service, inbox)
        (entry,) = await history(inbox, service, 43)
        assert entry["ended_at"] is None and entry["layers"] == [1, 3]  # still printing
        at_layer(inbox, attempt, 6, "interrupted")
        await tick(service, inbox)
        (entry,) = await history(inbox, service, 43)
        assert entry["ended_at"] and (entry["layers"], entry["total_layers"]) == ([1, 6], 10)
        assert (entry["grams"], entry["share_pct"]) == (5.5, 100)
    finally:
        await service.close()
