"""Stdlib-only gcode toolpath extractor (spec G4).

Parses the plain-text gcode found at ``Metadata/plate_1.gcode`` inside a
Bambu ``.gcode.3mf`` ZIP archive and produces a flat ``array('f')`` of
vertex pairs in GL_LINES layout (xyzxyz per segment) in mm, Z-up.

Extrusion moves only: a segment is recorded when E **strictly increases** and
at least one of X or Y changes.  Travel moves, retractions (E decreasing),
and pure Z-hops are excluded.

Flat-segment convention (recorded choice)
-----------------------------------------
Every recorded extrusion segment is stored with ``z0 == z1 == cur_z`` — the Z
of the layer the nozzle is currently on.  A move that simultaneously changes Z
while extruding (e.g. spiral-vase / "vase mode" or any continuous-Z slice) is
therefore *flattened* onto its starting Z; the segment's end Z is not raised to
``new_z``.  This is intentional for planar FDM, where every printed line lives
in a single horizontal layer, and it keeps the per-layer grouping
(``layers[cur_z]``), the collinear-merge, and the viewer's layer table coherent
(each segment belongs to exactly one Z bucket).  The cost is that spiral-vase
prints render as a stack of flat rings rather than one continuous helix.  Do
not "fix" this by recording ``z1 = new_z`` without also reworking the layer
bucketing — the two are coupled.

E-mode handling
---------------
Bambu sliced gcode uses one of two E styles:

* **Absolute E with G92 E0 resets** (``M82`` or no M-code before first move):
  E is an absolute odometer that is periodically reset to 0 with ``G92 E0``.
  Extrusion is detected by ``E_new > E_prev`` after each reset.

* **Relative E** (``M83``): every E value is a per-move delta.  Any positive
  E value means filament was extruded.

The parser tracks the active mode and handles both, plus ``M82`` switches back
to absolute.  ``G92 E0`` (or ``G92 E<any>`` but ``E0`` is the Bambu norm)
resets the absolute accumulator without marking a retraction.

XYZ are always absolute (``G90``) in Bambu gcode; ``G91`` relative-XYZ mode
is handled for correctness but is not expected in practice.

Collinear merge
---------------
Consecutive segments at the same Z whose direction vectors satisfy
``|cross(d1, d2)| < _COLLINEAR_EPS`` (unit-normalised cross product) are
merged into a single segment.

Budget
------
At most ``_SEGMENT_BUDGET`` (250 000) segments are returned.  If the raw
toolpath exceeds the budget after collinear merge, segments are thinned
uniformly within each Z-layer using a step-skip approach (keep every
``ceil(n_layer / budget_share)``-th segment) so that every layer retains
at least one segment.  ``decimated=True`` is set in the result.

Guards
------
* Input bytes > 120 MB: raises :class:`GcodeParseError`.
* Non-numeric coordinate fields: line is silently skipped.
* Zero extrusion segments: returns ``segment_count=0``; NOT an error —
  the viewer shows its own "no toolpath" message.
"""

from __future__ import annotations

import array
import io
import math
import zipfile
from dataclasses import dataclass

# ------------------------------------------------------------------ #
# Constants
# ------------------------------------------------------------------ #

_MAX_INPUT_BYTES = 120 * 1024 * 1024  # 120 MB raw input guard
_SEGMENT_BUDGET = 250_000             # max output segments
_COLLINEAR_EPS = 1e-6                 # cross-product magnitude cutoff (unit vecs)

# ------------------------------------------------------------------ #
# Error
# ------------------------------------------------------------------ #


