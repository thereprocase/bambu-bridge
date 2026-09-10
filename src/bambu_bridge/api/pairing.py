"""HTTPS-only enrollment and owner-managed device revocation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from bambu_bridge.api.auth import require_owner
from bambu_bridge.pairing import PairingStore

router = APIRouter(prefix="/pairing", tags=["pairing"])


class Claim(BaseModel):
    secret: str = Field(min_length=32, max_length=128, repr=False)
    name: str = Field(min_length=1, max_length=64, pattern=r"^[^\x00-\x1f\x7f]+$")


def store_for(request: Request) -> PairingStore:
    store = getattr(request.app.state, "pairing", None)
    if store is None:
        raise HTTPException(503, "Local pairing is not enabled")
    return store  # type: ignore[no-any-return]


@router.post("/claim")
def claim(body: Claim, request: Request, response: Response) -> dict[str, Any]:
    if request.url.scheme != "https":
        raise HTTPException(403, "Pairing requires HTTPS")
    result = store_for(request).claim(body.secret, body.name.strip())
    if result is None:
        raise HTTPException(401, "Pairing code expired or already used; create a new one")
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/devices", dependencies=[Depends(require_owner)])
def devices(request: Request) -> list[dict[str, Any]]:
    return store_for(request).devices()


@router.delete("/self", status_code=204)
def revoke_self(request: Request) -> Response:
    if request.url.scheme != "https":
        raise HTTPException(403, "Revocation requires HTTPS")
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else None
    store = store_for(request)
    device_id = store.authenticate(token)
    if not device_id:
        raise HTTPException(401, "Paired device credential required")
    store.revoke(device_id)
    return Response(status_code=204)


@router.delete("/devices/{device_id}", status_code=204, dependencies=[Depends(require_owner)])
def revoke(device_id: str, request: Request) -> Response:
    if not store_for(request).revoke(device_id):
        raise HTTPException(404, "Active device not found")
    return Response(status_code=204)
