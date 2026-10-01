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

import io
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
    # identify_id per pick-image pixel (0 = no listed object), or None.
    pick: np.ndarray | None = field(default=None, repr=False)

    @property
    def ids(self) -> frozenset[int]:
        return frozenset(o.id for o in self.objects)


def plate_index(raw: dict[str, Any]) -> int:
    """The printing plate: ``plate_idx`` when reported, else ``gcode_file``.

    MachineObject::parse_json reads ``plate_idx`` (number or numeric string);
    for a local task it takes N from ``.../plate_N.gcode``. PartSkipDialog
    falls back to plate 1.
    """
    value = raw.get("plate_idx")
    try:
        index = int(value) if isinstance(value, int | str) and not isinstance(value, bool) else 0
    except (TypeError, ValueError):
        index = 0
    if index > 0:
        return index
    match = re.search(r"_(\d+)\.[^._/]*$", str(raw.get("gcode_file") or ""))
    return int(match.group(1)) if match and int(match.group(1)) > 0 else 1


def skipped_ids(raw: dict[str, Any]) -> list[int]:
    """The printer's ``s_obj`` list (ids it has already skipped)."""
    value = raw.get("s_obj")
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, int) and not isinstance(v, bool)]


def part_skip_supported(raw: dict[str, Any]) -> bool:
    """``fun`` bit 49, the flag Orca shows its Skip button for.

    ``fun`` is a hex string (MachineObject::get_flag_bits). A printer that
    does not report it gets no Skip button in Orca (is_support_partskip stays
    false).
    """
    fun = raw.get("fun")
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
    # (print_type "system"; is_in_calibration: auto_cali_for_user gcode).
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
    """Parse the plate's objects and pick map from a project archive.

    Raises ValueError when the archive is unreadable.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            slice_info = _xml(zf, "Metadata/slice_info.config")
            model_settings = _xml(zf, "Metadata/model_settings.config")
            try:
                pick_png: bytes | None = zf.read(f"Metadata/pick_{plate}.png")
            except KeyError:
                pick_png = None
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"project archive unreadable: {exc}") from exc

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
    ordered = tuple(by_id[i] for i in sorted(by_id))
    pick = decode_pick(pick_png, frozenset(by_id)) if pick_png else None
    return SkipJob(plate, label_enabled, ordered, pick)


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
