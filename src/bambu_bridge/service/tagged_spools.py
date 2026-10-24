"""Put the spool an RFID tag names into its slot, with no tap.

Bambu tags are per spool, and the AMS reads one only when the spool is inserted.
The seating pass (service/slot_display.py) is the one place that decides a
slot's spool changed: on every fresh AMS frame it first releases what the AMS
no longer holds, then hands the same rows here. When a tagged tray shows an
identity the slot's assignment does not carry, the bridge acts once for that
tag: a tag it has seen before (the assignment history remembers the spool
behind every identity, in any slot) gets its spool back if Spoolman still has
it live; a new tag gets a spool created in Spoolman from what the tag says
(Bambu Lab, the tag's name, material, colour, weight and remaining percent),
reusing a filament record that already matches. The tag-to-spool link lives
only in the bridge's assignment history; Spoolman carries nothing of it.
Untagged trays are never touched.

A tag is acted on only once the AMS says it has finished reading it (the
tray's read-done bit set and its reading bit clear, where the firmware reports
them) and two consecutive seating passes show the same identity and the same
tag details, so a tray the printer reports in pieces (the tag before its name,
colour or weight) costs one tick instead of a second, misnamed spool. A create
whose reply is lost is never repeated: the spools of that filament are listed
before the write and again after, and a single new one is taken as ours.
Anything Spoolman refuses or cannot answer is tried again on a later report.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from bambu_bridge.inventory_journal import InventoryConflict
from bambu_bridge.inventory_sources import same_spool, source_choices
from bambu_bridge.spool_assignments import AssignmentConflict
from bambu_bridge.spoolman import Product, ProviderWriteError, SpoolmanError

log = structlog.get_logger(__name__)

VENDOR = "Bambu Lab"
BRAND = "Bambu"
# g/cm³ by the tag's material word; a variant such as PLA-CF takes its base.
DENSITIES = {
    "PLA": 1.24,
    "PETG": 1.27,
    "PET": 1.27,
    "ABS": 1.04,
    "ASA": 1.07,
    "TPU": 1.21,
    "PA": 1.14,
    "PAHT": 1.14,
    "PPA": 1.14,
    "PC": 1.20,
    "PVA": 1.23,
    "HIPS": 1.04,
    "PP": 0.90,
}
DEFAULT_DENSITY = 1.24
GIVE_UP_AFTER = 3  # failed tries per tag before it is left alone until it changes
FRESH_FRAME_S = 30.0  # a tag is acted on only from a frame this recent

EventSink = Callable[..., Awaitable[Any]]


def density_for(material: str) -> float:
    return DENSITIES.get(material.upper().split("-")[0].strip(), DEFAULT_DENSITY)


def _color6(value: str | None) -> str:
    return (value or "").strip().upper()[:6]


class TagAdopter:
    """Acts once per (printer, slot, tag) seen while this bridge runs."""

    def __init__(self, inventory: Any, events: EventSink | None = None) -> None:
        self.inventory = inventory
        self.events = events
        self._done: set[tuple[str, str, str]] = set()
        self._failures: dict[tuple[str, str, str], int] = {}
        # What the last pass showed for a tag not yet acted on, and the tags two
        # passes agreed on; a tag that is still settling never gets this far.
        self._pending: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._confirmed: set[tuple[str, str, str]] = set()
        # Spool ids of a filament listed before a create whose reply was lost.
        self._before: dict[str, set[int]] = {}

    async def adopt(
        self, printer: Any, choices: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """Assign every newly seen tagged tray on `printer`; returns what was done.

        `choices` are the seating pass's rows from the frame it just acted on;
        without them the printer's current frame is read.
        """
        now = time.time()
        rows = [
            row
            for row in (source_choices(printer) if choices is None else choices)
            if row["tagged"]
            and row["assignable"]
            and row.get("tag")
            and not row.get("tag_reading")
            and row.get("tag_read_done") is not False
            and row.get("observed_at") is not None
            and 0 <= now - row["observed_at"] <= FRESH_FRAME_S
            and (printer.serial, row["source"], row["observed_identity"]) not in self._done
        ]
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            key = (printer.serial, row["source"], row["observed_identity"])
            seen.add(key)
            if key not in self._confirmed:
                if self._pending.get(key) != row["tag"]:
                    self._pending[key] = dict(row["tag"])  # first sight: wait one pass
                else:
                    self._confirmed.add(key)
        for key in [k for k in self._pending if k[0] == printer.serial and k not in seen]:
            del self._pending[key]
        rows = [
            row
            for row in rows
            if (printer.serial, row["source"], row["observed_identity"]) in self._confirmed
        ]
        if not rows:
            return []
        snapshot = await self.inventory.assignments.snapshot()
        done: list[dict[str, Any]] = []
        for row in rows:
            key = (printer.serial, row["source"], row["observed_identity"])
            current = next(
                (
                    a
                    for a in snapshot.assignments
                    if (a.printer_id, a.source) == (printer.serial, row["source"])
                ),
                None,
            )
            # Judged by the tag, as the seating pass does: a tagged slot whose
            # declaration changed still holds this tag's spool, and must never
            # be adopted again as a new one.
            if current is not None and same_spool(current, row):
                self._done.add(key)  # the slot already holds this tag's spool
                continue
            try:
                done.append(await self._adopt(printer, row))
            except (SpoolmanError, InventoryConflict, AssignmentConflict, ValueError) as exc:
                failures = self._failures.get(key, 0) + 1
                self._failures[key] = failures
                log.info(
                    "inventory.tag_adoption_deferred",
                    printer=printer.serial,
                    source=row["source"],
                    reason=str(exc) or type(exc).__name__,
                    tries=failures,
                )
                if failures >= GIVE_UP_AFTER:
                    self._done.add(key)
                    log.warning(
                        "inventory.tag_adoption_given_up",
                        printer=printer.serial,
                        source=row["source"],
                    )
                continue
            self._done.add(key)
            self._failures.pop(key, None)
        return done

    async def _adopt(self, printer: Any, row: dict[str, Any]) -> dict[str, Any]:
        tag, identity = row["tag"], row["observed_identity"]
        spool_id = await self._known_spool(identity)
        new = spool_id is None
        if new:
            product = await self._product(tag)
            spool_id = await self._spool(identity, product, tag)
        await self.inventory.assign(
            spool_id=spool_id,
            printer_id=printer.serial,
            source=row["source"],
            observed_identity=identity,
            choices=lambda: source_choices(printer),
            origin="rfid_new" if new else "rfid_known",
        )
        name = f"{BRAND} {tag['brand'] or tag['material']}".strip()
        text = (
            f"New spool #{spool_id} · {name} · {row['label']} (from its RFID tag)"
            if new
            else f"Spool #{spool_id} · {name} · {row['label']} (matched by its RFID tag)"
        )
        payload = {
            "spool_id": spool_id,
            "new": new,
            "name": name,
            "slot_label": row["label"],
            "wire_slot": row["wire_slot"],
            "source": row["source"],
            "text": text,
        }
        if self.events is not None:
            await self.events(
                printer_id=printer.serial,
                event_type="spool_adopted",
                payload=payload,
                severity="info",
            )
        log.info("inventory.tag_adopted", printer=printer.serial, **payload)
        return payload

    async def _known_spool(self, identity: str) -> int | None:
        """The spool last seen behind this tag, in any slot, if Spoolman still has it live."""
        history = await self.inventory.assignments.last_spools()
        seen = [(at, spool) for (ident, _), (spool, at) in history.items() if ident == identity]
        if not seen:
            return None
        spool_id = max(seen)[1]
        try:
            spool = await self.inventory.provider.get_spool(spool_id)
        except SpoolmanError as exc:
            if exc.kind == "not_found":
                return None
            raise
        return None if spool.archived else spool_id

    async def _product(self, tag: dict[str, Any]) -> Product:
        """The Bambu Lab filament this tag names, found by vendor, name and colour, or created."""
        name = tag["brand"] or tag["material"]
        material, color = tag["material"], tag["color"]
        for product in await self.inventory.provider.search_products(name, limit=100):
            if (
                product.vendor is not None
                and product.vendor.name.strip().lower() == VENDOR.lower()
                and (product.name or "").strip().lower() == name.lower()
                and (product.material or "").strip().upper() == material
                and (not color or _color6(product.color_hex) == color)
            ):
                return product
        vendor_id = next(
            (
                v.id
                for v in await self.inventory.provider.search_vendors(VENDOR, limit=100)
                if v.name.strip().lower() == VENDOR.lower()
            ),
            None,
        )
        if vendor_id is None:
            vendor_id = (await self.inventory.provider.write("create_vendor", {"name": VENDOR}))[
                "id"
            ]
        body: dict[str, Any] = {
            "name": name,
            "vendor_id": vendor_id,
            "material": material,
            "density": density_for(material),
            "diameter": 1.75,
            "weight": tag["weight_g"] or 1000,
        }
        if color:
            body["color_hex"] = color
        return Product.model_validate(await self.inventory.provider.write("create_product", body))

    async def _spool(self, identity: str, product: Product, tag: dict[str, Any]) -> int:
        """Create the tag's spool once; after a lost reply, find it instead of creating again."""
        weight = float(tag["weight_g"] or product.weight or 1000)
        remain = tag["remain_percent"]
        remaining = round(weight * remain / 100, 1) if remain is not None else weight
        before = self._before.get(identity)
        if before is not None:
            new = sorted((await self._spool_ids(product)) - before)
            if len(new) == 1:
                self._before.pop(identity)
                return new[0]
            if len(new) > 1:
                raise InventoryConflict(
                    f"more than one new #{product.id} spool appeared while a reply was lost"
                )
        else:
            self._before[identity] = await self._spool_ids(product)
        try:
            record = await self.inventory.provider.write(
                "create_spool",
                {
                    "filament_id": product.id,
                    "initial_weight": weight,
                    "remaining_weight": remaining,
                },
            )
        except ProviderWriteError:
            raise  # the before-list stays, so the next try looks before it creates
        self._before.pop(identity, None)
        return int(record["id"])

    async def _spool_ids(self, product: Product) -> set[int]:
        ids: set[int] = set()
        offset = 0
        while offset < 1000:
            page = await self.inventory.provider.search_spools(
                product.name or "",
                offset=offset,
                limit=100,
                include_archived=True,
                material=product.material or "",
            )
            ids |= {s.id for s in page if s.filament.id == product.id}
            if len(page) < 100:
                break
            offset += 100
        return ids
