"""Skip Objects, as OrcaSlicer 2.4.2's PartSkipDialog does it.

Orca (PartSkipDialog::DownloadPartsFile) fetches three members of the running
project from the printer: ``Metadata/pick_<plate>.png``,
``Metadata/model_settings.config`` and ``Metadata/slice_info.config``. The
bridge reads the same members from the job's .gcode.3mf on the printer's
storage (found by ``subtask_name``, like the viewer).

* Objects: the ``<object identify_id name>`` entries of the matching
  ``<plate>`` in slice_info.config, and its ``label_object_enabled`` flag
  (SkipPartCanvas.cpp ModelSettingHelper). A CLI slice made without arranging
  carries no ``<object>`` entries there; the plate's ``model_instance``
  identify_ids in model_settings.config are the same ids the G-code labels
  (``; model label id:``), so they stand in, named after their object.
* Map: each object is painted in pick_<plate>.png in the colour
  ``R | G << 8 | B << 16 == identify_id`` (GLCanvas3D::render_thumbnail_internal
  for_picking; decoded by SkipPartCanvas::GetIdAtImagePt). A tap hits the
  object whose id is at that pixel, if it is in the object list.
* Already skipped: the printer's ``s_obj`` list (MachineObject::parse_json).
* Command: ``print.skip_objects`` with ``obj_list`` (command_task_partskip).
  Selecting every remaining object stops the print instead
  (PartSkipDialog::OnApplyDialog calls command_task_abort).
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# PartSkipDialog::UpdateApplyButtonStatus: "Over 64 objects in single plate".
MAX_OBJECTS = 64
# DeviceManager.cpp parse "fun": is_support_partskip = get_flag_bits(fun, 49).
PART_SKIP_FUN_BIT = 49
# StatusPanel: enable_partskip_button(obj, true) only while printing and not
# preparing/slicing (PREPARE, SLICING) or finished (FINISH, FAILED).
SKIP_STATES = frozenset({"RUNNING", "PAUSE"})

ENABLE_ENV = "BRIDGE_ENABLE_SKIP_OBJECTS"

# SkipPartCanvas::Render colours (0-255).
_PLATE = (230, 230, 230)
_PART = (255, 255, 255)
_CHECKED = ((239, 175, 175), (208, 27, 27))   # fill, bound
_SKIPPED = ((159, 159, 159), (95, 95, 95))


def enabled() -> bool:
    """Operator opt-in that lifts skip_objects out of "under review"."""
    return os.environ.get(ENABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PartObject:
    id: int
    name: str


@dataclass(frozen=True, eq=False)
class SkipJob:
    """What PartSkipDialog::InitDialogUI loads for one plate."""

    plate: int
    label_object_enabled: bool
    objects: tuple[PartObject, ...]
    # identify_id per map pixel (0 = no listed object), or None.
    pick: np.ndarray | None = field(default=None, repr=False)
    # "pick" (Orca's pick_<plate>.png) or "gcode" (printed footprint).
    map_source: str | None = None
    # "; total layer number: N" from the plate G-code's header.
    total_layers: int | None = None

    @property
    def ids(self) -> frozenset[int]:
        return frozenset(o.id for o in self.objects)


_PLATE_MEMBER = re.compile(r"(?:^|/)plate_(\d+)\.gcode$")


def archive_plates(data: bytes) -> list[int]:
    """The N of every ``Metadata/plate_N.gcode`` in a project archive."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"project archive unreadable: {exc}") from exc
    found = (re.fullmatch(r"Metadata/plate_(\d+)\.gcode", n) for n in names)
    return sorted(int(m.group(1)) for m in found if m and int(m.group(1)) > 0)


def resolve_plate(
    raw: dict[str, Any], plates: list[int], recorded: int | None = None
) -> int | None:
    """The plate this archive is printing, or None when nothing proves it.

    In order: ``plate_idx`` (MachineObject::parse_json); a ``plate_N.gcode``
    in ``gcode_file`` (MachineObject's local-task parse), the printer's live
    report; the plate the bridge sent to start this very run (RunLog);
    else the archive's only plate. Named sources that disagree refuse. The
    deep-merged ``param`` is never read: any print.* echo overwrites it.
    PartSkipDialog falls back to plate 1; with several plates that would
    show and skip another plate's objects, so several plates and no name is
    None, as is a named plate the archive does not hold.
    """
    value = raw.get("plate_idx")
    try:
        index = int(value) if isinstance(value, int | str) and not isinstance(value, bool) else 0
    except (TypeError, ValueError):
        index = 0
    match = _PLATE_MEMBER.search(str(raw.get("gcode_file") or ""))
    named = {
        n for n in (index or None, int(match.group(1)) if match else None, recorded) if n
    }
    if len(named) > 1:
        return None
    if named:
        plate = named.pop()
        return plate if plate in plates else None
    return plates[0] if len(plates) == 1 else None


