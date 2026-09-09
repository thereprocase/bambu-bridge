"""Unit tests for the stdlib gcode toolpath parser (spec G4).

All tests are hermetic: gcode is synthesised as plain strings.
No FTPS calls, no live printer interaction.

Coverage matrix
---------------
* Square perimeter at 2 layers (absolute XYZ + M83 relative E) —
  expected segment pairs, bbox, Z values.
* Travel moves excluded (no E param).
* Retraction moves excluded (negative E delta in M83, or E decrease in M82).
* Z-hop moves excluded (Z changes but no X/Y extrusion).
* G92 E0 mid-stream resets absolute E accumulator.
* M83 relative-E mode: positive E value = extrusion.
* M82 absolute-E mode with G92 resets.
* G91 relative XYZ mode.
* Collinear merge: multiple colinear segments on the same Z are merged to one.
* Budget decimation: > 250 000 segments across 3 layers => decimated=True,
  every layer retains at least one segment.
* Input > 120 MB raises GcodeParseError.
* Zero extrusion segments returns segment_count=0 (not an error).
* positions_b64 decodes to segment_count * 2 * 3 * 4 bytes.
* parse_gcode_from_archive: happy path, missing gcode member, bad zip.
"""

from __future__ import annotations

import io
import struct
import zipfile

import pytest

from bambu_bridge.protocol.gcode_path import (
    _SEGMENT_BUDGET,
    GcodeParseError,
    GcodeToolpath,
    parse_gcode_from_archive,
    parse_gcode_toolpath,
)

# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _gcode(lines: list[str]) -> bytes:
    """Join lines with newlines and encode to bytes."""
    return "\n".join(lines).encode("utf-8")


