"""A print is debited by counting: grams per layer per filament, per slot, per spool."""

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from bambu_bridge.gcode_usage import count_extrusion, layer_plan
from bambu_bridge.native_inbox import NativeInbox
from bambu_bridge.service import consumption_delivery
from bambu_bridge.spool_assignments import SpoolAssignment
from bambu_bridge.usage_split import slot_usage
from tests.test_accounting_attempts import manifest
from tests.test_inventory_service import PRODUCT, SPOOL, Provider, open_service

FIXTURES = Path(__file__).parent / "fixtures" / "orca"
FLAT = [{"0": 1.0}, {"0": 1.0}, {"0": 1.0}, {"0": 1.0}]  # 4 g over 4 layers


# --- reading the G-code ------------------------------------------------------


@pytest.mark.parametrize("name", ["single1", "multi3", "sparse3"])
def test_real_orca_files_add_up_to_the_slicer_figure(name):
    with zipfile.ZipFile(FIXTURES / f"{name}.gcode.3mf") as bundle:
        lines = bundle.read("Metadata/plate_1.gcode").decode().splitlines()
    plan, header = layer_plan(lines)
    for tool, grams in enumerate(header["filament used [g]"]):
        counted = sum(row.get(str(tool), 0) for row in plan)
        assert counted == pytest.approx(grams, abs=0.01)


def test_each_filament_is_counted_on_its_own_layers():
    gcode = """
; filament used [g] = 2.0, 6.0
M83
G1 E5 ; purge before the first layer counts for layer 1
M73 L1
T0
G1 E5
G1 E-1
G1 E1
M73 L2
M620 S1A
G1 E-2 ; pulled back before the change: stays on filament 0
T1
G1 E10 ; flush belongs to the new filament
T255
T1000
G1 E20
"""
    counts, header = count_extrusion(gcode.splitlines())
    assert counts == {1: {0: 10}, 2: {0: -2, 1: 30}}
    plan, _ = layer_plan(gcode.splitlines())
    assert plan == [{"0": 2.5}, {"0": -0.5, "1": 6.0}]


def test_a_filament_without_a_weight_is_named_not_dropped():
    gcode = "; filament used [g] = 2.0\nM83\nM73 L1\nT0\nG1 E5\nT1\nG1 E5\n"
    plan, header = layer_plan(gcode.splitlines())
    assert plan == [{"0": 2.0}]
    assert header["unweighed_filaments"] == [1.0]
    plan, header = layer_plan(["M83", "M73 L1", "G1 E5"])  # no slicer header at all
    assert plan == [{}] and header["unweighed_filaments"] == [0.0]


def test_absolute_extrusion_and_coordinate_resets():
    counts, _ = count_extrusion(
        ["M82", "G92 E0", "M73 L1", "G1 E5", "G1 E4", "G1 E6", "G92 E0", "G1 E2"]
    )
    assert counts == {1: {0: 8}}


# --- counting a print --------------------------------------------------------


def test_completed_stopped_and_refilled_prints():
    m = {"layer_grams": FLAT, "tool_wires": {"0": 0}}
    assert slot_usage(m, None) == {0: 4.0}
    assert slot_usage(m, 2) == {0: 1.5}  # layer 1, then half of the layer it stopped on
    assert slot_usage(m, None, [(0, 2, 2)]) == {0: 1.5, 2: 2.5}
    assert slot_usage(m, 3, [(0, 2, 2)]) == {0: 1.5, 2: 1.0}
    assert slot_usage(m, 0) == {}


def test_multicolour_prints_charge_each_slot_its_own_filament():
    m = {"layer_grams": [{"0": 1, "1": 2}, {"0": 1, "1": 2}], "tool_wires": {"0": 0, "1": 254}}
    assert slot_usage(m, None) == {0: 2.0, 254: 4.0}
    assert slot_usage(m, None, [(0, 3, 2)]) == {0: 1.5, 3: 0.5, 254: 4.0}


