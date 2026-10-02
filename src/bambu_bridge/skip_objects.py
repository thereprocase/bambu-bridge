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
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
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

    @property
    def ids(self) -> frozenset[int]:
        return frozenset(o.id for o in self.objects)


def plate_index(raw: dict[str, Any]) -> int:
    """The printing plate: ``plate_idx`` when reported, else ``gcode_file``.

    MachineObject::parse_json reads ``plate_idx`` (number or numeric string);
    for a local task it takes N from ``.../plate_N.gcode``. Anything else
    (e.g. a ``job.gcode.3mf`` name) leaves PartSkipDialog on plate 1.
    """
    value = raw.get("plate_idx")
    try:
        index = int(value) if isinstance(value, int | str) and not isinstance(value, bool) else 0
    except (TypeError, ValueError):
        index = 0
    if index > 0:
        return index
    match = re.search(r"plate_(\d+)\.gcode$", str(raw.get("gcode_file") or ""))
    return int(match.group(1)) if match and int(match.group(1)) > 0 else 1


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


def unavailable_reason(raw: dict[str, Any]) -> str | None:
    """Printer-side reasons Orca would not offer skipping, else None."""
    if not part_skip_supported(raw):
        return "The printer does not report support for skipping objects"
    # enable_partskip_button: never for a system print or calibration
    # (push_status "print_type" == "system", parse_json; is_in_calibration:
    # an auto_cali_for_user gcode_file).
    calibrating = "auto_cali_for_user" in str(raw.get("gcode_file") or "")
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
            if pick is None and ids:
                try:
                    with zf.open(f"Metadata/plate_{plate}.gcode") as gcode:
                        pick = gcode_footprint(gcode, ids, _bed(zf))
                    source = "gcode" if pick is not None else None
                except KeyError:
                    pass
    except (zipfile.BadZipFile, OSError, EOFError) as exc:
        raise ValueError(f"project archive unreadable: {exc}") from exc
    return SkipJob(plate, objects[0], objects[1], pick, source)


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
    pick = np.zeros((height, width), dtype=np.uint32)
    for object_id, segs in segments.items():
        mask = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(mask)
        for c0, r0, c1, r1, w in segs:
            draw.line((c0, r0, c1, r1), fill=255, width=w)
            if w > 2:   # round the joints so corners are not notched
                rr = w / 2
                draw.ellipse((c1 - rr, r1 - rr, c1 + rr, r1 + rr), fill=255)
        filled = _fill_holes(np.asarray(mask) > 0)
        pick[filled & (pick == 0)] = object_id
    return pick


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


def job_reason(job: SkipJob) -> str | None:
    """PartSkipDialog::UpdateApplyButtonStatus refusals for the whole job."""
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
