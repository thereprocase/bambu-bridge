"""Bounded finished exterior-path preview, orthographic fixed-scale platter rotation."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
import time
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, BinaryIO

import structlog
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from bambu_bridge.protocol.gcode_path import parse_gcode_toolpath

MAX_GCODE = 120 * 1024 * 1024
MAX_SEGMENTS = 6000
ShapeLoader = Callable[[dict[str, Any]], "Shape | None | Awaitable[Shape | None]"]
PITCH = math.atan(1 / math.sqrt(2))
WALL_TYPES = frozenset(
    {
        "Outer wall",
        "Overhang wall",
        "External perimeter",
        "Overhang perimeter",
        "WALL-OUTER",
        "wall-outer",
    }
)
SURFACE_TYPES = frozenset(
    {
        "Top surface",
        "Bottom surface",
        "Bridge",
        "Top solid infill",
        "SKIN",
        "skin",
    }
)


@dataclass(frozen=True)
class Shape:
    segments: tuple[tuple[float, ...], ...]
    radius: float
    height: float
    wall_count: int | None = None
    faces: tuple[tuple[float, ...], ...] = ()
    face_parts: tuple[int, ...] = ()
    ink_edges: tuple[tuple[bool, ...], ...] = ()
    two_sided: bool = False
    content_id: str = ""


def archive_shape(source: BinaryIO, plate: int = 1) -> Shape | None:
    """Read the selected plate or plain G-code with bounded source expansion."""
    if plate < 1:
        raise ValueError("Preview plate must be positive")
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            name = f"Metadata/plate_{plate}.gcode"
            if name not in archive.namelist():
                if any(re.fullmatch(r"Metadata/plate_\d+\.gcode", n) for n in archive.namelist()):
                    raise ValueError("Selected print plate unavailable")
                from bambu_bridge.protocol.threemf import parse_3mf

                source.seek(0)
                data = source.read(MAX_GCODE + 1)
                if len(data) > MAX_GCODE:
                    raise ValueError("Preview source exceeds size limit")
                mesh = parse_3mf(data)
                from bambu_bridge.preview_geometry import crease_edges, mesh_faces, simplify_faces

                faces, parts, two_sided = mesh_faces(mesh.vertices, mesh.indices)
                faces, parts = simplify_faces(faces, parts)
                if not faces:
                    return None
                radius = max(
                    math.sqrt(2) * 128,
                    *(math.hypot(f[i], f[i + 1]) for f in faces for i in (0, 3, 6)),
                )
                height = max(0.0, *(f[i] for f in faces for i in (2, 5, 8)))
                return Shape(
                    (),
                    radius,
                    height,
                    faces=faces,
                    face_parts=parts,
                    ink_edges=crease_edges(faces),
                    two_sided=two_sided,
                )
            info = archive.getinfo(name)
            if info.file_size > MAX_GCODE:
                raise ValueError("Preview gcode exceeds size limit")
            data = archive.read(info)
    else:
        source.seek(0)
        data = source.read(MAX_GCODE + 1)
        if len(data) > MAX_GCODE:
            raise ValueError("Preview gcode exceeds size limit")
    walls = parse_gcode_toolpath(data, features=WALL_TYPES, preserve_z=True, arcs=True)
    surfaces = parse_gcode_toolpath(
        data, features=SURFACE_TYPES, preserve_z=True, arcs=True
    )
    if not walls.segment_count and not surfaces.segment_count:
        # Slicers without recognized feature annotations still provide useful
        # extrusion geometry. The reconstruction stage can refine its exterior.
        walls = parse_gcode_toolpath(data, preserve_z=True, arcs=True)
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
    from bambu_bridge.preview_geometry import (
        body_faces,
        continuous_faces,
        crease_edges,
        simplify_faces,
    )

    positions = walls.positions if walls.segment_count else surfaces.positions
    continuous = any(
        abs(positions[i + 2] - positions[i + 5]) > 0.0001 for i in range(0, len(positions), 6)
    )
    header = data[: 128 * 1024].decode("utf-8", errors="replace")

    def dimension(keys: str, default: float, maximum: float) -> float:
        match = re.search(rf"(?m)^;\s*(?:{keys})\s*=\s*([\d.]+)", header)
        try:
            value = float(match[1]) if match else default
        except ValueError:
            return default
        return value if math.isfinite(value) and 0.02 <= value <= maximum else default

    layer_height = dimension("layer_height", 0.2, 1.2)
    width = dimension(
        "outer_wall_line_width|external_perimeter_extrusion_width|line_width", 0.44, 2
    )
    faces, parts = (
        continuous_faces(positions, layer_height)
        if continuous
        else body_faces(positions, layer_height, width)
    )
    faces, parts = simplify_faces(faces, parts)
    return Shape(
        segments, radius, height, wall_count, faces, parts, crease_edges(faces), continuous
    )


# bench-ink-v1 from peg-board50: four constant light bands and charcoal ink.
# Keep this CPU renderer small; Blender/Freestyle is not a live-video dependency.
INK = (5, 7, 9, 255)
BASE = (82, 218, 193)
BANDS = (0.38, 0.60, 0.82, 1.0)
PALETTE = (
    BASE,
    (242, 151, 117),
    (144, 175, 245),
    (235, 206, 110),
    (191, 151, 222),
    (126, 205, 145),
    (237, 155, 189),
    (135, 208, 226),
)
MAX_FACES = 6000


def exterior_faces(walls: Any, skins: Any) -> tuple[tuple[float, ...], ...]:
    """Extrusion ribbons, not a guessed CAD solid. Merge identical stacked walls.

    Layer heights come from exterior paths. Never connect successive paths across
    a travel, fill a hole, or bridge a missing layer. Coordinates remain on plate.
    Each face stores four xyz vertices and a unit normal.
    """
    levels = sorted({round(walls[i], 4) for i in range(2, len(walls), 6)})
    floors = {z: levels[i - 1] if i else max(0.0, z - 0.2) for i, z in enumerate(levels)}
    merged: dict[tuple[float, ...], list[list[float]]] = {}
    for i in range(0, len(walls), 6):
        x, y, z, u, v, q = (round(float(n), 4) for n in walls[i : i + 6])
        if abs(z - q) > 0.001 or z not in floors or math.hypot(u - x, v - y) < 0.001:
            continue
        if not all(math.isfinite(n) and abs(n) <= 1000 for n in (x, y, z, u, v, q)):
            continue
        key = min((x, y, u, v), (u, v, x, y))
        spans = merged.setdefault(key, [])
        bottom = floors[z]
        if spans and abs(spans[-1][1] - bottom) < 0.001:
            spans[-1][1] = z
        elif not spans or spans[-1] != [bottom, z]:
            spans.append([bottom, z])
    faces = []
    for (x, y, u, v), spans in merged.items():
        length = math.hypot(u - x, v - y)
        for low, high in spans:
            faces.append(
                (
                    x - 128,
                    y - 128,
                    low,
                    u - 128,
                    v - 128,
                    low,
                    u - 128,
                    v - 128,
                    high,
                    x - 128,
                    y - 128,
                    high,
                    (v - y) / length,
                    (x - u) / length,
                    0.0,
                )
            )
    # Skins are narrow extrusion ribbons; no polygon fill across cutouts.
    for i in range(0, len(skins), 6):
        x, y, z, u, v, q = (float(n) for n in skins[i : i + 6])
        length = math.hypot(u - x, v - y)
        if length < 0.001 or not all(
            math.isfinite(n) and abs(n) <= 1000 for n in (x, y, z, u, v, q)
        ):
            continue
        dx, dy = -(v - y) / length * 0.25, (u - x) / length * 0.25
        faces.append(
            (
                x - 128 + dx,
                y - 128 + dy,
                z,
                u - 128 + dx,
                v - 128 + dy,
                q,
                u - 128 - dx,
                v - 128 - dy,
                q,
                x - 128 - dx,
                y - 128 - dy,
                z,
                0.0,
                0.0,
                1.0,
            )
        )
    stride = max(1, math.ceil(len(faces) / MAX_FACES))
    return tuple(faces[::stride])


def cel_color(
    normal: tuple[float, ...], angle: float, base: tuple[int, ...] = BASE
) -> tuple[int, ...]:
    nx, ny, nz = normal
    rx, ry = (
        nx * math.cos(angle) - ny * math.sin(angle),
        nx * math.sin(angle) + ny * math.cos(angle),
    )
    # Two-sided exterior ribbons: orient toward the camera before lighting.
    if ry * math.cos(PITCH) + nz * math.sin(PITCH) < 0:
        rx, ry, nz = -rx, -ry, -nz
    vertical = -ry * math.sin(PITCH) + nz * math.cos(PITCH)
    facing = ry * math.cos(PITCH) + nz * math.sin(PITCH)
    light = max(0.0, min(1.0, (-0.65 * rx + 0.85 * vertical + 1.3 * facing) / 1.683))
    band = BANDS[sum(light >= threshold for threshold in (0.2, 0.48, 0.78))]
    return (*(round(c * band) for c in base), 255)


def paint_cel(
    panel: Image.Image, shape: Shape, project: Callable[..., tuple[float, float]], angle: float
) -> None:
    layer = Image.new("RGBA", panel.size)
    draw = ImageDraw.Draw(layer)
    sinr, cosr = math.sin(angle), math.cos(angle)

    def depth(face: tuple[float, ...]) -> float:
        x = face[0] + face[3] + face[6] + face[9]
        y = face[1] + face[4] + face[7] + face[10]
        z = face[2] + face[5] + face[8] + face[11]
        return (x * sinr + y * cosr) * math.cos(PITCH) + z * math.sin(PITCH)

    colors: dict[tuple[Any, ...], tuple[int, ...]] = {}
    for index in sorted(range(len(shape.faces)), key=lambda index: depth(shape.faces[index])):
        face = shape.faces[index]
        normal = face[12:15]
        if shape.face_parts and not shape.two_sided:
            # Closed bodies have a true exterior. Cull rear faces so bottom
            # caps cannot paint diagonal patches over the visible side walls.
            nx, ny, nz = normal
            ry = nx * sinr + ny * cosr
            if ry * math.cos(PITCH) + nz * math.sin(PITCH) <= 1e-8:
                continue
        part = shape.face_parts[index] if shape.face_parts else 0
        key = normal, part
        if key not in colors:
            colors[key] = cel_color(normal, angle, part_color(part))
        points = [
            project(face[0], face[1], face[2]),
            project(face[3], face[4], face[5]),
            project(face[6], face[7], face[8]),
            project(face[9], face[10], face[11]),
        ]
        draw.polygon(points, fill=colors[key])
        if shape.ink_edges:
            for edge, visible in enumerate(shape.ink_edges[index]):
                if visible:
                    draw.line([points[edge], points[(edge + 1) % 4]], fill=INK, width=2)
    # Ink only the visible silhouette: no wireframe or extrusion seam clutter.
    mask = layer.getchannel("A")
    ink = Image.new("RGBA", panel.size, INK)
    panel.paste(ink, (0, 0), mask.filter(ImageFilter.MaxFilter(5)))
    panel.alpha_composite(layer)


def part_color(part: int) -> tuple[int, ...]:
    if part < len(PALETTE):
        return PALETTE[part]
    # Extend the palette deterministically for plates with many distinct bodies.
    import colorsys

    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb((part * 0.61803398875) % 1, 0.45, 0.9))


def projection(
    shape: Shape, width: int, height: int, angle: float, pixel_scale: int = 1
) -> Callable[..., tuple[float, float]]:
    # Rotation-invariant envelope. No per-frame fitting, zoom or camera motion.
    sinp, cosp = math.sin(PITCH), math.cos(PITCH)
    scale = min(
        (width - 18 * pixel_scale) / (2 * shape.radius),
        (height - 38 * pixel_scale) / (2 * shape.radius * sinp + shape.height * cosp),
    )
    cy = 24 * pixel_scale + (height - 38 * pixel_scale) / 2 + shape.height * cosp * scale / 2
    cosr, sinr = math.cos(angle), math.sin(angle)

    def project(x: float, y: float, z: float) -> tuple[float, float]:
        rx, ry = x * cosr - y * sinr, x * sinr + y * cosr
        return width / 2 + rx * scale, cy + (ry * sinp - z * cosp) * scale

    return project


def draw_shape(
    canvas: Image.Image,
    shape: Shape,
    seconds: float,
    left_width: int = 0,
    bottom_height: int = 0,
    frames: Any = None,
) -> None:
    width, height = canvas.size
    margin = max(10, round(width / 64))
    w = max(110, round(width * 0.19))
    h = round(w * 0.76)
    x, y = width - w - margin, height - h - margin
    if x < left_width + margin:
        y -= bottom_height + 8
    panel = (
        frames.panel(shape, seconds, w, h, max(9, round(width / 100)))
        if frames is not None
        else render_panel(shape, seconds, w, h, max(9, round(width / 100)))
    )
    canvas.paste(panel, (x, max(margin, y)), panel)


def render_panel(shape: Shape, seconds: float, w: int, h: int, font_size: int) -> Image.Image:
    """Render a transparent rotation panel independently of the camera frame."""
    return _render_panel(shape, seconds, w * 2, h * 2, font_size * 2).resize(
        (w, h), Image.Resampling.LANCZOS
    )


def _render_panel(shape: Shape, seconds: float, w: int, h: int, font_size: int) -> Image.Image:
    panel = Image.new("RGBA", (w, h))
    draw = ImageDraw.Draw(panel)
    draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=20, fill=(12, 19, 26, 190))
    draw.rounded_rectangle((0, 18, 6, h - 20), radius=4, fill="#9ee8cf")
    font = ImageFont.load_default(size=font_size)
    draw.text((20, 16), "Finished shape", font=font, fill="#9ee8cf")
    project = projection(shape, w, h, (seconds % 60) * math.tau / 60 + math.pi / 4, pixel_scale=2)
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
        return ry * math.cos(PITCH) + z * math.sin(PITCH)

    if shape.faces:
        paint_cel(panel, shape, project, angle)
    else:
        for index, segment in sorted(enumerate(shape.segments), key=depth):
            wall = shape.wall_count is None or index < shape.wall_count
            color = (62, 193, 180, 255) if wall else (102, 218, 199, 255)
            draw.line([project(*segment[:3]), project(*segment[3:])], fill=color, width=1)
    return panel


def job_key(snapshot: dict[str, Any]) -> str:
    raw = snapshot.get("_raw", {})
    if raw.get("gcode_state") not in {"PREPARE", "SLICING", "RUNNING", "PAUSE", "FINISH", "FAILED"}:
        return ""
    filename = str(raw.get("gcode_file") or raw.get("subtask_name") or raw.get("param") or "")
    if not filename:
        return ""
    identity = [
        snapshot.get("printer_id"),
        snapshot.get("job", {}).get("started_at"),
        raw.get("subtask_id"),
        raw.get("subtask_name"),
        raw.get("plate_idx"),
        raw.get("param"),
    ]
    return filename + ("|" + json.dumps(identity) if any(identity) else "")


class ShapeCache:
    """Never block frame delivery on parsing; discard late results for old jobs."""

    def __init__(self, loader: ShapeLoader):
        self.loader = loader
        self.key = ""
        self.shape: Shape | None = None
        self.task: asyncio.Task[Shape | None] | None = None
        self.loading_key = ""
        self.retry_at = 0.0
        self.state = "idle"
        self.error: str | None = None
        self.failures = 0

    def retry(self) -> None:
        if self.task:
            self.task.cancel()
            self.task = None
        self.shape, self.retry_at = None, 0.0
        self.state, self.error, self.failures = "loading" if self.key else "idle", None, 0

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def update(self, snapshot: dict[str, Any]) -> Shape | None:
        key = job_key(snapshot)
        if key != self.key:
            self.key, self.shape, self.retry_at = key, None, 0.0
            self.state, self.error, self.failures = "loading" if key else "idle", None, 0
        if self.task and self.task.done():
            try:
                result = self.task.result()
            except Exception as exc:
                structlog.get_logger().warning(
                    "preview.load_failed", error=type(exc).__name__, job=key
                )
                if self.loading_key == key:
                    self.error = type(exc).__name__
                result = None
            if self.loading_key == key:
                self.shape = result
                if result is None:
                    self.failures += 1
                    self.state = "retrying"
                    self.retry_at = time.monotonic() + min(60, 5 * 2 ** min(self.failures - 1, 4))
                    if self.error in {"ValueError", "ParseError", "GcodeParseError"}:
                        self.retry_at = float("inf")
                else:
                    self.state, self.error, self.failures = "ready", None, 0
            self.task = None
        if key and self.shape is None and self.task is None and time.monotonic() >= self.retry_at:
            self.loading_key = key
            self.task = asyncio.create_task(self._load(snapshot))
        return self.shape

    async def _load(self, snapshot: dict[str, Any]) -> Shape | None:
        def invoke() -> Shape | None | Awaitable[Shape | None]:
            return self.loader(snapshot)

        result = (
            invoke()
            if inspect.iscoroutinefunction(self.loader)
            else await asyncio.to_thread(invoke)
        )
        if inspect.isawaitable(result):
            return await result
        return result
