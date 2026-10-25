"""Inventory coordination. No method in this service sends printer commands."""

from __future__ import annotations

import asyncio
import dataclasses
import fcntl
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

from bambu_bridge.display_policy import DisplayPolicy
from bambu_bridge.inventory_journal import InventoryConflict, InventoryJournal
from bambu_bridge.inventory_sources import same_spool
from bambu_bridge.material_mappings import MaterialMappings
from bambu_bridge.refill import RefillController, RefillStore
from bambu_bridge.spool_assignments import (
    AssignmentConflict,
    SpoolAssignment,
    SpoolAssignmentStore,
)
from bambu_bridge.spoolman import (
    Product,
    ProviderSpool,
    SpoolmanClient,
    SpoolmanError,
)

if TYPE_CHECKING:
    from bambu_bridge.service.inventory_projection import InventoryProjection

log = structlog.get_logger(__name__)

# Writes one row to the printer's Activity feed (EventRepo.add).
EventSink = Callable[..., Awaitable[Any]]


class InventoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class NewVendor(InventoryInput):
    name: str = Field(min_length=1, max_length=64)
    empty_spool_weight: float | None = Field(default=None, ge=0, strict=True)


class NewProduct(InventoryInput):
    name: str = Field(min_length=1, max_length=64)
    vendor_id: int | None = Field(default=None, gt=0, strict=True)
    material: str = Field(min_length=1, max_length=64)
    color_hex: str = Field(pattern=r"^[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")
    density: float = Field(gt=0, strict=True)
    diameter: float = Field(default=1.75, gt=0, strict=True)
    weight: float = Field(gt=0, strict=True)
    spool_weight: float | None = Field(default=None, ge=0, strict=True)
    # Spoolman's catalog id when the product came from it (owner picks it in the app).
    external_id: str | None = Field(default=None, min_length=1, max_length=128)


class NewSpool(InventoryInput):
    filament_id: int = Field(gt=0, strict=True)
    # Package capacity is in Product.weight; initial_weight may represent a partial spool.
    initial_weight: float | None = Field(default=None, ge=0, strict=True)
    remaining_weight: float | None = Field(default=None, ge=0, strict=True)
    spool_weight: float | None = Field(default=None, ge=0, strict=True)
    comment: str | None = Field(default=None, max_length=900)


class Weight(InventoryInput):
    remaining_weight: float | None = Field(default=None, ge=0, strict=True)
    gross_weight: float | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def one_weight(self) -> Weight:
        if (self.remaining_weight is None) == (self.gross_weight is None):
            raise ValueError("enter either remaining filament weight or gross spool weight")
        return self


class SpoolWeight(Weight):
    spool_id: int = Field(gt=0, strict=True)


class ArchiveSpool(InventoryInput):
    spool_id: int = Field(gt=0, strict=True)
    # Weighed once the spool is empty. Stored as the spool's own tare; the vendor's
    # default is only offered (see record) because it changes every spool of that vendor.
    empty_spool_weight: float | None = Field(default=None, gt=0, strict=True)


class ArchiveBody(InventoryInput):
    empty_spool_weight: float | None = Field(default=None, gt=0, strict=True)


class VendorEmptyWeight(InventoryInput):
    vendor_id: int = Field(gt=0, strict=True)
    empty_spool_weight: float = Field(gt=0, strict=True)


def slot_label(assignment: SpoolAssignment) -> str:
    """'AMS A · slot 2' or 'the external holder', from the assignment's wire slot."""
    wire = assignment.wire_slot
    if assignment.source == "external" or wire is None or wire == 254:
        return "the external holder"
    return f"AMS {chr(ord('A') + wire // 4)} · slot {wire % 4 + 1}"


