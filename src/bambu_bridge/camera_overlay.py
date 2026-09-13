"""Shared read-only native-camera HUD. Raw camera/vision frames remain untouched."""

from __future__ import annotations

import asyncio
import contextlib
import io
import math
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Any

import structlog
from PIL import Image, ImageDraw, ImageFont

STALE_TELEMETRY_S = 60
STALE_FRAME_S = 5


def number(value: Any) -> str:
    return f"{value:.0f}" if type(value) in (int, float) and math.isfinite(value) else "--"


def bridge_line(receipts: list[dict[str, Any]], now: float) -> str:
    # Prefer an unresolved start; never let an incidental upload hide its status.
    active = {"queued", "dispatching", "sent", "accepted", "running", "unknown"}
    row = next((r for r in receipts if r.get("start_state") in active), None)
    if row is None:
        if not receipts:
            return "Bridge: waiting for next job"
        row = max(receipts, key=lambda r: r.get("created") or 0)
        if now - (row.get("created") or 0) > 300:
            return "Bridge: waiting for next job"
    start = str(row.get("start_state") or "")
    labels = {
        "dispatching": "sending start - awaiting printer",
        "sent": "start sent - awaiting acknowledgement",
        "accepted": "printer acknowledged - awaiting active telemetry",
        "running": "active print confirmed",
        "unknown": "start uncertain - needs review; do not resend",
        "blocked": "start blocked",
        "rejected": "start rejected",
        "completed": "print completion recorded",
        "resolved": "previous start resolved",
        "cancelled": "queued start cancelled",
    }
    label = labels.get(start)
    if label is None:
        label = {
            "receiving": "receiving file",
            "stored": "file stored - waiting for delivery",
            "delivering": "delivering file to printer",
            "delivered": "file delivered - checking readiness"
            if start == "queued"
            else "file delivered; no start requested",
            "failed": "file delivery failed",
        }.get(str(row.get("state") or ""), "waiting for next job")
    if start in {"blocked", "rejected", "unknown"} or row.get("state") == "failed":
        label += " / " + str(row.get("code") or "unknown reason")
    return "Bridge: " + label


def status_lines(
    snapshot: dict[str, Any], receipts: list[dict[str, Any]], now: float, frame_age: float | None
) -> tuple[list[str], bool]:
    session = snapshot.get("session", {})
    age = None
    try:
        stamp = datetime.fromisoformat(session.get("last_telemetry_at", ""))
        if stamp.tzinfo is not None:
            age = max(0.0, now - stamp.timestamp())
    except (TypeError, ValueError):
        pass
    stale = age is None or age > STALE_TELEMETRY_S
    disconnected = not session.get("connected")
    phase = str(snapshot.get("phase") or "unknown")
    job, temps = snapshot.get("job", {}), snapshot.get("temps", {})
    title = phase.upper()
    if phase == "completed":
        title = "FINISHED - waiting for next job"
    if disconnected or stale:
        title = ("DISCONNECTED" if disconnected else "STALE TELEMETRY") + " | last: " + title
    title += f" | Layer {number(job.get('layer_num'))}/{number(job.get('total_layer_num'))}"
    if phase == "printing":
        title += f" | {number(job.get('percent'))}%"
    remaining = job.get("remaining_min") if phase in {"printing", "preparing", "paused"} else None
    nozzle, bed = temps.get("nozzle", {}), temps.get("bed", {})
    thermal = (
        f"Nozzle {number(nozzle.get('current_c'))}/{number(nozzle.get('target_c'))} C"
        f" | Bed {number(bed.get('current_c'))}/{number(bed.get('target_c'))} C"
        f" | ~{number(remaining)} min left"
    )
    health = f"Telemetry age {number(age)}s | Camera age {number(frame_age)}s"
    error = snapshot.get("print_error") or {}
    code = error.get("code")
    warning = disconnected or stale or phase in {"paused", "failed"}
    if code:
        health = "PRINTER ERROR " + str(code) + " | " + health
        warning = True
    if snapshot.get("job_anomaly"):
        health = "CHECK PRINT: ended early | " + health
        warning = True
    bridge = bridge_line(receipts, now)
    warning |= any(
        text in bridge
        for text in ("needs review", "start blocked", "start rejected", "delivery failed")
    )
    hms = next((item for item in snapshot.get("hms", []) if not item.get("stale")), None)
    if hms and not code:
        health = "HMS " + str(hms.get("hex") or hms.get("code")) + " | " + health
        warning = True
    if phase in {"completed", "idle", "failed"} and any(
        r.get("start_state") == "running" for r in receipts
    ):
        bridge = "Bridge: printer terminal; receipt reconciliation pending"
        warning = True
    return [title, thermal, bridge, health], warning


