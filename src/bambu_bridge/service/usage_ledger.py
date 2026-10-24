"""Debit Spoolman as a print goes: get, put, get.

For every tracked print and every spool it draws from, the ledger keeps the
grams counted so far (the frozen layer plan, the layer the printer is on and
any refill) and the grams already written to Spoolman. A tick writes only the
difference, at most once per spool every WRITE_INTERVAL_S, and witnesses it by
reading the spool's used_weight before and after: moved by the delta means
applied; not moved means not sent, so it is tried again; moved by anything
else means someone edited the spool meanwhile, which is then the truth, and
the delta is counted as written because `use` is relative. A write whose
verifying read was lost stays "sent" and is re-verified before anything else
goes to that spool, so a restart never charges twice. A spool that is
archived or gone is held and charged no further.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any

import structlog

from bambu_bridge.spoolman import ProviderSpool, SpoolmanError

log = structlog.get_logger(__name__)

WRITE_INTERVAL_S = 30.0
# Written to the hundredth of a gram: a delta goes out once it reaches 0.01 g and
# a write is witnessed to 0.005 g, so applied and not applied always read apart.
# Every delta is the counted total less the witnessed written total, never a sum
# of earlier deltas, so rounding cannot accumulate.
WRITE_MINIMUM_G = 0.01
TOLERANCE_G = 0.005


def rows(inbox: Any, attempt_id: str) -> dict[int, dict[str, Any]]:
    """The ledger rows of one print, by spool."""
    with inbox.connect() as db:
        found = db.execute(
            "SELECT * FROM usage_ledger WHERE attempt_id=? ORDER BY spool_id", (attempt_id,)
        ).fetchall()
    return {int(row["spool_id"]): dict(row) for row in found}


def _row(inbox: Any, attempt_id: str, spool_id: int) -> dict[str, Any]:
    with inbox.connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO usage_ledger (attempt_id, spool_id) VALUES (?, ?)",
            (attempt_id, spool_id),
        )
        return dict(
            db.execute(
                "SELECT * FROM usage_ledger WHERE attempt_id=? AND spool_id=?",
                (attempt_id, spool_id),
            ).fetchone()
        )


def _set(inbox: Any, attempt_id: str, spool_id: int, **fields: Any) -> None:
    columns = ", ".join(f"{key}=?" for key in fields)
    with inbox.connect() as db:
        db.execute(
            f"UPDATE usage_ledger SET {columns} WHERE attempt_id=? AND spool_id=?",
            (*fields.values(), attempt_id, spool_id),
        )


async def _read(provider: Any, spool_id: int) -> ProviderSpool | str | None:
    """The spool now; a hold reason if it is gone; None if Spoolman is unreachable."""
    try:
        spool = await provider.get_spool(spool_id)
    except SpoolmanError as exc:
        return "missing" if exc.kind == "not_found" else None
    if spool.archived:
        return "archived"
    if spool.used_weight is None:
        return "no_used_weight"
    return spool


def _witness(before: float, delta: float, after: ProviderSpool) -> str:
    moved = after.used_weight - before  # type: ignore[operator]
    if math.isclose(moved, delta, rel_tol=0, abs_tol=TOLERANCE_G):
        return "applied"
    if math.isclose(moved, 0, rel_tol=0, abs_tol=TOLERANCE_G):
        return "not_applied"
    return "edited"


async def settle(
    inventory: Any,
    inbox: Any,
    attempt_id: str,
    spool_id: int,
    counted: float,
    *,
    final: bool = False,
    now: float | None = None,
    layers: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Bring Spoolman up to `counted` grams for this print and spool; returns the row.

    `final` ignores the write interval and closes the row once nothing is owed.
    `layers` is the (first, last) layer this spool fed, kept with the row.
    """
    now = time.time() if now is None else now
    provider = inventory.provider
    counted = round(counted, 2)
    # SQLite steps run off the loop: the inbox waits up to 10 s for a busy lock.
    row = await asyncio.to_thread(_row, inbox, attempt_id, spool_id)
    fields: dict[str, Any] = {}
    if row["counted_g"] != counted:
        fields["counted_g"] = counted
    if layers is not None and (row["first_layer"], row["last_layer"]) != layers:
        fields["first_layer"], fields["last_layer"] = layers
    if fields:
        await asyncio.to_thread(_set, inbox, attempt_id, spool_id, **fields)
        row.update(fields)
    if row["state"] == "done" and counted >= row["written_g"] + WRITE_MINIMUM_G:
        # More is asked of a closed row (a charge the owner placed): reopen and top up.
        await asyncio.to_thread(_set, inbox, attempt_id, spool_id, state="open", reason=None)
        row.update(state="open", reason=None)
    if row["state"] in ("held", "done"):
        return row
    if row["state"] == "sent":
        # The outcome of the last write was never read: witness it first.
        spool = await _read(provider, spool_id)
        if spool is None:
            return row  # Spoolman unreachable: nothing more is sent until it answers
        if isinstance(spool, str):
            return await asyncio.to_thread(_hold, inbox, row, spool)
        row = await asyncio.to_thread(_settle_witness, inbox, row, spool)
    delta = round(counted - row["written_g"], 2)
    if delta < WRITE_MINIMUM_G:
        if delta <= -WRITE_MINIMUM_G:
            # A refill learnt late moves layers to another spool; nothing is taken back.
            log.info("inventory.usage_recount", attempt=attempt_id, spool=spool_id, delta=delta)
        return await asyncio.to_thread(_finish, inbox, row) if final else row
    if not final and row["last_write_at"] and now - row["last_write_at"] < WRITE_INTERVAL_S:
        return row
    before = await _read(provider, spool_id)
    if before is None:
        return row
    if isinstance(before, str):
        return await asyncio.to_thread(_hold, inbox, row, before)
    if before.remaining_weight is not None:
        delta = min(delta, round(before.remaining_weight, 2))  # an empty spool stops at zero
        if delta < WRITE_MINIMUM_G:
            log.info("inventory.usage_spool_empty", attempt=attempt_id, spool=spool_id)
            return await asyncio.to_thread(_finish, inbox, row, "empty") if final else row
    row, after = await _send(provider, inbox, row, before, delta, now)
    if not isinstance(after, ProviderSpool):
        return row  # unreachable (stays "sent", re-verified first next time) or held
    if final and row["written_g"] > counted - WRITE_MINIMUM_G:
        return await asyncio.to_thread(_finish, inbox, row)
    if final and after.remaining_weight is not None and after.remaining_weight < WRITE_MINIMUM_G:
        # The last grams emptied it in Spoolman; the rest is for the owner to place.
        log.info("inventory.usage_spool_empty", attempt=attempt_id, spool=spool_id)
        return await asyncio.to_thread(_finish, inbox, row, "empty")
    return row  # the write did not land: the final settle is tried again


