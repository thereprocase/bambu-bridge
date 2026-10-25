"""Debit prints from Spoolman when BRIDGE_INVENTORY_AUTO_DEDUCT is on.

Grams are counted per spool from the dispatch manifest: every layer's grams
per filament, the slot feeding each filament, the layer the printer is on and
any AMS auto-refill. While a print runs, each tick brings Spoolman up to the
count so far (usage_ledger: get, put, get); when it finishes or stops, one
last settle closes the print and the event is marked delivered. A slot with
no spool assigned is noted, not guessed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Any

import structlog

from bambu_bridge.service import usage_ledger
from bambu_bridge.service.shadow_accounting import assignment_stands, slot_identities
from bambu_bridge.usage_split import slot_layers, slot_usage

log = structlog.get_logger(__name__)

DELIVERY_INTERVAL_S = 10.0
# Spoolman silent at a print's end: kept trying this long, then the print waits in review.
RETRY_BUDGET_S = 6 * 3600.0


def _epoch(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    return None


def window(event: dict[str, Any], attempt: dict[str, Any]) -> tuple[float | None, float]:
    """(dispatch time, end of the print) between which a refill belongs to it.

    The end event is stamped in whole seconds, so the window closes at the end
    of that second: a refill observed later in the same second still counts.
    """
    begun = _epoch(attempt.get("dispatched_at")) or _epoch(attempt.get("created"))
    ended = _epoch(event.get("created_at"))
    return begun, time.time() if ended is None else ended + 1


def refill_switches(
    printer: Any, begun: float, ended: float, manifest: dict[str, Any] | None = None
) -> list[tuple[int, int, int]]:
    """(from wire slot, to wire slot, layer) for each refill during the print, oldest first.

    With the print's manifest, a switch inferred from tray_now alone is dropped when a
    filament of the file is wired to the destination: that print uses both slots on
    purpose, so the move is a colour change.
    """
    used = {int(w) for w in ((manifest or {}).get("tool_wires") or {}).values()}
    rows: list[dict[str, Any]] = []
    for archived in getattr(printer.diagnostic_journal, "archived_refills", []):
        rows.extend(archived.get("refills") or [])
    # The same row is live until the next print starts, then archived; an archived
    # copy can be an older snapshot, so the live row is read last and wins.
    rows.extend(getattr(printer.auto_refill, "transitions", []))
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        observed = _epoch(row.get("observed_at"))
        if observed is not None and begun <= observed <= ended:
            key = (str(row.get("from_slot")), str(row.get("started_at") or row["observed_at"]))
            unique[key] = row
    switches = []
    for row in sorted(
        unique.values(), key=lambda r: _epoch(r.get("started_at")) or _epoch(r["observed_at"])
    ):
        values = (row.get("from_slot"), row.get("to_slot"), row.get("layer"))
        if row.get("status") != "completed" or any(type(v) is not int for v in values):
            continue  # not a finished switch the printer placed; the count stays put
        if row.get("source") == "tray_now" and values[1] - 1 in used:
            continue
        switches.append((values[0] - 1, values[1] - 1, values[2]))
    return switches


def inferred_refills(
    printer: Any, begun: float, ended: float, manifest: dict[str, Any]
) -> list[str]:
    """Reasons for the owner to look at a print whose refill the firmware never announced."""
    counted = refill_switches(printer, begun, ended, manifest)
    rows = list(getattr(printer.auto_refill, "transitions", []))
    for archived in getattr(printer.diagnostic_journal, "archived_refills", []):
        rows.extend(archived.get("refills") or [])
    found = []
    for row in rows:
        observed = _epoch(row.get("observed_at"))
        if row.get("source") != "tray_now" or observed is None or not begun <= observed <= ended:
            continue
        move = (row["from_slot"] - 1, row["to_slot"] - 1, row["layer"])
        if move in counted:
            found.append(f"refill_inferred_slot_{row['from_slot']}_to_{row['to_slot']}")
    return sorted(set(found))


def slot_spools(
    manifest: dict[str, Any],
    assignments: Any,
    printer_id: str,
    slots: dict[str, dict[str, Any]] | None = None,
) -> dict[int, int]:
    """Spool per slot: frozen at dispatch, else the slot's assignment now.

    With `slots` (slot_identities), an assignment whose tagged slot now holds
    another spool is left out, so the slot is asked about instead.
    """
    frozen = {int(w): s for w, s in (manifest.get("wire_spools") or {}).items()}
    for a in assignments:
        wire = 254 if a.source == "external" else a.wire_slot
        if a.printer_id != printer_id or wire is None:
            continue
        if slots is not None and not assignment_stands(a, slots.get(str(wire))):
            continue
        frozen.setdefault(int(wire), a.spool_id)
    return frozen


def tag_changes(manifest: dict[str, Any], slots: dict[str, dict[str, Any]]) -> list[int]:
    """Slots whose tagged spool is not the one the print was dispatched with."""
    frozen = manifest.get("slot_identities") or {}
    return sorted(
        int(wire)
        for wire, slot in slots.items()
        if slot["tagged"] and wire in frozen and slot["identity"] != frozen[wire]
    )


def counted_usage(
    manifest: dict[str, Any],
    layer: int | None,
    switches: list[tuple[int, int, int]],
    spools: dict[int, int],
) -> tuple[dict[int, float], list[str], dict[int, float]]:
    """(grams per spool, notes, grams per slot with no spool) for a print at `layer`.

    `layer` None means the print completed. The notes name a filament the
    start never mapped to a slot and a filament the G-code gave no weight
    for; a slot used with no spool assigned is returned with its grams so the
    owner can be asked what was in it.
    """
    usage = slot_usage(manifest, layer, switches)
    per_spool: dict[int, float] = {}
    unassigned: dict[int, float] = {}
    for wire, grams in usage.items():
        if wire in spools:
            per_spool[spools[wire]] = round(per_spool.get(spools[wire], 0) + grams, 2)
        else:
            unassigned[wire] = grams
    uncharged: list[str] = []
    mapped = set((manifest.get("tool_wires") or {}).keys())
    uncharged += [
        f"filament_{t}"
        for t in sorted({t for row in manifest.get("layer_grams") or [] for t in row})
        if t not in mapped
    ]
    uncharged += [
        issue.removeprefix("unweighed_")
        for issue in manifest.get("analysis_issues") or []
        if issue.startswith("unweighed_filament_")
    ]
    return per_spool, uncharged, unassigned


def spool_layers(
    manifest: dict[str, Any],
    layer: int | None,
    switches: list[tuple[int, int, int]],
    spools: dict[int, int],
) -> dict[int, tuple[int, int]]:
    """(first, last) layer each spool fed: its slots' ranges merged."""
    found: dict[int, tuple[int, int]] = {}
    for wire, (first, last) in slot_layers(manifest, layer, switches).items():
        spool = spools.get(wire)
        if spool is None:
            continue
        a, b = found.get(spool, (first, last))
        found[spool] = (min(a, first), max(b, last))
    return found