def render_frame(jpeg: bytes | None, lines: list[str], warning: bool) -> bytes:
    """Render off the event loop; bound decoded dimensions before allocating RGB."""
    canvas = None
    if jpeg:
        try:
            with Image.open(io.BytesIO(jpeg)) as source:
                if (
                    source.format == "JPEG"
                    and 320 <= source.width <= 1920
                    and 240 <= source.height <= 1080
                ):
                    canvas = source.convert("RGB")
        except (OSError, ValueError, Image.DecompressionBombError):
            pass
    missing = canvas is None
    if canvas is None:
        canvas = Image.new("RGB", (1280, 720), "#111923")
    width, height = canvas.size
    size = max(12, round(width / 53))
    font = ImageFont.load_default(size=size)
    draw = ImageDraw.Draw(canvas)
    padding, pitch = max(6, size // 2), size + 7
    top = height - len(lines) * pitch - 2 * padding
    draw.rectangle((0, top, width, height), fill="#101820")
    color = "#ffba69" if warning else "#77edc3"
    draw.rectangle((0, top, width, top + 3), fill=color)
    if missing:
        draw.text((padding * 2, height // 3), "CAMERA UNAVAILABLE", font=font, fill="#ffba69")
        draw.text(
            (padding * 2, height // 3 + pitch),
            "Printer status continues below",
            font=font,
            fill="white",
        )
    for index, line in enumerate(lines):
        # Bound externally supplied diagnostic strings; fit without wrapping into the image.
        line = " ".join(str(line).split())[:240]
        if draw.textlength(line, font=font) > width - 2 * padding:
            while line and draw.textlength(line + "...", font=font) > width - 2 * padding:
                line = line[:-1]
            line += "..."
        draw.text(
            (padding, top + padding + index * pitch),
            line,
            font=font,
            fill=color if index == 0 else "#eef3fa",
        )
    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=85)
    return output.getvalue()


class OverlayStream:
    """One subscriber/renderer for all Orca viewers, latest-only fanout at <=1 Hz."""

    def __init__(self, service: Callable[[], Any], receipts: Callable[[], list[dict[str, Any]]]):
        self.service, self.receipts = service, receipts
        self._subscribers: set[asyncio.Queue[bytes | None]] = set()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[bytes | None]]:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1)
        async with self._lock:
            self._subscribers.add(queue)
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run())
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers.discard(queue)
                if not self._subscribers and self._task:
                    self._task.cancel()
                    await asyncio.gather(self._task, return_exceptions=True)
                    self._task = None

    def _publish(self, frame: bytes | None) -> None:
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(frame)

    async def _run(self) -> None:
        try:
            service = self.service()
            async with service.camera.subscribe() as raw:
                frame: bytes | None = None
                last_frame: float | None = None
                while True:
                    tick = time.monotonic()
                    if self.service() is not service:
                        return  # replaced/deleted printer: never leak the old camera
                    while not raw.empty():
                        frame = raw.get_nowait()
                        last_frame = tick
                    age = tick - last_frame if last_frame is not None else None
                    lines, warning = status_lines(
                        service.snapshot(), self.receipts(), time.time(), age
                    )
                    task = asyncio.create_task(
                        asyncio.to_thread(
                            render_frame,
                            frame if age is not None and age <= STALE_FRAME_S else None,
                            lines,
                            warning,
                        )
                    )
                    try:
                        result = await asyncio.shield(task)
                    except asyncio.CancelledError:
                        await asyncio.gather(task, return_exceptions=True)
                        raise
                    self._publish(result)
                    await asyncio.sleep(max(0, 1 - (time.monotonic() - tick)))
        except Exception as exc:
            structlog.get_logger().warning("camera.overlay_failed", error=type(exc).__name__)
        finally:
            self._publish(None)  # wake clients if the service fence/renderer fails
