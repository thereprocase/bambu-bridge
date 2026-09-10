"""HTTPS-only enrollment and owner-managed device revocation."""

from __future__ import annotations

import io
import json
from typing import Annotated, Any, Literal

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import BaseModel, Field

from bambu_bridge.api.auth import require_owner
from bambu_bridge.pairing import PairingStore, digest, invitation_payload, validate_base

router = APIRouter(prefix="/pairing", tags=["pairing"])


class Claim(BaseModel):
    secret: str = Field(min_length=32, max_length=128, repr=False)
    name: str = Field(min_length=1, max_length=64, pattern=r"^[^\x00-\x1f\x7f]+$")


def store_for(request: Request) -> PairingStore:
    store = getattr(request.app.state, "pairing", None)
    if store is None:
        raise HTTPException(503, "Local pairing is not enabled")
    return store  # type: ignore[no-any-return]


def secure_owner(request: Request, response: Response, _: None = Depends(require_owner)) -> None:
    if request.url.scheme != "https":
        raise HTTPException(403, "Open the dashboard over HTTPS to manage phone pairing")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Authorization"
    response.headers["Referrer-Policy"] = "no-referrer"


def targets_for(request: Request) -> list[dict[str, str]]:
    # Do not trust Host/Forwarded-Host, or pair a reverse proxy's certificate
    # with the bridge's key. Only host-configured direct listener URLs are used.
    from bambu_bridge.local_server import local_url

    settings = request.app.state.settings
    try:
        local = validate_base(settings.bridge_pairing_url or local_url(settings))
        targets = [{"id": "local", "label": "Home Wi-Fi", "base_url": local}]
        if settings.bridge_pairing_remote_url:
            targets.append(
                {
                    "id": "remote",
                    "label": "Tailscale / away from home",
                    "base_url": validate_base(settings.bridge_pairing_remote_url),
                }
            )
        return targets
    except (ValueError, OSError) as exc:
        raise HTTPException(
            409, "Pairing addresses need to be configured on the bridge host"
        ) from exc


class InvitationRequest(BaseModel):
    target: Literal["local", "remote"] = "local"


@router.get("/options", dependencies=[Depends(secure_owner)])
def options(request: Request) -> dict[str, Any]:
    store_for(request)
    return {"targets": targets_for(request)}


@router.post("/invitations", dependencies=[Depends(secure_owner)])
def invite(body: InvitationRequest, request: Request) -> dict[str, Any]:
    store = store_for(request)
    target = next((t for t in targets_for(request) if t["id"] == body.target), None)
    if target is None:
        raise HTTPException(409, "Remote pairing is not configured on this bridge")
    payload = invitation_payload(store, target["base_url"])
    data = json.loads(payload)
    image = qrcode.make(payload, image_factory=qrcode.image.svg.SvgPathImage)
    svg = io.BytesIO()
    image.save(svg)
    return {
        "id": digest(data["secret"]),
        "expires": data["expires"],
        "payload": payload,
        "qr_svg": svg.getvalue().decode(),
        "base_url": target["base_url"],
    }


InvitationId = Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")]


@router.get("/invitations/{invitation_id}", dependencies=[Depends(secure_owner)])
def invitation_status(invitation_id: InvitationId, request: Request) -> dict[str, bool]:
    return {"active": store_for(request).invitation_active(invitation_id)}


@router.delete(
    "/invitations/{invitation_id}", status_code=204, dependencies=[Depends(secure_owner)]
)
def cancel_invitation(invitation_id: InvitationId, request: Request) -> Response:
    store_for(request).cancel_invitation(invitation_id)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.post("/claim")
def claim(body: Claim, request: Request, response: Response) -> dict[str, Any]:
    if request.url.scheme != "https":
        raise HTTPException(403, "Pairing requires HTTPS")
    result = store_for(request).claim(body.secret, body.name.strip())
    if result is None:
        raise HTTPException(401, "Pairing code expired or already used; create a new one")
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/devices", dependencies=[Depends(secure_owner)])
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


@router.delete("/devices/{device_id}", status_code=204, dependencies=[Depends(secure_owner)])
def revoke(device_id: str, request: Request) -> Response:
    if not store_for(request).revoke(device_id):
        raise HTTPException(404, "Active device not found")
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