# --- debiting ------------------------------------------------------------------


class Spools(Provider):
    def __init__(self) -> None:
        super().__init__()
        self.spools = {
            43: {**SPOOL, "used_weight": 0.0},
            44: {**SPOOL, "id": 44, "remaining_weight": 900, "used_weight": 0.0},
        }
        self.uses: list[tuple[int, float]] = []
        self.broken = False

    async def __call__(self, request):
        path = request.url.path
        if "/spool/" in path:
            spool_id = int(path.split("/spool/")[1].split("/")[0])
            if path.endswith("/use"):
                if self.broken:
                    raise httpx.ConnectError("offline", request=request)
                used = json.loads(request.content)["use_weight"]
                self.uses.append((spool_id, used))
                self.spools[spool_id]["remaining_weight"] -= used
                self.spools[spool_id]["used_weight"] += used
            return httpx.Response(200, json=self.spools[spool_id])
        if request.method == "GET" and path.endswith("/spool"):
            first = int(request.url.params.get("offset", "0")) == 0
            return httpx.Response(200, json=list(self.spools.values()) if first else [])
        if "/filament/" in path:
            return httpx.Response(200, json=PRODUCT)
        return await super().__call__(request)


def printer(refills=()):
    return SimpleNamespace(
        auto_refill=SimpleNamespace(transitions=list(refills)),
        diagnostic_journal=SimpleNamespace(archived_refills=[]),
    )


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


async def setup(tmp_path, *, outcome="completed", layer=None, assigned=(), **fields):
    provider = Spools()
    service = await open_service(tmp_path / "inventory", provider)
    service.consumption_qualified = True
    snapshot = await service.assignments.snapshot()
    await service.assignments.replace(
        tuple(assigned), provider_id=snapshot.provider_id, expected_revision=snapshot.revision
    )
    inbox = NativeInbox(tmp_path / "inbox")
    attempt = inbox.claim_external("fixture", {"print": {"command": "project_file"}}, reserved=True)
    context = manifest()
    context.update({"layer_grams": FLAT, "tool_wires": {"0": 0}, "wire_spools": {"0": 43}} | fields)
    inbox.freeze_accounting(attempt, context)
    with inbox.connect() as db:
        db.execute(
            "UPDATE uploads SET start_state=?,layer_reached=? WHERE id=?",
            (outcome, layer, attempt),
        )
    return provider, service, inbox, attempt


async def deliver(service, inbox, device=None, started_at=0.0, failures=None):
    await consumption_delivery.deliver_once(
        service, inbox, lambda _id: device or printer(), started_at, failures
    )


async def test_a_completed_print_is_debited_once(tmp_path):
    provider, service, inbox, attempt = await setup(tmp_path)
    try:
        await deliver(service, inbox)
        await deliver(service, inbox)
        assert provider.uses == [(43, 4.0)]
        assert inbox.accounting_event(attempt)["delivery_state"] == "delivered"
    finally:
        await service.close()


async def test_a_stopped_print_is_debited_to_its_layer(tmp_path):
    provider, service, inbox, attempt = await setup(tmp_path, outcome="interrupted", layer=2)
    try:
        await deliver(service, inbox)
        assert provider.uses == [(43, 1.5)]
    finally:
        await service.close()


async def test_a_print_stopped_before_its_first_layer_costs_nothing(tmp_path):
    provider, service, inbox, attempt = await setup(tmp_path, outcome="interrupted")
    try:
        await deliver(service, inbox)
        assert provider.uses == []
        assert inbox.accounting_event(attempt)["delivery_state"] == "delivered"
    finally:
        await service.close()


