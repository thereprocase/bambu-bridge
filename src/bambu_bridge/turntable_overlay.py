"""Bounded finished exterior-path preview, orthographic fixed-scale platter rotation."""

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
WALL_TYPES = frozenset({"Outer wall", "Overhang wall", "External perimeter", "Overhang perimeter"})
SURFACE_TYPES = frozenset({"Top surface", "Bottom surface", "Bridge", "Top solid infill"})


@dataclass(frozen=True)
class Shape:
    segments: tuple[tuple[float, ...], ...]
    radius: float
    height: float
    wall_count: int | None = None


def archive_shape(source: BinaryIO) -> Shape | None:
    with zipfile.ZipFile(source) as archive:
        info = archive.getinfo("Metadata/plate_1.gcode")
        if info.file_size > MAX_GCODE:
            raise ValueError("Preview gcode exceeds size limit")
        data = archive.read(info)
    walls = parse_gcode_toolpath(data, features=WALL_TYPES)
    surfaces = parse_gcode_toolpath(data, features=SURFACE_TYPES)
    # Preserve the silhouette first; spend the remaining budget on visible skins.
    wall_budget = (
        min(walls.segment_count, MAX_SEGMENTS * 2 // 3) if surfaces.segment_count else MAX_SEGMENTS
    )
    selected = []
    wall_count = 0
    for toolpath, budget in [
        (walls, wall_budget),
        (surfaces, MAX_SEGMENTS - min(walls.segment_count, wall_budget)),
    ]:
        if not toolpath.segment_count or not budget:
            continue
        positions = toolpath.positions
        stride = max(1, math.ceil(toolpath.segment_count / budget))
        for i in range(0, len(positions), 6 * stride):
            if all(math.isfinite(v) and -1000 <= v <= 1000 for v in positions[i : i + 6]):
                selected.append(
                    (
                        positions[i] - 128,
                        positions[i + 1] - 128,
                        positions[i + 2],
                        positions[i + 3] - 128,
                        positions[i + 4] - 128,
                        positions[i + 5],
                    )
                )
        if toolpath is walls:
            wall_count = len(selected)
    segments = tuple(selected)
    if not segments:
        return None
    radius = max(
        math.sqrt(2) * 128, *(math.hypot(s[i], s[i + 1]) for s in segments for i in (0, 3))
    )
    height = max(0.0, *(s[2] for s in segments), *(s[5] for s in segments))
    return Shape(segments, radius, height, wall_count)


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
    angle = (seconds % 60) * math.tau / 60 + math.pi / 4

    def depth(item: tuple[int, tuple[float, ...]]) -> float:
        _, segment = item
        x, y, z = ((segment[i] + segment[i + 3]) / 2 for i in range(3))
        ry = x * math.sin(angle) + y * math.cos(angle)
        return -ry * math.cos(PITCH) + z * math.sin(PITCH)

    for index, segment in sorted(enumerate(shape.segments), key=depth):
        wall = shape.wall_count is None or index < shape.wall_count
        color = (174, 225, 216, 255) if wall else (102, 155, 161, 235)
        draw.line([project(*segment[:3]), project(*segment[3:])], fill=color, width=1)
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
