"""A held print's one action: charge it, charge nothing, or mark it charged by hand."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from bambu_bridge.api.auth import require_auth
from bambu_bridge.api.inventory import captures_for, get_inventory
from bambu_bridge.service import usage_ledger, usage_review
from bambu_bridge.service.inventory import InventoryService

router = APIRouter(
    prefix="/inventory/accounting", tags=["inventory"], dependencies=[Depends(require_auth)]
)

AttemptId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


class Charge(BaseModel):
    """A spool and grams, or a slot and the spool that was in it (grams from the count)."""

    model_config = ConfigDict(extra="forbid")
    spool_id: int = Field(gt=0, lt=2**31, strict=True)
    grams: float | None = Field(default=None, gt=0, le=10_000)
    slot: int | None = Field(default=None, ge=0, le=254, strict=True)

    @model_validator(mode="after")
    def _one_form(self) -> Charge:
        if (self.grams is None) == (self.slot is None):
            raise ValueError("give either grams or the slot the spool was in")
        return self


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["charge", "nothing", "charged_elsewhere"]
    charges: list[Charge] = []


def _inbox(request: Request) -> Any:
    inbox = getattr(getattr(request.app.state, "native_gateway", None), "inbox", None)
    if inbox is None:
        raise HTTPException(503, "The print record is not available on this bridge")
    return inbox


def _printer_for(request: Request) -> Any:
    return request.app.state.registry.get


@router.get("/{attempt_id}")
async def held_print(
    attempt_id: AttemptId,
    request: Request,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    try:
        body = await usage_review.detail(
            service, _inbox(request), _printer_for(request), attempt_id
        )
    except usage_review.ReviewError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    return await _with_capture(request, body)


@router.post("/{attempt_id}/resolve")
async def resolve_held_print(
    attempt_id: AttemptId,
    body: Resolution,
    request: Request,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    charges = [c.model_dump(exclude_none=True) for c in body.charges]
    try:
        result = await usage_review.resolve(
            service, _inbox(request), _printer_for(request), attempt_id, body.action, charges
        )
    except usage_review.ReviewError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    return await _with_capture(request, result)


async def _with_capture(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    captures = await captures_for(request, [body["attempt_id"]])
    body["capture_id"] = captures.get(body["attempt_id"])
    return body


class Reassign(BaseModel):
    """Move this print's grams from one spool to another; grams defaults to all of them."""

    model_config = ConfigDict(extra="forbid")
    from_spool_id: int = Field(gt=0, lt=2**31, strict=True)
    to_spool_id: int = Field(gt=0, lt=2**31, strict=True)
    grams: float | None = Field(default=None, gt=0, le=10_000)


@router.post("/{attempt_id}/reassign")
async def reassign_print(
    attempt_id: AttemptId,
    body: Reassign,
    request: Request,
    service: Annotated[InventoryService, Depends(get_inventory)],
) -> dict[str, Any]:
    try:
        result = await usage_review.reassign(
            service,
            _inbox(request),
            _printer_for(request),
            attempt_id,
            body.from_spool_id,
            body.to_spool_id,
            body.grams,
        )
    except usage_review.ReviewError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    return await _with_capture(request, result)


spool_router = APIRouter(
    prefix="/inventory/spools", tags=["inventory"], dependencies=[Depends(require_auth)]
)


@spool_router.get("/{spool_id}/history")
async def spool_history(
    spool_id: Annotated[int, Path(gt=0, lt=2**31)],
    request: Request,
    service: Annotated[InventoryService, Depends(get_inventory)],
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """The prints this spool was charged for, newest first, with layers and share."""
    inbox = _inbox(request)
    prints = await asyncio.to_thread(usage_ledger.history, inbox, spool_id, limit)
    captures = await captures_for(request, [p["attempt_id"] for p in prints])
    for entry in prints:
        entry["capture_id"] = captures.get(entry["attempt_id"])
    return {"spool_id": spool_id, "prints": prints}