def layer_reason(job: SkipJob, raw: dict[str, Any]) -> str | None:
    """Refuse unless the plate G-code's layer count is the running print's.

    The bytes are the file of that name on the printer now; this proves they
    are the G-code the printer is running (an overwritten namesake or another
    plate of the archive almost always differs).
    """
    reported = raw.get("total_layer_num")
    try:
        layers = int(reported) if isinstance(reported, int | str) else None
    except ValueError:
        layers = None
    if isinstance(reported, bool):
        layers = None
    if job.total_layers is None or layers != job.total_layers:
        return "The file on the printer is not the one printing"
    return None


def _basename(value: Any) -> str:
    return str(value or "").rsplit("/", 1)[-1]


def skipped_ids(raw: dict[str, Any]) -> list[int]:
    """The printer's ``s_obj`` list (ids it has already skipped)."""
    value = raw.get("s_obj")
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, int) and not isinstance(v, bool)]


def part_skip_supported(raw: dict[str, Any]) -> bool:
    """Whether the printer can skip objects.

    With ``fun`` (a hex string), bit 49 decides, as Orca's
    is_support_partskip (MachineObject::get_flag_bits(fun, 49)).

    Deliberate deviation: Orca reads ``fun`` only in
    MachineObject::parse_new_info, which returns early unless the report
    carries cfg/fun/aux/stat (check_enable_np). A printer on the legacy push
    format, like the P1S, never sends ``fun``, so Orca would never offer the
    button. For such a printer the ``s_obj`` list it reports (the skipped
    objects parse_json reads) is taken as the support signal.
    """
    fun = raw.get("fun")
    if fun is None:
        return isinstance(raw.get("s_obj"), list)
    if not isinstance(fun, str) or not fun:
        return False
    try:
        return bool((int(fun, 16) >> PART_SKIP_FUN_BIT) & 1)
    except ValueError:
        return False


# CalibUtils::get_calib_mode_by_name: the subtask names of Orca's calibration
# prints ("retration" is Orca's spelling).
CALIB_MODE_NAMES = frozenset({
    "pa_line_calib_mode", "pa_pattern_calib_mode", "auto_pa_line_calib_mode",
    "flow_rate_coarse_calib_mode", "flow_rate_fine_calib_mode", "temp_tower_calib_mode",
    "vol_speed_tower_calib_mode", "vfa_tower_calib_mode", "retration_tower_calib_mode",
    "input_shaping_freq_calib_mode", "input_shaping_damp_calib_mode", "cornering_calib_mode",
})


def unavailable_reason(raw: dict[str, Any]) -> str | None:
    """Printer-side reasons Orca would not offer skipping, else None."""
    if not part_skip_supported(raw):
        return "The printer does not report support for skipping objects"
    # PrintingTaskPanel::enable_partskip_button refuses print_type "system"
    # (push_status, parse_json) and a subtask_name that
    # CalibUtils::get_calib_mode_by_name knows; StatusPanel hides the button
    # while is_in_calibration (an auto_cali_for_user gcode_file).
    calibrating = (
        raw.get("subtask_name") in CALIB_MODE_NAMES
        or "auto_cali_for_user" in str(raw.get("gcode_file") or "")
    )
    if raw.get("print_type") == "system" or calibrating:
        return "Calibration prints cannot skip objects"
    state = raw.get("gcode_state")
    if state not in SKIP_STATES:
        return f"Printer state: {state}"
    return None