def _make_gcode_3mf(gcode_text: str) -> bytes:
    """Build a minimal .gcode.3mf archive containing the given gcode."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("Metadata/plate_1.gcode", gcode_text)
        # Minimal 3MF model (not parsed by gcode_path, but realistic)
        zf.writestr("3D/3dmodel.model", "<model/>")
    return buf.getvalue()


def _decode_positions(tp: GcodeToolpath) -> list[tuple[float, float, float]]:
    """Return the positions array as a list of (x, y, z) vertex tuples."""
    raw = tp.positions
    n = len(raw)
    assert n % 3 == 0, "positions length must be multiple of 3"
    verts = []
    for i in range(0, n, 3):
        verts.append((raw[i], raw[i + 1], raw[i + 2]))
    return verts


# ------------------------------------------------------------------ #
# Tests — square perimeter at 2 layers
# ------------------------------------------------------------------ #

# A 10 mm × 10 mm square perimeter at Z=0.2 then Z=0.4.
# Uses M83 (relative E): every positive E = extrusion.
# G1 X10 E0.5 → extrusion; G1 Z0.2 → Z hop (no X/Y, excluded)
_SQUARE_GCODE = """\
; Square perimeter test
G90            ; absolute XYZ
M83            ; relative E
G92 E0
; Layer 1 at Z=0.2
G1 Z0.2 F1200  ; Z hop — excluded (no XY change)
G1 X0 Y0 F6000 ; travel (no E) — excluded
G1 X10 Y0 E0.5 F1200  ; segment (0,0,0.2)->(10,0,0.2)
G1 X10 Y10 E0.5       ; segment (10,0,0.2)->(10,10,0.2)
G1 X0 Y10 E0.5        ; segment (10,10,0.2)->(0,10,0.2)
G1 X0 Y0 E0.5         ; segment (0,10,0.2)->(0,0,0.2)
G92 E0                ; reset E mid-layer (common between layers)
; Layer 2 at Z=0.4
G1 Z0.4 F1200  ; Z hop — excluded
G1 X10 Y0 E0.5 F1200  ; segment (0,0,0.4)->(10,0,0.4)  [x from last pos=0]
G1 X10 Y10 E0.5       ; segment (10,0,0.4)->(10,10,0.4)
G1 X0 Y10 E0.5        ; segment (10,10,0.4)->(0,10,0.4)
G1 X0 Y0 E0.5         ; segment (0,10,0.4)->(0,0,0.4)
"""


def test_square_segment_count() -> None:
    """Square perimeter at 2 layers produces 8 segments total (4 per layer)."""
    tp = parse_gcode_toolpath(_gcode(_SQUARE_GCODE.strip().splitlines()))
    # 4 sides × 2 layers = 8; collinear merge won't affect perpendicular sides
    assert tp.segment_count == 8


def test_square_bbox() -> None:
    """Bounding box of the square covers 0..10 in X and Y."""
    tp = parse_gcode_toolpath(_gcode(_SQUARE_GCODE.strip().splitlines()))
    assert tp.bbox_min[0] == pytest.approx(0.0, abs=1e-4)
    assert tp.bbox_min[1] == pytest.approx(0.0, abs=1e-4)
    assert tp.bbox_max[0] == pytest.approx(10.0, abs=1e-4)
    assert tp.bbox_max[1] == pytest.approx(10.0, abs=1e-4)


def test_square_z_values() -> None:
    """Segments from layer 1 have Z=0.2; layer 2 Z=0.4."""
    tp = parse_gcode_toolpath(_gcode(_SQUARE_GCODE.strip().splitlines()))
    verts = _decode_positions(tp)
    # Each segment is two vertices: start and end.  All starts for the layer
    # should be at that layer's Z.
    z_values = [v[2] for v in verts]
    assert any(abs(z - 0.2) < 1e-3 for z in z_values), f"no Z=0.2 in {z_values}"
    assert any(abs(z - 0.4) < 1e-3 for z in z_values), f"no Z=0.4 in {z_values}"


def test_square_positions_array_type() -> None:
    """positions is array('f') — float32."""
    tp = parse_gcode_toolpath(_gcode(_SQUARE_GCODE.strip().splitlines()))
    assert tp.positions.typecode == "f"


def test_square_positions_length() -> None:
    """positions has segment_count * 2 * 3 floats (two xyz vertices per segment)."""
    tp = parse_gcode_toolpath(_gcode(_SQUARE_GCODE.strip().splitlines()))
    assert len(tp.positions) == tp.segment_count * 2 * 3


# ------------------------------------------------------------------ #
# Tests — move type exclusions
# ------------------------------------------------------------------ #


def test_travel_move_excluded() -> None:
    """G1 moves without E are travel; not included in segments."""
    gcode = _gcode([
        "G90",
        "M83",
        "G1 X10 Y10 F6000",  # travel, no E
        "G1 X20 Y10 E0.5",   # extrusion
    ])
    tp = parse_gcode_toolpath(gcode)
    assert tp.segment_count == 1


def test_retraction_excluded_m83() -> None:
    """Negative E in M83 mode = retraction; excluded."""
    gcode = _gcode([
        "G90",
        "M83",
        "G1 X10 Y10 E0.5",   # extrusion
        "G1 E-1.0",           # retraction (negative E delta)
        "G1 X20 Y10 E0.5",   # extrusion
    ])
    tp = parse_gcode_toolpath(gcode)
    assert tp.segment_count == 2  # only the two extrusion moves


def test_retraction_excluded_m82() -> None:
    """In M82 mode: E decreasing = retraction; excluded."""
    gcode = _gcode([
        "G90",
        "M82",
        "G1 X10 Y10 E1.0",   # extrusion (E: 0 -> 1, delta=+1)
        "G1 X10 Y10 E0.5",   # retraction (E: 1 -> 0.5, delta=-0.5) — excluded
        # No X/Y change on second line so it's also excluded on that basis,
        # but the E sign is the primary guard.
        "G1 X20 Y10 E1.0",   # extrusion (E: 0.5 -> 1.0, delta=+0.5)
    ])
    tp = parse_gcode_toolpath(gcode)
    assert tp.segment_count == 2


def test_z_hop_excluded() -> None:
    """G1 Z move without XY change is a Z hop; excluded."""
    gcode = _gcode([
        "G90",
        "M83",
        "G1 Z0.2",            # Z hop only — excluded
        "G1 X10 Y10 E0.5",   # extrusion
    ])
    tp = parse_gcode_toolpath(gcode)
    assert tp.segment_count == 1


def test_e_zero_move_excluded() -> None:
    """G1 with E=0 (no extrusion) is excluded."""
    gcode = _gcode([
        "G90",
        "M83",
        "G1 X10 Y10 E0.0",  # E=0 delta: not an extrusion
        "G1 X20 Y10 E0.5",  # real extrusion
    ])
    tp = parse_gcode_toolpath(gcode)
    assert tp.segment_count == 1


# ------------------------------------------------------------------ #
# Tests — G92 E0 mid-stream
# ------------------------------------------------------------------ #


def test_g92_e0_reset_absolute_mode() -> None:
    """G92 E0 resets the absolute E accumulator; first move after is extrusion.

    Uses a direction change at the reset point so the two segments are NOT
    collinear (and thus not merged by collinear merge).
    """
    gcode = _gcode([
        "G90",
        "M82",              # absolute E
        "G1 X10 Y0 E5.0",  # extrusion (E: 0->5): (0,0)->(10,0) [+X direction]
        "G92 E0",           # reset to 0
        "G1 X10 Y10 E0.5", # extrusion (E: 0->0.5): (10,0)->(10,10) [+Y direction]
    ])
    tp = parse_gcode_toolpath(gcode)
    assert tp.segment_count == 2


def test_g92_e_nonzero_reset() -> None:
    """G92 E<n> resets to n, so subsequent moves measure from n."""
    gcode = _gcode([
        "G90",
        "M82",
        "G92 E10.0",        # reset to 10
        "G1 X10 Y0 E10.5",  # E: 10 -> 10.5, delta = +0.5 = extrusion
        "G1 X20 Y0 E10.0",  # E: 10.5 -> 10.0, delta = -0.5 = retraction, excluded
    ])
    tp = parse_gcode_toolpath(gcode)
    assert tp.segment_count == 1


def test_g92_e0_in_m83_mode_resets_accumulator() -> None:
    """G92 E0 in M83 mode does not affect the delta computation (E is relative anyway).

    Uses a direction change to prevent collinear merge.
    """
    gcode = _gcode([
        "G90",
        "M83",
        "G1 X10 Y0 E0.5",  # (0,0)->(10,0) [+X]
        "G92 E0",           # reset; in M83 mode doesn't affect extrusion detection
        "G1 X10 Y10 E0.5", # (10,0)->(10,10) [+Y] — different direction
    ])
    tp = parse_gcode_toolpath(gcode)
    assert tp.segment_count == 2


# ------------------------------------------------------------------ #
# Tests — M83 relative E mode
# ------------------------------------------------------------------ #


def test_m83_relative_e_basic() -> None:
    """In M83 mode every positive E value is an extrusion regardless of history."""
    gcode = _gcode([
        "G90",
        "M83",
        "G1 X0 Y0 F6000",   # travel
        "G1 X10 Y0 E0.3",   # extrusion
        "G1 X10 Y10 E0.3",  # extrusion
    ])
    tp = parse_gcode_toolpath(gcode)
    assert tp.segment_count == 2


def test_m82_m83_switch() -> None:
    """M82 → M83 mid-stream switches E interpretation correctly.

    Uses a direction change to avoid collinear merge between the two extrusion
    segments (both would be collinear along X if they went in the same direction).
    """
    gcode = _gcode([
        "G90",
        "M82",              # absolute E
        "G92 E0",
        "G1 X10 Y0 E1.0",  # extrusion (absolute: 0->1): (0,0)->(10,0) [+X]
        "M83",              # switch to relative
        "G1 X10 Y10 E0.5", # extrusion (relative: delta=+0.5): (10,0)->(10,10) [+Y]
        "G1 X10 Y10 E-0.5",# retraction (relative: delta=-0.5) excluded; no XY change anyway
    ])
    tp = parse_gcode_toolpath(gcode)
    assert tp.segment_count == 2


# ------------------------------------------------------------------ #
# Tests — G91 relative XYZ mode
# ------------------------------------------------------------------ #


def test_g91_relative_xyz() -> None:
    """G91 mode: X/Y values are deltas; extrusion still detected."""
    gcode = _gcode([
        "G90",
        "G91",              # switch to relative XYZ
        "M83",
        "G1 X10 Y0 E0.5",  # relative: from (0,0) -> (10,0)
        "G1 X0 Y10 E0.5",  # relative: from (10,0) -> (10,10)
        "G90",              # back to absolute
        "G1 X0 Y0 E0.5",   # absolute: from (10,10) -> (0,0) — extrusion
    ])
    tp = parse_gcode_toolpath(gcode)
    assert tp.segment_count == 3
    # Check absolute positions of first segment
    verts = _decode_positions(tp)
    x0, y0, z0 = verts[0]
    x1, y1, z1 = verts[1]
    assert x0 == pytest.approx(0.0, abs=1e-4)
    assert y0 == pytest.approx(0.0, abs=1e-4)
    assert x1 == pytest.approx(10.0, abs=1e-4)
    assert y1 == pytest.approx(0.0, abs=1e-4)


# ------------------------------------------------------------------ #
# Tests — collinear merge
# ------------------------------------------------------------------ #


def test_collinear_merge_straight_run() -> None:
    """Multiple collinear segments along the same axis merge to one."""
    # Three segments all moving in +X direction at Z=0.2
    gcode = _gcode([
        "G90",
        "M83",
        "G1 Z0.2",
        "G1 X5 Y0 E0.2",    # (0,0,0.2)->(5,0,0.2)
        "G1 X10 Y0 E0.2",   # (5,0,0.2)->(10,0,0.2) — collinear with previous
        "G1 X15 Y0 E0.2",   # (10,0,0.2)->(15,0,0.2) — collinear with previous
    ])
    tp = parse_gcode_toolpath(gcode)
    # All three collinear → merged into one segment
    assert tp.segment_count == 1
    verts = _decode_positions(tp)
    # Start should be at x=0, end at x=15
    assert verts[0][0] == pytest.approx(0.0, abs=1e-4)
    assert verts[1][0] == pytest.approx(15.0, abs=1e-4)


def test_collinear_merge_non_collinear_not_merged() -> None:
    """Perpendicular segments at the same Z are NOT merged."""
    gcode = _gcode([
        "G90",
        "M83",
        "G1 Z0.2",
        "G1 X10 Y0 E0.5",   # +X direction
        "G1 X10 Y10 E0.5",  # +Y direction — perpendicular, not collinear
    ])
    tp = parse_gcode_toolpath(gcode)
    assert tp.segment_count == 2


def test_collinear_merge_different_z_not_merged() -> None:
    """Collinear segments at different Z layers are NOT merged."""
    gcode = _gcode([
        "G90",
        "M83",
        "G1 Z0.2",
        "G1 X10 Y0 E0.5",
        "G1 Z0.4",
        "G1 X20 Y0 E0.5",  # same direction but different Z — not merged
    ])
    tp = parse_gcode_toolpath(gcode)
    assert tp.segment_count == 2


def test_collinear_merge_reduces_long_straight() -> None:
    """A 100-segment straight line collapses to a single segment."""
    lines = ["G90", "M83", "G1 Z0.2"]
    for i in range(1, 101):
        lines.append(f"G1 X{i} Y0 E0.01")
    gcode = _gcode(lines)
    tp = parse_gcode_toolpath(gcode)
    assert tp.segment_count == 1


# ------------------------------------------------------------------ #
# Tests — budget decimation
# ------------------------------------------------------------------ #


def _make_large_gcode(n_layers: int = 3, segs_per_layer: int = 100_000) -> bytes:
    """Synthesise gcode with ~n_layers * segs_per_layer extrusion segments.

    Uses M83 relative E.  All segments are non-collinear (zigzag in X/Y)
    to prevent collinear merge from reducing the count.
    """
    lines = ["G90", "M83"]
    total_z = 0.0
    for layer in range(n_layers):
        total_z = round(0.2 * (layer + 1), 4)
        lines.append(f"G1 Z{total_z}")
        x = 0.0
        y = 0.0
        for i in range(segs_per_layer):
            # Alternate X and Y moves to avoid collinear accumulation.
            if i % 2 == 0:
                x += 0.1
                lines.append(f"G1 X{x:.4f} Y{y:.4f} E0.001")
            else:
                y += 0.1
                lines.append(f"G1 X{x:.4f} Y{y:.4f} E0.001")
    return "\n".join(lines).encode("utf-8")


def test_budget_decimation_triggers() -> None:
    """When raw segment count > budget, decimated=True."""
    # Use enough segments per layer that after any collinear merge the
    # total clearly exceeds 250_000.
    gcode = _make_large_gcode(n_layers=3, segs_per_layer=100_000)
    tp = parse_gcode_toolpath(gcode)
    assert tp.decimated is True
    assert tp.segment_count <= _SEGMENT_BUDGET


def test_budget_decimation_every_layer_has_segments() -> None:
    """After decimation, every layer retains at least one segment."""
    gcode = _make_large_gcode(n_layers=3, segs_per_layer=100_000)
    tp = parse_gcode_toolpath(gcode)

    # Reconstruct Z values from the positions array.
    verts = _decode_positions(tp)
    z_values = [v[2] for v in verts]

    # We generated 3 layers at Z=0.2, 0.4, 0.6
    assert any(abs(z - 0.2) < 1e-3 for z in z_values), f"no Z=0.2 in sample of {z_values[:10]}"
    assert any(abs(z - 0.4) < 1e-3 for z in z_values), f"no Z=0.4 in sample of {z_values[:10]}"
    assert any(abs(z - 0.6) < 1e-3 for z in z_values), f"no Z=0.6 in sample of {z_values[:10]}"


def test_no_decimation_under_budget() -> None:
    """When segment count <= budget, decimated=False."""
    tp = parse_gcode_toolpath(_gcode(_SQUARE_GCODE.strip().splitlines()))
    assert tp.decimated is False


# ------------------------------------------------------------------ #
# Tests — guard: input size
# ------------------------------------------------------------------ #


def test_oversized_input_raises_parse_error() -> None:
    """Input bytes > 120 MB raises GcodeParseError."""
    oversized = b"G1 X1 Y1 E1\n" * (11 * 1024 * 1024)  # ~120 MB+
    assert len(oversized) > 120 * 1024 * 1024
    with pytest.raises(GcodeParseError, match="limit"):
        parse_gcode_toolpath(oversized)


# ------------------------------------------------------------------ #
# Tests — guard: zero extrusion segments
# ------------------------------------------------------------------ #


def test_no_extrusion_returns_empty_toolpath() -> None:
    """A gcode file with only travel moves returns segment_count=0, no error."""
    gcode = _gcode([
        "G90",
        "M83",
        "G1 X10 Y10 F6000",   # travel, no E
        "G1 X20 Y20 F6000",   # travel, no E
    ])
    tp = parse_gcode_toolpath(gcode)
    assert tp.segment_count == 0
    assert len(tp.positions) == 0
    assert tp.bbox_min == [0.0, 0.0, 0.0]
    assert tp.bbox_max == [0.0, 0.0, 0.0]
    assert tp.decimated is False


def test_empty_gcode_returns_empty_toolpath() -> None:
    """Empty bytes / blank gcode returns segment_count=0."""
    tp = parse_gcode_toolpath(b"")
    assert tp.segment_count == 0


def test_comment_only_gcode() -> None:
    """Gcode with only comments returns segment_count=0."""
    tp = parse_gcode_toolpath(_gcode(["; This is just a comment", "  ", "; Another"]))
    assert tp.segment_count == 0


# ------------------------------------------------------------------ #
# Tests — non-numeric coordinates skipped
# ------------------------------------------------------------------ #


def test_non_numeric_coordinate_skipped() -> None:
    """Lines with non-numeric coordinates are silently skipped."""
    gcode = _gcode([
        "G90",
        "M83",
        "G1 X?? Y!! E0.5",   # bad coords — skip
        "G1 X10 Y0 E0.5",    # valid
    ])
    tp = parse_gcode_toolpath(gcode)
    assert tp.segment_count == 1


# ------------------------------------------------------------------ #
# Tests — parse_gcode_from_archive
# ------------------------------------------------------------------ #


def test_archive_happy_path() -> None:
    """parse_gcode_from_archive extracts and parses gcode from a .gcode.3mf."""
    gcode = _SQUARE_GCODE
    archive = _make_gcode_3mf(gcode)
    tp = parse_gcode_from_archive(archive)
    assert tp.segment_count == 8


def test_archive_missing_gcode_member() -> None:
    """Archive without Metadata/plate_1.gcode raises GcodeParseError."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    with pytest.raises(GcodeParseError, match="plate_1.gcode"):
        parse_gcode_from_archive(buf.getvalue())