class InventoryService:
    def __init__(
        self,
        assignments: SpoolAssignmentStore,
        journal: InventoryJournal,
        provider: SpoolmanClient,
        http: httpx.AsyncClient,
        lock_file: Any,
    ) -> None:
        self.assignments = assignments
        self.journal = journal
        self.provider = provider
        self._http = http
        self._lock_file = lock_file
        self._writes = asyncio.Lock()
        # Set from BRIDGE_INVENTORY_AUTO_DEDUCT at startup (main.py).
        self.consumption_qualified = False
        # The printer Activity feed; set at startup (main.py). Spool changes and
        # print charges are written there so the owner can see them in the app.
        self.events: EventSink | None = None
        self.provider_last_ok_at: float | None = None
        self.refill: RefillController | None = None
        self.materials = MaterialMappings(journal)
        self.display_policy = DisplayPolicy(journal)

    @classmethod
    async def open(
        cls,
        directory: Path,
        url: str,
        *,
        bearer: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> InventoryService:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Spoolman URL must be an HTTP(S) server URL without credentials or query"
            )
        base = url.rstrip("/")
        if not base.endswith("/api/v1"):
            base += "/api/v1"
        base += "/"
        directory.mkdir(parents=True, exist_ok=True)
        lock_file = (directory / "writer.lock").open("a")
        assignments = journal = http = None
        refill_store: RefillStore | None = None
        try:
            # One coordinator owns reconciliation and remote writes for this inventory.
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = directory / "inventory.sqlite"
            assignments = await SpoolAssignmentStore.open(path)
            await assignments.bind_provider(hashlib.sha256(base.encode()).hexdigest())
            journal = await InventoryJournal.open(path)
            http = httpx.AsyncClient(
                base_url=base,
                timeout=8,
                follow_redirects=False,
                transport=transport,
                headers={"Authorization": f"Bearer {bearer}"} if bearer else {},
            )
            service = cls(assignments, journal, SpoolmanClient(http), http, lock_file)
            refill_store = RefillStore(directory / "refill")
            service.refill = RefillController(refill_store)
            return service
        except BaseException:
            if refill_store is not None:
                refill_store.close()
            if http is not None:
                await http.aclose()
            if journal is not None:
                await journal.close()
            if assignments is not None:
                await assignments.close()
            lock_file.close()
            raise

    async def close(self) -> None:
        if self.refill is not None:
            self.refill.store.close()
        await self._http.aclose()
        await self.journal.close()
        await self.assignments.close()
        self._lock_file.close()

    @property
    def writes(self) -> asyncio.Lock:
        """Held by every provider write, the usage ledger's included."""
        return self._writes

    async def record(self, kind: str, request: InventoryInput) -> dict[str, Any]:
        """Create, weigh or archive a Spoolman record and return Spoolman's answer.

        The bridge only translates: a gross weight becomes remaining filament
        through the tare, and an archive is refused while a slot still holds
        the spool. The write is sent once and never journaled; a lost reply is
        reported as such, so the operator checks Spoolman instead of the
        bridge guessing.
        """
        payload = request.model_dump(exclude_none=True, mode="json")
        async with self._writes:
            wire = await self._wire(kind, payload)
            offer = wire.pop("_vendor_offer", None)
            result = await self.provider.write(kind if kind != "weigh" else wire.pop("_kind"), wire)
            if offer:
                result["vendor_offer"] = offer
            return result

    async def _wire(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind == "create_vendor":
            return payload
        if kind == "create_product":
            return payload
        if kind == "create_spool":
            product = await self.provider.get_product(payload["filament_id"])
            if product.weight is None:
                raise ValueError("product package weight is required")
            return payload
        if kind == "set_vendor_empty_weight":
            await self.provider.get_vendor(payload["vendor_id"])  # a missing vendor is a 404
            return payload
        if kind not in {"weigh", "archive"}:
            raise ValueError("unsupported inventory operation")
        spool = await self.provider.get_spool(payload["spool_id"])
        if spool.archived:
            raise ValueError("this spool is archived")
        if kind == "archive":
            snapshot = await self.assignments.snapshot()
            holder = next((a for a in snapshot.assignments if a.spool_id == spool.id), None)
            if holder is not None:
                raise InventoryConflict(f"Remove #{spool.id} from {slot_label(holder)} first")
            wire: dict[str, Any] = {"spool_id": spool.id, "archived": True}
            measured = payload.get("empty_spool_weight")
            if measured is not None:
                # One PATCH, so the archive and the tare land together.
                wire["spool_weight"] = measured
                vendor = spool.filament.vendor
                if vendor is not None and vendor.empty_spool_weight != measured:
                    wire["_vendor_offer"] = {
                        "vendor_id": vendor.id,
                        "vendor": vendor.name,
                        "current_empty_spool_weight": vendor.empty_spool_weight,
                        "proposed_empty_spool_weight": measured,
                    }
            return wire
        gross = payload.get("gross_weight")
        if gross is not None:
            tare = spool.spool_weight
            if tare is None:
                tare = spool.filament.spool_weight
            if tare is None:
                raise ValueError("tare is unknown; enter remaining filament weight instead")
            if gross < tare:
                raise ValueError("gross weight is smaller than the empty spool weight")
            return {
                "spool_id": spool.id,
                "remaining_weight": gross - tare,
                "_kind": "set_remaining",
            }
        return {
            "spool_id": spool.id,
            "remaining_weight": payload["remaining_weight"],
            "_kind": "set_remaining",
        }

    async def status(self) -> dict[str, Any]:
        async with self._writes:
            snapshot = await self.assignments.snapshot()
            try:
                await self.provider.health()
                reachable = True
                self.provider_last_ok_at = time.time()
            except SpoolmanError:
                reachable = False
        return {
            "configured": True,
            "provider_reachable": reachable,
            "provider_last_ok_at": self.provider_last_ok_at,
            "api_version": 2,
            "provider_id": snapshot.provider_id,
            "revision": snapshot.revision,
            "assignments": [asdict(a) for a in snapshot.assignments],
            "accounting_mode": "automatic" if self.consumption_qualified else "disabled",
            "native_overlay_enabled": False,
        }

    async def record_event(
        self, printer_id: str, kind: str, payload: dict[str, Any], severity: str = "info"
    ) -> None:
        """Add a row to the printer's Activity feed. The action it describes has
        already happened, so a failed write is logged and never undoes it."""
        if self.events is None:
            return
        try:
            await self.events(
                printer_id=printer_id, event_type=kind, payload=payload, severity=severity
            )
        except Exception:  # noqa: BLE001 — the feed is a record, not part of the action
            log.exception("inventory.event_write_failed", printer=printer_id, kind=kind)

    async def assign(
        self,
        *,
        spool_id: int | None,
        printer_id: str,
        source: str,
        observed_identity: str | None,
        choices: Callable[[], list[dict[str, Any]]],
        provider_id: str | None = None,
        expected_revision: int | None = None,
        origin: str = "owner",
    ) -> dict[str, Any]:
        """Put a spool in a slot, or take it out. The revision is optional: an app
        that sends none assigns against the inventory as it is now.

        The owner's changes and refusals go to the Activity feed; a tag seating a
        spool writes its own spool_adopted row.
        """
        seen: dict[str, Any] = {}
        try:
            result = await self._assign(
                spool_id=spool_id,
                printer_id=printer_id,
                source=source,
                choices=choices,
                provider_id=provider_id,
                expected_revision=expected_revision,
                origin=origin,
                seen=seen,
            )
        except InventoryConflict as exc:
            # A revision race (AssignmentConflict) is the app's to retry and is not
            # recorded; these are the reasons the owner has to act on.
            if spool_id is not None and origin == "owner":
                await self.record_event(
                    printer_id,
                    "assignment_refused",
                    {
                        "spool_id": spool_id,
                        "slot_label": seen.get("label") or source,
                        "source": source,
                        "text": str(exc),
                    },
                    "warn",
                )
            raise
        if origin != "owner":
            return result
        label = seen.get("label") or source
        if spool_id is not None:
            spool = seen["spool"]
            await self.record_event(
                printer_id,
                "spool_assigned",
                {
                    "spool_id": spool_id,
                    "name": spool.filament.name or "",
                    "material": (spool.filament.material or "").strip().upper(),
                    "slot_label": label,
                    "wire_slot": seen.get("wire_slot"),
                    "source": source,
                },
            )
        elif seen.get("previous") is not None:
            previous: SpoolAssignment = seen["previous"]
            await self.record_event(
                printer_id,
                "spool_unassigned",
                {
                    "spool_id": previous.spool_id,
                    "slot_label": slot_label(previous),
                    "wire_slot": previous.wire_slot,
                    "source": source,
                },
            )
        return result

    async def _assign(
        self,
        *,
        spool_id: int | None,
        printer_id: str,
        source: str,
        choices: Callable[[], list[dict[str, Any]]],
        provider_id: str | None,
        expected_revision: int | None,
        origin: str,
        seen: dict[str, Any],
    ) -> dict[str, Any]:
        """assign() without the feed; `seen` collects what the feed row names."""
        async with self._writes:
            snapshot = await self.assignments.snapshot()
            if provider_id is None:
                provider_id = snapshot.provider_id
            if expected_revision is None:
                expected_revision = snapshot.revision
            if snapshot.provider_id != provider_id or snapshot.revision != expected_revision:
                raise AssignmentConflict("inventory changed; refresh and try again")
            retained = [
                a for a in snapshot.assignments if (a.printer_id, a.source) != (printer_id, source)
            ]
            seen["previous"] = next(
                (
                    a
                    for a in snapshot.assignments
                    if (a.printer_id, a.source) == (printer_id, source)
                ),
                None,
            )
            if spool_id is not None:
                spool = await self.provider.get_spool(spool_id)
                seen["spool"] = spool
                # The bridge reads the slot itself at commit time; what the phone
                # saw when the list loaded is not compared (observed_identity is
                # accepted from older apps and ignored).
                choice = next((c for c in choices() if c["source"] == source), None)
                if choice is not None:
                    seen["label"], seen["wire_slot"] = choice["label"], choice["wire_slot"]
                if choice is None:
                    raise InventoryConflict(
                        "The printer is not reporting this slot right now. Check that it is "
                        "connected, then try again."
                    )
                if "source_empty" in choice["issues"]:
                    raise InventoryConflict(
                        f"The AMS reports {choice['label']} as empty. Load the spool, let the "
                        "AMS read it, then try again."
                    )
                if not str(choice["material"] or "").strip():
                    raise InventoryConflict(
                        f"The AMS has not declared a material for {choice['label']}. Set the "
                        "slot on the printer or let the AMS read the spool, then try again."
                    )
                if not choice["assignable"]:
                    raise InventoryConflict(
                        f"{choice['label']} cannot be assigned right now: "
                        + ", ".join(i.replace("_", " ") for i in choice["issues"])
                    )
                if spool.archived:
                    raise InventoryConflict(f"#{spool_id} is archived.")
                material = (spool.filament.material or "").strip().upper()
                if material != str(choice["material"] or "").strip().upper():
                    raise InventoryConflict(
                        f"{choice['label']} reports {choice['material'] or 'no material'}; "
                        f"#{spool_id} is {material or 'unknown material'}. Pick a "
                        f"{choice['material']} spool or change the slot on the printer."
                    )
                # Explicit assignment is an atomic move, not a second copy of the spool.
                retained = [a for a in retained if a.spool_id != spool_id]
                # Strictly later than whatever the slot held before, so a seat or
                # an unload is never matched to the wrong assignment by its time.
                # A stamp far ahead (the clock stepped back) is not inherited:
                # unloads would be ignored until the clock caught up.
                now = time.time()
                before = [
                    a.assigned_at
                    for a in snapshot.assignments
                    if (a.printer_id, a.source) == (printer_id, source) and a.assigned_at - now <= 5
                ]
                assigned_at = max([now, *(at + 0.001 for at in before)])
                retained.append(
                    SpoolAssignment(
                        printer_id,
                        source,
                        spool_id,
                        assigned_at=assigned_at,
                        observed_identity=choice["observed_identity"],
                        wire_slot=choice["wire_slot"],
                        origin=origin,
                        spool_key=choice["spool_key"],
                        tray_type=str(choice["material"] or "").strip().upper(),
                    )
                )
            updated = await self.assignments.replace(
                tuple(retained),
                provider_id=provider_id,
                expected_revision=expected_revision,
            )
            return asdict(updated)

    async def release_unseated(
        self, printer_id: str, choices: list[dict[str, Any]], unloaded: dict[int, float]
    ) -> list[SpoolAssignment]:
        """Drop assignments whose spool the AMS says left the slot.

        Only physical evidence ends one: an unload reported after it was made,
        an empty slot, another RFID tag, or another chemistry (same_spool).
        Product, temperatures and colour never do, so a refill alias, a colour
        sync or a relabel on the printer screen keeps the slot's spool. Assignments
        stored before spool_key are stamped from the first slot report that
        still matches them.
        """
        async with self._writes:
            snapshot = await self.assignments.snapshot()
            by_source = {choice["source"]: choice for choice in choices}
            kept: list[SpoolAssignment] = []
            dropped: list[SpoolAssignment] = []
            stamped = False
            for assignment in snapshot.assignments:
                choice = by_source.get(assignment.source)
                if assignment.printer_id != printer_id:
                    kept.append(assignment)
                    continue
                # Only an unload after the assignment was made ends it; a
                # reassignment that raced a pending unload stays. assign() read
                # the slot as loaded, so an unload stamped at the same instant
                # came before it.
                unloaded_at = (
                    unloaded.get(assignment.wire_slot) if assignment.wire_slot is not None else None
                )
                same = same_spool(assignment, choice) if choice is not None else None
                if (
                    (unloaded_at is not None and unloaded_at > assignment.assigned_at)
                    or (choice is not None and "source_empty" in choice["issues"])
                    or same is False
                ):
                    dropped.append(assignment)
                    continue
                if same and assignment.spool_key is None:
                    assert choice is not None
                    assignment = dataclasses.replace(
                        assignment,
                        spool_key=choice["spool_key"],
                        tray_type=str(choice["material"] or "").strip().upper(),
                    )
                    stamped = True
                kept.append(assignment)
            if dropped or stamped:
                await self.assignments.replace(
                    tuple(kept),
                    provider_id=snapshot.provider_id,
                    expected_revision=snapshot.revision,
                )
            return dropped

    async def select_material(
        self, *, provider_id: str, revision: int, product_id: int, stock_key: str | None
    ) -> dict[str, Any]:
        async with self._writes:
            if (await self.assignments.snapshot()).provider_id != provider_id:
                raise InventoryConflict(
                    "inventory provider changed; refresh before selecting a material"
                )
            if stock_key is not None:
                product = await self.provider.get_product(product_id)
            else:
                product = Product(id=product_id)  # Clearing is local and remains possible offline.
            return await self.materials.select(provider_id, product, stock_key, revision=revision)

    async def native_projection(self, printer: Any) -> InventoryProjection:
        """Read candidate bindings only; never enable the native display overlay."""
        from bambu_bridge.service.inventory_projection import build_projection

        async with self._writes:
            return await build_projection(self, printer)

    async def set_display_policy(
        self,
        *,
        provider_id: str,
        revision: int,
        enabled: bool,
        printers: list[str],
        qualified: bool,
    ) -> dict[str, Any]:
        async with self._writes:
            if (await self.assignments.snapshot()).provider_id != provider_id:
                raise InventoryConflict("inventory provider changed; reload display preferences")
            if (enabled or printers) and not qualified:
                raise InventoryConflict("native inventory display is not runtime qualified")
            return await self.display_policy.replace(provider_id, revision, enabled, printers)

    async def describe_spool(self, spool: ProviderSpool) -> dict[str, Any]:
        provider_id = (await self.assignments.snapshot()).provider_id
        return {
            **spool.model_dump(mode="json"),
            "remaining_percent": spool.remaining_percent,
            "orca_material": await self.materials.lookup(provider_id, spool.filament),
            "over_capacity": bool(
                spool.remaining_weight is not None
                and spool.filament.weight is not None
                and spool.remaining_weight > spool.filament.weight
            ),
        }
