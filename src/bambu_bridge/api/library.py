"""Archive endpoints. None of these routes invoke printer or job commands."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from bambu_bridge.api.auth import require_auth, require_owner
from bambu_bridge.api.orca import require_slicer
from bambu_bridge.library import CHUNK_LIMIT, IDENTIFIER, Capture, LibraryError, LibraryStore

router = APIRouter(prefix="/library", tags=["library"], dependencies=[Depends(require_auth)])
ingest = APIRouter(prefix="/orca/{printer_id}/library", tags=["library"])


def store(request: Request) -> LibraryStore:
    value: LibraryStore | None = getattr(request.app.state, "library", None)
    if value is None:
        raise HTTPException(503, "The bridge library is not enabled")
    return value


def invoke(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except LibraryError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


@router.get("/captures")
def captures(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    before: float | None = Query(None, ge=0, allow_inf_nan=False),
    before_id: str = Query("", pattern=f"({IDENTIFIER})|^$"),
) -> Any:
    return invoke(store(request).list, limit=limit, before=before, before_id=before_id)


@router.get("/usage")
def usage(request: Request) -> Any:
    return store(request).usage()


@router.post("/verify", dependencies=[Depends(require_owner)])
def verify(request: Request) -> Any:
    return store(request).verify()


@router.post("/collect-unreferenced", dependencies=[Depends(require_owner)])
def collect_unreferenced(request: Request) -> dict[str, int]:
    return {"removed_blobs": store(request).collect_unreferenced()}


@router.get("/captures/{cid}")
def capture(cid: str, request: Request) -> Any:
    return invoke(store(request).get, cid)


@router.delete("/captures/{cid}", status_code=204, dependencies=[Depends(require_owner)])
def delete_capture(cid: str, request: Request) -> Response:
    invoke(store(request).delete, cid)
    return Response(status_code=204)


@router.get("/captures/{cid}/files/{name}")
def download(cid: str, name: str, request: Request) -> FileResponse:
    path, digest = invoke(store(request).download, cid, name)
    return FileResponse(
        path,
        filename=name,
        media_type="application/octet-stream",
        headers={
            "ETag": '"' + digest + '"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@ingest.post("/captures", status_code=201)
def create_capture(
    body: Capture, request: Request, client: dict[str, Any] = Depends(require_slicer)
) -> Any:
    return invoke(store(request).create, client["id"], body)


@ingest.get("/captures")
def scoped_captures(request: Request, client: dict[str, Any] = Depends(require_slicer)) -> Any:
    return invoke(store(request).list, owner=client["id"])


@ingest.get("/captures/{cid}")
def upload_status(
    cid: str, request: Request, client: dict[str, Any] = Depends(require_slicer)
) -> Any:
    return invoke(store(request).get, cid, client["id"])


@ingest.put("/captures/{cid}/files/{name}")
async def chunk(
    cid: str,
    name: str,
    request: Request,
    offset: int = Query(ge=0),
    client: dict[str, Any] = Depends(require_slicer),
) -> Any:
    import asyncio

    data = bytearray()
    async for part in request.stream():
        if len(data) + len(part) > CHUNK_LIMIT:
            raise HTTPException(413, "Upload chunks must not exceed 4 MiB")
        data.extend(part)
    # Validate revocation again after receiving the body, before committing data.
    current = request.app.state.orca.authenticate(
        request.headers.get("x-api-key"), request.path_params["printer_id"]
    )
    if not current or current["id"] != client["id"]:
        raise HTTPException(401, "Slicer key was revoked during upload")
    return await asyncio.to_thread(
        invoke, store(request).append, cid, name, offset, bytes(data), client["id"]
    )


@ingest.post("/captures/{cid}/finalize")
def finalize(cid: str, request: Request, client: dict[str, Any] = Depends(require_slicer)) -> Any:
    return invoke(store(request).finalize, cid, client["id"])
