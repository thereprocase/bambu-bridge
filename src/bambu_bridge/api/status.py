"""Live status WebSocket (spec 7).

Wire sequence per connection:

1. ``{"type": "hello", "protocol_version": 1, "printer_id": ...}``
2. ``{"type": "snapshot", "data": <full state>}``
3. ``{"type": "delta", ...}`` / ``{"type": "event", ...}`` as they occur
4. ``{"type": "ping"}`` every 30 s; client echoes a pong (any frame)

Subscription is taken *before* the snapshot is read, so a change landing in
the gap is delivered as a follow-up delta rather than lost.

Auth (spec 10): browsers can't set Authorization on a WebSocket, so the token
may arrive as ``?token=`` or in the header. Unauthorized / unknown printer =>
close 1008 without accepting.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from bambu_bridge.api.auth import check_ws_token
from bambu_bridge.service.events import Subscription, diff_state
from bambu_bridge.service.printer import PrinterService
from bambu_bridge.service.registry import PrinterNotFoundError, Registry

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/printers", tags=["status"])

PROTOCOL_VERSION = 1
_HEARTBEAT_S = 30
_WS_POLICY_VIOLATION = 1008


@router.websocket("/{printer_id}/status")
async def printer_status(websocket: WebSocket, printer_id: str) -> None:
    settings = websocket.app.state.settings
    registry: Registry = websocket.app.state.registry

    token = websocket.query_params.get("token")
    if token is None:
        header = websocket.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:]
    if not check_ws_token(token, settings):
        await websocket.close(code=_WS_POLICY_VIOLATION, reason="unauthorized")
        return

    try:
        service = registry.get(printer_id)
    except PrinterNotFoundError:
        await websocket.close(code=_WS_POLICY_VIOLATION, reason="unknown printer")
        return

    await websocket.accept()
    log.info("ws.connected", printer_id=printer_id)
    async with service.bus.subscribe() as sub:
        await websocket.send_json(
            {
                "type": "hello",
                "protocol_version": PROTOCOL_VERSION,
                "printer_id": printer_id,
            }
        )
        initial = service.snapshot()
        await websocket.send_json({"type": "snapshot", "data": initial})

        sender = asyncio.create_task(_pump(websocket, sub, service, initial))
        receiver = asyncio.create_task(_drain_client(websocket))
        try:
            done, pending = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        finally:
            log.info("ws.disconnected", printer_id=printer_id)


async def _pump(
    websocket: WebSocket,
    sub: Subscription,
    service: PrinterService,
    last_translated: dict[str, Any],
) -> None:
    """Forward bus events as a uniformly §6-translated stream; ping when idle.

    The bus carries *raw* P1S ``snapshot`` / ``delta`` events (the FSM and
    the event persister consume them that way). The WS client, however, was
    handed a §6-translated snapshot as its first frame — so every frame
    after must stay translated too (contract §5.3: ``delta`` frames carry
    translated changed leaves). Named ``event`` frames are already
    contract-shaped and pass through verbatim; raw ``snapshot`` / ``delta``
    events are re-derived from ``service.snapshot()`` here so no raw P1S key
    ever leaks into the translated stream.
    """
    while True:
        try:
            event = await asyncio.wait_for(sub.get(), timeout=_HEARTBEAT_S)
        except TimeoutError:
            await websocket.send_json({"type": "ping"})
            continue
        if event.type == "event":
            await websocket.send_json(event.to_wire())
            continue
        # Raw snapshot/delta bus event → re-translate the full current state.
        current = service.snapshot()
        if event.type == "snapshot":
            await websocket.send_json({"type": "snapshot", "data": current})
            last_translated = current
        else:  # delta — emit the translated changed leaves only
            changed = diff_state(last_translated, current)
            if changed:
                await websocket.send_json({"type": "delta", "data": changed})
                last_translated = current


async def _drain_client(websocket: WebSocket) -> None:
    """Consume client frames (pong/keepalive); return on disconnect."""
    with contextlib.suppress(WebSocketDisconnect):
        while True:
            await websocket.receive_text()
