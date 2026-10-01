"""The small OctoPrint upload contract used by stock OrcaSlicer 2.4.2.

This is a print-host adapter, not an OctoPrint server or a native Bambu DLL.
Upload never implies print. A scoped key must explicitly permit a fixed AMS
mapping before upload-and-print can enter the existing validated job lifecycle.
"""

from __future__ import annotations

import asyncio
import io
import re
import secrets
import zipfile
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from bambu_bridge import __version__
from bambu_bridge.api import errors
from bambu_bridge.api.auth import require_owner
from bambu_bridge.api.capabilities import reported_nozzle
from bambu_bridge.api.uploads import read_upload
from bambu_bridge.orca import OrcaStore
from bambu_bridge.protocol.commands import AmsMapping
from bambu_bridge.protocol.ftps import FtpsTransfer
from bambu_bridge.service.registry import PrinterNotFoundError
from bambu_bridge.slicedoc import validate

management = APIRouter(prefix="/orca/clients", tags=["orca"])
host = APIRouter(prefix="/orca/{printer_id}", tags=["orca"])


def store_for(request: Request) -> OrcaStore:
    store: OrcaStore | None = request.app.state.orca
    if store is None:
        raise HTTPException(503, "Enable local pairing on the bridge to manage slicer keys")
    return store


def secure_owner(request: Request, response: Response, _: None = Depends(require_owner)) -> None:
    if request.url.scheme != "https":
        raise HTTPException(403, "Open the dashboard over HTTPS to manage slicer keys")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


def printer_for(request: Request, printer_id: str) -> Any:
    try:
        return request.app.state.registry.get(printer_id)
    except PrinterNotFoundError as exc:
        raise HTTPException(404, "Printer not found") from exc


class ClientRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[^\x00-\x1f\x7f]+$")
    printer_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    # null = upload only; [] = external spool; else Orca's ams_mapping: one
    # tray (0 = slot 1) or -1 (unused) per project filament.
    ams_mapping: AmsMapping | None = None


@management.get("", dependencies=[Depends(secure_owner)])
def clients(request: Request) -> list[dict[str, Any]]:
    return store_for(request).clients()


@management.post("", status_code=201, dependencies=[Depends(secure_owner)])
def create_client(body: ClientRequest, request: Request) -> dict[str, Any]:
    printer_for(request, body.printer_id)
    result = store_for(request).create(body.name, body.printer_id, body.ams_mapping)
    # Relative paths cannot be poisoned by a request Host/Forwarded-Host header.
    result["host_path"] = "/orca/" + quote(body.printer_id, safe="")
    return result


@management.delete("/{client_id}", status_code=204, dependencies=[Depends(secure_owner)])
def revoke_client(client_id: str, request: Request) -> Response:
    store_for(request).revoke(client_id)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


def require_slicer(
    printer_id: str,
    request: Request,
    response: Response,
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    if request.url.scheme != "https":
        raise HTTPException(403, "Use HTTPS with a trusted certificate for the Orca print host")
    client = store_for(request).authenticate(x_api_key, printer_id)
    if client is None:
        raise HTTPException(401, "Invalid, revoked, or wrong-printer slicer key")
    printer_for(request, printer_id)
    response.headers["Cache-Control"] = "no-store"
    return client


@host.get("/api/version", dependencies=[Depends(require_slicer)])
def version() -> dict[str, str]:
    return {"api": "0.1", "server": __version__, "text": "OctoPrint compatible Bambu Bridge"}


@host.get("", include_in_schema=False)
@host.get("/", include_in_schema=False)
def dashboard() -> RedirectResponse:
    return RedirectResponse("/app", status_code=303)


def validate_plate(data: bytes, plateindex: int) -> None:
    """Reject unsupported selections instead of silently starting plate 1.

    Archive bounds are validate()'s G1. Files are kept byte-for-byte; no tool
    changes or plate renumbering occur.
    """
    if plateindex != 1:
        raise HTTPException(422, "This preview sends plate 1 only. Move the plate to position 1.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            plates = [n for n in names if re.fullmatch(r"Metadata/plate_\d+\.gcode", n)]
            if plates != ["Metadata/plate_1.gcode"]:
                raise HTTPException(422, "Export one sliced plate at position 1 for this preview")
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        raise HTTPException(422, "Upload a sliced .gcode.3mf file from OrcaSlicer") from exc


@host.post("/api/files/local", status_code=201)
async def upload(
    printer_id: str,
    request: Request,
    file: UploadFile,
    print_now: bool = Form(default=False, alias="print"),
    path: str = Form(default=""),
    plateindex: int = Form(default=1),
    client: dict[str, Any] = Depends(require_slicer),
) -> dict[str, Any]:
    if path not in ("", "/"):
        raise HTTPException(422, "Leave the upload folder empty; Bambu files use the SD card root")
    name = file.filename or ""
    if (
        not name.lower().endswith(".gcode.3mf")
        or len(name) > 160
        or any(c in name for c in "/\\:?#")
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
    ):
        raise HTTPException(422, "Use a plain filename ending in .gcode.3mf")
    mapping = client["ams_mapping"]
    if print_now and mapping is None:
        raise HTTPException(
            403,
            "This key allows upload only. Start later at the printer, "
            "or create a key with an explicit filament mapping in Settings.",
        )
    data = await read_upload(file)
    await asyncio.to_thread(validate_plate, data, plateindex)
    # Orca checks the sliced nozzle against the printer before it starts a job.
    nozzle = reported_nozzle(printer_for(request, printer_id)) if print_now else None
    # mapping: None = upload only (no start); [] = external spool, single filament.
    report = await asyncio.to_thread(
        validate,
        data,
        expected_ams_mapping=mapping if print_now else None,
        expected_nozzle=nozzle,
    )
    if not report.ok:
        raise HTTPException(422, "3MF validation failed: " + "; ".join(report.issues))
    if store_for(request).authenticate(request.headers.get("x-api-key"), printer_id) is None:
        raise HTTPException(401, "Slicer key was revoked during upload; nothing was sent")
    service = printer_for(request, printer_id)
    if errors.cert_gate(service) is not None:
        raise HTTPException(403, "Printer certificate changed; review it in bridge Settings first")
    if not service.connected:
        raise HTTPException(409, "Printer is offline; nothing was sent")
    # A unique name avoids replacing a file being printed or a prior upload.
    stored_name = name[:-10] + "-" + secrets.token_hex(4) + ".gcode.3mf"
    result: dict[str, Any] = {
        "files": {"local": {"name": stored_name, "path": stored_name, "origin": "local"}},
        "done": True,
    }
    if print_now:
        jobs = request.app.state.jobs
        if service.summary().get("gcode_state") not in ("IDLE", "FINISH", "FAILED"):
            raise HTTPException(409, "Printer is busy or its idle state is not yet known")
        if any(not j.state.terminal for j in await jobs.history(printer_id=printer_id, limit=200)):
            raise HTTPException(409, "A bridge job is already active for this printer")
        # Serialize concurrent Orca submissions across the queued-row DB write.
        async with request.app.state.orca_submit_lock:
            if any(
                not j.state.terminal for j in await jobs.history(printer_id=printer_id, limit=200)
            ):
                raise HTTPException(409, "A bridge job is already active for this printer")
            job = await jobs.submit(printer_id, data, stored_name, ams_mapping=mapping or None)
        result.update(job_id=job.id, bridge_state="queued")
    else:
        try:
            await FtpsTransfer(
                service.ip, service.access_code, port=request.app.state.ftps_port
            ).upload_bytes(data, stored_name, remote_dir="")
        except Exception as exc:
            raise HTTPException(502, "Printer upload failed; no print was started") from exc
    return result
