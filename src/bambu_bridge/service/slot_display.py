"""Virtual slot status: which spool each AMS slot shows, and keeping it true.

Three pieces, one job. build_projection turns the verified assignments into
candidate native bindings for one printer (InventoryService holds its writer
lock throughout; no caller may use the candidates as qualification or bypass
NativeSpoolOverlay's physical gate). InventoryDisplay refreshes those bindings
into the overlay on a bounded loop, for downstream native status only. The
seating worker releases an assignment the AMS says no longer holds: a slot keeps
its spool through any relabel; an unload, another RFID tag or another chemistry
ends it (inventory_sources.same_spool), and choosing a different spool for the
same material is a manual reassignment in the app.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from bambu_bridge.inventory_journal import InventoryConflict
from bambu_bridge.inventory_sources import same_spool, source_choices
from bambu_bridge.native_spools import NativeSpoolOverlay, VirtualSpool, physical_identity
from bambu_bridge.service.slot_colour import ColourSync
from bambu_bridge.service.tagged_spools import EventSink, TagAdopter

if TYPE_CHECKING:
    from bambu_bridge.service.inventory import InventoryService

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class InventoryProjection:
    provider_id: str
    assignment_revision: int
    bindings: tuple[VirtualSpool, ...]
    issues: tuple[tuple[str, str], ...]


async def build_projection(inventory: InventoryService, printer: Any) -> InventoryProjection:
    snapshot = await inventory.assignments.snapshot()
    choices = source_choices(printer)
    frame = printer.material_inventory.snapshot()
    bindings: list[VirtualSpool] = []
    issues: list[tuple[str, str]] = []
    for assignment in snapshot.assignments:
        if assignment.printer_id != printer.serial:
            continue
        source = assignment.source
        choice = next((c for c in choices if c["source"] == source), None)
        if (
            choice is None
            or not choice["assignable"]
            or same_spool(assignment, choice) is not True
            or choice["wire_slot"] != assignment.wire_slot
        ):
            issues.append((source, "source_confirmation_required"))
            continue
        wire = choice["wire_slot"]
        if wire == 254:
            issues.append((source, "external_display_not_qualified"))
            continue
        aid, tid = divmod(wire, 4)
        unit = next(u for u in frame["ams"]["ams"] if int(u["id"]) == aid)
        tray = next(t for t in unit["tray"] if int(t["id"]) == tid)
        tag = tray.get("tag_uid")
        if not isinstance(tag, str) or not tag or any(c != "0" for c in tag):
            issues.append((source, "physical_rfid_or_tag_unknown"))
            continue
        spool = await inventory.provider.get_spool(assignment.spool_id)
        if spool.archived:
            issues.append((source, "spool_archived"))
            continue
        material = await inventory.materials.lookup(snapshot.provider_id, spool.filament)
        selection = material["selection"]
        if selection is None:
            issues.append((source, material["issue"]))
            continue
        if selection["material"].strip().upper() != choice["material"].strip().upper():
            issues.append((source, "physical_chemistry_conflict"))
            continue
        color = spool.filament.color_hex or ""
        if re.fullmatch(r"[0-9a-fA-F]{6}", color):
            color += "FF"
        if not re.fullmatch(r"[0-9a-fA-F]{8}", color) or not spool.filament.weight:
            issues.append((source, "product_color_or_capacity_unknown"))
            continue
        bindings.append(
            VirtualSpool(
                provider=snapshot.provider_id,
                spool_id=spool.id,
                wire_slot=wire,
                material_id=selection["material_id"],
                material=selection["material"],
                color=color,
                nominal_g=spool.filament.weight,
                remaining_g=spool.remaining_weight,
                observed_identity=physical_identity(tray),
            )
        )

    def context(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # An identical complete receipt may advance timestamps during HTTP I/O.
        # Its source epoch stays stable; expiration still removes the choice.
        return [{k: v for k, v in row.items() if k != "observed_at"} for row in rows]

    if await inventory.assignments.snapshot() != snapshot or context(
        source_choices(printer)
    ) != context(choices):
        raise InventoryConflict("inventory or physical sources changed during projection")
    return InventoryProjection(
        snapshot.provider_id, snapshot.revision, tuple(bindings), tuple(issues)
    )


class InventoryDisplay:
    def __init__(
        self,
        namespace: str,
        inventory: Callable[[], InventoryService | None],
        printer: Callable[[], Any],
        notify: Callable[[], None],
        *,
        qualified: bool = False,
        interval: float = 5,
        timeout: float = 10,
        load_policy: Callable[[], Awaitable[tuple[bool, bool]]] | None = None,
    ) -> None:
        self.overlay = NativeSpoolOverlay(namespace)
        self.inventory = inventory
        self.printer = printer
        self.notify = notify
        self.qualified = qualified
        self.global_enabled = False
        self.printer_enabled = False
        self.interval = interval
        self.timeout = timeout
        self.load_policy = load_policy
        self.reason = "disabled"
        self._policy_revision = 0
        self._lock = asyncio.Lock()
        self._published: tuple[Any, ...] = (False, ())

    def policy(self, *, global_enabled: bool, printer_enabled: bool) -> None:
        self._policy_revision += 1
        self.global_enabled, self.printer_enabled = global_enabled, printer_enabled
        if not self.active:
            self.withdraw("disabled" if self.qualified else "not_qualified")

    @property
    def active(self) -> bool:
        return self.qualified and self.global_enabled and self.printer_enabled

    def _publish(self) -> None:
        value = (self.overlay.enabled, tuple(self.overlay.bindings.values()))
        if value != self._published:
            self._published = value
            self.notify()

    def withdraw(self, reason: str) -> None:
        self.overlay.enabled = False
        self.overlay.replace_bindings(())
        self.reason = reason
        self._publish()

    async def refresh(self) -> None:
        async with self._lock:
            if self.load_policy is not None:
                try:
                    policy_revision = self._policy_revision
                    async with asyncio.timeout(self.timeout):
                        enabled, printer_enabled = await self.load_policy()
                    if policy_revision != self._policy_revision:
                        return  # an explicit policy change wins over an older read
                    if (enabled, printer_enabled) != (self.global_enabled, self.printer_enabled):
                        self.policy(global_enabled=enabled, printer_enabled=printer_enabled)
                except asyncio.CancelledError:
                    self.withdraw("stopped")
                    raise
                except Exception:
                    self.withdraw("policy_unavailable")
                    return
            if not self.active:
                self.withdraw("disabled" if self.qualified else "not_qualified")
                return
            revision = self._policy_revision
            inventory = self.inventory()
            if inventory is None:
                self.withdraw("inventory_unavailable")
                return
            try:
                async with asyncio.timeout(self.timeout):
                    projection = await inventory.native_projection(self.printer())
                if revision != self._policy_revision or not self.active:
                    return  # policy() already withdrew; never publish a late read.
                self.overlay.replace_bindings(projection.bindings)
                self.overlay.enabled = True
                self.reason = "candidate"  # qualification still belongs to the configured runtime
                self._publish()
            except asyncio.CancelledError:
                self.withdraw("stopped")
                raise
            except Exception:
                # Provider errors and race diagnostics can contain sensitive data.
                # No stale bindings survive a failed or timed-out refresh.
                self.withdraw("refresh_unavailable")

    async def run(self) -> None:
        try:
            while True:
                await self.refresh()
                await asyncio.sleep(self.interval)
        finally:
            self.withdraw("stopped")


SEATING_INTERVAL_S = 2.0


async def release_unseated(
    printers: Callable[[], Iterable[Any]],
    inventory: Any,
    adopter: TagAdopter | None = None,
    colours: ColourSync | None = None,
) -> None:
    while True:
        for printer in list(printers()):
            # Unloads are recorded as they arrive, so a quick unload and reload
            # between two passes is still seen here.
            unloaded = printer.material_inventory.take_unloaded()
            choices = source_choices(printer) if printer.connected else []
            if not unloaded and not choices:
                continue
            try:
                dropped = await inventory.release_unseated(printer.serial, choices, unloaded)
            except Exception:
                printer.material_inventory.mark_unloaded(unloaded)
                log.exception("inventory.seating_failed", printer=printer.serial)
                continue
            for assignment in dropped:
                log.info(
                    "inventory.assignment_released",
                    printer=assignment.printer_id,
                    wire_slot=assignment.wire_slot,
                    spool_id=assignment.spool_id,
                )
            # The same frame, after the releases: a new RFID tag seats its spool.
            if adopter is not None and choices:
                try:
                    await adopter.adopt(printer, choices)
                except Exception:
                    log.exception("inventory.tag_adoption_failed", printer=printer.serial)
            # Then every assigned untagged slot shows its spool's colour.
            if colours is not None and any(c["assignable"] and not c["tagged"] for c in choices):
                try:
                    snapshot = await inventory.assignments.snapshot()
                    await colours.sync(printer, choices, snapshot)
                except Exception:
                    log.exception("inventory.slot_colour_failed", printer=printer.serial)
        await asyncio.sleep(SEATING_INTERVAL_S)


@contextlib.asynccontextmanager
async def seating_worker(
    printers: Callable[[], Iterable[Any]],
    inventory: Any,
    events: EventSink | None = None,
    inbox: Callable[[], Any | None] = lambda: None,
) -> AsyncIterator[None]:
    """Release what the AMS no longer holds, seat what a new RFID tag names, and keep
    each untagged slot's colour matching its spool."""
    task = asyncio.create_task(
        release_unseated(
            printers,
            inventory,
            TagAdopter(inventory, events),
            ColourSync(inventory, events, inbox),
        )
    )
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
