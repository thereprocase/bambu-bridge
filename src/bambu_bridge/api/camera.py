"""Camera endpoints (spec 5.3 / 6 "Camera").

``stream.mjpeg`` is ``multipart/x-mixed-replace`` — the boundary framing
browsers and RN render natively. ``snapshot.jpg`` returns the most recent
buffered frame, briefly opening the upstream if nothing is cached.

Both go through the per-printer :class:`CameraStream`, so any number of
viewers share one printer-side connection and it closes when they all leave.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse

from bambu_bridge.api.auth import require_media_auth
from bambu_bridge.api.printers import get_registry
from bambu_bridge.camera_overlay import OverlayStream
from bambu_bridge.service.printer import PrinterService
from bambu_bridge.service.registry import PrinterNotFoundError, Registry

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/printers", tags=["camera"], dependencies=[Depends(require_media_auth)])

_BOUNDARY = "frame"
_SNAPSHOT_WAIT_S = 10.0


def _overlay(request: Request, printer_id: str, enabled: bool) -> OverlayStream | None:
    """Share the configured native printer's HUD with web, APK and HA viewers."""
    gateway = getattr(request.app.state, "native_gateway", None)
    if (
        enabled
        and gateway
        and gateway.config
        and gateway.config.get("printer_id") == printer_id
        and request.app.state.settings.bridge_native_camera_overlay
    ):
        return cast(OverlayStream, gateway.camera_overlay)
    return None


def mjpeg_part(jpeg: bytes) -> bytes:
    """One ``multipart/x-mixed-replace`` part for a JPEG frame (spec 5.3)."""
    return (
        (
            f"--{_BOUNDARY}\r\n"
            f"Content-Type: image/jpeg\r\n"
            f"Content-Length: {len(jpeg)}\r\n\r\n"
        ).encode()
        + jpeg
        + b"\r\n"
    )


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
    printer_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
    overlay: bool = True,
) -> StreamingResponse:
    service = _service(registry, printer_id)
    hud = _overlay(request, printer_id, overlay)
    source = hud if hud is not None else service.camera

    async def frames() -> AsyncIterator[bytes]:
        async with source.subscribe() as queue:
            while True:
                jpeg = await queue.get()
                if jpeg is None:
                    return
                yield mjpeg_part(jpeg)

    return StreamingResponse(
        frames(),
        media_type=f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
        headers={"Cache-Control": "no-store", "X-Camera-Overlay": str(hud is not None).lower()},
    )


@router.get("/{printer_id}/camera/snapshot.jpg")
async def camera_snapshot(
    printer_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
    overlay: bool = True,
) -> Response:
    service = _service(registry, printer_id)
    hud = _overlay(request, printer_id, overlay)
    if hud is not None:
        async with hud.subscribe() as queue:
            try:
                frame = await asyncio.wait_for(queue.get(), _SNAPSHOT_WAIT_S)
            except TimeoutError:
                frame = None
    else:
        frame = await service.camera.wait_for_frame(_SNAPSHOT_WAIT_S)
    if frame is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no camera frame available",
        )
    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store", "X-Camera-Overlay": str(hud is not None).lower()},
    )
