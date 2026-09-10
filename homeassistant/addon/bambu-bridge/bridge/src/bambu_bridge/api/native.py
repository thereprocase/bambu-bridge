"""Owner-controlled native P1S gateway and repeatable HTTPS code retrieval."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from bambu_bridge.api.orca import secure_owner
from bambu_bridge.native_gateway import NativeGateway
from bambu_bridge.service.registry import PrinterNotFoundError

router = APIRouter(prefix="/native", tags=["native"], dependencies=[Depends(secure_owner)])


def gateway(request: Request) -> NativeGateway:
    instance: NativeGateway | None = request.app.state.native_gateway
    if instance is None:
        raise HTTPException(
            503, "Set BRIDGE_NATIVE_HOST to the server's private/Tailscale IPv4 address"
        )
    return instance


class Enable(BaseModel):
    printer_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


@router.get("")
def status(request: Request) -> dict[str, Any]:
    instance = request.app.state.native_gateway
    return instance.status() if instance else {"configured": False, "enabled": False}


@router.post("")
async def enable(body: Enable, request: Request) -> dict[str, Any]:
    instance = gateway(request)
    try:
        service = request.app.state.registry.get(body.printer_id)
    except PrinterNotFoundError as exc:
        raise HTTPException(404, "Printer not found") from exc
    if service.model and "P1S" not in service.model.upper() and service.model != "C12":
        raise HTTPException(422, "Native P1S mode requires a registered P1S")
    try:
        return await instance.enable(body.printer_id)
    except Exception as exc:
        raise HTTPException(
            503, "Native listener failed; check the server's bind address and ports"
        ) from exc


@router.get("/access-code")
def access_code(request: Request) -> dict[str, str | None]:
    return {"access_code": gateway(request).saved_code()}


@router.delete("", status_code=204)
async def disable(request: Request) -> Response:
    await gateway(request).disable()
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.post("/announce", status_code=204)
async def announce(request: Request) -> Response:
    try:
        if request.client is None:
            raise ValueError("Client address unavailable")
        await gateway(request).announce(request.client.host)
    except (ValueError, OSError) as exc:
        raise HTTPException(
            422, "Use a private IPv4 connection from the computer running Orca"
        ) from exc
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
