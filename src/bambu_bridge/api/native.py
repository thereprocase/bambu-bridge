"""Administrator-controlled native P1S gateway and recovery actions."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import uuid
import zipfile
from typing import Any, Literal, cast

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from bambu_bridge.api.auth import require_auth
from bambu_bridge.native_gateway import NativeGateway
from bambu_bridge.native_recovery import (
    MAX_BACKUP_BYTES,
    backup_path,
    backups_root,
    create_backup,
    list_backups,
    stage_backup,
)
from bambu_bridge.native_setup import setup_material
from bambu_bridge.service.registry import PrinterNotFoundError


def secure_manager(request: Request, response: Response, _: None = Depends(require_auth)) -> None:
    """Owner key and paired phones have the same administrator authority."""
    if request.url.scheme != "https":
        raise HTTPException(403, "Use HTTPS to manage native access and recovery")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


router = APIRouter(prefix="/native", tags=["native"], dependencies=[Depends(secure_manager)])
log = structlog.get_logger(__name__)


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


class RestoreAction(BaseModel):
    confirm: Literal["Restore native inbox and review every pending start"]


class AcknowledgePastReviews(BaseModel):
    confirm: Literal["Ignore past review warnings; keep active starts"]


@router.post("/readiness")
async def readiness(request: Request) -> dict[str, Any]:
    """Exercise the start guard with status traffic only; never enqueue a print."""
    instance = gateway(request)
    async with instance.inbox_dispatch_lock:
        try:
            await instance.ensure_idle(force_refresh=True)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        state = instance.service().native_snapshot().get("print", {}).get("gcode_state")
    return {"ready": True, "gcode_state": state, "print_commands_sent": 0}


@router.post("/uploads/{identifier}")
async def upload_action(identifier: str, body: UploadAction, request: Request) -> dict[str, Any]:
    instance = gateway(request)
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise HTTPException(404, "Upload not found")
    async with instance.change_lock, instance.inbox_dispatch_lock:
        inbox = instance.inbox
        if inbox is None:
            raise HTTPException(409, "Durable inbox is not enabled")
        try:
            row = await asyncio.to_thread(inbox.get, identifier)
            if row["printer"] != cast(dict[str, str], instance.config)["printer_id"]:
                raise HTTPException(404, "Upload not found")
            if body.action != "cancel":
                instance.require_idle()
            method = getattr(inbox, body.action)
            await asyncio.to_thread(method, identifier)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        instance.inbox_wake.set()
        instance.inbox_status = await asyncio.to_thread(inbox.status)
    return {"uploads": instance.inbox_status}


@router.get("")
def status(request: Request) -> dict[str, Any]:
    instance = request.app.state.native_gateway
    return instance.status() if instance else {"configured": False, "enabled": False}


@router.get("/queue")
async def queue_status(
    request: Request,
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
    view: Literal["all", "review", "active"] = Query("all"),
) -> dict[str, Any]:
    """Fresh receipt state for dashboard and paired-phone recovery screens."""
    instance = gateway(request)
    async with instance.change_lock:
        inbox, config = instance.inbox, instance.config
        if inbox is None or config is None:
            raise HTTPException(409, "Durable inbox is not enabled")
        uploads = await asyncio.to_thread(
            inbox.status, printer=config["printer_id"], limit=limit + 1, offset=offset,
            view=view,
        )
        has_more = len(uploads) > limit
        uploads = uploads[:limit]
        overview = await asyncio.to_thread(inbox.queue_overview, config["printer_id"])
    return {
        "printer_id": config["printer_id"],
        **overview,
        "view": view,
        "offset": offset,
        "has_more": has_more,
        "uploads": uploads,
    }


@router.post("/queue/acknowledge")
async def acknowledge_past_reviews(
    body: AcknowledgePastReviews, request: Request
) -> dict[str, Any]:
    """Dismiss historical warnings without resolving or hiding active starts."""
    instance = recovery_inbox(request)
    async with instance.change_lock:
        assert instance.inbox is not None and instance.config is not None
        printer = instance.config["printer_id"]
        count = await asyncio.to_thread(instance.inbox.acknowledge_past_reviews, printer)
        overview = await asyncio.to_thread(instance.inbox.queue_overview, printer)
    return {"acknowledged": count, **overview}


def recovery_inbox(request: Request) -> NativeGateway:
    instance = gateway(request)
    if instance.inbox is None or instance.config is None:
        raise HTTPException(409, "Durable inbox is not enabled")
    return instance


@router.get("/recovery/backups")
async def recovery_backups(request: Request) -> list[dict[str, Any]]:
    instance = recovery_inbox(request)
    return await asyncio.to_thread(list_backups, instance.store.directory)


@router.post("/recovery/backups", status_code=201)
async def recovery_backup(request: Request) -> dict[str, Any]:
    instance = recovery_inbox(request)
    async with instance.change_lock:
        inbox, config = instance.inbox, instance.config
        if inbox is None or config is None:
            raise HTTPException(409, "Durable inbox is not enabled")
        try:
            return await asyncio.to_thread(
                create_backup,
                inbox,
                instance.store.directory,
                config["printer_id"],
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc


@router.get("/recovery/backups/{identifier}")
async def recovery_download(identifier: str, request: Request) -> FileResponse:
    instance = recovery_inbox(request)
    try:
        path = backup_path(instance.store.directory, identifier)
    except ValueError as exc:
        raise HTTPException(404, "Backup not found") from exc
    if not path.is_file() or path.is_symlink():
        raise HTTPException(404, "Backup not found")
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"native-inbox-{identifier}.zip",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.delete("/recovery/backups/{identifier}", status_code=204)
async def recovery_delete(identifier: str, request: Request) -> Response:
    instance = recovery_inbox(request)
    async with instance.change_lock:
        try:
            path = backup_path(instance.store.directory, identifier)
        except ValueError as exc:
            raise HTTPException(404, "Backup not found") from exc
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, "Backup not found")
        await asyncio.to_thread(path.unlink)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.post("/recovery/backups/import", status_code=201)
async def recovery_import(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    instance = recovery_inbox(request)
    assert instance.config is not None
    identifier = uuid.uuid4().hex
    path = backup_path(instance.store.directory, identifier)
    partial = backups_root(instance.store.directory) / f".{identifier}.partial"
    written = 0
    try:
        fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as output:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_BACKUP_BYTES:
                    raise ValueError("Backup exceeds the allowed size")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        staged = await asyncio.to_thread(
            stage_backup, instance.store.directory, identifier, instance.config["printer_id"],
            source_path=partial,
        )
        await asyncio.to_thread(shutil.rmtree, staged.parent)
        await asyncio.to_thread(os.replace, partial, path)
        return {"id": identifier, "bytes": written}
    except asyncio.CancelledError:
        partial.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise
    except (
        OSError, ValueError, KeyError, TypeError, sqlite3.DatabaseError, zipfile.BadZipFile
    ) as exc:
        partial.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise HTTPException(409, str(exc)) from exc
    finally:
        await file.close()


@router.post("/recovery/backups/{identifier}/restore")
async def recovery_restore(
    identifier: str, body: RestoreAction, request: Request
) -> dict[str, Any]:
    instance = recovery_inbox(request)
    assert instance.config is not None
    async with instance.change_lock, instance.inbox_dispatch_lock:
        inbox = instance.inbox
        if inbox is None:
            raise HTTPException(409, "Durable inbox is not enabled")
        staged = None
        try:
            staged = await asyncio.to_thread(
                stage_backup, instance.store.directory, identifier, instance.config["printer_id"]
            )
            safety = await asyncio.to_thread(
                create_backup, inbox, instance.store.directory, instance.config["printer_id"]
            )
            active = inbox.directory
            rollback = backups_root(instance.store.directory) / f"rollback-{uuid.uuid4().hex}"
            await instance.close()
            try:
                await asyncio.to_thread(os.replace, active, rollback)
            except OSError:
                await instance.start()
                raise
            try:
                await asyncio.to_thread(os.replace, staged, active)
                await instance.start()
                try:
                    await asyncio.to_thread(shutil.rmtree, rollback)
                except OSError:
                    log.warning("native.restore_cleanup_failed", rollback=str(rollback))
            except Exception as exc:
                await instance.close()
                failed = (
                    backups_root(instance.store.directory) / f"failed-restore-{uuid.uuid4().hex}"
                )
                if active.exists():
                    await asyncio.to_thread(os.replace, active, failed)
                await asyncio.to_thread(os.replace, rollback, active)
                await instance.start()
                raise ValueError("Restore could not start; previous inbox was reinstated") from exc
        except (
            OSError, ValueError, KeyError, TypeError, sqlite3.DatabaseError, zipfile.BadZipFile
        ) as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            if staged is not None:
                await asyncio.to_thread(shutil.rmtree, staged.parent, ignore_errors=True)
    return {"restored": identifier, "safety_backup": safety["id"]}


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