async def _send(
    provider: Any,
    inbox: Any,
    row: dict[str, Any],
    before: ProviderSpool,
    delta: float,
    now: float,
) -> tuple[dict[str, Any], ProviderSpool | str | None]:
    """One signed write (put) between its two reads, witnessed on used_weight.

    Returns the row and the spool read after the write: None when that read
    failed (the row stays "sent" and is witnessed before anything else goes to
    the spool), a hold reason when the spool is gone.
    """
    attempt_id, spool_id = row["attempt_id"], row["spool_id"]
    # The interval counts from the send, not from whenever its outcome is read.
    sent = {
        "state": "sent",
        "sent_before_g": before.used_weight,
        "sent_delta_g": delta,
        "remaining_g": before.remaining_weight,
    }
    await asyncio.to_thread(_set, inbox, attempt_id, spool_id, last_write_at=now, **sent)
    row.update(sent, last_write_at=now)
    try:
        await provider.write("consume", {"spool_id": spool_id, "use_weight": delta})
    except Exception:  # noqa: BLE001 — the verifying read decides what happened
        log.info("inventory.usage_write_unconfirmed", attempt=attempt_id, spool=spool_id)
    after = await _read(provider, spool_id)
    if after is None:
        return row, None
    if isinstance(after, str):
        return await asyncio.to_thread(_hold, inbox, row, after), after
    return await asyncio.to_thread(_settle_witness, inbox, row, after), after


