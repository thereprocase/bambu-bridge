"""File endpoints (spec 6 "Files").

Two-step alternative to upload-and-start: stage a 3MF on the printer's FTPS
storage, then reference it. Thin shell over :class:`FtpsTransfer`; the FTPS
port is read from app.state (990 in prod; overridden in tests).

P1S directory layout (confirmed on hardware 2026-06-11):
  /          — root; .gcode.3mf and .gcode files live here directly
  /cache/    — transient per-job expanded files (.gcode, .bbl, one .3mf)
  /timelapse/ — .avi clips (optional; may not exist on all firmware variants)
There is NO /model/ directory; the default upload/list target is root ("")
which resolves to "/" via PurePosixPath("/") / "" / name == "/name".
"""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from fastapi.responses import Response

from bambu_bridge.api.auth import require_auth
from bambu_bridge.api.printers import get_registry
from bambu_bridge.api.uploads import read_upload
from bambu_bridge.protocol.ftps import (
    UPLOAD_DIR_CACHE,
    UPLOAD_DIR_PERSISTENT,
    FileEntry,
    FtpsTransfer,
    SlicedDateMemo,
    TransferTooLarge,
    sort_files_newest_first,
)
from bambu_bridge.service.registry import PrinterNotFoundError, Registry

# Root ("") + cache are confirmed on hardware; timelapse is a real optional
# dir. The old "model" slug is gone from the printer and therefore not allowed.
_ALLOWED_DIRS = {UPLOAD_DIR_PERSISTENT, UPLOAD_DIR_CACHE, "timelapse"}


def _check_dir(remote_dir: str) -> str:
    if remote_dir not in _ALLOWED_DIRS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"dir must be one of {sorted(_ALLOWED_DIRS)!r}",
        )
    return remote_dir

router = APIRouter(
    prefix="/printers", tags=["files"], dependencies=[Depends(require_auth)]
)


def _ftps_for(request: Request, registry: Registry, printer_id: str) -> FtpsTransfer:
    try:
        service = registry.get(printer_id)
    except PrinterNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"printer {printer_id} not found",
        ) from exc
    return FtpsTransfer(
        service.ip,
        service.access_code,
        port=request.app.state.ftps_port,
    )


@router.post("/{printer_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_file(
    printer_id: str,
    file: UploadFile,
    request: Request,
    registry: Registry = Depends(get_registry),
) -> dict[str, str]:
    """Upload a 3MF to the printer's root storage (``/``); return its path."""
    ftps = _ftps_for(request, registry, printer_id)
    name = PurePosixPath(file.filename or "upload.3mf").name
    data = await read_upload(file)
    try:
        path = await ftps.upload_bytes(data, name, remote_dir=UPLOAD_DIR_PERSISTENT)
    except Exception as exc:  # noqa: BLE001 — FTPS failure -> 502
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"FTPS upload failed: {exc!s}",
        ) from exc
    cache = getattr(request.app.state, "viz_cache_obj", None)
    if cache is not None:
        cache.invalidate(printer_id, name)
    return {"path": path, "name": name}


def _iso(dt: datetime | None) -> str | None:
    """Format a UTC datetime as ISO-8601 with trailing Z, or return None."""
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry_to_dict(e: FileEntry, sliced_at: datetime | None) -> dict[str, Any]:
    """Serialise a :class:`FileEntry` to the wire shape for the listing response."""
    return {
        "name": e.name,
        "sliced_at": _iso(sliced_at),
        "modified_at": _iso(e.modified_at),
        "created_at": _iso(e.created_at),
        "sort_basis": e.sort_basis,
    }