async def context_for(
    inventory: Any,
    printer_for: Callable[[str], Any],
    attempt: dict[str, Any],
    manifest: dict[str, Any],
    ended: float,
) -> tuple[list[tuple[int, int, int]], dict[int, int], dict[str, dict[str, Any]]]:
    """(refill switches during the print, spool per slot, what the slots hold now)."""
    switches: list[tuple[int, int, int]] = []
    slots: dict[str, dict[str, Any]] = {}
    with contextlib.suppress(Exception):
        printer = printer_for(attempt["printer"])
        begun = _epoch(attempt.get("dispatched_at")) or _epoch(attempt.get("created"))
        switches = refill_switches(printer, begun or 0.0, ended, manifest)
        slots = slot_identities(printer)
    snapshot = await inventory.assignments.snapshot()
    return switches, slot_spools(manifest, snapshot.assignments, attempt["printer"], slots), slots


async def charges_for(
    inventory: Any,
    printer_for: Callable[[str], Any],
    attempt: dict[str, Any],
    manifest: dict[str, Any],
    layer: int | None,
    ended: float,
) -> tuple[dict[int, float], list[str], dict[int, float]]:
    switches, spools, _ = await context_for(inventory, printer_for, attempt, manifest, ended)
    return counted_usage(manifest, layer, switches, spools)


def slicer_grams(manifest: dict[str, Any]) -> float | None:
    """The slicer's total for a manifest with no layer plan."""
    mass = (manifest.get("sliced_settings") or {}).get("slicer_mass_g") or []
    total = sum(g for g in mass if isinstance(g, int | float) and math.isfinite(g) and g > 0)
    if total > 0:
        return round(total, 2)
    planned = manifest.get("planned_grams")
    return round(float(planned), 2) if planned else None


