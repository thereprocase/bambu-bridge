"""Camera endpoints (spec 5.3 / 6 "Camera").

``stream.mjpeg`` is ``multipart/x-mixed-replace`` — the boundary framing
browsers and RN render natively. ``snapshot.jpg`` returns the most recent
buffered frame, briefly opening the upstream if nothing is cached.

Both go through the per-printer :class:`CameraStream`, so any number of
viewers share one printer-side connection and it closes when they all leave.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response, StreamingResponse

from bambu_bridge.api.auth import require_media_auth
from bambu_bridge.api.printers import get_registry
from bambu_bridge.service.printer import PrinterService
from bambu_bridge.service.registry import PrinterNotFoundError, Registry

log = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/printers", tags=["camera"], dependencies=[Depends(require_media_auth)]
)

_BOUNDARY = "frame"
_SNAPSHOT_WAIT_S = 10.0


def mjpeg_part(jpeg: bytes) -> bytes:
    """One ``multipart/x-mixed-replace`` part for a JPEG frame (spec 5.3)."""
    return (
        f"--{_BOUNDARY}\r\n"
        f"Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(jpeg)}\r\n\r\n"
    ).encode() + jpeg + b"\r\n"


def _service(registry: Registry, printer_id: str) -> PrinterService:
    try:
        return registry.get(printer_id)
    except PrinterNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"printer {printer_id} not found",
        ) from exc


@router.get("/{printer_id}/camera/stream.mjpeg")
async def camera_stream(
    printer_id: str, registry: Registry = Depends(get_registry)
) -> StreamingResponse:
    service = _service(registry, printer_id)

    async def frames() -> AsyncIterator[bytes]:
        async with service.camera.subscribe() as queue:
            while True:
                jpeg = await queue.get()
                yield mjpeg_part(jpeg)

    return StreamingResponse(
        frames(),
        media_type=f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{printer_id}/camera/snapshot.jpg")
async def camera_snapshot(
    printer_id: str, registry: Registry = Depends(get_registry)
) -> Response:
    service = _service(registry, printer_id)
    frame = await service.camera.wait_for_frame(_SNAPSHOT_WAIT_S)
    if frame is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no camera frame available",
        )
    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )
