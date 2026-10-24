"""One action for a print the bridge could not fully charge.

A print whose debit left a shortfall waits in review: an unassigned slot, a
filament the file could not name or weigh, a spool archived, gone or run
dry, a Spoolman that stayed silent past the retry budget, or a manifest
from before counting. The detail shows what was charged and what was not,
with a charge prefilled; one call closes it: charge the grams to spools
(get, put, get, topping up whatever the ledger already wrote), charge
nothing, or record that it was charged by hand in Spoolman. The shortfalls
stay on the record afterwards so the history still says what happened.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

from bambu_bridge.service import usage_ledger
from bambu_bridge.service.consumption_delivery import (
    context_for,
    counted_usage,
    shortfalls_for,
    slicer_grams,
    window,
)
from bambu_bridge.service.shadow_accounting import tray_materials

ACTIONS = ("charge", "nothing", "charged_elsewhere")

REFUSALS = {
    "archived": "#{spool} is archived in Spoolman",
    "missing": "#{spool} isn't in Spoolman any more",
    "no_used_weight": "#{spool} isn't in Spoolman any more",
    "empty": "#{spool} ran out in Spoolman",
}


class ReviewError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _resolution(inbox: Any, attempt_id: str) -> dict[str, Any] | None:
    with inbox.connect() as db:
        row = db.execute(
            "SELECT * FROM usage_resolutions WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
    if row is None:
        return None
    return {
        "action": row["action"],
        "charges": json.loads(row["charges"]),
        "resolved_at": row["resolved_at"],
    }


def _record(inbox: Any, attempt_id: str, action: str, charges: list[dict[str, Any]]) -> None:
    with inbox.connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO usage_resolutions VALUES (?,?,?,?)",
            (attempt_id, action, json.dumps(charges), time.time()),
        )


def _title(attempt: dict[str, Any]) -> str:
    command = json.loads(attempt.get("command") or "{}").get("print") or {}
    return str(command.get("subtask_name") or "")


async def detail(
    inventory: Any, inbox: Any, printer_for: Callable[[str], Any], attempt_id: str
) -> dict[str, Any]:
    """A print's debit: what was charged, what could not be, and the charge to offer."""
    event = await asyncio.to_thread(inbox.accounting_event, attempt_id)
    if event is None:
        raise ReviewError(404, "No record of this print")
    attempt = await asyncio.to_thread(inbox.get, attempt_id)
    manifest = event["manifest"]
    completed = event["outcome"] == "completed"
    layer = None if completed else attempt.get("layer_reached")
    switches, spools, _ = await context_for(
        inventory, printer_for, attempt, manifest, window(event, attempt)[1]
    )
    rows = await asyncio.to_thread(usage_ledger.rows, inbox, attempt_id)
    suggested: list[dict[str, Any]] = []
    if "layer_grams" in manifest and (completed or layer):
        charges, _, _ = counted_usage(manifest, layer, switches, spools)
        suggested = [{"spool_id": s, "grams": g} for s, g in sorted(charges.items())]
    elif "layer_grams" not in manifest and (grams := slicer_grams(manifest)) is not None:
        assigned = sorted(set(spools.values()))
        suggested = [{"spool_id": assigned[0] if len(assigned) == 1 else None, "grams": grams}]
    found = await asyncio.to_thread(usage_ledger.shortfalls, inbox, attempt_id)
    if found is None:
        # Closed by a release before shortfalls were recorded: work them out now.
        gave_up = (await asyncio.to_thread(inbox.accounting_review, attempt_id)) == "delivery_error"
        found = shortfalls_for(manifest, layer, switches, spools, rows, give_up=gave_up)
    if not manifest.get("tray_materials"):
        # Frozen before trays were kept: name the slot from the printer as it is now.
        trays = _live_trays(printer_for, attempt["printer"])
        for shortfall in found:
            if shortfall["material"] is None and shortfall["wire_slot"] is not None:
                shortfall["material"] = (trays.get(str(shortfall["wire_slot"])) or {}).get(
                    "material"
                )
    snapshot = await inventory.assignments.snapshot()
    return {
        "attempt_id": attempt_id,
        "printer_id": attempt["printer"],
        "title": _title(attempt),
        "outcome": event["outcome"],
        "delivery_state": event["delivery_state"],
        "review_reason": await asyncio.to_thread(inbox.accounting_review, attempt_id),
        "layer_reached": attempt.get("layer_reached"),
        "created_at": attempt.get("created"),
        "ended_at": event.get("created_at"),
        "capture_id": None,
        "suggested": suggested,
        "charged": [
            {"spool_id": s, "grams": r["written_g"], "layers": _layers(r)}
            for s, r in rows.items()
            if r["written_g"] > 0
        ],
        "shortfalls": found,
        "written": [
            {
                "spool_id": s,
                "written_g": r["written_g"],
                "state": r["state"],
                "reason": r["reason"],
            }
            for s, r in rows.items()
        ],
        "candidates": [
            {"spool_id": a.spool_id, "wire_slot": 254 if a.source == "external" else a.wire_slot}
            for a in snapshot.assignments
            if a.printer_id == attempt["printer"]
        ],
        "resolution": _resolution(inbox, attempt_id),
        "moves": [
            {
                "from_spool_id": m["from_spool"],
                "to_spool_id": m["to_spool"],
                "grams": m["grams"],
                "state": m["state"],
                "started_at": m["started_at"],
                "finished_at": m["finished_at"],
            }
            for m in await asyncio.to_thread(usage_ledger.moves, inbox, attempt_id)
        ],
    }


async def resolve(
    inventory: Any,
    inbox: Any,
    printer_for: Callable[[str], Any],
    attempt_id: str,
    action: str,
    charges: list[dict[str, Any]],
) -> dict[str, Any]:
    """Close a print in review: charge the given grams, nothing, or note it was charged by hand.

    A charge names a spool and grams (the print's total on that spool), or a
    slot and the spool that was in it (the slot's waiting grams, added to what
    the spool already has from this print).
    """
    if action not in ACTIONS:
        raise ReviewError(422, "Unknown action")
    async with inventory.writes:  # one resolve at a time, and none while a settle runs
        event = await asyncio.to_thread(inbox.accounting_event, attempt_id)
        if event is None:
            raise ReviewError(404, "No record of this print")
        if event["delivery_state"] == "delivered":
            raise ReviewError(409, "This print is already settled")
        if action == "charge":
            targets = await _targets(inventory, inbox, printer_for, attempt_id, charges)
            landed: list[tuple[int, float]] = []
            for spool, grams in targets:
                row = await usage_ledger.settle(
                    inventory, inbox, attempt_id, spool, grams, final=True
                )
                if row["reason"] in REFUSALS:
                    # Archived, gone or run dry: nothing more was taken; the print stays in review.
                    raise ReviewError(
                        409, REFUSALS[row["reason"]].format(spool=spool) + _landed(landed)
                    )
                if row["state"] in ("open", "sent"):
                    raise ReviewError(409, "Spoolman didn't answer; try again" + _landed(landed))
                landed.append((spool, row["written_g"]))
            record = [{"spool_id": s, "grams": g} for s, g in landed]
        else:
            record = []  # nothing was written; the request's figures are not a charge
        await asyncio.to_thread(_record, inbox, attempt_id, action, record)
        await asyncio.to_thread(inbox.set_delivery_state, attempt_id, "delivered")
    return await detail(inventory, inbox, printer_for, attempt_id)


async def _targets(
    inventory: Any,
    inbox: Any,
    printer_for: Callable[[str], Any],
    attempt_id: str,
    charges: list[dict[str, Any]],
) -> list[tuple[int, float]]:
    """(spool, grams the print should total on it) per charge, in request order.

    Each slot and each spool may be named once.
    """
    if not charges:
        raise ReviewError(422, "A charge needs at least one spool")
    waiting: dict[int, float] | None = None
    rows = await asyncio.to_thread(usage_ledger.rows, inbox, attempt_id)
    seen_slots: set[int] = set()
    targets: dict[int, float] = {}
    for charge in charges:
        spool = int(charge["spool_id"])
        if spool <= 0:
            raise ReviewError(422, "Each charge needs a spool")
        if spool in targets:
            raise ReviewError(422, f"#{spool} is named twice; add its grams up into one charge")
        if charge.get("slot") is not None:
            slot = int(charge["slot"])
            if waiting is None:
                found = (await detail(inventory, inbox, printer_for, attempt_id))["shortfalls"]
                waiting = {
                    s["wire_slot"]: s["grams"] for s in found if s["kind"] == "unassigned_slot"
                }
            if slot not in waiting:
                raise ReviewError(422, f"Slot {slot} has nothing waiting to be charged")
            if slot in seen_slots:
                raise ReviewError(422, f"Slot {slot} is named twice; one spool per slot")
            seen_slots.add(slot)
            grams = round(rows.get(spool, {}).get("written_g", 0.0) + waiting[slot], 2)
        else:
            grams = round(float(charge["grams"]), 2)
        if not 0 < grams <= 10_000:
            raise ReviewError(422, "Each charge needs grams between 0 and 10000")
        targets[spool] = grams
    return list(targets.items())


def _layers(row: dict[str, Any]) -> list[int] | None:
    return None if row.get("first_layer") is None else [row["first_layer"], row["last_layer"]]


def _live_trays(printer_for: Callable[[str], Any], printer_id: str) -> dict[str, Any]:
    try:
        return tray_materials(printer_for(printer_id))
    except Exception:  # noqa: BLE001 — no printer, no names
        return {}


def _landed(landed: list[tuple[int, float]]) -> str:
    """Names what an earlier charge of the same request already wrote."""
    if not landed:
        return ""
    return "; already charged " + ", ".join(f"#{s} {g} g" for s, g in landed)