async def credit(
    inventory: Any,
    inbox: Any,
    attempt_id: str,
    spool_id: int,
    grams: float,
    *,
    written_before: float,
    now: float | None = None,
) -> dict[str, Any]:
    """Give whole `grams` of this print back to a spool, witnessed like a charge.

    `written_before` is the row's written_g when the move began: a credit that
    already landed (a retry after a lost read) is seen there and not sent
    again. The caller reads the outcome from the row: written_g down by
    `grams` means landed; "sent" means the read was lost; a reason means the
    spool is gone.
    """
    now = time.time() if now is None else now
    provider = inventory.provider
    row = await asyncio.to_thread(_row, inbox, attempt_id, spool_id)
    if row["state"] == "sent":
        spool = await _read(provider, spool_id)
        if spool is None:
            return row
        if isinstance(spool, str):
            return await asyncio.to_thread(_hold, inbox, row, spool)
        row = await asyncio.to_thread(_settle_witness, inbox, row, spool)
    if row["written_g"] <= written_before - grams + TOLERANCE_G:
        return row  # landed earlier
    before = await _read(provider, spool_id)
    if before is None or isinstance(before, str):
        return row if before is None else await asyncio.to_thread(_hold, inbox, row, before)
    if before.used_weight < grams - TOLERANCE_G:
        raise ValueError(f"#{spool_id} has only {before.used_weight:g} g used in Spoolman")
    row, _ = await _send(provider, inbox, row, before, -float(grams), now)
    if row["written_g"] <= written_before - grams + TOLERANCE_G:
        # The count follows what was given back, so nothing shows as owed.
        counted = max(0.0, round(row["counted_g"] - grams, 2))
        await asyncio.to_thread(_set, inbox, attempt_id, spool_id, counted_g=counted)
        row["counted_g"] = counted
    return row


def raise_counted(inbox: Any, attempt_id: str, spool_id: int, grams: float) -> dict[str, Any]:
    """Make a spool's row owe `grams` more for this print (a move's charge side)."""
    row = _row(inbox, attempt_id, spool_id)
    counted = round(row["counted_g"] + grams, 2)
    _set(inbox, attempt_id, spool_id, counted_g=counted)
    return {**row, "counted_g": counted}