def test_archive_bad_zip_raises() -> None:
    """Non-ZIP bytes raise GcodeParseError."""
    with pytest.raises(GcodeParseError, match="ZIP"):
        parse_gcode_from_archive(b"this is not a zip file at all")


def test_archive_empty_bytes_raises() -> None:
    """Empty bytes raise GcodeParseError."""
    with pytest.raises(GcodeParseError):
        parse_gcode_from_archive(b"")


# ------------------------------------------------------------------ #
# Tests — positions_b64 byte layout
# ------------------------------------------------------------------ #


def test_positions_bytes_are_float32_le() -> None:
    """tobytes() on positions produces little-endian float32 pairs."""
    tp = parse_gcode_toolpath(_gcode(_SQUARE_GCODE.strip().splitlines()))
    raw = tp.positions.tobytes()
    # Decode first vertex (x, y, z) of first segment.
    x, y, z = struct.unpack_from("<fff", raw, 0)
    assert tp.positions[0] == pytest.approx(x, abs=1e-6)
    assert tp.positions[1] == pytest.approx(y, abs=1e-6)
    assert tp.positions[2] == pytest.approx(z, abs=1e-6)


def test_positions_byte_length() -> None:
    """positions.tobytes() has exactly segment_count * 2 * 3 * 4 bytes."""
    tp = parse_gcode_toolpath(_gcode(_SQUARE_GCODE.strip().splitlines()))
    expected = tp.segment_count * 2 * 3 * 4
    assert len(tp.positions.tobytes()) == expected


# ------------------------------------------------------------------ #
# Tests — bbox coverage
# ------------------------------------------------------------------ #


def test_bbox_z_reflects_layer_heights() -> None:
    """bbox_min.z / bbox_max.z reflect the Z range of extrusion moves."""
    tp = parse_gcode_toolpath(_gcode(_SQUARE_GCODE.strip().splitlines()))
    assert tp.bbox_min[2] == pytest.approx(0.2, abs=1e-4)
    assert tp.bbox_max[2] == pytest.approx(0.4, abs=1e-4)


# ------------------------------------------------------------------ #
# Tests — G0 with E increase (rare but specified)
# ------------------------------------------------------------------ #


def test_g0_with_e_increase_is_extrusion() -> None:
    """G0 move where E strictly increases and X/Y changes is treated as extrusion."""
    gcode = _gcode([
        "G90",
        "M83",
        "G0 X10 Y0 E0.5",   # G0 with E — rare but valid
    ])
    tp = parse_gcode_toolpath(gcode)
    assert tp.segment_count == 1
