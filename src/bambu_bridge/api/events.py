"""Per-printer event feed (contract §8.4–§8.6).

Three v0 endpoints powering the APK's NotificationsScreen:

* ``GET /api/v1/printers/{id}/events?since&limit&severity`` — flat feed
  of named events (print_started, print_completed, filament_runout, …).
  Skips bookkeeping rows (`state_change`, `job_created`) that the FSM
  writes for its own purposes; those live in the per-job event log at
  ``GET /api/v1/jobs/{id}`` instead.
* ``POST /api/v1/printers/{id}/events/{id}/dismiss`` — mark one row
  dismissed.
* ``POST /api/v1/printers/{id}/events/clear`` — bulk-dismiss everything
  active for this printer.

Each persisted row is rendered into the §8.4 wire shape — title/detail/
context derived from event_type + payload so the APK renders verbatim.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response

from bambu_bridge.api import errors
from bambu_bridge.api.auth import require_auth
from bambu_bridge.api.printers import get_registry
from bambu_bridge.db.jobs import EventRecord, EventRepo
from bambu_bridge.service.registry import PrinterNotFoundError, Registry

router = APIRouter(tags=["events"], dependencies=[Depends(require_auth)])


# Bookkeeping event types that JobRun writes for its own FSM and are not
# user-facing notifications. The per-job event log surfaces them; this
# feed filters them out.
_INTERNAL_EVENT_TYPES = frozenset({"state_change", "job_created"})


def _event_repo(request: Request) -> EventRepo:
    return EventRepo(request.app.state.db)


# --------------------------------------------------------------------------- #
# GET /printers/{id}/events
# --------------------------------------------------------------------------- #


@router.get("/printers/{printer_id}/events")
async def list_printer_events(
    printer_id: str,
    request: Request,
    since: str | None = Query(default=None, description="ISO 8601 inclusive lower bound"),
    until: str | None = Query(default=None, description="ISO 8601 inclusive upper bound"),
    limit: int = Query(default=50, ge=1, le=200),
    severity: str | None = Query(default=None, pattern="^(info|warn|error)$"),
    include_dismissed: bool = Query(default=True),
    registry: Registry = Depends(get_registry),
) -> list[dict[str, Any]]:
    """Flat feed for the NotificationsScreen.

    Returns the §8.4 contract shape: id, ts (ISO), severity, kind, title,
    detail, context, job_id, dismissed. Bookkeeping rows are filtered out.
    """
    try:
        registry.get(printer_id)
    except PrinterNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"printer {printer_id} not found",
        ) from exc

    since_ms = _iso_to_ms(since)
    until_ms = _iso_to_ms(until)
    repo = _event_repo(request)
    # Over-fetch by 4x so the post-filter for internal types can still
    # return up to `limit` user-facing rows in the common case.
    raw = await repo.list_for_printer(
        printer_id,
        limit=limit * 4,
        since_ms=since_ms,
        until_ms=until_ms,
        severity=severity,
        include_dismissed=include_dismissed,
    )
    return [
        _to_wire(r) for r in raw if r.event_type not in _INTERNAL_EVENT_TYPES
    ][:limit]


# --------------------------------------------------------------------------- #
# POST /printers/{id}/events/{id}/dismiss
# --------------------------------------------------------------------------- #


@router.post(
    "/printers/{printer_id}/events/{event_id}/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def dismiss_event(
    printer_id: str,
    event_id: int,
    request: Request,
    registry: Registry = Depends(get_registry),
) -> Response:
    """Mark one event dismissed. Idempotent — re-dismissing the same
    row rewrites the timestamp; the row stays exactly one row."""
    try:
        registry.get(printer_id)
    except PrinterNotFoundError:
        return errors.not_found("printer", printer_id)
    repo = _event_repo(request)
    existing = await repo.get(event_id)
    if existing is None or existing.printer_id != printer_id:
        return errors.not_found("event", str(event_id))
    await repo.dismiss(event_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# POST /printers/{id}/events/clear
# --------------------------------------------------------------------------- #


@router.post(
    "/printers/{printer_id}/events/clear",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def clear_events(
    printer_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
) -> Response:
    """Bulk-dismiss every undismissed event for a printer."""
    try:
        registry.get(printer_id)
    except PrinterNotFoundError:
        return errors.not_found("printer", printer_id)
    await _event_repo(request).dismiss_all_for_printer(printer_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Wire-shape rendering (event_type + payload → §8.4)
# --------------------------------------------------------------------------- #


def _to_wire(rec: EventRecord) -> dict[str, Any]:
    """Render one EventRecord as the §8.4 contract envelope.

    `kind` is the bus event name (which already matches the contract
    enum: print_started, print_completed, filament_runout, etc.).
    Title/detail/context are derived from event_type + payload.
    """
    payload = rec.payload
    title, detail, context = _format(rec.event_type, payload)
    return {
        "id": rec.id,
        "ts": _ms_to_iso(rec.ts),
        "severity": rec.severity or "info",
        "kind": rec.event_type,
        "title": title,
        "detail": detail,
        "context": context,
        "job_id": rec.job_id,
        "dismissed": rec.dismissed_at is not None,
    }


def _format(event_type: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    """Static title/detail/context renderers per event_type.

    Pre-formatted English. i18n is v0.1. Each renderer is total — when a
    payload field is missing the string degrades gracefully.
    """
    subtask = payload.get("subtask_name") or ""
    if event_type == "print_started":
        return ("Print started", subtask or "—", _layer_context(payload))
    if event_type == "print_completed":
        return ("Print completed", subtask or "—", _layer_context(payload))
    if event_type == "print_failed":
        err = payload.get("print_error")
        err = err if isinstance(err, dict) else {}
        return (
            "Print failed",
            err.get("text") or "Print failed",
            f"Code: {err.get('code') or '—'}",
        )
    if event_type == "print_progress":
        layer = payload.get("layer_num")
        total = payload.get("total_layer_num")
        return (
            "Printing — layer 1",
            f"Layer {layer}/{total}" if total else f"Layer {layer}",
            subtask or "—",
        )
    if event_type == "filament_runout":
        slot = payload.get("slot")
        where = f"Slot {slot}" if slot is not None else "The active spool"
        return (
            "Filament runout",
            f"{where} ran out — swap the spool and resume.",
            f"Code: {payload.get('code') or '—'}",
        )
    if event_type == "error":
        err = payload.get("print_error")
        err = err if isinstance(err, dict) else {}
        return (
            "Printer error",
            err.get("text") or "Printer reported an error",
            f"Code: {err.get('code') or '—'}",
        )
    if event_type == "connection_lost":
        return ("Connection lost", "The bridge can't reach the printer.", "—")
    if event_type == "connection_restored":
        return ("Reconnected", "Telemetry is flowing again.", "—")
    if event_type == "cert_changed":
        return (
            "Printer security key changed",
            "The printer's TLS certificate rotated. Re-trust to resume.",
            "POST /printers/{id}/trust",
        )
    if event_type == "cert_trusted":
        return ("Printer re-trusted", "Security key pin updated.", "—")
    if event_type == "slot_colour_set":
        slot, spool = payload.get("slot_label") or "A slot", payload.get("spool_id")
        return (payload.get("text") or f"{slot} colour set to match #{spool}", "—", "—")
    if event_type == "spool_adopted":
        spool, slot = payload.get("spool_id"), payload.get("slot_label") or "a slot"
        name = payload.get("name") or "Spool"
        text = payload.get("text") or (
            f"New spool #{spool} · {name} · {slot} (from its RFID tag)"
            if payload.get("new")
            else f"Spool #{spool} · {name} · {slot} (matched by its RFID tag)"
        )
        return (text, "—", "—")
    if event_type in _SPOOL_RENDERERS:
        return _SPOOL_RENDERERS[event_type](payload)
    # Catch-all
    return (event_type.replace("_", " ").title(), "—", "—")


# --------------------------------------------------------------------------- #
# Spool activity: which spool sits in which slot, and what each print charged
# --------------------------------------------------------------------------- #


def _spool(payload: dict[str, Any]) -> str:
    spool = payload.get("spool_id")
    return f"#{spool}" if spool is not None else "a spool"


def _slot(payload: dict[str, Any]) -> str:
    # Titles start with the slot; slot_label() reads "the external holder".
    label = str(payload.get("slot_label") or "A slot")
    return label[:1].upper() + label[1:]


def _spool_named(payload: dict[str, Any]) -> str:
    name = str(payload.get("name") or "").strip()
    return _spool(payload) + (f" {name}" if name else "")


def _grams(value: Any) -> str:
    return f"{float(value):.1f} g" if isinstance(value, int | float) else "—"


def _spool_assigned(payload: dict[str, Any]) -> tuple[str, str, str]:
    # The app and the owner's scripts send the same request, so "by hand" is all
    # the bridge can say about who made the link.
    detail = " · ".join(
        part
        for part in (
            "Linked by hand",
            payload.get("material") or "",
            f"moved from {payload['moved_from']}" if payload.get("moved_from") else "",
        )
        if part
    )
    return (f"{_slot(payload)}: {_spool_named(payload)} linked", detail, "—")


def _spool_unassigned(payload: dict[str, Any]) -> tuple[str, str, str]:
    return (f"{_slot(payload)}: {_spool(payload)} unlinked", "Unlinked by hand", "—")


def _spool_released(payload: dict[str, Any]) -> tuple[str, str, str]:
    return (
        f"{_slot(payload)}: {_spool(payload)} released",
        payload.get("reason") or "The AMS no longer holds it.",
        "—",
    )


def _assignment_refused(payload: dict[str, Any]) -> tuple[str, str, str]:
    return (
        f"{_slot(payload)} couldn't take {_spool_named(payload)}",
        payload.get("text") or "The bridge refused the assignment.",
        "—",
    )


def _slot_set(payload: dict[str, Any]) -> tuple[str, str, str]:
    material = payload.get("material") or "?"
    before = payload.get("previous_material")
    detail = f"Was {before}" if before else "—"
    return (f"{_slot(payload)} set to {material} for {_spool_named(payload)}", detail, "—")


def _charges(payload: dict[str, Any]) -> str:
    charged = payload.get("charged") or []
    parts = [
        f"#{c.get('spool_id')} −{_grams(c.get('grams'))}"
        for c in charged
        if isinstance(c, dict)
    ]
    return ", ".join(parts) or "nothing charged"


def _close_note(note: str) -> str:
    """A note the bridge kept on a charged print, in words (see deliver_once)."""
    if m := re.fullmatch(r"refill_inferred_slot_(\d+)_to_(\d+)", note):
        return f"Slot {m[1]} ran out and slot {m[2]} finished the print."
    if m := re.fullmatch(r"tag_changed_slot_(\d+)", note):
        return f"Slot {int(m[1]) + 1} held a different tagged spool by the end."
    if note.startswith("over_charged_"):
        return "Spoolman was charged more than the print used; check the spool's weight."
    return note.replace("_", " ").capitalize()


def _usage_charged(payload: dict[str, Any]) -> tuple[str, str, str]:
    title = payload.get("title") or "Print"
    planned = payload.get("planned_g")
    parts = [f"Planned {_grams(planned)}" if planned is not None else ""]
    parts += [_close_note(str(n)) for n in payload.get("notes") or []]
    return (f"{title}: {_charges(payload)}", " · ".join(p for p in parts if p) or "—", "—")


_UNTRACED = "The bridge couldn't tell which spool fed part of it."
_REVIEW_REASONS = {
    "unassigned_slot": "A slot it used had no spool linked.",
    "spool_archived": "A spool it used is archived in Spoolman.",
    "spool_missing": "A spool it used is missing from Spoolman.",
    "spool_empty": "A spool it used was already empty.",
    "unmapped_filament": _UNTRACED,
    "unweighed_filament": _UNTRACED,
    "no_layer_plan": _UNTRACED,
    "manifest_predates_layer_plan": _UNTRACED,
    "delivery_error": "Spoolman didn't confirm the charge.",
}


def _usage_review(payload: dict[str, Any]) -> tuple[str, str, str]:
    title = payload.get("title") or "Print"
    reason = _REVIEW_REASONS.get(str(payload.get("reason") or ""), "Some filament wasn't charged.")
    return (
        f"{title}: charge needs review",
        f"{reason} Charge or skip it on the Filament tab.",
        f"Charged so far: {_charges(payload)}",
    )


_SPOOL_RENDERERS = {
    "spool_assigned": _spool_assigned,
    "spool_unassigned": _spool_unassigned,
    "spool_released": _spool_released,
    "assignment_refused": _assignment_refused,
    "slot_set": _slot_set,
    "usage_charged": _usage_charged,
    "usage_review": _usage_review,
}


def _layer_context(payload: dict[str, Any]) -> str:
    layer = payload.get("layer_num")
    total = payload.get("total_layer_num")
    if total:
        return f"Layer {layer or 0}/{total}"
    if layer is not None:
        return f"Layer {layer}"
    return "—"


def _ms_to_iso(ts_ms: int) -> str:
    return (
        datetime.fromtimestamp(ts_ms / 1000, UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _iso_to_ms(iso: str | None) -> int | None:
    if iso is None:
        return None
    s = iso.rstrip("Z")
    try:
        dt = datetime.fromisoformat(s + "+00:00") if "+" not in s else datetime.fromisoformat(iso)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid ISO timestamp: {iso}",
        ) from exc
    return int(dt.timestamp() * 1000)
