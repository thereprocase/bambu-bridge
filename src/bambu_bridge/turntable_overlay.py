"""Bounded finished exterior-path preview, orthographic fixed-scale platter rotation."""

from __future__ import annotations

import asyncio
import math
import time
import weakref
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, BinaryIO

from PIL import Image, ImageDraw, ImageFilter, ImageFont

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
    faces: tuple[tuple[float, ...], ...] = ()


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
    return Shape(
        segments, radius, height, wall_count, exterior_faces(walls.positions, surfaces.positions)
    )


# bench-ink-v1 from peg-board50: four constant light bands and charcoal ink.
# Keep this CPU renderer small; Blender/Freestyle is not a live-video dependency.
INK = (5, 7, 9, 255)
BASE = (82, 218, 193)
BANDS = (0.22, 0.48, 0.76, 1.0)
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
    if len(faces) <= MAX_FACES:
        return tuple(faces)
    # Over budget (big multi-part plates: ~150k wall segments, 79 layers). Thinning
    # faces[::stride] left random 0.16 mm slivers all through the height. Spend the
    # budget on whole layers instead: complete outlines at evenly spaced heights,
    # always including the top, each ribbon spanning down to the level below.
    return _banded_faces(walls, skins)


def _segments_by_level(path: Any) -> dict[float, list[tuple[float, float, float, float]]]:
    levels: dict[float, list[tuple[float, float, float, float]]] = {}
    for i in range(0, len(path), 6):
        x, y, z, u, v, q = (float(n) for n in path[i : i + 6])
        if abs(z - q) > 0.001 or math.hypot(u - x, v - y) < 0.001:
            continue
        if not all(math.isfinite(n) and abs(n) <= 1000 for n in (x, y, z, u, v, q)):
            continue
        levels.setdefault(round(z, 4), []).append((x, y, u, v))
    return levels


def _chords(
    segments: list[tuple[float, float, float, float]], step: int
) -> list[tuple[float, ...]]:
    """Merge runs of connected segments into chords of up to ``step`` segments."""
    if step <= 1:
        return list(segments)
    out: list[tuple[float, ...]] = []
    run: list[tuple[float, float, float, float]] = []
    for seg in segments:
        if run and (
            len(run) >= step or math.hypot(seg[0] - run[-1][2], seg[1] - run[-1][3]) > 0.01
        ):
            out.append((run[0][0], run[0][1], run[-1][2], run[-1][3]))
            run = []
        run.append(seg)
    if run:
        out.append((run[0][0], run[0][1], run[-1][2], run[-1][3]))
    return [c for c in out if math.hypot(c[2] - c[0], c[3] - c[1]) >= 0.001]