def _tool_grams(plan: list[dict[str, float]], layer: int | None, tool: str) -> float:
    """One filament's grams up to `layer` (None: the whole plan), the last layer by half."""
    last = len(plan) if layer is None else min(max(layer, 0), len(plan))
    total = 0.0
    for index in range(1, last + 1):
        part = 0.5 if layer is not None and index == last else 1.0
        total += (plan[index - 1].get(tool) or 0.0) * part
    return round(total, 2)


SPOOL_SHORTFALLS = {
    "archived": "spool_archived",
    "missing": "spool_missing",
    "no_used_weight": "spool_missing",
    "empty": "spool_empty",
}


def shortfalls_for(
    manifest: dict[str, Any],
    layer: int | None,
    switches: list[tuple[int, int, int]],
    spools: dict[int, int],
    rows: dict[int, dict[str, Any]],
    *,
    give_up: bool = False,
) -> list[dict[str, Any]]:
    """What a print's debit could not settle, each with the grams and where they belong.

    kinds: unassigned_slot, unmapped_filament, unweighed_filament, spool_archived,
    spool_missing, spool_empty, delivery_error (only when giving up on a silent
    Spoolman), no_layer_plan. `material` is what the slot reported at dispatch.
    """
    trays = manifest.get("tray_materials") or {}

    def entry(kind: str, wire: int | None, spool: int | None, grams: float) -> dict[str, Any]:
        material = (trays.get(str(wire)) or {}).get("material") if wire is not None else None
        return {
            "kind": kind,
            "wire_slot": wire,
            "spool_id": spool,
            "grams": grams,
            "material": material,
        }

    if "layer_grams" not in manifest:
        assigned = sorted(set(spools.values()))
        spool = assigned[0] if len(assigned) == 1 else None
        return [entry("no_layer_plan", None, spool, slicer_grams(manifest) or 0.0)]
    found = []
    _, _, unassigned = counted_usage(manifest, layer, switches, spools)
    for wire, grams in sorted(unassigned.items()):
        found.append(entry("unassigned_slot", wire, None, grams))
    plan = manifest.get("layer_grams") or []
    mapped = set((manifest.get("tool_wires") or {}).keys())
    for tool in sorted({t for row in plan for t in row}):
        if tool not in mapped:
            found.append(entry("unmapped_filament", None, None, _tool_grams(plan, layer, tool)))
    for issue in manifest.get("analysis_issues") or []:
        if issue.startswith("unweighed_filament_"):
            found.append(entry("unweighed_filament", None, None, 0.0))
    first_wire: dict[int, int] = {}
    for wire, spool in sorted(spools.items()):
        first_wire.setdefault(spool, wire)
    for spool, row in sorted(rows.items()):
        owed = round(row["counted_g"] - row["written_g"], 2)
        if owed < usage_ledger.WRITE_MINIMUM_G:
            continue  # within a gram: settled
        if row.get("reason") in SPOOL_SHORTFALLS:
            kind = SPOOL_SHORTFALLS[row["reason"]]
        elif give_up and row["state"] in ("open", "sent"):
            kind = "delivery_error"
        else:
            continue
        found.append(entry(kind, first_wire.get(spool), spool, owed))
    return found