def _xml(zf: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        return ET.fromstring(zf.read(name))
    except (KeyError, ET.ParseError):
        return None


def _meta(element: ET.Element) -> dict[str, str]:
    return {m.get("key", ""): m.get("value", "") for m in element.findall("metadata")}


def _model_settings_objects(root: ET.Element | None, plate: int) -> list[PartObject]:
    if root is None:
        return []
    names = {o.get("id"): _meta(o).get("name", "") for o in root.findall("object")}
    for p in root.findall("plate"):
        if _meta(p).get("plater_id") != str(plate):
            continue
        out = []
        for inst in p.findall("model_instance"):
            m = _meta(inst)
            if m.get("identify_id", "").isdigit():
                out.append(PartObject(int(m["identify_id"]), names.get(m.get("object_id"), "")))
        return out
    return []


def read_job(data: bytes, plate: int) -> SkipJob:
    """Parse the plate's objects and map from a project archive.

    The map is Orca's pick image when the project has one; otherwise (CLI
    slices, which render no thumbnails) the objects' printed footprint from
    the plate's G-code. Raises ValueError when the archive is unreadable.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            slice_info = _xml(zf, "Metadata/slice_info.config")
            model_settings = _xml(zf, "Metadata/model_settings.config")
            objects = _objects(slice_info, model_settings, plate)
            ids = frozenset(o.id for o in objects[1])
            try:
                pick = decode_pick(zf.read(f"Metadata/pick_{plate}.png"), ids)
                source = "pick" if pick is not None else None
            except KeyError:
                pick, source = None, None
            try:
                with zf.open(f"Metadata/plate_{plate}.gcode") as gcode:
                    layers = _total_layers(gcode)
                if pick is None and ids:
                    with zf.open(f"Metadata/plate_{plate}.gcode") as gcode:
                        pick = gcode_footprint(gcode, ids, _bed(zf))
                    source = "gcode" if pick is not None else None
            except KeyError:
                layers = None
    except (zipfile.BadZipFile, OSError, EOFError) as exc:
        raise ValueError(f"project archive unreadable: {exc}") from exc
    return SkipJob(plate, objects[0], objects[1], pick, source, layers)


_LAYERS = b"; total layer number:"


def _total_layers(lines: Any) -> int | None:
    """``; total layer number: N`` from the G-code header block."""
    for n, raw in enumerate(lines):
        if raw.startswith(_LAYERS):
            try:
                return int(raw[len(_LAYERS):])
            except ValueError:
                return None
        if n > 200 or raw.startswith(b"; HEADER_BLOCK_END"):
            return None
    return None


def _objects(
    slice_info: ET.Element | None, model_settings: ET.Element | None, plate: int
) -> tuple[bool, tuple[PartObject, ...]]:
    label_enabled = False
    objects: list[PartObject] = []
    for p in slice_info.findall("plate") if slice_info is not None else []:
        if _meta(p).get("index") != str(plate):
            continue
        label_enabled = _meta(p).get("label_object_enabled") == "true"
        for o in p.findall("object"):
            if (o.get("identify_id") or "").isdigit():
                objects.append(PartObject(int(o.get("identify_id", "")), o.get("name", "")))
    if not objects:
        objects = _model_settings_objects(model_settings, plate)
    # PartSkipDialog keeps parts in a std::map keyed by id: listed by id.
    by_id = {o.id: o for o in objects}
    return label_enabled, tuple(by_id[i] for i in sorted(by_id))


# G-code footprint map ------------------------------------------------------ #

PX_PER_MM = 2.0
_DEFAULT_WIDTH_MM = 0.45
_BED_MM = (0.0, 0.0, 256.0, 256.0)   # P1S printable_area
_START = b"; start printing object, unique label id:"
_STOP = b"; stop printing object"
_WIDTH = b"; LINE_WIDTH:"
_ARC_STEP_MM = 1.0


def _bed(zf: zipfile.ZipFile) -> tuple[float, float, float, float]:
    """The printable_area bounds (min x, min y, max x, max y) in mm."""
    try:
        area = json.loads(zf.read("Metadata/project_settings.config"))["printable_area"]
        pts = [tuple(float(v) for v in p.split("x")) for p in area]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        if max(xs) > min(xs) and max(ys) > min(ys):
            return min(xs), min(ys), max(xs), max(ys)
    except (KeyError, ValueError, TypeError, IndexError):
        pass
    return _BED_MM


def gcode_footprint(
    lines: Any, ids: frozenset[int], bed: tuple[float, float, float, float] = _BED_MM
) -> np.ndarray | None:
    """identify_id per pixel of the bed from the objects' extrusions, or None.

    Every extruding move between "; start printing object, unique label id: N"
    and "; stop printing object" is drawn at its LINE_WIDTH (else 0.45 mm),
    the union over all layers, with holes inside each object filled. Pixels
    are PX_PER_MM, X to the right and Y up, as Orca's top-down pick image
    shows the plate. ``lines`` yields bytes lines (a streamed member).
    """
    segments: dict[int, set[tuple[int, int, int, int, int]]] = {}
    x0, y0, x1, y1 = bed
    width = int(round((x1 - x0) * PX_PER_MM))
    height = int(round((y1 - y0) * PX_PER_MM))

    def px(x: float, y: float) -> tuple[int, int]:
        return round((x - x0) * PX_PER_MM), round((y1 - y) * PX_PER_MM)

    x = y = e = 0.0
    abs_xy, abs_e = True, False
    current: set[tuple[int, int, int, int, int]] | None = None
    line_px = max(1, round(_DEFAULT_WIDTH_MM * PX_PER_MM))
    for raw in lines:
        head = raw[:1]
        if head == b";":
            if raw.startswith(_START):
                try:
                    object_id = int(raw[len(_START):])
                except ValueError:
                    object_id = 0
                current = segments.setdefault(object_id, set()) if object_id in ids else None
            elif raw.startswith(_STOP):
                current = None
            elif raw.startswith(_WIDTH):
                with contextlib.suppress(ValueError):
                    line_px = max(1, round(float(raw[len(_WIDTH):]) * PX_PER_MM))
            continue
        if head not in (b"G", b"M"):
            continue
        words = raw.split(b";", 1)[0].split()
        if not words:
            continue
        cmd = words[0]
        if cmd == b"G90":
            abs_xy = True
        elif cmd == b"G91":
            abs_xy = False
        elif cmd == b"M82":
            abs_e = True
        elif cmd == b"M83":
            abs_e = False
        elif cmd == b"G92":
            for w in words[1:]:
                if w[:1] == b"E":
                    with contextlib.suppress(ValueError):
                        e = float(w[1:] or 0)
        elif cmd in (b"G0", b"G1", b"G2", b"G3"):
            v: dict[bytes, float] = {}
            for w in words[1:]:
                try:  # noqa: SIM105 — hot loop; suppress() costs a context per word
                    v[w[:1]] = float(w[1:])
                except ValueError:
                    pass
            nx = (v[b"X"] if abs_xy else x + v[b"X"]) if b"X" in v else x
            ny = (v[b"Y"] if abs_xy else y + v[b"Y"]) if b"Y" in v else y
            de = 0.0
            if b"E" in v:
                de = v[b"E"] - e if abs_e else v[b"E"]
                e = v[b"E"] if abs_e else e + v[b"E"]
            if current is not None and de > 0 and (nx, ny) != (x, y):
                pts = _arc(x, y, nx, ny, v, cmd == b"G2") if cmd in (b"G2", b"G3") else [
                    (x, y), (nx, ny)
                ]
                for (ax, ay), (bx, by) in zip(pts, pts[1:], strict=False):
                    current.add((*px(ax, ay), *px(bx, by), line_px))
            x, y = nx, ny
    if not segments:
        return None
    # Printed pixels first, for every object; then hole fills, smallest
    # first, only where still empty: a part inside another's opening keeps
    # its own pixels and its own interior.
    pick = np.zeros((height, width), dtype=np.uint32)
    masks: dict[int, np.ndarray] = {}
    for object_id, segs in segments.items():
        mask = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(mask)
        for c0, r0, c1, r1, w in segs:
            draw.line((c0, r0, c1, r1), fill=255, width=w)
            if w > 2:   # round the joints so corners are not notched
                rr = w / 2
                draw.ellipse((c1 - rr, r1 - rr, c1 + rr, r1 + rr), fill=255)
        masks[object_id] = np.asarray(mask) > 0
        pick[masks[object_id] & (pick == 0)] = object_id
    # Then every enclosed hole, one connected component at a time across all
    # objects, smallest first: a hole nested in another hole is always the
    # smaller, so a body inside an opening keeps its own bore.
    holes = [
        (int(part.sum()), object_id, part)
        for object_id, printed in masks.items()
        for part in _components(_fill_holes(printed) & ~printed)
    ]
    for _area, object_id, part in sorted(holes, key=lambda h: (h[0], h[1])):
        pick[part & (pick == 0)] = object_id
    return pick


def _components(mask: np.ndarray) -> list[np.ndarray]:
    """4-connected regions of ``mask``, by union-find over row runs."""
    runs: list[tuple[int, int, int]] = []          # (row, start, stop)
    row_runs: list[list[int]] = []
    for r, row in enumerate(mask):
        edges = np.flatnonzero(np.diff(np.concatenate(([0], row.view(np.uint8), [0]))))
        row_runs.append(list(range(len(runs), len(runs) + len(edges) // 2)))
        runs.extend((r, int(a), int(b)) for a, b in zip(edges[0::2], edges[1::2], strict=True))
    parent = list(range(len(runs)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for r in range(1, len(row_runs)):
        above = row_runs[r - 1]
        for i in row_runs[r]:
            _, a, b = runs[i]
            for j in above:
                _, c, d = runs[j]
                if a < d and c < b:   # column ranges overlap: 4-connected
                    parent[root(i)] = root(j)
    groups: dict[int, list[int]] = {}
    for i in range(len(runs)):
        groups.setdefault(root(i), []).append(i)
    out = []
    for members in groups.values():
        part = np.zeros_like(mask)
        for i in members:
            r, a, b = runs[i]
            part[r, a:b] = True
        out.append(part)
    return out


def _arc(
    x: float, y: float, nx: float, ny: float, v: dict[bytes, float], clockwise: bool
) -> list[tuple[float, float]]:
    """A G2/G3 arc (I/J centre offsets) as short chords."""
    cx, cy = x + v.get(b"I", 0.0), y + v.get(b"J", 0.0)
    r = math.hypot(x - cx, y - cy)
    a0, a1 = math.atan2(y - cy, x - cx), math.atan2(ny - cy, nx - cx)
    sweep = a1 - a0
    if clockwise and sweep >= 0:
        sweep -= 2 * math.pi
    elif not clockwise and sweep <= 0:
        sweep += 2 * math.pi
    n = max(2, math.ceil(abs(sweep) * r / _ARC_STEP_MM))
    return [(cx + r * math.cos(a0 + sweep * i / n), cy + r * math.sin(a0 + sweep * i / n))
            for i in range(n + 1)]


def _fill_holes(mask: np.ndarray) -> np.ndarray:
    """``mask`` plus every background region it encloses (not reaching its box edge)."""
    rows, cols = np.flatnonzero(mask.any(1)), np.flatnonzero(mask.any(0))
    if not rows.size:
        return mask
    r0, r1, c0, c1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
    box = np.pad(mask[r0:r1, c0:c1], 1)
    open_ = ~box
    reach = np.zeros_like(box)
    reach[0, :] = reach[-1, :] = reach[:, 0] = reach[:, -1] = True
    reach &= open_
    while True:
        grown = reach.copy()
        grown[1:] |= reach[:-1]
        grown[:-1] |= reach[1:]
        grown[:, 1:] |= reach[:, :-1]
        grown[:, :-1] |= reach[:, 1:]
        grown &= open_
        if np.array_equal(grown, reach):
            break
        reach = grown
    out = mask.copy()
    out[r0:r1, c0:c1] |= ~reach[1:-1, 1:-1]
    return out


def decode_pick(png: bytes, ids: frozenset[int]) -> np.ndarray | None:
    """identify_id per pixel; colours that are not a listed id become 0.

    Alpha is dropped as SkipPartCanvas::LoadPickImage does (BGRA2BGR).
    """
    try:
        with Image.open(io.BytesIO(png)) as im:
            rgb = np.asarray(im.convert("RGB"), dtype=np.uint32)
    except (OSError, ValueError):
        return None
    decoded = rgb[..., 0] | (rgb[..., 1] << 8) | (rgb[..., 2] << 16)
    known = np.array(sorted(ids), dtype=np.uint32)
    return np.where(np.isin(decoded, known), decoded, 0).astype(np.uint32)


def run_rows(pick: np.ndarray) -> list[list[int]]:
    """Run-length rows ``[id, count, id, count, ...]`` for client hit-testing."""
    rows = []
    for row in pick:
        starts = np.flatnonzero(np.r_[True, row[1:] != row[:-1]])
        counts = np.diff(np.append(starts, row.size))
        rows.append([int(v) for pair in zip(row[starts], counts, strict=True) for v in pair])
    return rows


def render_map(pick: np.ndarray, checked: frozenset[int], skipped: frozenset[int]) -> bytes:
    """PNG of the plate in SkipPartCanvas::Render's colours.

    Every part is white; checked parts are pink with a red bound, skipped
    parts grey with a dark grey bound, on a light grey rounded plate.
    """
    h, w = pick.shape
    out = np.zeros((h, w, 4), dtype=np.uint8)
    plate = Image.new("L", (w, h), 0)
    ImageDraw.Draw(plate).rounded_rectangle(
        (0, 0, w - 1, h - 1), radius=min(w, h) * 0.05, fill=255
    )
    out[np.asarray(plate) > 0] = (*_PLATE, 255)
    out[pick > 0] = (*_PART, 255)
    for ids, (fill, bound) in ((checked, _CHECKED), (skipped, _SKIPPED)):
        if not ids:
            continue
        mask = np.isin(pick, np.array(sorted(ids), dtype=np.uint32))
        eroded = Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.MinFilter(5))
        inner = np.asarray(eroded)
        out[mask] = (*bound, 255)
        out[inner > 0] = (*fill, 255)
    buf = io.BytesIO()
    Image.fromarray(out, "RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


UNKNOWN_PLATE = "The printer does not say which plate of this file is printing"


PENDING_S = 30.0
START_WINDOW_S = 3600.0


@dataclass(frozen=True)
class StartRecord:
    """A project_file start the bridge published, and the run it began."""

    archive: str
    plate: int | None
    subtask: str | None
    at: float
    started_at: str | None = None


class RunLog:
    """What the bridge itself sent for the current run of one printer.

    PrinterService feeds it every command it publishes, whoever asked (the
    web, the app, the queue, Orca's print-host or native relay, a library
    replay), and the run edges. It remembers:

    * the plate of the last project_file start, bound to the run that next
      reaches RUNNING with the same subtask (a start that never ran expires);
    * the ids of every print.skip_objects sent this run, which count as
      skipped until the printer echoes them in s_obj or PENDING_S passes,
      like Orca's set_part_skipped_dirty filter after an apply.
    """

    def __init__(self) -> None:
        self.start: StartRecord | None = None
        self.run: StartRecord | None = None
        self.skips: dict[int, float] = {}

    def sent(self, envelope: dict[str, Any], now: float | None = None) -> None:
        body = envelope.get("print")
        if not isinstance(body, dict):
            return
        now = time.monotonic() if now is None else now
        if body.get("command") == "project_file":
            match = _PLATE_MEMBER.search(str(body.get("param") or ""))
            self.start = StartRecord(
                _basename(body.get("url")),
                int(match.group(1)) if match else None,
                body.get("subtask_name"),
                now,
            )
        elif body.get("command") == "skip_objects":
            for object_id in body.get("obj_list") or []:
                if isinstance(object_id, int) and not isinstance(object_id, bool):
                    self.skips[object_id] = now + PENDING_S

    def started(self, subtask: Any, started_at: str | None, now: float | None = None) -> None:
        """A fresh RUNNING edge: bind the last start to this run, if it is this run's."""
        now = time.monotonic() if now is None else now
        record, self.start = self.start, None
        if record and now - record.at <= START_WINDOW_S and record.subtask == subtask:
            self.run = replace(record, started_at=started_at)
        else:
            self.run = None
        self.skips = {}

    def ended(self) -> None:
        """FINISH, FAILED or a lost job: nothing carries into the next run."""
        self.run = None
        self.skips = {}

    def plate(self, subtask: Any, archive: str, started_at: Any) -> int | None:
        run = self.run
        if run and started_at and (run.started_at, run.subtask, run.archive) == (
            started_at, subtask, archive
        ):
            return run.plate
        return None

    def pending(self, reported: list[int], now: float | None = None) -> set[int]:
        now = time.monotonic() if now is None else now
        self.skips = {i: t for i, t in self.skips.items() if t > now and i not in reported}
        return set(self.skips)


def job_reason(job: SkipJob) -> str | None:
    """PartSkipDialog::UpdateApplyButtonStatus refusals for the whole job."""
    if job.plate <= 0:
        return UNKNOWN_PLATE
    if not job.label_object_enabled:
        return "The current print job cannot be skipped"
    if len(job.objects) > MAX_OBJECTS:
        return f"Over {MAX_OBJECTS} objects in single plate"
    return None


def plan(job: SkipJob, skipped: list[int], requested: list[int]) -> tuple[str, list[int]]:
    """Check a selection; ``("skip", ids)``, or ``("stop", [])`` when none would remain.

    Orca's dialog only offers listed, not-yet-skipped objects; anything else
    raises ValueError.
    """
    chosen = list(dict.fromkeys(requested))
    if not chosen:
        raise ValueError("Nothing selected")
    unknown = [i for i in chosen if i not in job.ids]
    if unknown:
        raise ValueError(f"Objects not in the current print: {unknown}")
    done = [i for i in chosen if i in skipped]
    if done:
        raise ValueError(f"Objects already skipped: {done}")
    remaining = job.ids - set(skipped) - set(chosen)
    return ("skip", chosen) if remaining else ("stop", [])
