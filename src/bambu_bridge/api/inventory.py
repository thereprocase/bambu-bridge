"""Authenticated inventory APIs; provider credentials remain server-side."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable
from dataclasses import asdict
from typing import Annotated, Any, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import Field

from bambu_bridge.api.auth import require_auth
from bambu_bridge.inventory_journal import InventoryConflict
from bambu_bridge.inventory_sources import refresh_source_frame, source_choices
from bambu_bridge.material_mappings import catalogue, catalogue_digest
from bambu_bridge.refill import backups as refill_backups
from bambu_bridge.service import refill_adapter, usage_ledger
from bambu_bridge.service.consumption_delivery import refill_switches, slot_spools
from bambu_bridge.service.inventory import (
    ArchiveBody,
    ArchiveSpool,
    InventoryInput,
    InventoryService,
    NewProduct,
    NewSpool,
    NewVendor,
    SpoolWeight,
    VendorEmptyWeight,
    Weight,
)
from bambu_bridge.service.printer import PrinterService
from bambu_bridge.service.registry import PrinterNotFoundError
from bambu_bridge.service.shadow_accounting import slot_identities
from bambu_bridge.service.slot_colour import refill_alias_wires, wires_in_use
from bambu_bridge.slot_setting import slot_setting, write_slot
from bambu_bridge.spool_assignments import AssignmentConflict
from bambu_bridge.spoolman import ProviderWriteError, SpoolmanError
from bambu_bridge.usage_split import slot_usage

T = TypeVar("T")

router = APIRouter(prefix="/inventory", tags=["inventory"], dependencies=[Depends(require_auth)])


def get_inventory(request: Request) -> InventoryService:
    service = getattr(request.app.state, "inventory", None)
    if not isinstance(service, InventoryService):
        raise HTTPException(503, "Spool inventory is not configured on this bridge")
    return service


async def _call(awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except (InventoryConflict, AssignmentConflict) as exc:
        raise HTTPException(409, str(exc)) from None
    except SpoolmanError as exc:
        code = 404 if exc.kind == "not_found" else 503
        raise HTTPException(code, f"Spoolman: {exc.kind}") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.get("")
async def capabilities(request: Request) -> dict[str, Any]:
    service = getattr(request.app.state, "inventory", None)
    if not isinstance(service, InventoryService):
        return {
            "configured": False,
            "api_version": 2,
            "accounting_mode": "disabled",
            "native_overlay_enabled": False,
        }
    status = await service.status()
    controller = _display_controller(request)
    status["native_overlay_enabled"] = bool(
        controller and controller.active and controller.overlay.enabled
    )
    settings = getattr(request.app.state, "settings", None)
    status["spoolman_web_url"] = getattr(settings, "bridge_spoolman_web_url", None) or None
    inbox = getattr(getattr(request.app.state, "native_gateway", None), "inbox", None)
    status["review_count"] = await asyncio.to_thread(inbox.review_count) if inbox else 0
    return status


async def captures_for(request: Request, attempt_ids: list[str]) -> dict[str, str]:
    """Library capture per inbox row, for thumbnails; empty when the library is off.

    The library keeps the link (its attempts table); the inbox never learns it.
    """
    lookup = getattr(getattr(request.app.state, "library", None), "captures_for", None)
    if lookup is None or not attempt_ids:
        return {}
    try:
        return dict(await asyncio.to_thread(lookup, list(attempt_ids)))
    except Exception:  # noqa: BLE001 — a thumbnail is never worth failing the page
        return {}


@router.get("/accounting")
async def accounting_shadow(
    request: Request,
    service: Annotated[InventoryService, Depends(get_inventory)],
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    delivery_state: str | None = Query(None, pattern=r"^(shadow|delivered|review)$"),
) -> dict[str, Any]:
    gateway = getattr(request.app.state, "native_gateway", None)
    inbox = gateway.inbox if gateway is not None else None
    attempts = (
        await asyncio.to_thread(inbox.accounting_page, limit, offset, delivery_state)
        if inbox
        else []
    )
    captures = await captures_for(request, [row["attempt_id"] for row in attempts])
    for row in attempts:
        row["capture_id"] = captures.get(row["attempt_id"])
    return {
        "mode": "automatic" if service.consumption_qualified else "shadow",
        "automatic_consumption_enabled": service.consumption_qualified,
        "attempt_store_available": inbox is not None,
        "attempts": attempts,
    }


def _display_controller(request: Request) -> Any:
    gateway = getattr(request.app.state, "native_gateway", None)
    return getattr(gateway, "inventory_display", None)


@router.get("/display")
async def display_status(
    request: Request,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    provider = (await service.assignments.snapshot()).provider_id
    policy = await service.display_policy.snapshot(provider)
    controller = _display_controller(request)
    return {
        **policy,
        "runtime_qualified": bool(controller and controller.qualified),
        "effective_enabled": bool(controller and controller.active and controller.overlay.enabled),
        "reason": controller.reason if controller else "native_gateway_unavailable",
        "projected_slots": sorted(controller.overlay.bindings) if controller else [],
    }


class SetDisplayPolicy(InventoryInput):
    provider_id: UUID
    expected_revision: int = Field(ge=0, strict=True)
    global_enabled: bool = Field(strict=True)
    printers: list[str] = Field(max_length=64)


@router.put("/display")
async def update_display_policy(
    request: Request,
    body: SetDisplayPolicy,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    controller = _display_controller(request)
    policy = await _call(
        service.set_display_policy(
            provider_id=str(body.provider_id),
            revision=body.expected_revision,
            enabled=body.global_enabled,
            printers=body.printers,
            qualified=bool(controller and controller.qualified),
        )
    )
    gateway = getattr(request.app.state, "native_gateway", None)
    if controller is not None and gateway is not None:
        printer = (gateway.config or {}).get("printer_id")
        controller.policy(
            global_enabled=policy["global_enabled"],
            printer_enabled=printer in policy["printers"],
        )
    return policy


@router.get("/stock-materials")
async def stock_materials(
    material: str = Query("", max_length=40),
) -> dict[str, Any]:
    return {
        "upstream_tag": "v2.4.2",
        "catalogue_digest": catalogue_digest(),
        "runtime_qualified": False,
        "profiles": [
            asdict(p)
            for p in catalogue()
            if not material or p.material.upper() == material.strip().upper()
        ],
    }


@router.get("/material-mappings")
async def material_mappings(
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    provider = (await service.assignments.snapshot()).provider_id
    return await service.materials.snapshot(provider)


class SelectStockMaterial(InventoryInput):
    provider_id: UUID
    expected_revision: int = Field(ge=0, strict=True)
    stock_key: str | None = Field(..., min_length=1, max_length=250)
    catalogue_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


@router.put("/material-mappings/{product_id}")
async def select_stock_material(
    product_id: int,
    body: SelectStockMaterial,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    if product_id < 1:
        raise HTTPException(422, "product ID must be positive")
    if body.stock_key is not None and body.catalogue_digest != catalogue_digest():
        raise HTTPException(409, "Stock catalogue changed; reload the choices before selecting")
    return await _call(
        service.select_material(
            provider_id=str(body.provider_id),
            revision=body.expected_revision,
            product_id=product_id,
            stock_key=body.stock_key,
        )
    )


@router.get("/health")
async def provider_health(
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    return await _call(service.provider.health())


@router.get("/spools")
async def search_spools(
    service: Annotated[InventoryService, Depends(get_inventory)],
    query: str = "",
    material: str = "",
    include_archived: bool = False,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> Any:
    """Spools most recently assigned first, then by number; archived last.

    Spoolman pages by number, so every page is read before sorting; a hobby
    inventory is a few dozen spools.
    """
    records: list[Any] = []
    while len(records) < 1000:
        page = await _call(
            service.provider.search_spools(
                query,
                offset=len(records),
                limit=100,
                include_archived=include_archived,
                material=material,
            )
        )
        records.extend(page)
        if len(page) < 100:
            break
    newest: dict[int, float] = {}
    for spool_id, at in (await service.assignments.last_spools()).values():
        newest[spool_id] = max(newest.get(spool_id, 0.0), at)
    records.sort(key=lambda s: (s.archived, -newest.get(s.id, 0.0), s.id))
    return [await service.describe_spool(spool) for spool in records[offset : offset + limit]]


@router.get("/spools/{spool_id}")
async def get_spool(
    spool_id: int,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> Any:
    spool = await _call(service.provider.get_spool(spool_id))
    return await service.describe_spool(spool)


@router.get("/products")
async def search_products(
    service: Annotated[InventoryService, Depends(get_inventory)],
    query: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> Any:
    return await _call(service.provider.search_products(query, offset=offset, limit=limit))


@router.get("/catalog")
async def search_catalog(
    service: Annotated[InventoryService, Depends(get_inventory)],
    query: str = "",
    limit: int = Query(30, ge=1, le=100),
) -> Any:
    """Spoolman's built-in filament catalog, for a product Spoolman doesn't have yet.

    Word match over manufacturer, name and material (marks like ™ ignored),
    1.75 mm by default. Picking one in the app prefills the new-product form.
    """
    entries = await _call(service.provider.catalog(query, limit=limit))
    return [entry.model_dump(mode="json") for entry in entries]


@router.get("/vendors")
async def search_vendors(
    service: Annotated[InventoryService, Depends(get_inventory)],
    query: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> Any:
    return await _call(service.provider.search_vendors(query, offset=offset, limit=limit))


def _record_error(exc: ProviderWriteError) -> HTTPException:
    # The app shows these words as they are.
    if exc.outcome == "rejected":
        return HTTPException(422, "Spoolman rejected the change; check the values")
    return HTTPException(503, "Spoolman didn't answer")


async def _record(service: InventoryService, kind: str, body: InventoryInput) -> dict[str, Any]:
    """Forward one record change to Spoolman and return its answer."""
    try:
        return await service.record(kind, body)
    except ProviderWriteError as exc:
        raise _record_error(exc) from None
    except InventoryConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except SpoolmanError as exc:  # a read before the write failed
        if exc.kind == "not_found":
            raise HTTPException(404, "Spoolman has no such record") from None
        raise HTTPException(503, "Spoolman didn't answer") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/vendors", status_code=201)
async def create_vendor(
    body: NewVendor, service: Annotated[InventoryService, Depends(get_inventory)]
) -> dict[str, Any]:
    return await _record(service, "create_vendor", body)


@router.post("/products", status_code=201)
async def create_product(
    body: NewProduct, service: Annotated[InventoryService, Depends(get_inventory)]
) -> dict[str, Any]:
    return await _record(service, "create_product", body)


@router.post("/spools", status_code=201)
async def create_spool(
    body: NewSpool, service: Annotated[InventoryService, Depends(get_inventory)]
) -> dict[str, Any]:
    return await _record(service, "create_spool", body)


@router.post("/spools/{spool_id}/weigh")
async def weigh_spool(
    spool_id: Annotated[int, Path(gt=0)],
    body: Weight,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    weight = SpoolWeight(spool_id=spool_id, **body.model_dump(exclude_none=True))
    return await _record(service, "weigh", weight)


@router.post("/spools/{spool_id}/archive")
async def archive_spool(
    spool_id: Annotated[int, Path(gt=0)],
    service: Annotated[InventoryService, Depends(get_inventory)],
    body: ArchiveBody | None = None,
) -> dict[str, Any]:
    """Archive a spool; an optional measured bare-spool weight is stored on the spool.

    When it differs from the vendor's empty_spool_weight the answer carries a
    `vendor_offer`, which the owner may accept through the vendor route below.
    """
    weight = body.empty_spool_weight if body else None
    return await _record(
        service, "archive", ArchiveSpool(spool_id=spool_id, empty_spool_weight=weight)
    )


@router.post("/vendors/{vendor_id}/empty_spool_weight")
async def set_vendor_empty_weight(
    vendor_id: Annotated[int, Path(gt=0)],
    body: ArchiveBody,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    if body.empty_spool_weight is None:
        raise HTTPException(422, "empty_spool_weight is required")
    return await _record(
        service,
        "set_vendor_empty_weight",
        VendorEmptyWeight(vendor_id=vendor_id, empty_spool_weight=body.empty_spool_weight),
    )


@router.get("/printers/{printer_id}/refill")
async def refill_status(
    printer_id: str, request: Request, service: Annotated[InventoryService, Depends(get_inventory)]
) -> dict[str, Any]:
    try:
        printer = request.app.state.registry.get(printer_id)
    except PrinterNotFoundError:
        raise HTTPException(404, "Printer not found") from None
    return await refill_status_body(printer_id, printer, service)


async def refill_status_body(
    printer_id: str, printer: PrinterService, service: InventoryService
) -> dict[str, Any]:
    if service.refill is None:
        raise HTTPException(503, "Refill ownership store is unavailable")
    report = printer.refill_reports.snapshot(connected=printer.connected)
    status = await asyncio.to_thread(service.refill.status, printer_id, report)
    age = (
        time.monotonic() - report.eligibility_reported_at
        if report.eligibility_reported_at is not None
        else None
    )
    fresh = report.connected and age is not None and 0 <= age <= 30
    return {
        **status,
        "provider_id": (await service.assignments.snapshot()).provider_id,
        "printer_auto_refill": report.auto_refill if fresh else None,
        "last_reported_auto_refill": report.auto_refill,
        "printer_connected": report.connected,
        "backups": refill_backups(status, report),
        "eligibility_fresh": fresh,
        "eligibility_age_seconds": age,
        "qualification_issue": "target_firmware_not_qualified"
        if not service.refill.qualified
        else None,
        # Executing writes needs both the ownership controller and the physical
        # adapter qualified; saving intent needs only the controller.
        "execution_qualified": service.refill.qualified and refill_adapter.QUALIFIED,
        "slots": [
            {
                "wire_slot": wire,
                "declaration": asdict(slot.declaration),
                "source_generation": slot.source_generation,
                "editable": slot.editable,
                "tagged": slot.tagged,
                "available": report.connected and 0 <= time.monotonic() - slot.reported_at <= 30,
                "age_seconds": max(0, time.monotonic() - slot.reported_at),
            }
            for wire, slot in sorted(report.slots.items())
        ],
    }


@router.get("/printers/{printer_id}/sources")
async def sources(
    printer_id: str, request: Request, service: Annotated[InventoryService, Depends(get_inventory)]
) -> list[dict[str, Any]]:
    try:
        printer = request.app.state.registry.get(printer_id)
    except PrinterNotFoundError:
        raise HTTPException(404, "Printer not found") from None
    await refresh_source_frame(printer)
    last = await service.assignments.last_spools()
    # The last known slots stay listed while no fresh frame has arrived.
    rows = source_choices(printer, stale_ok=True)
    for row in rows:
        # The spool that last sat behind this identity in this slot. Two untagged
        # trays with the same material share an identity, so the slot tells them
        # apart; an RFID spool keeps its identity wherever it sits, so it is
        # offered in any slot.
        identity = row["observed_identity"]
        found = last.get((identity, row["source"]))
        if found is None and row.get("tagged"):
            elsewhere = [v for (i, _), v in last.items() if i == identity]
            found = max(elsewhere, key=lambda v: v[1]) if elsewhere else None
        row["last_spool_id"] = found[0] if found else None
    return rows


class AssignSpool(InventoryInput):
    provider_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=0, strict=True)
    spool_id: int | None = Field(default=None, gt=0, strict=True)
    source: str = Field(min_length=1, max_length=150)
    observed_identity: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # Set the slot on the printer to this spool's material first, when it differs.
    set_slot: bool = False


async def _refused(
    service: InventoryService, printer_id: str, body: AssignSpool, label: str, message: str
) -> None:
    """Record a refusal the bridge made before assign() ran (stale AMS, slot rewrite)."""
    await service.record_event(
        printer_id,
        "assignment_refused",
        {"spool_id": body.spool_id, "slot_label": label, "source": body.source, "text": message},
        "warn",
    )


async def _set_slot_for(
    service: InventoryService, printer: Any, body: AssignSpool, request: Request
) -> dict[str, Any] | None:
    """Make an untagged slot report the spool's material before it is assigned.

    The Activity feed row describing the rewrite when the slot was rewritten,
    else None.
    """
    assert body.spool_id is not None
    choice = next((c for c in source_choices(printer) if c["source"] == body.source), None)
    if choice is None or "source_empty" in choice["issues"]:
        return None  # assign() explains a missing or empty slot
    spool = await service.provider.get_spool(body.spool_id)
    if spool.archived:
        raise InventoryConflict(f"#{body.spool_id} is archived.")
    material = (spool.filament.material or "").strip().upper()
    if material == choice["material"].strip().upper():
        return None
    if choice["tagged"]:
        raise InventoryConflict(
            f"{choice['label']} holds an RFID spool that reads {choice['material']}; the tag "
            "sets its material. Put a different spool there, or pick a "
            f"{choice['material']} spool."
        )
    if choice["wire_slot"] in refill_alias_wires(service, printer.serial):
        raise InventoryConflict(
            f"{choice['label']} is part of a refill backup, which sets its material. Remove "
            "the backup first (it puts both slots back), then assign this spool."
        )
    inbox = getattr(getattr(request.app.state, "native_gateway", None), "inbox", None)
    if str(choice["wire_slot"]) in await wires_in_use(printer, inbox):
        raise InventoryConflict(
            f"{choice['label']} is part of the current print; change it after the print."
        )
    setting = slot_setting(material, spool.filament.color_hex)

    def reported() -> bool:
        now = next((c for c in source_choices(printer) if c["source"] == body.source), None)
        return now is not None and now["material"].strip().upper() == setting["tray_type"]

    if not await write_slot(printer, choice["wire_slot"], setting, reported):
        raise InventoryConflict(
            f"The printer did not confirm {choice['label']} as {setting['tray_type']}; "
            "nothing was assigned. Check the printer, then try again."
        )
    return {
        "spool_id": body.spool_id,
        "slot_label": choice["label"],
        "wire_slot": choice["wire_slot"],
        "source": body.source,
        "material": setting["tray_type"],
        "previous_material": choice["material"].strip().upper(),
        "color": setting["tray_color"][:6],
    }


@router.get("/printers/{printer_id}/usage")
async def live_usage(
    printer_id: str,
    request: Request,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    """Filament used so far by the print running now, counted layer by layer.

    Display only: nothing is written to the provider until the print ends.
    """
    inbox = getattr(getattr(request.app.state, "native_gateway", None), "inbox", None)
    found = await asyncio.to_thread(inbox.printing_attempt, printer_id) if inbox else None
    if found is None:
        return {"printing": False}
    attempt, manifest = found
    layer = attempt.get("layer_reached") or 0
    switches: list[tuple[int, int, int]] = []
    slots: dict[str, dict[str, Any]] = {}
    with contextlib.suppress(PrinterNotFoundError):
        printer = request.app.state.registry.get(printer_id)
        begun = attempt.get("dispatched_at") or attempt.get("created") or 0
        switches = refill_switches(printer, float(begun), time.time())
        slots = slot_identities(printer)
    snapshot = await service.assignments.snapshot()
    spools = slot_spools(manifest, snapshot.assignments, printer_id, slots)
    usage = slot_usage(manifest, layer, switches) if layer else {}
    planned = slot_usage(manifest, None, switches)  # the whole plan, per slot
    plan = manifest.get("layer_grams") or []
    written = await asyncio.to_thread(usage_ledger.rows, inbox, attempt["id"])
    command = json.loads(attempt.get("command") or "{}").get("print") or {}
    return {
        "printing": True,
        "attempt_id": attempt["id"],
        "title": command.get("subtask_name") or "",
        "layer": layer or None,
        "total_layers": len(plan) or None,
        "planned_grams": manifest.get("planned_grams"),
        # Counted so far, the slot's whole plan, what Spoolman has been charged,
        # and what Spoolman last said was left on the spool.
        "slots": [
            {
                "wire_slot": wire,
                "spool_id": spools.get(wire),
                "used_g": grams,
                "planned_g": planned.get(wire, 0.0),
                "written_g": written.get(spools.get(wire), {}).get("written_g", 0.0),
                "spool_remaining_g": written.get(spools.get(wire), {}).get("remaining_g"),
                "state": written.get(spools.get(wire), {}).get("state"),
            }
            for wire, grams in sorted(usage.items())
        ],
    }


@router.put("/printers/{printer_id}/assignment")
async def assign_spool(
    printer_id: str,
    body: AssignSpool,
    request: Request,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    # Removing a saved assignment must still work after the printer was deleted.
    printer = None
    expected_revision = body.expected_revision
    if body.spool_id is not None:
        try:
            printer = request.app.state.registry.get(printer_id)
        except PrinterNotFoundError:
            raise HTTPException(404, "Printer not found") from None
        await refresh_source_frame(printer)
        listed = next(
            (c for c in source_choices(printer, stale_ok=True) if c["source"] == body.source), None
        )
        label = listed["label"] if listed is not None else body.source
        if listed is not None and listed["stale"]:
            message = (
                f"The printer hasn't reported its AMS for {int(listed['age_seconds'] or 0)} s; "
                "try again in a moment"
            )
            await _refused(service, printer_id, body, label, message)
            raise HTTPException(409, message)
        if body.set_slot:
            try:
                rewritten = await _call(_set_slot_for(service, printer, body, request))
            except HTTPException as exc:
                if exc.status_code == 409:
                    await _refused(service, printer_id, body, label, str(exc.detail))
                raise
            if rewritten:
                await service.record_event(printer_id, "slot_set", rewritten)
                # The rewrite changed the slot's chemistry, which ends its old
                # assignment (same_spool); assign against the inventory as it is now.
                expected_revision = (await service.assignments.snapshot()).revision

    def choices() -> list[dict[str, Any]]:
        return source_choices(printer) if printer is not None else []

    return await _call(
        service.assign(
            provider_id=str(body.provider_id) if body.provider_id else None,
            expected_revision=expected_revision,
            spool_id=body.spool_id,
            printer_id=printer_id,
            source=body.source,
            observed_identity=body.observed_identity,
            choices=choices,
        )
    )