async def settle_running(
    inventory: Any,
    inbox: Any,
    printer_for: Callable[[str], Any],
    running: list[tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    """Bring Spoolman up to what every running print has used so far."""
    for attempt, manifest in running:
        layer = attempt.get("layer_reached")
        if not layer or "layer_grams" not in manifest:
            continue
        switches, spools, _ = await context_for(
            inventory, printer_for, attempt, manifest, time.time()
        )
        charges, _, _ = counted_usage(manifest, layer, switches, spools)
        ranges = spool_layers(manifest, layer, switches, spools)
        for spool_id, grams in sorted(charges.items()):
            async with inventory.writes:
                await usage_ledger.settle(
                    inventory, inbox, attempt["id"], spool_id, grams, layers=ranges.get(spool_id)
                )


async def close_summary(
    inbox: Any,
    attempt_id: str,
    planned: Any,
    found: list[dict[str, Any]],
    reason: str | None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """What a closed print charged, for its log line and its Activity feed row."""
    attempt = await asyncio.to_thread(inbox.get, attempt_id)
    rows = await asyncio.to_thread(usage_ledger.rows, inbox, attempt_id)
    command = json.loads(attempt.get("command") or "{}").get("print") or {}
    charged = [
        {"spool_id": spool, "grams": round(row["written_g"], 2)}
        for spool, row in sorted(rows.items())
        if row["written_g"] > 0
    ]
    return {
        "attempt_id": attempt_id,
        "printer_id": attempt.get("printer"),
        "title": attempt.get("title") or command.get("subtask_name") or "",
        "charged": charged,
        "total_g": round(sum(c["grams"] for c in charged), 2),
        "planned_g": planned if isinstance(planned, int | float) else None,
        "review": bool(found),
        "reason": (reason or found[0]["kind"]) if found else None,
        "notes": list(notes or []),
    }


async def _record_close(inventory: Any, summary: dict[str, Any]) -> None:
    """One Activity feed row per closed print: what it charged, or why it waits."""
    record = getattr(inventory, "record_event", None)
    if record is None or not summary["printer_id"]:
        return
    payload = {k: v for k, v in summary.items() if k != "printer_id"}
    if summary["review"]:
        await record(summary["printer_id"], "usage_review", payload, "warn")
    else:
        await record(summary["printer_id"], "usage_charged", payload)


async def deliver_once(
    inventory: Any,
    inbox: Any,
    printer_for: Callable[[str], Any],
    started_at: float,
    failures: dict[str, float] | None = None,
) -> None:
    """Settle every running print, then close every finished or stopped one.

    A print closes delivered when every gram went to a spool; anything it
    could not settle is recorded as a shortfall and the print waits in review
    for the owner (usage_review). `failures` remembers when an end first went
    unconfirmed, so a silent Spoolman is retried for RETRY_BUDGET_S.
    """
    failures = {} if failures is None else failures
    if not inventory.consumption_qualified:
        return
    running = await asyncio.to_thread(inbox.printing_attempts)
    # Prints that finished before deduction was on in this process stay shadow.
    events = await asyncio.to_thread(inbox.pending_accounting_events, started_at)
    if not running and not events:
        return
    await settle_running(inventory, inbox, printer_for, running)
    for event in events:
        attempt_id = event["attempt_id"]

        async def close(
            found: list[dict[str, Any]],
            reason: str | None = None,
            attempt_id: str = attempt_id,
            planned: Any = (event.get("manifest") or {}).get("planned_grams"),
            notes: list[str] | None = None,
        ) -> None:
            """Record the shortfalls; review when there are any, else delivered."""
            current = await asyncio.to_thread(inbox.accounting_event, attempt_id)
            if current is None or current["delivery_state"] != "shadow":
                return  # the owner closed it through resolve while this pass ran
            await asyncio.to_thread(usage_ledger.set_shortfalls, inbox, attempt_id, found)
            if found:
                await asyncio.to_thread(
                    inbox.review_accounting, attempt_id, reason or found[0]["kind"]
                )
                await asyncio.to_thread(inbox.set_delivery_state, attempt_id, "review")
            else:
                await asyncio.to_thread(inbox.set_delivery_state, attempt_id, "delivered")
            try:
                summary = await close_summary(inbox, attempt_id, planned, found, reason, notes)
            except Exception:  # noqa: BLE001 — the print is closed; only its summary is missing
                log.exception("inventory.usage_summary_failed", attempt=attempt_id)
                summary = {"printer_id": None, "title": "", "total_g": None}
            log.info(
                "inventory.usage_closed",
                attempt=attempt_id,
                printer=summary["printer_id"],
                title=summary["title"],
                charged_g=summary["total_g"],
                reason=reason,
                shortfalls=found,
            )
            try:
                await _record_close(inventory, summary)
            except Exception:  # noqa: BLE001 — the print is closed whether or not the feed took it
                log.exception("inventory.event_write_failed", attempt=attempt_id)

        attempt = dict(await asyncio.to_thread(inbox.get, attempt_id))
        manifest = event["manifest"]
        completed = event["outcome"] == "completed"
        layer = None if completed else attempt.get("layer_reached")
        if not completed and not layer:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    inbox.review_accounting, attempt_id, "stopped_before_first_layer"
                )
                await close([])
            continue
        _, ended = window(event, attempt)
        switches, spools, slots = await context_for(
            inventory, printer_for, attempt, manifest, ended
        )
        if "layer_grams" not in manifest:
            # Frozen by a release that did not count layers: nothing to count from;
            # the owner is offered the slicer's figure. An empty plan is different:
            # the file extruded nothing and costs 0 g.
            with contextlib.suppress(Exception):
                found = shortfalls_for(manifest, None, switches, spools, {})
                await close(found, "manifest_predates_layer_plan")
            continue
        charges, _, _ = counted_usage(manifest, layer, switches, spools)
        try:
            # Every row the print opened closes now: a spool whose layers a late
            # refill moved elsewhere settles at 0 and keeps what it was written.
            opened = await asyncio.to_thread(usage_ledger.rows, inbox, attempt_id)
            final = {spool: 0.0 for spool in opened} | charges
            ranges = spool_layers(manifest, layer, switches, spools)
            over_charged: dict[int, float] = {}
            for spool_id, grams in sorted(final.items()):
                async with inventory.writes:
                    row = await usage_ledger.settle(
                        inventory,
                        inbox,
                        attempt_id,
                        spool_id,
                        grams,
                        final=True,
                        layers=ranges.get(spool_id),
                    )
                if row["written_g"] - grams >= usage_ledger.WRITE_MINIMUM_G:
                    over_charged[spool_id] = round(row["written_g"] - grams, 2)
            rows = await asyncio.to_thread(usage_ledger.rows, inbox, attempt_id)
            if any(row["state"] in ("open", "sent") for row in rows.values()):
                first = failures.setdefault(attempt_id, time.time())
                if time.time() - first < RETRY_BUDGET_S:
                    raise ConnectionError("Spoolman write unconfirmed")  # retried next pass
                found = shortfalls_for(manifest, layer, switches, spools, rows, give_up=True)
                await close(found, "delivery_error")
            else:
                # Notes for the one who reads the record; they are gathered before
                # close() so the Activity row names them too.
                notes: list[str] = []
                with contextlib.suppress(Exception):
                    # Counted from tray_now alone, without the firmware's notice.
                    notes += inferred_refills(
                        printer_for(attempt["printer"]),
                        _epoch(attempt.get("dispatched_at"))
                        or _epoch(attempt.get("created"))
                        or 0.0,
                        ended,
                        manifest,
                    )
                # The spool the print was dispatched with was charged; the tag in
                # that slot changed before the end. "Wrong spool?" moves the grams
                # if the owner wants.
                try:
                    notes += [f"tag_changed_slot_{wire}" for wire in tag_changes(manifest, slots)]
                except Exception:  # noqa: BLE001 — a note must never keep the print open
                    log.exception("inventory.tag_check_failed", attempt=attempt_id)
                if over_charged:
                    # Never taken back; the record says so.
                    note = "_".join(
                        f"spool_{s}_{int(g + 0.5)}g" for s, g in sorted(over_charged.items())
                    )
                    notes.append(("over_charged_" + note)[:80])
                await close(shortfalls_for(manifest, layer, switches, spools, rows), notes=notes)
                for note in notes:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(inbox.review_accounting, attempt_id, note)
        except Exception:
            # One event must never block the ones after it.
            log.exception("inventory.usage_delivery_retry", attempt=attempt_id)
            continue
        failures.pop(attempt_id, None)


async def deliver_completed(
    inventory: Any,
    inbox_for: Callable[[], Any],
    printer_for: Callable[[str], Any],
    wake: asyncio.Event | None = None,
) -> None:
    failures: dict[str, int] = {}
    wake = asyncio.Event() if wake is None else wake
    loop = asyncio.get_running_loop()

    def waker() -> None:
        loop.call_soon_threadsafe(wake.set)

    while True:
        inbox = inbox_for()
        if inbox is not None:
            if getattr(inbox, "on_layer", None) is not waker:
                # The inbox reports each layer advance from a worker thread; a
                # tick follows at once instead of at the next interval. A
                # restarted worker replaces the one an older loop installed.
                inbox.on_layer = waker
            started_at = await asyncio.to_thread(inbox.deduct_since)
            try:
                await deliver_once(inventory, inbox, printer_for, started_at, failures)
            except Exception:
                log.exception("inventory.usage_delivery_failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), DELIVERY_INTERVAL_S)
        wake.clear()


@contextlib.asynccontextmanager
async def delivery_worker(
    inventory: Any,
    inbox_for: Callable[[], Any],
    printer_for: Callable[[str], Any],
    wake: asyncio.Event | None = None,
) -> AsyncIterator[None]:
    task = asyncio.create_task(deliver_completed(inventory, inbox_for, printer_for, wake))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
