"""Camera endpoints (spec 5.3 / 6 "Camera").

``stream.mjpeg`` is ``multipart/x-mixed-replace`` — the boundary framing
browsers and RN render natively. ``snapshot.jpg`` returns the most recent
buffered frame, briefly opening the upstream if nothing is cached.

Both go through the per-printer :class:`CameraStream`, so any number of
viewers share one printer-side connection and it closes when they all leave.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import cast

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse

from bambu_bridge.api.auth import require_media_auth, require_owner
from bambu_bridge.api.printers import get_registry
from bambu_bridge.camera_overlay import OverlayStream
from bambu_bridge.service.printer import PrinterService
from bambu_bridge.service.registry import PrinterNotFoundError, Registry

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/printers", tags=["camera"], dependencies=[Depends(require_media_auth)])

_BOUNDARY = "frame"
_SNAPSHOT_WAIT_S = 10.0


def hls_resource(resource: str, query: dict[str, str]) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+\.(?:m3u8|mp4|m4s|ts)", resource)) and all(
        (
            key in {"_HLS_msn", "_HLS_part"}
            and value.isascii()
            and value.isdigit()
            and len(value) <= 12
        )
        or (key == "_HLS_skip" and value in {"YES", "v2"})
        or (key == "session" and bool(re.fullmatch(r"[A-Za-z0-9-]{1,64}", value)))
        or (key == "cookieCheck" and value == "1")
        for key, value in query.items()
    )


@router.get("/{printer_id}/camera/hls/{resource}")
async def camera_hls(printer_id: str, resource: str, request: Request) -> Response:
    """Authenticated HTTPS facade for the shared H.264 encoder's LL-HLS output."""
    gateway = getattr(request.app.state, "native_gateway", None)
    query = dict(request.query_params)
    # Each playlist/part request must carry its own header/cookie authentication.
    if (
        request.url.scheme != "https"
        or not hls_resource(resource, query)
        or not gateway
        or not gateway.config
        or gateway.config.get("printer_id") != printer_id
        or not gateway.video.ready
    ):
        raise HTTPException(404, "Video unavailable")
    code = await asyncio.to_thread(gateway.saved_code)
    if not code:
        raise HTTPException(503, "Video unavailable")
    url = f"http://127.0.0.1:18888/streaming/live/1/{resource}"
    try:
        async with (
            httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False) as client,
            # No browser cookies cross the facade. Ask MediaMTX for its explicit
            # playlist-session URLs instead of following its cookie-probe redirect.
            client.stream("GET", url, params={"cookieCheck": "1", **query},
                          auth=("bblp", code)) as upstream,
        ):
            if upstream.status_code != 200:
                log.warning("camera.hls_upstream_status", status=upstream.status_code)
                raise HTTPException(
                    404 if upstream.status_code == 404 else 503, "Video unavailable"
                )
            chunks = bytearray()
            async for chunk in upstream.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > 9 * 1024 * 1024:
                    raise HTTPException(502, "Video segment too large")
            return Response(
                bytes(chunks),
                media_type=upstream.headers.get("content-type"),
                headers={"Cache-Control": "no-store"},
            )
    except httpx.HTTPError as exc:
        log.warning("camera.hls_upstream_failed", error=type(exc).__name__)
        raise HTTPException(503, "Video unavailable") from exc


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


@router.get("/{printer_id}/camera/video.rgb", dependencies=[Depends(require_owner)])
async def camera_video(printer_id: str, request: Request) -> StreamingResponse:
    """Private, fixed-size RGB feed for the single local on-demand encoder."""
    gateway = getattr(request.app.state, "native_gateway", None)
    if (
        not request.client
        or request.client.host != "127.0.0.1"
        or not gateway
        or not gateway.config
        or gateway.config.get("printer_id") != printer_id
        or not request.app.state.settings.bridge_native_video
    ):
        raise HTTPException(404, "Video encoder unavailable")

    async def frames() -> AsyncIterator[bytes]:
        async with gateway.video_overlay.subscribe() as queue:
            while (frame := await queue.get()) is not None:
                yield frame

    return StreamingResponse(
        frames(), media_type="application/octet-stream", headers={"Cache-Control": "no-store"}
    )


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
