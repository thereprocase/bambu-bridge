"""Refill: one call sets a backup slot, one removes it, both only while the printer idles.

The older preview, plan, execute and review routes stay for installed apps.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import Field

from bambu_bridge.api.auth import require_auth
from bambu_bridge.api.inventory import get_inventory, refill_status_body
from bambu_bridge.inventory_sources import refresh_source_frame, source_choices
from bambu_bridge.refill import RefillConflict, RefillController, RefillReport
from bambu_bridge.service.inventory import InventoryInput, InventoryService
from bambu_bridge.service.printer import PrinterService
from bambu_bridge.service.refill_adapter import PhysicalRefillAdapter
from bambu_bridge.service.registry import PrinterNotFoundError

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/inventory", tags=["inventory"], dependencies=[Depends(require_auth)])


class RefillPreview(InventoryInput):
    wires: list[Annotated[int, Field(ge=0, le=3, strict=True)]] = Field(min_length=2, max_length=4)
    color: str = Field(pattern=r"^[0-9a-fA-F]{8}$")


class RefillPlan(RefillPreview):
    provider_id: UUID
    expected_revision: int = Field(ge=0, strict=True)
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class RefillRevision(InventoryInput):
    provider_id: UUID
    expected_revision: int = Field(ge=0, strict=True)


def physical(
    request: Request, service: InventoryService, printer_id: str
) -> tuple[PrinterService, RefillController]:
    try:
        printer = request.app.state.registry.get(printer_id)
    except PrinterNotFoundError:
        raise HTTPException(404, "Printer not found") from None
    if service.refill is None:
        raise HTTPException(503, "Refill ownership store is unavailable")
    return printer, service.refill


async def seat_first(
    service: InventoryService, printer: PrinterService, printer_id: str, wires: list[int]
) -> None:
    """Stamp these slots' stored assignments before a refill relabels them.

    An assignment stored before spool_key is still judged by its declared
    identity, which includes the product a refill alias changes; the seating
    pass stamps it from a matching report, and this runs that pass now rather
    than racing it. Without a report to stamp from, nothing is planned.
    """

    def legacy(snapshot: Any) -> bool:
        return any(
            a.printer_id == printer_id and a.wire_slot in wires and a.spool_key is None
            for a in snapshot.assignments
        )

    if not legacy(await service.assignments.snapshot()):
        return
    await refresh_source_frame(printer)  # a quiet idle printer is asked for one
    await service.release_unseated(printer_id, source_choices(printer), {})
    if legacy(await service.assignments.snapshot()):
        raise HTTPException(
            409, "The printer hasn't reported its AMS recently; try again in a moment"
        )


def plan_digest(printer_id: str, plan: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps({"printer_id": printer_id, "plan": plan}, sort_keys=True).encode()
    ).hexdigest()


def reviewed(
    controller: RefillController, printer: PrinterService, printer_id: str, body: RefillPreview
) -> dict[str, Any]:
    plan = controller.preview(
        printer.refill_reports.snapshot(connected=printer.connected), body.wires, body.color
    )
    return {
        "plan": plan,
        "preview_digest": plan_digest(printer_id, plan),
        "writes_qualified": controller.qualified,
    }


@router.post("/printers/{printer_id}/refill/preview")
async def preview_refill(
    printer_id: str,
    body: RefillPreview,
    request: Request,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    printer, controller = physical(request, service, printer_id)
    try:
        return reviewed(controller, printer, printer_id, body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.put("/printers/{printer_id}/refill")
async def plan_refill(
    printer_id: str,
    body: RefillPlan,
    request: Request,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    printer, controller = physical(request, service, printer_id)
    await seat_first(service, printer, printer_id, body.wires)
    async with service._writes:
        if (await service.assignments.snapshot()).provider_id != str(body.provider_id):
            raise HTTPException(409, "Inventory provider changed; reload before planning refill")
        if not controller.qualified:
            raise HTTPException(409, "Physical refill writes are not runtime qualified")
        try:
            preview = reviewed(controller, printer, printer_id, body)
            if preview["preview_digest"] != body.preview_digest:
                raise HTTPException(409, "Physical refill sources changed; review a new preview")
            # No await between fresh preview validation and durable intent. This
            # route records ownership only; it does not call a physical sender.
            return controller.plan(
                printer_id,
                printer.refill_reports.snapshot(connected=printer.connected),
                body.wires,
                body.color,
                revision=body.expected_revision,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None


@router.post("/printers/{printer_id}/refill/disable")
async def disable_refill(
    printer_id: str,
    body: RefillRevision,
    request: Request,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    # A disconnected or removed printer must not prevent recording restoration
    # intent. This never claims the actual printer's Auto Refill has stopped.
    controller = service.refill
    if controller is None:
        raise HTTPException(503, "Refill ownership store is unavailable")
    async with service._writes:
        if (await service.assignments.snapshot()).provider_id != str(body.provider_id):
            raise HTTPException(409, "Inventory provider changed; reload refill ownership")
        try:
            return controller.disable(printer_id, revision=body.expected_revision)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None


@router.post("/printers/{printer_id}/refill/execute")
async def execute_refill(
    printer_id: str,
    body: RefillRevision,
    request: Request,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    """Apply or restore the reviewed group: idle only, bounded readback, no retry.

    The operator triggers this explicitly; a lost response is resolved by
    reading status, never by repeating the request automatically.
    """
    printer, controller = physical(request, service, printer_id)
    adapter = PhysicalRefillAdapter(printer)
    if not (controller.qualified and adapter.qualified):
        raise HTTPException(409, "Physical refill writes are not runtime qualified")
    async with service._writes:
        if (await service.assignments.snapshot()).provider_id != str(body.provider_id):
            raise HTTPException(409, "Inventory provider changed; reload refill ownership")
    try:
        await controller.drive(
            printer_id, adapter.observe, adapter.send, revision=body.expected_revision
        )
    except RefillConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except (ValueError, TimeoutError, ConnectionError):
        pass  # The controller durably recorded the step as uncertain; status shows it.
    return await refill_status_body(printer_id, printer, service)


class RefillReview(RefillRevision):
    wire: int = Field(ge=0, le=3, strict=True)
    source_generation: str = Field(min_length=1, max_length=128)
    note: str = Field(min_length=1, max_length=1000)


async def review_refill(
    request: Request,
    service: InventoryService,
    printer_id: str,
    body: RefillReview,
    *,
    release: bool,
) -> dict[str, Any]:
    printer, controller = physical(request, service, printer_id)
    async with service._writes:
        if (await service.assignments.snapshot()).provider_id != str(body.provider_id):
            raise HTTPException(409, "Inventory provider changed; reload refill ownership")
        report = printer.refill_reports.snapshot(connected=printer.connected)
        slot = report.slots.get(body.wire)
        if slot is None or slot.source_generation != body.source_generation:
            raise HTTPException(409, "Physical source changed; review fresh readback")
        try:
            if release:
                return controller.release_external_change(
                    printer_id, body.wire, report, revision=body.expected_revision, note=body.note
                )
            # Adoption requires an actual fresh matching declaration. This API
            # cannot assert a write was never delivered based on an old value.
            return controller.reconcile(
                printer_id,
                body.wire,
                report,
                revision=body.expected_revision,
                applied=True,
                note=body.note,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None


@router.post("/printers/{printer_id}/refill/reconcile")
async def reconcile_refill(
    printer_id: str,
    body: RefillReview,
    request: Request,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    return await review_refill(request, service, printer_id, body, release=False)


class RefillRelease(RefillRevision):
    wire: int = Field(ge=0, le=3, strict=True)
    # The generation the owner reviewed; an empty slot has none.
    source_generation: str | None = Field(default=None, min_length=1, max_length=128)
    note: str = Field(min_length=1, max_length=1000)
    # The owner confirms the slot holds no spool, so there is nothing to keep or restore.
    slot_empty: bool = Field(default=False, strict=True)


@router.post("/printers/{printer_id}/refill/release")
async def release_refill(
    printer_id: str,
    body: RefillRelease,
    request: Request,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    """Keep a conflicted slot's own declaration, or clear the step of an emptied slot.

    An emptied slot has no source to review, so it needs `slot_empty: true`
    instead of a source generation; only a fresh report of a spool there
    refuses it.
    """
    if not body.slot_empty:
        if body.source_generation is None:
            raise HTTPException(422, "source_generation is required unless slot_empty is set")
        printer, _ = physical(request, service, printer_id)
        report = printer.refill_reports.snapshot(connected=printer.connected)
        if body.wire not in report.slots and report.empty is not None and body.wire in report.empty:
            raise HTTPException(
                409, f"Slot {body.wire + 1} is empty; confirm that to clear its backup step"
            )
        review = RefillReview(**body.model_dump(exclude={"slot_empty"}))
        return await review_refill(request, service, printer_id, review, release=True)
    printer, controller = physical(request, service, printer_id)
    async with service._writes:
        if (await service.assignments.snapshot()).provider_id != str(body.provider_id):
            raise HTTPException(409, "Inventory provider changed; reload refill ownership")
        report = printer.refill_reports.snapshot(connected=printer.connected)
        try:
            return controller.release_empty(
                printer_id, body.wire, report, revision=body.expected_revision, note=body.note
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None


class RefillBackup(InventoryInput):
    from_wire: int = Field(ge=0, le=3, strict=True)
    to_wire: int = Field(ge=0, le=3, strict=True)


class RefillBackupSet(RefillBackup):
    # From POST …/refill/backup/preview: refuse if the slots changed since.
    preview_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


IDLE_ONLY = "The printer accepts this change when it's idle"


def unfit(report: RefillReport, wires: list[int]) -> str | None:
    """Why these slots cannot back each other up, in the owner's words.

    Product, temperatures and colour may differ: the backup declares them. RFID
    and material rules are the controller's (RefillController.preview).
    """
    if wires[0] == wires[1]:
        return "Choose two different slots"
    for wire in wires:
        if report.slots.get(wire) is None:
            return f"Slot {wire + 1} is empty"
    return None


def backup_color(report: RefillReport, wires: list[int]) -> str:
    """The group's colour: the tag's when an RFID slot anchors it, else from_wire's."""
    anchor = next((w for w in wires if report.slots[w].tagged), wires[0])
    return report.slots[anchor].declaration.tray_color


def backup_plan(
    controller: RefillController, report: RefillReport, printer_id: str, wires: list[int]
) -> dict[str, Any]:
    """The group set_backup would store: to_wire declared as from_wire's filament,
    or, when one of them holds an RFID spool, the other declared as the tag's."""
    if reason := unfit(report, wires):
        raise HTTPException(422, reason)
    color = backup_color(report, wires)
    try:
        return controller.preview(report, wires, color)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


async def drive(printer_id: str, printer: PrinterService, controller: RefillController) -> None:
    """Apply or restore the stored group; an outcome it cannot confirm is stored, not raised."""
    adapter = PhysicalRefillAdapter(printer)
    group = controller.store.get(printer_id)
    assert group is not None
    with contextlib.suppress(ValueError, TimeoutError, ConnectionError):
        await controller.drive(
            printer_id, adapter.observe, adapter.send, revision=group["revision"]
        )


@router.post("/printers/{printer_id}/refill/backup/preview")
async def preview_backup(
    printer_id: str,
    body: RefillBackup,
    request: Request,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    """What setting this backup would change on each slot, without storing or sending."""
    printer, controller = physical(request, service, printer_id)
    report = printer.refill_reports.snapshot(connected=printer.connected)
    plan = backup_plan(controller, report, printer_id, [body.from_wire, body.to_wire])
    return {
        "steps": [
            {
                k: step[k]
                for k in ("wire", "anchor", "tagged", "writes", "changes", "before", "target")
            }
            for step in plan["steps"]
        ],
        # Disabling the backup puts every changed field back.
        "restored_on_disable": True,
        "preview_digest": plan_digest(printer_id, plan),
    }


@router.put("/printers/{printer_id}/refill/backup")
async def set_backup(
    printer_id: str,
    body: RefillBackupSet,
    request: Request,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    """When from_wire runs out, continue from to_wire: planned and applied in one call.

    to_wire is declared as from_wire's filament (product, temperatures and
    colour) so the printer groups them; when either holds an RFID spool, the
    other is declared as the tag's filament and the tag's slot is never
    written. Sent again for the same pair, it is the owner's Try again.
    """
    printer, controller = physical(request, service, printer_id)
    wires = [body.from_wire, body.to_wire]
    retried = False
    await seat_first(service, printer, printer_id, wires)
    # A quiet idle printer's last report ages out; Try again settles a slot
    # only from a fresh one. Asked outside the write lock: it awaits the printer.
    await refresh_source_frame(printer)
    async with service.writes:
        report = printer.refill_reports.snapshot(connected=printer.connected)
        if not report.idle:
            raise HTTPException(409, IDLE_ONLY)
        group = controller.store.get(printer_id)
        if group is not None and group["state"] != "disabled":
            if [s["wire"] for s in group["steps"]] != wires:
                first = group["steps"][0]["wire"] + 1
                raise HTTPException(409, f"Remove the slot {first} backup first")
            controller.retry(printer_id, report, revision=group["revision"])
            retried = True
        else:
            plan = backup_plan(controller, report, printer_id, wires)
            if body.preview_digest is not None and body.preview_digest != plan_digest(
                printer_id, plan
            ):
                raise HTTPException(409, "Physical refill sources changed; review a new preview")
            color = backup_color(report, wires)
            try:
                controller.plan(
                    printer_id, report, wires, color, revision=group["revision"] if group else 0
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
    await drive(printer_id, printer, controller)
    status = await refill_status_body(printer_id, printer, service)
    if not retried:
        # Try again on an existing backup is not a new change for the feed.
        await _record_backup(service, printer_id, "backup_set", wires, status)
    return status


async def _record_backup(
    service: InventoryService,
    printer_id: str,
    kind: str,
    wires: list[int],
    status: dict[str, Any],
) -> None:
    """One Activity row for a backup the owner set or removed, with where it stands
    and the bridge's reason when it is not settled (refill.backups() message)."""
    # The group state status() derives: needs_attention while a step is
    # uncertain, restoration_pending while a removal waits for the printer.
    state = status["state"]
    chain = next((b for b in status["backups"] if b["from_wire"] == wires[0]), None)
    await service.record_event(
        printer_id,
        kind,
        {
            "from_wire": wires[0],
            "to_wire": wires[-1],
            "state": state,
            "message": chain.get("message") if chain is not None else None,
        },
        "warn" if state == "needs_attention" else "info",
    )


@router.delete("/printers/{printer_id}/refill/backup/{from_wire}")
async def remove_backup(
    printer_id: str,
    request: Request,
    service: Annotated[InventoryService, Depends(get_inventory)],
    from_wire: Annotated[int, Path(ge=0, le=3)],
) -> dict[str, Any]:
    """Put both slots back as they were. A slot changed on the printer since keeps its change.

    Sent again while a restore needs attention, it is the owner's Try again.
    While the printer is busy or offline the request is recorded and the
    restoration worker puts the slots back once it is idle.
    """
    printer, controller = physical(request, service, printer_id)
    # As in set_backup: without a fresh report a restored slot stays uncertain.
    await refresh_source_frame(printer)
    # The slots once disable() has stored the removal. From then on the owner's
    # change stands, so it gets its Activity row even if a later step is refused;
    # the next call is a Try again (or a 404) and writes none.
    removed: list[int] | None = None
    try:
        async with service.writes:
            report = printer.refill_reports.snapshot(connected=printer.connected)
            group = controller.store.get(printer_id)
            if (
                group is None
                or group["state"] == "disabled"
                or group["steps"][0]["wire"] != from_wire
            ):
                raise HTTPException(404, f"Slot {from_wire + 1} has no backup")
            wires = [step["wire"] for step in group["steps"]]
            # idle comes from the last state report, so an offline printer can look idle.
            if not (report.idle and report.connected):
                if group["enabled"]:
                    try:
                        controller.disable(printer_id, revision=group["revision"])
                    except ValueError as exc:
                        raise HTTPException(409, str(exc)) from None
                    removed = wires
                return await refill_status_body(printer_id, printer, service)
            try:
                if group["enabled"]:
                    group = controller.disable(printer_id, revision=group["revision"])
                    removed = wires
                controller.retry(printer_id, report, revision=group["revision"])
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
        # Restore first: the drive settles emptied slots and restores a conflicted
        # one whose fresh same-source readback still shows the alias. Releasing
        # before it would keep that alias on the slot.
        await drive(printer_id, printer, controller)
        async with service.writes:
            report = printer.refill_reports.snapshot(connected=printer.connected)
            try:
                controller.release_unrestorable(
                    printer_id, report, note="Backup removed; kept the printer's own setting"
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
        return await refill_status_body(printer_id, printer, service)
    finally:
        if removed is not None:
            await _record_removal(service, printer_id, printer, removed)


async def _record_removal(
    service: InventoryService, printer_id: str, printer: PrinterService, wires: list[int]
) -> None:
    """The backup_removed row, from the state the app is shown now."""
    try:
        status = await refill_status_body(printer_id, printer, service)
    except Exception:  # noqa: BLE001 — never mask the request's own outcome
        log.exception("inventory.event_write_failed", printer=printer_id, kind="backup_removed")
        return
    await _record_backup(service, printer_id, "backup_removed", wires, status)
