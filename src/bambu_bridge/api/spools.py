"""Off-AMS spool cabinet inventory (contract §16 v0-promoted).

User-managed list of spools the operator owns outside the AMS — what the
design's Filament / Cabinet section renders. Bridge does not auto-discover
these; the APK creates / updates / deletes them explicitly.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from bambu_bridge.api import errors
from bambu_bridge.api.auth import require_auth
from bambu_bridge.db.jobs import Spool, SpoolRepo

router = APIRouter(prefix="/spools", tags=["spools"], dependencies=[Depends(require_auth)])


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #


class CreateSpool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    material: str = Field(min_length=1, max_length=40)
    color_hex: str = Field(
        pattern=r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$",
        description="CSS hex color, 6 or 8 digits with leading '#'",
    )
    brand: str | None = Field(default=None, max_length=60)
    total_g: float | None = Field(default=None, ge=0)
    remaining_g: float | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=500)


class UpdateSpool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    material: str | None = Field(default=None, min_length=1, max_length=40)
    color_hex: str | None = Field(
        default=None, pattern=r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$"
    )
    brand: str | None = Field(default=None, max_length=60)
    total_g: float | None = Field(default=None, ge=0)
    remaining_g: float | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=500)


# --------------------------------------------------------------------------- #
# Helper
# --------------------------------------------------------------------------- #


def _spool_repo(request: Request) -> SpoolRepo:
    return SpoolRepo(request.app.state.db)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.get("")
async def list_spools(request: Request) -> Any:
    spools = await _spool_repo(request).list()
    return [s.model_dump(mode="json") for s in spools]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_spool(body: CreateSpool, request: Request) -> Any:
    spool = Spool(
        id=uuid.uuid4().hex,
        name=body.name,
        material=body.material,
        color_hex=body.color_hex,
        brand=body.brand,
        total_g=body.total_g,
        remaining_g=body.remaining_g,
        notes=body.notes,
        added_at=int(time.time()),
    )
    saved = await _spool_repo(request).add(spool)
    return saved.model_dump(mode="json")


@router.get("/{spool_id}")
async def get_spool(spool_id: str, request: Request) -> Any:
    spool = await _spool_repo(request).get(spool_id)
    if spool is None:
        return errors.not_found("spool", spool_id)
    return spool.model_dump(mode="json")


@router.patch("/{spool_id}")
async def update_spool(spool_id: str, body: UpdateSpool, request: Request) -> Any:
    repo = _spool_repo(request)
    existing = await repo.get(spool_id)
    if existing is None:
        return errors.not_found("spool", spool_id)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return existing.model_dump(mode="json")
    updated = await repo.update(spool_id, **fields)
    assert updated is not None
    return updated.model_dump(mode="json")


@router.delete("/{spool_id}")
async def delete_spool(spool_id: str, request: Request) -> Response:
    removed = await _spool_repo(request).delete(spool_id)
    if not removed:
        return errors.not_found("spool", spool_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
