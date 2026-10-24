"""Make an untagged slot show the colour of the spool it holds.

The owner's rule: the printer follows the spool's colour unless the bridge is
lying for a refill. For an assigned, untagged AMS slot (or the external holder)
whose reported tray_color differs from the spool's Spoolman colour, the bridge
writes the slot's colour and keeps its type, SKU and nozzle range exactly, so
the slot's identity and its assignment stand. It never touches a tagged slot
(the tag owns the colour), a slot inside a refill group (the group declares
its whole filament on purpose, and restores it when removed), or a slot the
current print is using (it waits until the slot is free). Each (slot, colour)
is written at most once per bridge run, confirmed from the printer's own report
the way every slot write is.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from bambu_bridge.inventory_sources import source_choices
from bambu_bridge.slot_setting import write_slot
from bambu_bridge.spoolman import SpoolmanError

log = structlog.get_logger(__name__)

SPOOL_COLOUR_TTL_S = 60.0  # how long a spool's Spoolman colour is trusted before re-reading
PRINTING_STATES = {"RUNNING", "PAUSE", "PREPARE"}

EventSink = Callable[..., Awaitable[Any]]


def rgb(value: Any) -> str | None:
    """RRGGBB, upper case, from a 6- or 8-digit hex colour; None otherwise."""
    text = str(value or "").strip().lstrip("#").upper()
    return text[:6] if re.fullmatch(r"[0-9A-F]{6}([0-9A-F]{2})?", text) else None


def refill_alias_wires(inventory: Any, printer_id: str) -> set[int]:
    """Slots a refill group declares on purpose: from planning until restored."""
    controller = getattr(inventory, "refill", None)
    group = controller.store.get(printer_id) if controller is not None else None
    if not group or (not group.get("enabled") and group.get("state") == "disabled"):
        return set()
    return {int(step["wire"]) for step in group.get("steps", []) if "wire" in step}


async def wires_in_use(printer: Any, inbox: Any) -> set[str]:
    """The slots the current print is using: the one feeding now and every slot its
    attempt maps. Empty when nothing is printing. The one rule for every slot write."""
    view = printer.print_view()
    if view.get("gcode_state") not in PRINTING_STATES:
        return set()
    in_use = {str(view.get("tray_now"))}
    running = await asyncio.to_thread(inbox.printing_attempt, printer.serial) if inbox else None
    if running is not None:
        in_use |= {str(w) for w in (running[1].get("tool_wires") or {}).values()}
    return in_use


class ColourSync:
    """Writes each differing (slot, colour) once per run; one instance per bridge."""

    def __init__(
        self,
        inventory: Any,
        events: EventSink | None = None,
        inbox: Callable[[], Any | None] = lambda: None,
    ) -> None:
        self.inventory = inventory
        self.events = events
        self.inbox = inbox  # the native inbox, once the gateway is up; it names the print's slots
        self._written: set[tuple[str, str, str]] = set()
        self._colours: dict[int, tuple[str | None, float]] = {}
        self._aliased: dict[str, set[int]] = {}  # printer -> refill wires last pass

    async def spool_colour(self, spool_id: int) -> str | None:
        cached = self._colours.get(spool_id)
        if cached is not None and time.monotonic() - cached[1] < SPOOL_COLOUR_TTL_S:
            return cached[0]
        try:
            spool = await self.inventory.provider.get_spool(spool_id)
        except SpoolmanError:
            return None  # unknown right now: nothing is written on a guess
        colour = rgb(spool.filament.color_hex)
        self._colours[spool_id] = (colour, time.monotonic())
        return colour

    def forget(self, spool_id: int) -> None:
        """Re-read this spool's colour next time (after an edit the bridge made)."""
        self._colours.pop(spool_id, None)

    async def sync(
        self, printer: Any, rows: list[dict[str, Any]], snapshot: Any
    ) -> list[dict[str, Any]]:
        """Bring every assigned untagged slot on `printer` to its spool's colour."""
        by_source = {row["source"]: row for row in rows}
        aliases = refill_alias_wires(self.inventory, printer.serial)
        # A refill group owns its slots' whole declaration while it exists. Once
        # it is gone (restored), its slots are this pass's again, even for a
        # colour already written this run.
        handed_back = self._aliased.get(printer.serial, set()) - aliases
        self._aliased[printer.serial] = aliases
        if handed_back:
            sources = {row["source"] for row in rows if row["wire_slot"] in handed_back}
            self._written = {
                k for k in self._written if not (k[0] == printer.serial and k[1] in sources)
            }
        in_use: set[str] | None = None  # read once per pass, and only when a write is due
        done: list[dict[str, Any]] = []
        for assignment in snapshot.assignments:
            if assignment.printer_id != printer.serial:
                continue
            row = by_source.get(assignment.source)
            if row is None or not row["assignable"] or row["tagged"] or row["setting"] is None:
                continue
            wire = row["wire_slot"]
            if wire != 254 and wire in aliases:
                continue
            target = await self.spool_colour(assignment.spool_id)
            if target is None or rgb(row["setting"]["tray_color"]) == target:
                continue
            key = (printer.serial, assignment.source, target)
            if key in self._written:
                continue
            if in_use is None:
                in_use = await wires_in_use(printer, self.inbox())
            if str(wire) in in_use:
                log.info(
                    "inventory.slot_colour_deferred",
                    printer=printer.serial,
                    source=assignment.source,
                    spool_id=assignment.spool_id,
                )
                continue
            self._written.add(key)
            setting = {**row["setting"], "tray_color": target + "FF"}

            def reported(source: str = assignment.source, want: str = target) -> bool:
                now = next((r for r in source_choices(printer) if r["source"] == source), None)
                return (
                    now is not None
                    and now["setting"] is not None
                    and (rgb(now["setting"]["tray_color"]) == want)
                )

            confirmed = await write_slot(printer, wire, setting, reported)
            payload = {
                "spool_id": assignment.spool_id,
                "slot_label": row["label"],
                "wire_slot": wire,
                "source": assignment.source,
                "color": target,
                "confirmed": confirmed,
                "text": f"{row['label']} colour set to match #{assignment.spool_id}",
            }
            log.info("inventory.slot_colour_set", printer=printer.serial, **payload)
            if confirmed and self.events is not None:
                await self.events(
                    printer_id=printer.serial,
                    event_type="slot_colour_set",
                    payload=payload,
                    severity="info",
                )
            done.append(payload)
        return done