class GcodeParseError(ValueError):
    """Raised when the gcode input cannot be parsed at all (too large, etc.)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ------------------------------------------------------------------ #
# Output dataclass
# ------------------------------------------------------------------ #


@dataclass
class GcodeToolpath:
    """Parsed gcode toolpath (extrusion moves only).

    ``positions`` is a flat ``array('f')`` in GL_LINES layout:
    each segment occupies two consecutive vertices (start, end),
    each vertex is xyz, so the stride is 6 floats per segment.

    All coordinates are in millimeters, Z-up convention.
    """

    positions: array.array[float]  # array('f'), layout xyzxyz per segment
    segment_count: int
    bbox_min: list[float]  # [x, y, z]
    bbox_max: list[float]  # [x, y, z]
    decimated: bool = False


# ------------------------------------------------------------------ #
# Segment accumulator by Z layer
# ------------------------------------------------------------------ #


@dataclass
class _Segment:
    x0: float
    y0: float
    z0: float
    x1: float
    y1: float
    z1: float


# ------------------------------------------------------------------ #
# Collinear merge helpers
# ------------------------------------------------------------------ #


def _direction(seg: _Segment) -> tuple[float, float, float] | None:
    """Unit direction vector of a segment, or None if zero-length."""
    dx = seg.x1 - seg.x0
    dy = seg.y1 - seg.y0
    dz = seg.z1 - seg.z0
    mag = math.sqrt(dx * dx + dy * dy + dz * dz)
    if mag < 1e-9:
        return None
    return dx / mag, dy / mag, dz / mag


def _cross_mag(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> float:
    """Magnitude of the cross product of two 3-vectors."""
    cx = a[1] * b[2] - a[2] * b[1]
    cy = a[2] * b[0] - a[0] * b[2]
    cz = a[0] * b[1] - a[1] * b[0]
    return math.sqrt(cx * cx + cy * cy + cz * cz)


def _merge_collinear(segs: list[_Segment]) -> list[_Segment]:
    """Merge consecutive collinear segments that share the same Z into one."""
    if not segs:
        return segs

    out: list[_Segment] = []
    current = segs[0]
    cur_dir = _direction(current)

    for nxt in segs[1:]:
        if nxt.z0 != current.z0 or nxt.z1 != current.z1:
            out.append(current)
            current = nxt
            cur_dir = _direction(current)
            continue

        nxt_dir = _direction(nxt)
        collinear = False
        if cur_dir is not None and nxt_dir is not None:
            collinear = _cross_mag(cur_dir, nxt_dir) < _COLLINEAR_EPS
        elif cur_dir is None and nxt_dir is None:
            collinear = True

        if collinear:
            # Extend current to cover nxt's endpoint.
            current = _Segment(
                current.x0, current.y0, current.z0,
                nxt.x1, nxt.y1, nxt.z1,
            )
            # Recompute direction after extension for the next comparison.
            cur_dir = _direction(current)
        else:
            out.append(current)
            current = nxt
            cur_dir = _direction(current)

    out.append(current)
    return out


# ------------------------------------------------------------------ #
# Budget decimation
# ------------------------------------------------------------------ #


def _decimate(
    layers: dict[float, list[_Segment]], total: int, budget: int
) -> list[_Segment]:
    """Thin segments per-layer so total <= budget.  Every layer keeps >= 1."""
    out: list[_Segment] = []
    n_layers = len(layers)
    if n_layers == 0:
        return out

    # Allocate budget proportionally but floor at 1 per layer.
    for z in sorted(layers):
        layer_segs = layers[z]
        n = len(layer_segs)
        # Step: keep every step-th segment; step = ceil(n / share).
        # share = max(1, budget * n / total) rounded down but at least 1.
        share = max(1, (budget * n) // total)
        step = math.ceil(n / share)
        kept = [layer_segs[i] for i in range(0, n, step)]
        if not kept:
            kept = [layer_segs[0]]
        out.extend(kept)

    return out


# ------------------------------------------------------------------ #
# Core parser
# ------------------------------------------------------------------ #


def _parse_line(
    line: str,
) -> tuple[str, dict[str, str]] | None:
    """Split a gcode line into (command, params_dict).

    Returns None for blank lines or pure comments.
    Command letter+number is upper-cased.  Inline comments after ``;`` are
    stripped.  Parameters are returned as raw strings (caller converts).
    """
    # Strip inline comment and whitespace.
    semi = line.find(";")
    if semi >= 0:
        line = line[:semi]
    line = line.strip()
    if not line:
        return None

    parts = line.split()
    cmd = parts[0].upper()
    params: dict[str, str] = {}
    for part in parts[1:]:
        if part and part[0].isalpha():
            key = part[0].upper()
            val = part[1:]
            params[key] = val
    return cmd, params


def parse_gcode_toolpath(source: bytes | str) -> GcodeToolpath:
    """Parse gcode text/bytes and return extrusion-move toolpath segments.

    Parameters
    ----------
    source:
        The gcode as raw bytes (UTF-8, errors replaced) or text.
        If > 120 MB: raises :class:`GcodeParseError`. All real callers
        (the viz endpoint, the archive helper) pass bytes.

    Returns
    -------
    GcodeToolpath
        All fields populated.  Zero segments is NOT an error.
    """
    # ---- Normalise input to text lines ----------------------------------- #
    if len(source) > _MAX_INPUT_BYTES:
        raise GcodeParseError(
            f"Gcode input is {len(source) // (1024 * 1024)} MB "
            f"(limit {_MAX_INPUT_BYTES // (1024 * 1024)} MB)."
        )
    text = source.decode("utf-8", errors="replace") if isinstance(source, bytes) else source
    text_iter = io.StringIO(text)

    # ---- State machine --------------------------------------------------- #
    # XYZ mode: absolute (G90) or relative (G91)
    xyz_absolute = True
    # E mode: absolute (M82 or default) or relative (M83)
    e_relative = False

    cur_x = 0.0
    cur_y = 0.0
    cur_z = 0.0
    cur_e = 0.0     # absolute E accumulator (meaningless when e_relative=True)

    # Segments grouped by Z for decimation later.
    layers: dict[float, list[_Segment]] = {}

    for raw_line in text_iter:
        parsed = _parse_line(raw_line)
        if parsed is None:
            continue
        cmd, params = parsed

        # ---- Mode switches ----------------------------------------------- #
        if cmd == "G90":
            xyz_absolute = True
            continue
        if cmd == "G91":
            xyz_absolute = False
            continue
        if cmd == "M82":
            e_relative = False
            continue
        if cmd == "M83":
            e_relative = True
            continue

        # ---- G92 E reset -------------------------------------------------- #
        # G92 E0 is the dominant form; any G92 with an E key resets the
        # absolute E accumulator.  We also handle G92 without E (which resets
        # XYZ — irrelevant, but don't accidentally treat it as E reset).
        if cmd == "G92":
            if "E" in params:
                try:
                    cur_e = float(params["E"])
                except ValueError:
                    cur_e = 0.0
            continue

        # ---- G0 / G1 moves ----------------------------------------------- #
        if cmd not in ("G0", "G1"):
            continue

        # Parse coordinate parameters; skip line on any numeric error.
        new_x = cur_x
        new_y = cur_y
        new_z = cur_z
        new_e = cur_e
        has_e = False
        e_delta = 0.0

        try:
            if "X" in params:
                raw_x = float(params["X"])
                new_x = (cur_x + raw_x) if not xyz_absolute else raw_x
            if "Y" in params:
                raw_y = float(params["Y"])
                new_y = (cur_y + raw_y) if not xyz_absolute else raw_y
            if "Z" in params:
                raw_z = float(params["Z"])
                new_z = (cur_z + raw_z) if not xyz_absolute else raw_z
            if "E" in params:
                raw_e = float(params["E"])
                has_e = True
                if e_relative:
                    e_delta = raw_e
                    new_e = cur_e + raw_e
                else:
                    e_delta = raw_e - cur_e
                    new_e = raw_e
        except ValueError:
            # Non-numeric coordinate — skip.
            cur_x = new_x
            cur_y = new_y
            cur_z = new_z
            continue

        # ---- Classify the move ------------------------------------------- #
        # Extrusion = E strictly increases AND (X or Y changes).
        x_changed = abs(new_x - cur_x) > 1e-9
        y_changed = abs(new_y - cur_y) > 1e-9

        is_extrusion = has_e and e_delta > 0.0 and (x_changed or y_changed)

        if is_extrusion:
            # Flat-segment convention (see module docstring): both endpoints use
            # cur_z, so a Z-changing extrusion (spiral-vase) is flattened onto
            # the layer it started on.  This keeps each segment in exactly one
            # layers[cur_z] bucket; new_z is applied to state only afterwards.
            seg = _Segment(
                x0=cur_x, y0=cur_y, z0=cur_z,
                x1=new_x, y1=new_y, z1=cur_z,
            )
            z_key = cur_z
            if z_key not in layers:
                layers[z_key] = []
            layers[z_key].append(seg)

        # Advance state.
        cur_x = new_x
        cur_y = new_y
        cur_z = new_z
        cur_e = new_e

    # ---- Collinear merge per layer --------------------------------------- #
    merged_layers: dict[float, list[_Segment]] = {
        z: _merge_collinear(segs) for z, segs in layers.items()
    }
    total_merged = sum(len(v) for v in merged_layers.values())

    # ---- Budget decimation ---------------------------------------------- #
    decimated = False
    if total_merged > _SEGMENT_BUDGET:
        decimated = True
        final_segs = _decimate(merged_layers, total_merged, _SEGMENT_BUDGET)
    else:
        final_segs = [seg for segs in merged_layers.values() for seg in segs]

    # ---- Build output --------------------------------------------------- #
    segment_count = len(final_segs)
    positions: array.array[float] = array.array("f")

    if segment_count == 0:
        return GcodeToolpath(
            positions=positions,
            segment_count=0,
            bbox_min=[0.0, 0.0, 0.0],
            bbox_max=[0.0, 0.0, 0.0],
            decimated=decimated,
        )

    # Bounding box over all vertex positions.
    min_x = min_y = min_z = float("inf")
    max_x = max_y = max_z = float("-inf")

    for seg in final_segs:
        for vx, vy, vz in ((seg.x0, seg.y0, seg.z0), (seg.x1, seg.y1, seg.z1)):
            positions.append(vx)
            positions.append(vy)
            positions.append(vz)
            if vx < min_x:
                min_x = vx
            if vx > max_x:
                max_x = vx
            if vy < min_y:
                min_y = vy
            if vy > max_y:
                max_y = vy
            if vz < min_z:
                min_z = vz
            if vz > max_z:
                max_z = vz

    return GcodeToolpath(
        positions=positions,
        segment_count=segment_count,
        bbox_min=[min_x, min_y, min_z],
        bbox_max=[max_x, max_y, max_z],
        decimated=decimated,
    )


# ------------------------------------------------------------------ #
# Convenience: extract from a .gcode.3mf archive
# ------------------------------------------------------------------ #

GCODE_MEMBER = "Metadata/plate_1.gcode"


def parse_gcode_from_archive(data: bytes) -> GcodeToolpath:
    """Extract ``Metadata/plate_1.gcode`` from a ``.gcode.3mf`` ZIP and parse it.

    Raises :class:`GcodeParseError` if:
    * ``data`` is not a valid ZIP.
    * ``Metadata/plate_1.gcode`` is not present.
    * The decompressed gcode exceeds the input size guard.
    """
    if len(data) > _MAX_INPUT_BYTES:
        raise GcodeParseError(
            f"Archive is {len(data) // (1024 * 1024)} MB "
            f"(limit {_MAX_INPUT_BYTES // (1024 * 1024)} MB)."
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise GcodeParseError(f"Not a valid ZIP archive: {exc}") from exc

    with zf:
        names = set(zf.namelist())
        if GCODE_MEMBER not in names:
            raise GcodeParseError(
                f"Archive does not contain {GCODE_MEMBER!r}. "
                "Is this a .gcode.3mf produced by Bambu Studio?"
            )
        info = zf.getinfo(GCODE_MEMBER)
        if info.file_size > _MAX_INPUT_BYTES:
            raise GcodeParseError(
                f"Gcode member is {info.file_size // (1024 * 1024)} MB "
                f"(limit {_MAX_INPUT_BYTES // (1024 * 1024)} MB)."
            )
        gcode_bytes = zf.read(GCODE_MEMBER)

    return parse_gcode_toolpath(gcode_bytes)