async def test_an_auto_refill_moves_the_count_to_the_new_spool(tmp_path):
    provider, service, inbox, attempt = await setup(tmp_path, wire_spools={"0": 43, "2": 44})
    at = iso(float(inbox.accounting_event(attempt)["created_at"]))
    switch = {"from_slot": 1, "to_slot": 3, "status": "completed", "layer": 2, "observed_at": at}
    try:
        await deliver(service, inbox, printer([switch]))
        assert sorted(provider.uses) == [(43, 1.5), (44, 2.5)]
    finally:
        await service.close()


async def test_a_spool_assigned_during_the_print_is_charged(tmp_path):
    loaded = SpoolAssignment("fixture", "ams:fixture:tray:2", 44, wire_slot=2)
    provider, service, inbox, attempt = await setup(tmp_path, assigned=[loaded])
    at = iso(float(inbox.accounting_event(attempt)["created_at"]))
    switch = {"from_slot": 1, "to_slot": 3, "status": "completed", "layer": 2, "observed_at": at}
    try:
        await deliver(service, inbox, printer([switch]))
        assert sorted(provider.uses) == [(43, 1.5), (44, 2.5)]
    finally:
        await service.close()


async def test_multicolour_from_the_ams_and_the_external_holder(tmp_path):
    provider, service, inbox, attempt = await setup(
        tmp_path,
        layer_grams=[{"0": 1, "1": 2}, {"0": 1, "1": 2}],
        tool_wires={"0": 0, "1": 254},
        wire_spools={"0": 43, "254": 44},
    )
    try:
        await deliver(service, inbox)
        assert sorted(provider.uses) == [(43, 2.0), (44, 4.0)]
    finally:
        await service.close()


async def test_a_slot_without_a_spool_holds_the_print_after_charging_the_rest(tmp_path):
    provider, service, inbox, attempt = await setup(
        tmp_path,
        layer_grams=[{"0": 1, "1": 2}],
        tool_wires={"0": 0, "1": 3},
    )
    try:
        await deliver(service, inbox)
        assert provider.uses == [(43, 1.0)]
        # The owner is asked what was in slot 3 (usage_review); nothing is guessed.
        assert inbox.accounting_review(attempt) == "unassigned_slot"
        assert inbox.accounting_event(attempt)["delivery_state"] == "review"
    finally:
        await service.close()


async def test_archived_spools_are_skipped_and_empty_spools_stop_at_zero(tmp_path):
    provider, service, inbox, attempt = await setup(
        tmp_path,
        layer_grams=[{"0": 500.0, "1": 1.0}],
        tool_wires={"0": 0, "1": 1},
        wire_spools={"0": 43, "1": 44},
    )
    provider.spools[44]["archived"] = True
    try:
        await deliver(service, inbox)
        assert provider.uses == [(43, 436)]  # all that was left
    finally:
        await service.close()


async def test_a_print_frozen_without_a_layer_plan_is_held_for_review(tmp_path):
    # Frozen by a release before counting: the manifest has a total and a feed
    # table, and no per-layer grams. Nothing is guessed; the owner charges it.
    provider, service, inbox, attempt = await setup(tmp_path)
    legacy = {k: v for k, v in manifest().items() if k not in ("layer_grams", "tool_wires")}
    legacy.update(
        layer_feed_mm=[0, 1000, 2000],
        sliced_settings={"slicer_mass_g": [83.78, 0.0]},
        sources=[{"physical_source": "external", "spool_id": 43}],
    )
    with inbox.connect() as db:
        db.execute(
            "UPDATE accounting_shadow_events SET manifest=? WHERE attempt_id=?",
            (json.dumps(legacy), attempt),
        )
    try:
        await deliver(service, inbox)
        assert provider.uses == []
        assert inbox.accounting_review(attempt) == "manifest_predates_layer_plan"
        assert inbox.accounting_event(attempt)["delivery_state"] == "review"
    finally:
        await service.close()


async def test_a_file_that_extruded_nothing_costs_nothing_and_is_not_held(tmp_path):
    provider, service, inbox, attempt = await setup(tmp_path, layer_grams=[], tool_wires={})
    try:
        await deliver(service, inbox)
        assert provider.uses == []
        assert inbox.accounting_review(attempt) is None
        assert inbox.accounting_event(attempt)["delivery_state"] == "delivered"
    finally:
        await service.close()