def _straighten(
    segments: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    """Join connected, nearly collinear segments (arc-fit fragments, straight edges split
    by the slicer) so the face budget goes to shape, not to redundant vertices."""
    out: list[tuple[float, float, float, float]] = []
    for seg in segments:
        if out:
            x, y, u, v = out[-1]
            if math.hypot(seg[0] - u, seg[1] - v) < 0.01:
                a = math.atan2(v - y, u - x)
                b = math.atan2(seg[3] - seg[1], seg[2] - seg[0])
                if abs((a - b + math.pi) % math.tau - math.pi) < math.radians(6):
                    out[-1] = (x, y, seg[2], seg[3])
                    continue
        out.append(seg)
    return out


def _split(face: tuple[float, ...], piece: float) -> list[tuple[float, ...]]:
    """Cut a long wall ribbon into <= ``piece`` mm lengths so painter's sorting by centre
    does not draw a long far face over a short near one."""
    x, y, lo, u, v = face[0], face[1], face[2], face[3], face[4]
    hi = face[8]
    n = max(1, math.ceil(math.hypot(u - x, v - y) / piece))
    if n == 1:
        return [face]
    out = []
    for i in range(n):
        a, b = i / n, (i + 1) / n
        x0, y0 = x + (u - x) * a, y + (v - y) * a
        x1, y1 = x + (u - x) * b, y + (v - y) * b
        out.append((x0, y0, lo, x1, y1, lo, x1, y1, hi, x0, y0, hi, *face[12:]))
    return out


def _banded_faces(walls: Any, skins: Any) -> tuple[tuple[float, ...], ...]:
    wall_levels = {z: _straighten(v) for z, v in _segments_by_level(walls).items()}
    if not wall_levels:
        return ()
    levels = sorted(wall_levels)
    wall_budget = MAX_FACES * 3 // 4
    per_level = max(len(v) for v in wall_levels.values())
    bands = max(1, min(len(levels), wall_budget // max(1, per_level)))
    # evenly spaced from the top down, so the finished top outline is always exact
    picks = sorted(
        {
            levels[len(levels) - 1 - round(i * (len(levels) - 1) / max(1, bands - 1))]
            for i in range(bands)
        }
        if bands > 1
        else {levels[-1]}
    )
    budget_each = wall_budget // len(picks)
    faces: list[tuple[float, ...]] = []
    floor = 0.0
    for z in picks:
        segs = wall_levels[z]
        for x, y, u, v in _chords(segs, math.ceil(len(segs) / max(1, budget_each))):
            length = math.hypot(u - x, v - y)
            faces.append(
                (
                    x - 128,
                    y - 128,
                    floor,
                    u - 128,
                    v - 128,
                    floor,
                    u - 128,
                    v - 128,
                    z,
                    x - 128,
                    y - 128,
                    z,
                    (v - y) / length,
                    (x - u) / length,
                    0.0,
                )
            )
        floor = z
    # split long ribbons while there is room (depth-sort quality)
    room = MAX_FACES * 3 // 4 - len(faces)
    if room > 0:
        for piece in (8.0, 16.0, 32.0):
            split = [f for face in faces for f in _split(face, piece)]
            if len(split) - len(faces) <= room:
                faces = split
                break
    # Top cap: only the highest skin layer is visible from above; widen thinned
    # ribbons in proportion so the cap stays closed instead of striped.
    skin_levels = _segments_by_level(skins)
    if skin_levels:
        top = skin_levels[max(skin_levels)]
        room = max(1, MAX_FACES - len(faces))
        stride = max(1, math.ceil(len(top) / room))
        half = 0.25 * stride
        z = max(skin_levels)
        for x, y, u, v in top[::stride]:
            length = math.hypot(u - x, v - y)
            dx, dy = -(v - y) / length * half, (u - x) / length * half
            faces.append(
                (
                    x - 128 + dx,
                    y - 128 + dy,
                    z,
                    u - 128 + dx,
                    v - 128 + dy,
                    z,
                    u - 128 - dx,
                    v - 128 - dy,
                    z,
                    x - 128 - dx,
                    y - 128 - dy,
                    z,
                    0.0,
                    0.0,
                    1.0,
                )
            )
    return tuple(faces[:MAX_FACES])


def cel_color(normal: tuple[float, ...], angle: float) -> tuple[int, ...]:
    nx, ny, nz = normal
    rx, ry = (
        nx * math.cos(angle) - ny * math.sin(angle),
        nx * math.sin(angle) + ny * math.cos(angle),
    )
    # Two-sided exterior ribbons: orient toward the camera before lighting.
    if -ry * math.cos(PITCH) + nz * math.sin(PITCH) < 0:
        rx, ry, nz = -rx, -ry, -nz
    vertical = ry * math.sin(PITCH) + nz * math.cos(PITCH)
    facing = -ry * math.cos(PITCH) + nz * math.sin(PITCH)
    light = max(0.0, min(1.0, (-0.65 * rx + 0.85 * vertical + 1.3 * facing) / 1.683))
    band = BANDS[sum(light >= threshold for threshold in (0.2, 0.48, 0.78))]
    return (*(round(c * band) for c in BASE), 255)


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
        return -(x * sinr + y * cosr) * math.cos(PITCH) + z * math.sin(PITCH)

    colors: dict[tuple[float, ...], tuple[int, ...]] = {}
    for face in sorted(shape.faces, key=depth):
        normal = face[12:]
        if normal not in colors:
            colors[normal] = cel_color(normal, angle)
        points = [
            project(face[0], face[1], face[2]),
            project(face[3], face[4], face[5]),
            project(face[6], face[7], face[8]),
            project(face[9], face[10], face[11]),
        ]
        draw.polygon(points, fill=colors[normal])
    # Ink only the visible silhouette: no wireframe or extrusion seam clutter.
    mask = layer.getchannel("A")
    ink = Image.new("RGBA", panel.size, INK)
    panel.paste(ink, (0, 0), mask.filter(ImageFilter.MaxFilter(3)))
    panel.alpha_composite(layer)


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


FRAMES = 120  # cached angle steps per revolution (3 degrees, 0.5 s at 1 rpm)
SUPERSAMPLE = 2  # panel drawn at 2x then downsampled: smooth ink and ribbon edges
_PANELS: dict[tuple[int, int, int], tuple[Any, dict[int, Image.Image]]] = {}


def _panel(shape: Shape, w: int, h: int, index: int) -> Image.Image:
    """One rotation step of the finished-shape panel, rendered once per job and size."""
    key = (id(shape), w, h)
    entry = _PANELS.get(key)
    if entry is None or entry[0]() is not shape:
        _PANELS.clear()  # one job at a time: drop the old job's frames
        entry = (weakref.ref(shape), {})
        _PANELS[key] = entry
    frames = entry[1]
    cached = frames.get(index)
    if cached is not None:
        return cached
    k = SUPERSAMPLE
    big = Image.new("RGBA", (w * k, h * k))
    draw = ImageDraw.Draw(big)
    draw.rounded_rectangle((0, 0, w * k - 1, h * k - 1), radius=10 * k, fill=(12, 19, 26, 190))
    draw.rounded_rectangle((0, 9 * k, 3 * k, (h - 10) * k), radius=2 * k, fill="#9ee8cf")
    angle = index * math.tau / FRAMES + math.pi / 4
    project = projection(shape, w * k, h * k, angle)
    plate = [project(x, y, 0) for x, y in [(-128, -128), (128, -128), (128, 128), (-128, 128)]]
    draw.polygon(plate, fill=(40, 56, 67, 240), outline=(107, 137, 151, 255))
    for offset in (-64, 0, 64):
        draw.line(
            [project(offset, -128, 0), project(offset, 128, 0)], fill=(68, 85, 94, 255), width=k
        )
        draw.line(
            [project(-128, offset, 0), project(128, offset, 0)], fill=(68, 85, 94, 255), width=k
        )

    def depth(item: tuple[int, tuple[float, ...]]) -> float:
        _, segment = item
        x, y, z = ((segment[i] + segment[i + 3]) / 2 for i in range(3))
        ry = x * math.sin(angle) + y * math.cos(angle)
        return -ry * math.cos(PITCH) + z * math.sin(PITCH)

    if shape.faces:
        paint_cel(big, shape, project, angle)
    else:
        for i, segment in sorted(enumerate(shape.segments), key=depth):
            wall = shape.wall_count is None or i < shape.wall_count
            color = (62, 193, 180, 255) if wall else (102, 218, 199, 255)
            draw.line([project(*segment[:3]), project(*segment[3:])], fill=color, width=k)
    panel = big.resize((w, h), Image.Resampling.LANCZOS)
    font = ImageFont.load_default(size=max(9, round(w / 19)))
    ImageDraw.Draw(panel).text((10, 8), "Finished shape", font=font, fill="#9ee8cf")
    frames[index] = panel
    return panel


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
    index = int((seconds % 60) / 60 * FRAMES) % FRAMES
    panel = _panel(shape, w, h, index)
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
