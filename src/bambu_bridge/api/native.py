"""Owner-controlled native P1S gateway and repeatable HTTPS code retrieval."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from bambu_bridge.api.orca import secure_owner
from bambu_bridge.native_gateway import NativeGateway
from bambu_bridge.native_setup import setup_material
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


class ExistingCode(BaseModel):
    access_code: str = Field(min_length=8, max_length=8, pattern=r"^[A-Za-z0-9]+$")


class UploadAction(BaseModel):
    action: Literal["resolve", "retry_delivery", "discard", "cancel"]
    confirm: Literal["I checked the printer and this action"]


@router.post("/uploads/{identifier}")
async def upload_action(identifier: str, body: UploadAction, request: Request) -> dict[str, Any]:
    instance = gateway(request)
    if instance.inbox is None:
        raise HTTPException(409, "Durable inbox is not enabled")
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise HTTPException(404, "Upload not found")
    async with instance.inbox_dispatch_lock:
        try:
            row = await asyncio.to_thread(instance.inbox.get, identifier)
            if row["printer"] != instance.config["printer_id"]:
                raise HTTPException(404, "Upload not found")
            if body.action != "cancel":
                instance.require_idle()
            method = getattr(instance.inbox, body.action)
            await asyncio.to_thread(method, identifier)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    instance.inbox_wake.set()
    instance.inbox_status = await asyncio.to_thread(instance.inbox.status)
    return {"uploads": instance.inbox_status}


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
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            503, "Native listener failed; check the server's bind address and ports"
        ) from exc


@router.get("/access-code")
def access_code(request: Request) -> dict[str, str | None]:
    return {"access_code": gateway(request).saved_code()}


@router.get("/setup")
async def setup(request: Request) -> dict[str, str]:
    try:
        return setup_material(gateway(request))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            503, "Could not read the native certificate. Check bridge storage."
        ) from exc


@router.get("/setup-status")
async def setup_status(request: Request) -> dict[str, bool]:
    return gateway(request).setup_status(request.client.host if request.client else "")


@router.post("/access-code")
async def save_access_code(body: ExistingCode, request: Request) -> dict[str, str]:
    instance = gateway(request)
    peer = "owner:" + (request.client.host if request.client else "dashboard")
    if not await instance.authenticate("bblp", body.access_code, peer):
        raise HTTPException(422, "That code does not match the active native code")
    saved = instance.saved_code()
    if saved is None:
        raise HTTPException(503, "Could not save the code; check pairing storage")
    return {"access_code": saved}


@router.delete("", status_code=204)
async def disable(request: Request) -> Response:
    try:
        await gateway(request).disable()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
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