async def test_an_old_format_print_stopped_at_layer_zero_costs_nothing(tmp_path):
    provider, service, inbox, attempt = await setup(tmp_path, outcome="interrupted")
    legacy = {k: v for k, v in manifest().items() if k not in ("layer_grams", "tool_wires")}
    with inbox.connect() as db:
        db.execute(
            "UPDATE accounting_shadow_events SET manifest=? WHERE attempt_id=?",
            (json.dumps(legacy), attempt),
        )
    try:
        await deliver(service, inbox)
        assert provider.uses == []
        assert inbox.accounting_review(attempt) == "stopped_before_first_layer"
        assert inbox.accounting_event(attempt)["delivery_state"] == "delivered"
    finally:
        await service.close()


async def test_prints_finished_before_this_process_started_stay_untouched(tmp_path):
    provider, service, inbox, attempt = await setup(tmp_path)
    try:
        await deliver(service, inbox, started_at=float("inf"))
        assert provider.uses == []
        assert inbox.accounting_event(attempt)["delivery_state"] == "shadow"
    finally:
        await service.close()


async def test_a_provider_outage_keeps_the_print_pending_without_blocking(tmp_path):
    provider, service, inbox, attempt = await setup(tmp_path)
    provider.broken = True
    failures: dict[str, float] = {}
    try:
        for _ in range(3):
            await deliver(service, inbox, failures=failures)
        # Within the retry budget: nothing noted, tried again next pass.
        assert inbox.accounting_event(attempt)["delivery_state"] == "shadow"
        assert attempt in failures
        provider.broken = False
        await deliver(service, inbox, failures=failures)
        assert provider.uses == [(43, 4.0)]
        assert inbox.accounting_event(attempt)["delivery_state"] == "delivered"
        assert attempt not in failures
    finally:
        await service.close()


async def test_live_usage_counts_the_print_running_now(tmp_path):
    from bambu_bridge.api.inventory import live_usage

    provider, service, inbox, attempt = await setup(tmp_path, outcome="running", layer=2)
    state = SimpleNamespace(
        native_gateway=SimpleNamespace(inbox=inbox),
        registry=SimpleNamespace(get=lambda _id: printer()),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    try:
        body = await live_usage("fixture", request, service)
        assert body["printing"] and body["layer"] == 2 and body["total_layers"] == 4
        assert body["slots"] == [
            {
                "wire_slot": 0,
                "spool_id": 43,
                "used_g": 1.5,
                "planned_g": 4.0,
                "written_g": 0.0,
                "spool_remaining_g": None,
                "state": None,
            }
        ]
        assert await live_usage("other", request, service) == {"printing": False}
    finally:
        await service.close()


async def test_a_filament_the_gcode_could_not_weigh_is_noted(tmp_path):
    provider, service, inbox, attempt = await setup(
        tmp_path, analysis_issues=["unweighed_filament_1"]
    )
    try:
        await deliver(service, inbox)
        assert provider.uses == [(43, 4.0)]
        # The file gave filament 1 no weight: the owner is asked for the grams.
        assert inbox.accounting_review(attempt) == "unweighed_filament"
        assert inbox.accounting_event(attempt)["delivery_state"] == "review"
        assert inbox.accounting_page()[0]["analysis_issues"] == ["unweighed_filament_1"]
    finally:
        await service.close()


async def test_a_filament_without_a_slot_mapping_is_noted(tmp_path):
    provider, service, inbox, attempt = await setup(tmp_path, tool_wires={})
    try:
        await deliver(service, inbox)
        assert provider.uses == []
        assert inbox.accounting_review(attempt) == "unmapped_filament"
        assert inbox.accounting_event(attempt)["delivery_state"] == "review"
    finally:
        await service.close()
