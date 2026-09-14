"""Bounded finished outer-wall preview, orthographic fixed-scale platter rotation."""

from __future__ import annotations

import asyncio
import math
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, BinaryIO

from PIL import Image, ImageDraw, ImageFont

from bambu_bridge.protocol.gcode_path import parse_gcode_toolpath

MAX_GCODE = 120 * 1024 * 1024
MAX_SEGMENTS = 6000
PITCH = math.atan(1 / math.sqrt(2))


@dataclass(frozen=True)
class Shape:
    segments: tuple[tuple[float, ...], ...]
    radius: float
    height: float


def archive_shape(source: BinaryIO) -> Shape | None:
    with zipfile.ZipFile(source) as archive:
        info = archive.getinfo("Metadata/plate_1.gcode")
        if info.file_size > MAX_GCODE:
            raise ValueError("Preview gcode exceeds size limit")
        data = archive.read(info)
    toolpath = parse_gcode_toolpath(data, features=frozenset({"Outer wall", "External perimeter"}))
    if not toolpath.segment_count:
        return None
    positions = toolpath.positions
    # Center on the physical P1S plate, not the part: placement rotates with it.
    stride = max(1, math.ceil(toolpath.segment_count / MAX_SEGMENTS))
    segments = tuple(
        (
            positions[i] - 128,
            positions[i + 1] - 128,
            positions[i + 2],
            positions[i + 3] - 128,
            positions[i + 4] - 128,
            positions[i + 5],
        )
        for i in range(0, len(positions), 6 * stride)
        if all(math.isfinite(v) and -1000 <= v <= 1000 for v in positions[i : i + 6])
    )
    if not segments:
        return None
    radius = max(
        math.sqrt(2) * 128, *(math.hypot(s[i], s[i + 1]) for s in segments for i in (0, 3))
    )
    height = max(0.0, *(s[2] for s in segments), *(s[5] for s in segments))
    return Shape(segments, radius, height)


def projection(
    shape: Shape, width: int, height: int, angle: float
) -> Callable[..., tuple[float, float]]:
    # Rotation-invariant envelope. No per-frame fitting, zoom or camera motion.
    sinp, cosp = math.sin(PITCH), math.cos(PITCH)
    scale = min(
        (width - 18) / (2 * shape.radius),
        (height - 38) / (2 * shape.radius * sinp + shape.height * cosp),
    )
    cy = 24 + (height - 38) / 2 + shape.height * cosp * scale / 2
    cosr, sinr = math.cos(angle), math.sin(angle)

    def project(x: float, y: float, z: float) -> tuple[float, float]:
        rx, ry = x * cosr - y * sinr, x * sinr + y * cosr
        return width / 2 + rx * scale, cy + (ry * sinp - z * cosp) * scale

    return project


def draw_shape(
    canvas: Image.Image, shape: Shape, seconds: float, left_width: int = 0, bottom_height: int = 0
) -> None:
    width, height = canvas.size
    margin = max(10, round(width / 64))
    w = max(110, round(width * 0.19))
    h = round(w * 0.76)
    x, y = width - w - margin, height - h - margin
    if x < left_width + margin:
        y -= bottom_height + 8
    panel = Image.new("RGBA", (w, h))
    draw = ImageDraw.Draw(panel)
    draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=10, fill=(12, 19, 26, 190))
    draw.rounded_rectangle((0, 9, 3, h - 10), radius=2, fill="#9ee8cf")
    font = ImageFont.load_default(size=max(9, round(width / 100)))
    draw.text((10, 8), "Finished shape", font=font, fill="#9ee8cf")
    project = projection(shape, w, h, (seconds % 60) * math.tau / 60 + math.pi / 4)
    plate = [project(x, y, 0) for x, y in [(-128, -128), (128, -128), (128, 128), (-128, 128)]]
    draw.polygon(plate, fill=(40, 56, 67, 240), outline=(107, 137, 151, 255))
    for offset in (-64, 0, 64):
        draw.line([project(offset, -128, 0), project(offset, 128, 0)], fill=(68, 85, 94, 255))
        draw.line([project(-128, offset, 0), project(128, offset, 0)], fill=(68, 85, 94, 255))
    for segment in shape.segments:
        draw.line(
            [project(*segment[:3]), project(*segment[3:])], fill=(163, 213, 211, 235), width=1
        )
    canvas.paste(panel, (x, max(margin, y)), panel)


def job_key(snapshot: dict[str, Any]) -> str:
    raw = snapshot.get("_raw", {})
    return (
        str(raw.get("gcode_file") or "")
        if raw.get("gcode_state") in {"RUNNING", "PAUSE", "FINISH"}
        else ""
    )


class ShapeCache:
    """Never block frame delivery on parsing; discard late results for old jobs."""

    def __init__(self, loader: Callable[[dict[str, Any]], Shape | None]):
        self.loader = loader
        self.key = ""
        self.shape: Shape | None = None
        self.task: asyncio.Task[Shape | None] | None = None
        self.loading_key = ""
        self.retry_at = 0.0

    def update(self, snapshot: dict[str, Any]) -> Shape | None:
        key = job_key(snapshot)
        if key != self.key:
            self.key, self.shape, self.retry_at = key, None, 0.0
        if self.task and self.task.done():
            try:
                result = self.task.result()
            except Exception:
                result = None
            if self.loading_key == key:
                self.shape = result
                self.retry_at = time.monotonic() + 60
            self.task = None
        if key and self.shape is None and self.task is None and time.monotonic() >= self.retry_at:
            self.loading_key = key
            self.task = asyncio.create_task(asyncio.to_thread(self.loader, snapshot))
        return self.shape
