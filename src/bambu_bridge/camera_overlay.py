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
from zoneinfo import ZoneInfo

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
    snapshot: dict[str, Any],
    receipts: list[dict[str, Any]],
    now: float,
    frame_age: float | None,
    timezone: str = "UTC",
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
    completion = "Finish time --"
    if (
        phase in {"printing", "preparing"}
        and not stale
        and not disconnected
        and type(remaining) in (int, float)
        and math.isfinite(remaining)
        and 0 <= remaining <= 525600
    ):
        zone = ZoneInfo(timezone)
        finish = datetime.fromtimestamp(now - (age or 0) + remaining * 60, zone)
        today = datetime.fromtimestamp(now, zone).date()
        day = "" if finish.date() == today else finish.strftime("%a ")
        completion = "Finishes ~" + day + finish.strftime("%I:%M %p").lstrip("0")
    nozzle, bed = temps.get("nozzle", {}), temps.get("bed", {})
    thermal = (
        f"Nozzle {number(nozzle.get('current_c'))}/{number(nozzle.get('target_c'))} C"
        f" | Bed {number(bed.get('current_c'))}/{number(bed.get('target_c'))} C"
        f" | {completion}"
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
    size = max(12, round(width / 64))
    font = ImageFont.load_default(size=size)
    margin, padding, pitch = max(10, size), max(8, size // 2), size + 7
    shown = list(lines[:2])
    quiet_bridge = {
        "Bridge: active print confirmed",
        "Bridge: waiting for next job",
        "Bridge: print completion recorded",
    }
    if len(lines) > 2 and lines[2] not in quiet_bridge:
        shown.append(lines[2])
    if warning and len(lines) > 3:
        shown.append(lines[3])
    measure = ImageDraw.Draw(canvas)
    fitted = []
    for line in shown:
        line = " ".join(str(line).split()).replace(" | ", "  ·  ")[:240]
        if measure.textlength(line, font=font) > width - 2 * (margin + padding):
            while line and measure.textlength(line + "…", font=font) > width - 2 * (
                margin + padding
            ):
                line = line[:-1]
            line += "…"
        fitted.append(line)
    panel_width = min(
        width - 2 * margin,
        int(max(measure.textlength(s, font=font) for s in fitted)) + 2 * padding + 6,
    )
    panel_height = len(fitted) * pitch + 2 * padding
    panel = Image.new("RGBA", (panel_width, panel_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)
    color = "#ffc27c" if warning else "#9ee8cf"
    draw.rounded_rectangle(
        (0, 0, panel_width - 1, panel_height - 1), radius=10, fill=(12, 19, 26, 175)
    )
    draw.rounded_rectangle((0, 9, 3, panel_height - 10), radius=2, fill=color)
    for index, line in enumerate(fitted):
        draw.text(
            (padding + 3, padding + index * pitch),
            line,
            font=font,
            fill=color if index == 0 else "#eef3fa",
        )
    canvas.paste(panel, (margin, height - panel_height - margin), panel)
    if missing:
        draw = ImageDraw.Draw(canvas)
        draw.text((margin, height // 3), "Camera unavailable", font=font, fill="#ffc27c")
        draw.text(
            (margin, height // 3 + pitch), "Printer status continues below", font=font, fill="white"
        )
    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=85)
    return output.getvalue()


class OverlayStream:
    """Shared latest-only renderer at camera rate; status refresh/idle heartbeat at 1 Hz."""

    def __init__(
        self,
        service: Callable[[], Any],
        receipts: Callable[[], list[dict[str, Any]]],
        timezone: Callable[[], str] = lambda: "UTC",
    ):
        self.service, self.receipts = service, receipts
        self.timezone = timezone
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
                snapshot: dict[str, Any] = {}
                receipts: list[dict[str, Any]] = []
                status_at = float("-inf")
                while True:
                    got_frame = False
                    try:
                        frame = await asyncio.wait_for(raw.get(), 1)
                        last_frame = time.monotonic()
                        got_frame = True
                    except TimeoutError:
                        pass
                    tick = time.monotonic()
                    if self.service() is not service:
                        return  # replaced/deleted printer: never leak the old camera
                    while not raw.empty():
                        frame = raw.get_nowait()
                        last_frame = tick
                        got_frame = True
                    age = tick - last_frame if last_frame is not None else None
                    if not got_frame and age is not None and age <= STALE_FRAME_S:
                        continue  # do not manufacture duplicate "live" frames between arrivals
                    if tick - status_at >= 1:
                        snapshot, receipts = service.snapshot(), self.receipts()
                        status_at = tick
                    lines, warning = status_lines(
                        snapshot, receipts, time.time(), age, self.timezone()
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
        except Exception as exc:
            structlog.get_logger().warning("camera.overlay_failed", error=type(exc).__name__)
        finally:
            self._publish(None)  # wake clients if the service fence/renderer fails