async def reassign(
    inventory: Any,
    inbox: Any,
    printer_for: Callable[[str], Any],
    attempt_id: str,
    from_spool: int,
    to_spool: int,
    grams: float | None,
) -> dict[str, Any]:
    """Move whole grams of this print from one spool to another: credit, then charge.

    Both writes are get, put, get. A move interrupted between them is finished
    by the same request sent again: the credit is seen to have landed and only
    the charge is sent. Nothing is ever lost or counted twice.
    """
    from bambu_bridge.service.usage_ledger import TOLERANCE_G, WRITE_MINIMUM_G

    if from_spool == to_spool:
        raise ReviewError(422, "Pick a different spool")
    async with inventory.writes:
        event = await asyncio.to_thread(inbox.accounting_event, attempt_id)
        if event is None:
            raise ReviewError(404, "No record of this print")
        rows = await asyncio.to_thread(usage_ledger.rows, inbox, attempt_id)
        target = await _spool_state(inventory.provider, to_spool)
        if target is None:
            raise ReviewError(409, "Spoolman didn't answer; try again")
        if isinstance(target, str):
            raise ReviewError(409, REFUSALS[target].format(spool=to_spool))
        open_moves = [
            m
            for m in await asyncio.to_thread(usage_ledger.moves, inbox, attempt_id)
            if m["state"] != "done"
        ]
        if open_moves and (open_moves[0]["from_spool"], open_moves[0]["to_spool"]) != (
            from_spool,
            to_spool,
        ):
            a, b = open_moves[0]["from_spool"], open_moves[0]["to_spool"]
            raise ReviewError(409, f"Finish the move from #{a} to #{b} first")
        if open_moves:
            move = open_moves[0]
        else:
            have = round(rows.get(from_spool, {}).get("written_g", 0.0), 2)
            if have < WRITE_MINIMUM_G:
                raise ReviewError(422, f"#{from_spool} has nothing from this print")
            asked = have if grams is None else round(float(grams), 2)
            if asked < WRITE_MINIMUM_G:
                raise ReviewError(422, "Move at least 0.01 g")
            if asked > have + TOLERANCE_G:
                raise ReviewError(422, f"#{from_spool} has only {have:g} g from this print")
            move = await asyncio.to_thread(
                usage_ledger.start_move, inbox, attempt_id, from_spool, to_spool, float(asked), have
            )
        if move["state"] == "started":
            try:
                row = await usage_ledger.credit(
                    inventory,
                    inbox,
                    attempt_id,
                    from_spool,
                    move["grams"],
                    written_before=move["from_written_before"],
                )
            except ValueError as exc:
                raise ReviewError(409, str(exc)) from None
            if row["reason"] in REFUSALS:
                raise ReviewError(409, REFUSALS[row["reason"]].format(spool=from_spool))
            if row["written_g"] > move["from_written_before"] - move["grams"] + WRITE_MINIMUM_G / 2:
                raise ReviewError(409, "Spoolman didn't answer; try again")
            await asyncio.to_thread(
                usage_ledger.raise_counted, inbox, attempt_id, to_spool, move["grams"]
            )
            await asyncio.to_thread(usage_ledger.set_move, inbox, move["id"], state="credited")
            move["state"] = "credited"
        to_row = await asyncio.to_thread(usage_ledger._row, inbox, attempt_id, to_spool)
        to_row = await usage_ledger.settle(
            inventory, inbox, attempt_id, to_spool, to_row["counted_g"], final=True
        )
        credited = f"Gave #{from_spool} {move['grams']:g} g back; "
        if to_row["reason"] in REFUSALS:
            raise ReviewError(409, credited + REFUSALS[to_row["reason"]].format(spool=to_spool))
        if to_row["state"] in ("open", "sent"):
            raise ReviewError(409, credited + "Spoolman didn't answer; try again")
        # The layers travel with the grams: all of them on a full move, shared on a part.
        from_row = await asyncio.to_thread(usage_ledger._row, inbox, attempt_id, from_spool)
        if from_row["first_layer"] is not None:
            span = {"first_layer": from_row["first_layer"], "last_layer": from_row["last_layer"]}
            if to_row["first_layer"] is None:
                await asyncio.to_thread(usage_ledger._set, inbox, attempt_id, to_spool, **span)
            if from_row["written_g"] < WRITE_MINIMUM_G:
                await asyncio.to_thread(
                    usage_ledger._set,
                    inbox,
                    attempt_id,
                    from_spool,
                    first_layer=None,
                    last_layer=None,
                )
        await asyncio.to_thread(
            usage_ledger.set_move, inbox, move["id"], state="done", finished_at=time.time()
        )
    return await detail(inventory, inbox, printer_for, attempt_id)


async def _spool_state(provider: Any, spool_id: int) -> Any:
    """The spool, a REFUSALS key, or None when Spoolman is unreachable."""
    from bambu_bridge.service.usage_ledger import _read

    return await _read(provider, spool_id)