def history(inbox: Any, spool_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """The prints this spool was charged for, newest first: grams, layers and share.

    share_pct is this spool's whole grams over everything the print was
    charged, to the whole percent; ended_at is null while the print runs.
    """
    with inbox.connect() as db:
        rows = db.execute(
            "SELECT l.attempt_id, l.written_g, l.first_layer, l.last_layer, u.layer_reached,"
            " json_extract(u.command,'$.print.subtask_name') AS title,"
            " e.created_at AS ended_at, e.outcome,"
            " json_array_length(m.manifest,'$.layer_grams') AS total_layers,"
            " (SELECT SUM(o.written_g) FROM usage_ledger o WHERE o.attempt_id=l.attempt_id"
            "  AND o.written_g>0) AS total_g"
            " FROM usage_ledger l JOIN uploads u ON u.id=l.attempt_id"
            " LEFT JOIN accounting_shadow_events e ON e.attempt_id=l.attempt_id"
            " LEFT JOIN accounting_manifests m ON m.attempt_id=l.attempt_id"
            " WHERE l.spool_id=? AND l.written_g>0"
            " ORDER BY COALESCE(e.created_at, u.created) DESC, l.attempt_id LIMIT ?",
            (spool_id, limit),
        ).fetchall()
    found = []
    for row in rows:
        grams = round(row["written_g"], 2)
        total = row["total_g"] or 0.0
        found.append(
            {
                "attempt_id": row["attempt_id"],
                "title": row["title"] or "",
                "ended_at": row["ended_at"],
                "capture_id": None,
                "layers": [row["first_layer"], row["last_layer"]]
                if row["first_layer"] is not None
                else None,
                "total_layers": row["total_layers"],
                "grams": grams,
                "share_pct": math.floor(100 * grams / total + 0.5) if total > 0 else 0,
            }
        )
    return found


def moves(inbox: Any, attempt_id: str) -> list[dict[str, Any]]:
    """Every move of this print's grams, oldest first."""
    with inbox.connect() as db:
        rows = db.execute(
            "SELECT * FROM usage_moves WHERE attempt_id=? ORDER BY id", (attempt_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def start_move(
    inbox: Any, attempt_id: str, from_spool: int, to_spool: int, grams: float, before: float
) -> dict[str, Any]:
    with inbox.connect() as db:
        cursor = db.execute(
            "INSERT INTO usage_moves (attempt_id, from_spool, to_spool, grams, state,"
            " from_written_before, started_at) VALUES (?,?,?,?,'started',?,?)",
            (attempt_id, from_spool, to_spool, grams, before, time.time()),
        )
        return dict(
            db.execute("SELECT * FROM usage_moves WHERE id=?", (cursor.lastrowid,)).fetchone()
        )


def set_move(inbox: Any, move_id: int, **fields: Any) -> None:
    columns = ", ".join(f"{key}=?" for key in fields)
    with inbox.connect() as db:
        db.execute(f"UPDATE usage_moves SET {columns} WHERE id=?", (*fields.values(), move_id))


def _settle_witness(inbox: Any, row: dict[str, Any], after: ProviderSpool) -> dict[str, Any]:
    outcome = _witness(row["sent_before_g"], row["sent_delta_g"], after)
    fields: dict[str, Any] = {
        "state": "open",
        "sent_before_g": None,
        "sent_delta_g": None,
        "remaining_g": after.remaining_weight,
    }
    if outcome == "not_applied":
        fields["last_write_at"] = None  # nothing landed: the next tick may try again
    else:
        fields["written_g"] = round(row["written_g"] + row["sent_delta_g"], 2)
    if outcome == "edited":
        # The spool moved by something else: that edit is the truth now, and
        # `use` is relative, so the later deltas stay right.
        fields["record"] = json.dumps(after.model_dump(mode="json"))
    log.info(
        "inventory.usage_written",
        attempt=row["attempt_id"],
        spool=row["spool_id"],
        delta=row["sent_delta_g"],
        outcome=outcome,
    )
    _set(inbox, row["attempt_id"], row["spool_id"], **fields)
    return {**row, **fields}


def shortfalls(inbox: Any, attempt_id: str) -> list[dict[str, Any]] | None:
    """What this print's debit could not settle, as last recorded; None if never."""
    with inbox.connect() as db:
        row = db.execute(
            "SELECT shortfalls FROM usage_shortfalls WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
    return json.loads(row["shortfalls"]) if row else None


def set_shortfalls(inbox: Any, attempt_id: str, found: list[dict[str, Any]]) -> None:
    with inbox.connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO usage_shortfalls VALUES (?,?,?)",
            (attempt_id, json.dumps(found), time.time()),
        )


def _hold(inbox: Any, row: dict[str, Any], reason: str) -> dict[str, Any]:
    fields = {"state": "held", "reason": reason}
    _set(inbox, row["attempt_id"], row["spool_id"], **fields)
    log.info(
        "inventory.usage_held", attempt=row["attempt_id"], spool=row["spool_id"], reason=reason
    )
    return {**row, **fields}


def _finish(inbox: Any, row: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
    """Close the row; `reason` names a shortfall the print's note should carry."""
    fields = {"state": "done", "reason": reason or row.get("reason")}
    _set(inbox, row["attempt_id"], row["spool_id"], **fields)
    return {**row, **fields}
