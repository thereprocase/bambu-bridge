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
    if event_type == "feed_warning":
        since_ms = payload.get("since_ms")
        detail = (
            f"No extrusion for {int(since_ms) // 1000}s — the print may not be feeding."
            if since_ms
            else "The print may not be feeding filament."
        )
        return ("Filament not feeding", detail, payload.get("advice") or "—")
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
    # Catch-all
    return (event_type.replace("_", " ").title(), "—", "—")


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
