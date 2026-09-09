"""Filament memory endpoints (G3).

Per-slot user labels (make/model/profile) for AMS slots. The bridge
remembers them until that slot's tray TYPE changes, then forgets.

All routes are prefixed /printers (shared with files.py etc.) and
require Bearer auth.

Slot numbering: physical_slot is 1-based (1-4), matching the snapshot's
`ams.slots[].physical_slot` field and the P1S touchscreen. array_idx
0-3 is the protocol-level 0-based id; the API speaks physical only.
Verified: `translate._ams_slot` sets `physical_slot = array_idx + 1`,
so valid values the snapshot ever emits are 1-4.
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from bambu_bridge.api.auth import require_auth
from bambu_bridge.api.printers import get_registry
from bambu_bridge.db.jobs import FilamentMemory, FilamentMemoryRepo
from bambu_bridge.service.registry import PrinterNotFoundError, Registry

log = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/printers", tags=["filament"], dependencies=[Depends(require_auth)]
)

# AMS supports 4 slots; physical_slot is 1-based.
_VALID_SLOTS = frozenset({1, 2, 3, 4})
_MAX_FIELD_LEN = 120


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #


class FilamentMemoryPut(BaseModel):
    """Body for PUT /{printer_id}/filament-memory/{slot}.

    All fields optional strings (max 120 chars), but at least one must
    be non-empty — a completely empty PUT is rejected as 422.
    """

    model_config = ConfigDict(extra="forbid")

    make: Annotated[str | None, Field(max_length=_MAX_FIELD_LEN)] = None
    model: Annotated[str | None, Field(max_length=_MAX_FIELD_LEN)] = None
    profile: Annotated[str | None, Field(max_length=_MAX_FIELD_LEN)] = None

    @model_validator(mode="after")
    def at_least_one_field(self) -> FilamentMemoryPut:
        if not any(
            v for v in (self.make, self.model, self.profile) if v is not None and v.strip()
        ):
            raise ValueError("at least one of make, model, profile must be non-empty")
        return self


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _get_db(request: Request) -> FilamentMemoryRepo:
    return FilamentMemoryRepo(request.app.state.db)


def _resolve_printer(registry: Registry, printer_id: str) -> Any:
    try:
        return registry.get(printer_id)
    except PrinterNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"printer {printer_id!r} not found",
        ) from exc


def _check_slot(slot: int) -> int:
    if slot not in _VALID_SLOTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"slot must be one of {sorted(_VALID_SLOTS)} (physical_slot 1-4)",
        )
    return slot


def _memory_to_dict(mem: FilamentMemory) -> dict[str, Any]:
    return {
        "make": mem.make,
        "model": mem.model,
        "profile": mem.profile,
        "tray_type_seen": mem.tray_type_seen,
        "updated_at": mem.updated_at,
    }


def _current_tray_type(service: Any, slot: int) -> str | None:
    """Derive the current tray_type for *slot* from the service's live state.

    Used when writing a label so the memory is bound to what's loaded right now.
    Returns None when the slot is absent or empty.
    """
    from bambu_bridge.service.printer import _per_slot_types  # noqa: PLC0415

    types = _per_slot_types(service._state)  # noqa: SLF001
    return types.get(slot)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.get("/{printer_id}/filament-memory")
async def get_filament_memory(
    printer_id: str,
    registry: Registry = Depends(get_registry),
    db: FilamentMemoryRepo = Depends(_get_db),
) -> dict[str, Any]:
    """Return all stored filament labels for a printer's AMS slots.

    Response: ``{"slots": {"1": {make, model, profile, tray_type_seen,
    updated_at}, …}}``. Slots with no label are absent from the dict.
    """
    _resolve_printer(registry, printer_id)
    mem = await db.get_all(printer_id)
    return {"slots": {str(k): _memory_to_dict(v) for k, v in mem.items()}}


@router.put("/{printer_id}/filament-memory/{slot}", status_code=status.HTTP_200_OK)
async def put_filament_memory(
    printer_id: str,
    slot: int,
    body: FilamentMemoryPut,
    registry: Registry = Depends(get_registry),
    db: FilamentMemoryRepo = Depends(_get_db),
) -> dict[str, Any]:
    """Label what's loaded in an AMS slot.

    ``tray_type_seen`` is bound to the slot's **current** type from the
    live printer state, so the memory knows which material it belongs to.
    The bridge will auto-invalidate this label when the slot's material
    changes to a different non-empty type.

    Returns 404 for unknown printers, 422 for invalid slot or empty body.
    """
    service = _resolve_printer(registry, printer_id)
    _check_slot(slot)

    tray_type = _current_tray_type(service, slot)
    entry = await db.upsert(
        printer_id,
        slot,
        make=body.make,
        model=body.model,
        profile=body.profile,
        tray_type_seen=tray_type,
    )
    # Sync the in-memory cache so the snapshot reflects the new label immediately.
    service.set_filament_memory_entry(slot, entry)
    log.info(
        "filament_memory.set",
        printer_id=printer_id,
        slot=slot,
        tray_type_seen=tray_type,
    )
    return _memory_to_dict(entry)


@router.delete(
    "/{printer_id}/filament-memory/{slot}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_filament_memory(
    printer_id: str,
    slot: int,
    registry: Registry = Depends(get_registry),
    db: FilamentMemoryRepo = Depends(_get_db),
) -> Response:
    """Forget the filament label for one AMS slot.

    Idempotent — deleting a slot with no label is a no-op (204 either way).
    """
    service = _resolve_printer(registry, printer_id)
    _check_slot(slot)
    await db.delete(printer_id, slot)
    service.delete_filament_memory_entry(slot)
    log.info("filament_memory.deleted", printer_id=printer_id, slot=slot)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