@router.get("/{printer_id}/files")
async def list_files(
    printer_id: str,
    request: Request,
    dir: str = UPLOAD_DIR_PERSISTENT,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """List root ``""`` (persistent), ``cache`` (transient), or ``timelapse`` storage.

    Returns files sorted **newest-first** using the sliced → modified → created
    timestamp chain.  Each entry in ``files`` is an object:

    .. code-block:: json

        {
          "name": "benchy.gcode.3mf",
          "sliced_at": "2026-05-19T07:37:44Z",
          "modified_at": "2026-05-19T07:37:44Z",
          "created_at": null,
          "sort_basis": "sliced"
        }

    ``sliced_at`` is populated only when the bridge has already downloaded this
    file for another purpose (viz pre-warm) and cached its internal timestamp.
    ``modified_at`` / ``created_at`` come from the FTPS server (MLSD facts or
    LIST date field).  ``sort_basis`` records which timestamp was used for
    ordering: ``"sliced"``, ``"modified"``, ``"created"``, or ``"none"``
    (undated files sort last, stable relative order).

    For backward-compatibility the legacy ``files`` field is also present as a
    flat list of names (the old response shape).
    """
    remote_dir = _check_dir(dir)
    ftps = _ftps_for(request, registry, printer_id)
    try:
        entries = await ftps.list_dir_with_timestamps(remote_dir)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"FTPS list failed: {exc!s}",
        ) from exc

    # Pull the sliced-date memo from app state if available.
    memo: SlicedDateMemo | None = getattr(request.app.state, "sliced_date_memo", None)

    # Build a name → sliced_at map via one async DB query (or in-memory LRU
    # scan when no DB repo is attached).  This is the authoritative sliced-date
    # lookup for the listing; it replaces the old protected _data scan and the
    # two-pass sort.
    name_map: dict[str, datetime] = {}
    if memo is not None:
        entry_names = [e.name for e in entries]
        name_map = await memo.latest_by_names_async(entry_names)

    # Single sort pass: name_map feeds the sliced tier in sort_files_newest_first.
    sorted_entries = sort_files_newest_first(
        entries,
        name_map=name_map if name_map else None,
    )

    files_list = [
        _entry_to_dict(e, name_map.get(e.name))
        for e in sorted_entries
    ]
    return {
        "dir": remote_dir,
        # Enriched list (new shape): list of objects with timestamps.
        "files": files_list,
        # Backward-compatible flat name list.
        "file_names": [e.name for e in sorted_entries],
    }



@router.get("/{printer_id}/files/{filename}")
async def download_file(
    printer_id: str,
    filename: str,
    request: Request,
    dir: str = UPLOAD_DIR_PERSISTENT,
    registry: Registry = Depends(get_registry),
) -> Response:
    """Stream a stored file back to the client (3MF / gcode / timelapse)."""
    remote_dir = _check_dir(dir)
    safe = PurePosixPath(filename).name
    ftps = _ftps_for(request, registry, printer_id)
    try:
        data = await ftps.download_bytes(safe, remote_dir=remote_dir)
    except TransferTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"FTPS download failed: {exc!s}",
        ) from exc
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment; filename=download; filename*=UTF-8''"
            + quote(safe, safe=""),
        },
    )


@router.delete("/{printer_id}/files/{filename}")
async def delete_file(
    printer_id: str,
    filename: str,
    request: Request,
    dir: str = UPLOAD_DIR_PERSISTENT,
    registry: Registry = Depends(get_registry),
) -> dict[str, str]:
    """Delete a stored file. ``dir`` selects the storage directory and is
    validated/resolved exactly like :func:`download_file`, so a DELETE and a
    GET with the same ``filename`` + ``dir`` address the same remote file.
    """
    remote_dir = _check_dir(dir)
    ftps = _ftps_for(request, registry, printer_id)
    safe = PurePosixPath(filename).name
    # Build the remote path the same way download_bytes does
    # (``/<remote_dir>/<safe>``) so delete and download stay symmetric — a
    # DELETE under /cache or /timelapse no longer silently targets root.
    remote_path = str(PurePosixPath("/") / remote_dir / safe)
    try:
        await ftps.delete(remote_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"FTPS delete failed: {exc!s}",
        ) from exc
    cache = getattr(request.app.state, "viz_cache_obj", None)
    if cache is not None:
        cache.invalidate(printer_id, safe)
    return {"deleted": safe}
