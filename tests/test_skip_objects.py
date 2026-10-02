"""Skip Objects core: OrcaSlicer 2.4.2 PartSkipDialog / SkipPartCanvas rules.

Real fixtures (tests/fixtures/orca):

* gui_pick1.3mf — the pick map, slice_info and model_settings of a
  one-object plate sliced in the OrcaSlicer 2.4.2 GUI (identify_id 496).
* multi3.gcode.3mf — Orca 2.4.2 CLI slice of three objects with
  ``--arrange 1`` (cube, bar, frame): slice_info lists the objects. The CLI
  cannot render pick images here (no OpenGL context), so its map is the
  G-code footprint.
* sparse13.gcode.3mf — CLI slice with ``--arrange 0``: no ``<object>`` in
  slice_info, only model_settings identify_ids.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from bambu_bridge import skip_objects as skip

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "orca"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _label_ids(data: bytes) -> list[int]:
    """The G-code header ``; model label id: a,b,c`` the firmware skips by."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        head = zf.read("Metadata/plate_1.gcode")[:4096].decode()
    return [int(v) for v in re.search(r"; model label id: ([\d,]+)", head).group(1).split(",")]


def _orca_colour(object_id: int) -> tuple[int, int, int]:
    """GLCanvas3D::render_thumbnail_internal for_picking: r, g, b bytes of the id."""
    return object_id & 0xFF, (object_id >> 8) & 0xFF, (object_id >> 16) & 0xFF


def _png(pixels: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(pixels.astype(np.uint8), "RGBA").save(buf, format="PNG")
    return buf.getvalue()


def _synthetic_pick(ids: list[int], size: int = 64) -> bytes:
    """Columns of objects in Orca's pick colours on a transparent black bed."""
    img = np.zeros((size, size, 4), dtype=np.uint8)
    band = size // (len(ids) + 1)
    for n, object_id in enumerate(ids):
        img[8:56, (n + 1) * band - 3 : (n + 1) * band + 3] = (*_orca_colour(object_id), 255)
    return _png(img)


# --------------------------------------------------------------------------- #
# Colour -> identify_id
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("object_id", [1, 255, 256, 496, 65535, 65536, 0xABCDEF])
def test_decode_matches_orca_encoding(object_id):
    img = np.zeros((4, 4, 4), dtype=np.uint8)
    img[1, 2] = (*_orca_colour(object_id), 255)
    pick = skip.decode_pick(_png(img), frozenset({object_id}))
    assert pick[1, 2] == object_id
    assert int(pick.sum()) == object_id   # everything else is the bed


def test_decode_drops_alpha_and_unlisted_colours():
    img = np.zeros((2, 2, 4), dtype=np.uint8)
    img[0, 0] = (*_orca_colour(74), 0)     # alpha ignored, as BGRA2BGR does
    img[0, 1] = (*_orca_colour(75), 255)   # not an object of this plate
    pick = skip.decode_pick(_png(img), frozenset({74}))
    assert pick.tolist() == [[74, 0], [0, 0]]


def test_real_gui_pick_map_decodes_to_slice_info_object():
    job = skip.read_job(_fixture("gui_pick1.3mf"), 1)
    assert job.label_object_enabled is True
    assert job.objects == (skip.PartObject(496, "Brushwarden_Base"),)
    assert job.map_source == "pick"
    assert job.pick is not None and job.pick.shape == (512, 512)
    ids, counts = np.unique(job.pick, return_counts=True)
    assert ids.tolist() == [0, 496]
    # Every opaque pixel of Orca's pick image is the object.
    with zipfile.ZipFile(FIXTURES / "gui_pick1.3mf") as zf:
        alpha = np.asarray(Image.open(io.BytesIO(zf.read("Metadata/pick_1.png"))))[..., 3]
    assert counts[1] == int((alpha > 0).sum())


def test_corrupt_pick_image_means_no_map():
    assert skip.decode_pick(b"not a png", frozenset({1})) is None


# --------------------------------------------------------------------------- #
# Object list
# --------------------------------------------------------------------------- #


def test_slice_info_objects_listed_by_id_and_match_gcode_labels():
    data = _fixture("multi3.gcode.3mf")
    job = skip.read_job(data, 1)
    assert [(o.id, o.name) for o in job.objects] == [
        (63, "cube.stl"), (74, "bar.stl"), (85, "frame.stl"),
    ]
    assert sorted(job.ids) == _label_ids(data)
    assert job.label_object_enabled is True
    assert job.map_source == "gcode"


def test_cli_slice_without_objects_falls_back_to_model_settings():
    data = _fixture("sparse13.gcode.3mf")
    job = skip.read_job(data, 1)
    assert [(o.id, o.name) for o in job.objects] == [(45, "a.stl"), (56, "b.stl")]
    assert sorted(job.ids) == _label_ids(data)
    assert job.label_object_enabled is True


def test_model_settings_fallback_agrees_with_slice_info():
    data = _fixture("multi3.gcode.3mf")
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(buf, "w") as dst:
        for name in src.namelist():
            body = src.read(name)
            if name == "Metadata/slice_info.config":
                body = re.sub(rb"\s*<object [^>]*/>", b"", body)
            dst.writestr(name, body)
    assert skip.read_job(buf.getvalue(), 1).objects == skip.read_job(data, 1).objects


def test_other_plate_has_no_objects():
    job = skip.read_job(_fixture("multi3.gcode.3mf"), 2)
    assert job.objects == () and job.label_object_enabled is False


def test_unreadable_archive_raises():
    with pytest.raises(ValueError, match="unreadable"):
        skip.read_job(b"not a zip", 1)


def test_synthetic_multi_object_pick():
    members = {
        "Metadata/slice_info.config": (
            '<config><plate><metadata key="index" value="2"/>'
            '<metadata key="label_object_enabled" value="true"/>'
            '<object identify_id="300" name="B" skipped="false" />'
            '<object identify_id="12" name="A" skipped="false" />'
            '<object identify_id="70000" name="C" skipped="false" />'
            "</plate></config>"
        ),
        "Metadata/pick_2.png": _synthetic_pick([12, 300, 70000]),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    job = skip.read_job(buf.getvalue(), 2)
    assert [o.id for o in job.objects] == [12, 300, 70000]
    assert sorted(set(job.pick.flat)) == [0, 12, 300, 70000]


# --------------------------------------------------------------------------- #
# Hit-test rows and map rendering
# --------------------------------------------------------------------------- #


def test_run_rows_expand_back_to_the_pick_map():
    pick = skip.read_job(_fixture("gui_pick1.3mf"), 1).pick
    rows = skip.run_rows(pick)
    expanded = np.array(
        [[v for i in range(0, len(r), 2) for v in [r[i]] * r[i + 1]] for r in rows]
    )
    assert np.array_equal(expanded, pick)


def test_render_uses_orca_canvas_colours():
    pick = np.zeros((40, 60), dtype=np.uint32)
    pick[10:30, 5:15] = 1
    pick[10:30, 25:35] = 2
    pick[10:30, 45:55] = 3
    png = skip.render_map(pick, checked=frozenset({1}), skipped=frozenset({3}))
    out = np.asarray(Image.open(io.BytesIO(png)))
    assert tuple(out[20, 10]) == (239, 175, 175, 255)   # checked fill
    assert tuple(out[10, 5]) == (208, 27, 27, 255)      # checked bound
    assert tuple(out[20, 30]) == (255, 255, 255, 255)   # unchecked part
    assert tuple(out[20, 50]) == (159, 159, 159, 255)   # skipped fill
    assert tuple(out[10, 45]) == (95, 95, 95, 255)      # skipped bound
    assert tuple(out[20, 20]) == (230, 230, 230, 255)   # plate
    assert out[0, 0][3] == 0                            # rounded corner


# --------------------------------------------------------------------------- #
# Printer state and gating
# --------------------------------------------------------------------------- #


LIVE = "TW09-PF06-tweezer-v9-plus-latch-tune-PETG-z01.gcode.3mf"


@pytest.mark.parametrize(
    "raw,plates,plate",
    [
        ({"plate_idx": 3}, [1, 2, 3], 3),
        ({"plate_idx": "2"}, [1, 2], 2),
        ({"plate_idx": 0, "gcode_file": "/data/Metadata/plate_4.gcode"}, [4], 4),
        ({"gcode_file": "Metadata/plate_2.gcode"}, [1, 2], 2),
        # Live P1S: no plate_idx, gcode_file is the project name, one plate.
        ({"gcode_file": LIVE}, [1], 1),
        ({"gcode_file": "part_3.gcode.3mf"}, [1], 1),
        # A single-plate export of plate 3 keeps Orca's plate_3 naming.
        ({"gcode_file": "job.gcode.3mf"}, [3], 3),
        # Several plates and nothing names one: refuse, never assume plate 1.
        ({"gcode_file": "job.gcode.3mf"}, [1, 2], None),
        ({"plate_idx": True, "gcode_file": "job.gcode.3mf"}, [1, 2], None),
        # A named plate the archive does not hold is the wrong file.
        ({"plate_idx": 4}, [1, 2], None),
        ({}, [], None),
        # param counts only when its echo's url names this archive.
        ({"param": "Metadata/plate_2.gcode", "url": "ftp://job.gcode.3mf",
          "gcode_file": "job.gcode.3mf"}, [1, 2], 2),
        ({"param": "Metadata/plate_2.gcode", "url": "ftp://other.gcode.3mf",
          "gcode_file": "job.gcode.3mf"}, [1, 2], None),
        ({"param": "Metadata/plate_2.gcode", "gcode_file": "job.gcode.3mf"}, [1, 2], None),
    ],
)
def test_resolve_plate(raw, plates, plate):
    assert skip.resolve_plate(raw, plates, "job.gcode.3mf") == plate


def test_archive_plates():
    assert skip.archive_plates(_fixture("multi3.gcode.3mf")) == [1]
    assert skip.archive_plates(_fixture("gui_pick1.3mf")) == []
    with pytest.raises(ValueError):
        skip.archive_plates(b"not a zip")


def test_skipped_ids_reads_s_obj():
    assert skip.skipped_ids({"s_obj": [63, True, "74", 85]}) == [63, 85]
    assert skip.skipped_ids({"s_obj": None}) == []
    assert skip.skipped_ids({}) == []


@pytest.mark.parametrize(
    "raw,supported",
    [
        ({"fun": format(1 << 49, "X")}, True),
        ({"fun": format((1 << 49) | (1 << 31) | 0xFF, "x"), "s_obj": []}, True),
        # fun present: bit 49 is authoritative, even with s_obj reported.
        ({"fun": format((1 << 50) | (1 << 48), "X"), "s_obj": []}, False),
        ({"fun": "", "s_obj": []}, False),
        ({"fun": "not-hex"}, False),
        # Legacy push format (P1S): no fun; an s_obj list is the signal.
        ({"s_obj": []}, True),
        ({"s_obj": [63]}, True),
        ({"s_obj": None}, False),
        ({}, False),
    ],
)
def test_part_skip_support(raw, supported):
    assert skip.part_skip_supported(raw) is supported


FUN = format(1 << 49, "X")


@pytest.mark.parametrize(
    "raw,reason",
    [
        ({"fun": FUN, "gcode_state": "RUNNING"}, None),
        ({"fun": FUN, "gcode_state": "PAUSE"}, None),
        ({"fun": FUN, "gcode_state": "PREPARE"}, "Printer state: PREPARE"),
        ({"fun": FUN, "gcode_state": "FINISH"}, "Printer state: FINISH"),
        ({"gcode_state": "RUNNING"}, "The printer does not report support for skipping objects"),
        ({"s_obj": [], "gcode_state": "RUNNING"}, None),
        ({"s_obj": [], "gcode_state": "RUNNING", "print_type": "system"},
         "Calibration prints cannot skip objects"),
        ({"fun": FUN, "gcode_state": "RUNNING", "print_type": "system"},
         "Calibration prints cannot skip objects"),
        ({"fun": FUN, "gcode_state": "RUNNING", "gcode_file": "/usr/auto_cali_for_user.gcode"},
         "Calibration prints cannot skip objects"),
        # CalibUtils::get_calib_mode_by_name spellings, "retration" included.
        ({"fun": FUN, "gcode_state": "RUNNING", "subtask_name": "flow_rate_coarse_calib_mode"},
         "Calibration prints cannot skip objects"),
        ({"fun": FUN, "gcode_state": "RUNNING", "subtask_name": "retration_tower_calib_mode"},
         "Calibration prints cannot skip objects"),
        ({"fun": FUN, "gcode_state": "RUNNING", "subtask_name": "pa_line_calib_mode_v2"}, None),
    ],
)
def test_unavailable_reason(raw, reason):
    assert skip.unavailable_reason(raw) == reason


def _job(n: int = 3, labelled: bool = True) -> skip.SkipJob:
    return skip.SkipJob(1, labelled, tuple(skip.PartObject(i, f"o{i}") for i in range(1, n + 1)))


def test_job_reasons():
    assert skip.job_reason(_job()) is None
    assert skip.job_reason(skip.SkipJob(0, False, ())) == skip.UNKNOWN_PLATE
    assert skip.job_reason(_job(labelled=False)) == "The current print job cannot be skipped"
    assert skip.job_reason(_job(64)) is None
    assert skip.job_reason(_job(65)) == "Over 64 objects in single plate"


def test_plan_skips_the_new_selection():
    assert skip.plan(_job(), [], [2, 2, 1]) == ("skip", [2, 1])


def test_plan_stops_when_nothing_would_remain():
    # PartSkipDialog::OnApplyDialog: all skipped -> command_task_abort.
    assert skip.plan(_job(), [1], [2, 3]) == ("stop", [])
    assert skip.plan(_job(1), [], [1]) == ("stop", [])


@pytest.mark.parametrize(
    "skipped,requested,message",
    [
        ([], [], "Nothing selected"),
        ([], [4], r"not in the current print: \[4\]"),
        ([1], [1, 2], r"already skipped: \[1\]"),
    ],
)
def test_plan_refuses_what_the_dialog_never_offers(skipped, requested, message):
    with pytest.raises(ValueError, match=message):
        skip.plan(_job(), skipped, requested)


@pytest.mark.parametrize("value,expected", [("1", True), ("on", True), ("", False), ("0", False)])
def test_enable_flag(monkeypatch, value, expected):
    monkeypatch.setenv(skip.ENABLE_ENV, value)
    assert skip.enabled() is expected


# --------------------------------------------------------------------------- #
# G-code footprint (slices without a pick image)
# --------------------------------------------------------------------------- #


def _mm(col: float, row: float) -> tuple[float, float]:
    """Map pixel -> bed mm (2 px/mm, X right, Y up on a 256 mm bed)."""
    return col / skip.PX_PER_MM, 256 - row / skip.PX_PER_MM


def test_multi3_footprints_sit_in_their_plate_bboxes():
    data = _fixture("multi3.gcode.3mf")
    job = skip.read_job(data, 1)
    assert job.pick.shape == (512, 512)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        plate = json.loads(zf.read("Metadata/plate_1.json"))
    boxes = {b["name"]: b["bbox"] for b in plate["bbox_objects"]}
    spans = {}
    for obj in job.objects:
        rows, cols = np.nonzero(job.pick == obj.id)
        assert rows.size, obj.name
        cx, cy = _mm(cols.mean() + 0.5, rows.mean() + 0.5)
        x0, y0, x1, y1 = boxes[obj.name]
        assert x0 <= cx <= x1 and y0 <= cy <= y1, (obj.name, cx, cy, boxes[obj.name])
        # Within the bbox give or take 2.5 mm: the bbox also holds the
        # unlabelled brim, and this slice's plate_1.json sits 2 mm higher in Y
        # than its own G-code (cube: bbox Y 89.3-112.7, extrusions 89.2-108.8).
        fx0, fy1 = _mm(cols.min(), rows.min())
        fx1, fy0 = _mm(cols.max() + 1, rows.max() + 1)
        assert x0 - 2.5 < fx0 < fx1 < x1 + 2.5, (obj.name, fx0, fx1, x0, x1)
        assert y0 - 2.5 < fy0 < fy1 < y1 + 2.5, (obj.name, fy0, fy1, y0, y1)
        spans[obj.id] = (cols.min(), rows.min(), cols.max(), rows.max())
    # Disjoint: no two footprints' boxes even touch.
    items = list(spans.values())
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            assert a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1]


def test_cube_footprint_matches_its_extrusions():
    # cube.stl's extruding moves span X 126.151-145.731, Y 89.21-108.79 in
    # plate_1.gcode; the footprint adds half a line width and pixel rounding.
    pick = skip.read_job(_fixture("multi3.gcode.3mf"), 1).pick
    rows, cols = np.nonzero(pick == 63)
    fx0, fy1 = _mm(cols.min(), rows.min())
    fx1, fy0 = _mm(cols.max() + 1, rows.max() + 1)
    for got, want in ((fx0, 126.151), (fx1, 145.731), (fy0, 89.21), (fy1, 108.79)):
        assert abs(got - want) <= 0.75, (got, want)


def test_frame_hole_is_filled_so_the_part_reads_solid():
    job = skip.read_job(_fixture("multi3.gcode.3mf"), 1)
    rows, cols = np.nonzero(job.pick == 85)   # frame.stl: 30 mm square, 18 mm hole
    centre = job.pick[(rows.min() + rows.max()) // 2, (cols.min() + cols.max()) // 2]
    assert centre == 85


def _footprint(gcode: str, ids=frozenset({7})) -> np.ndarray:
    return skip.gcode_footprint(io.BytesIO(gcode.encode()), ids, (0.0, 0.0, 20.0, 20.0))


def test_footprint_draws_only_labelled_extrusions():
    pick = _footprint(
        "G90\nM83\nG1 X1 Y1 E1\n"                        # extrusion outside any object
        "; start printing object, unique label id: 7\n"
        "; LINE_WIDTH: 1\n"
        "G1 X2 Y10 F3000\n"                               # travel, no E
        "G1 X10 Y10 E.5\n"                                # extrusion along y = 10
        "G1 X10 Y18 E-.8\n"                               # retract while moving
        "; stop printing object, unique label id: 7\n"
        "G1 X18 Y2 E1\n"
    )
    assert pick.shape == (40, 40)
    ids = set(np.unique(pick).tolist())
    assert ids == {0, 7}
    rows, cols = np.nonzero(pick == 7)
    assert rows.min() >= 18 and rows.max() <= 21          # y = 10 mm -> row 20
    assert cols.min() >= 3 and cols.max() <= 21           # x 2..10 mm


def test_footprint_tracks_relative_moves_absolute_e_and_g92():
    pick = _footprint(
        "; start printing object, unique label id: 7\n"
        "G90\nM82\nG92 E0\nG1 X5 Y5\n"
        "G91\nG1 X5 E1\nG90\n"                           # relative +5 mm with absolute E 1
        "G1 X5 Y15 E1\n"                                  # same E: not extruding
        "G92 E0\nG1 X15 Y15 E0.5\n"                       # extruding after the reset
    )
    rows, cols = np.nonzero(pick == 7)
    assert pick[30, 15] == 7                                # (7.5, 5) on the first line
    assert pick[10, 20] == 7                                # (10, 15) on the last line
    assert pick[20, 10] == 0                                # (5, 10): the travel


def test_footprint_segments_arcs():
    # G3 from (15,10) round to (5,10) about (10,10): the upper half circle.
    pick = _footprint(
        "; start printing object, unique label id: 7\nM83\nG1 X15 Y10\n"
        "G3 X5 Y10 I-5 J0 E1\n"
    )
    assert pick[10, 20] == 7                                # top of the arc (10, 15)
    assert pick[30, 20] == 0                                # (10, 5) is on the other half


def test_footprint_without_labels_is_none():
    assert _footprint("M83\nG1 X1 Y1 E1\n") is None
    assert _footprint("; start printing object, unique label id: 8\nM83\nG1 X5 Y5 E1\n") is None


RING_THEN_PART = (
    "M83\n; LINE_WIDTH: 1\n"
    "; start printing object, unique label id: 1\n"
    "G1 X10 Y10\nG1 X50 Y10 E1\nG1 X50 Y50 E1\nG1 X10 Y50 E1\nG1 X10 Y10 E1\n"
    "; stop printing object, unique label id: 1\n"
)
PART = (
    "; start printing object, unique label id: 2\n"
    "G1 X25 Y25\nG1 X35 Y25 E1\nG1 X35 Y35 E1\nG1 X25 Y35 E1\nG1 X25 Y25 E1\n"
    "G1 X25 Y30 E1\nG1 X35 Y30 E1\n"
    "; stop printing object, unique label id: 2\n"
)


@pytest.mark.parametrize("order", ["ring first", "part first"])
def test_part_inside_a_ring_keeps_its_pixels(order):
    # Review #3/#5/#11: the ring's hole fill must not swallow the part.
    head = "M83\n; LINE_WIDTH: 1\n"
    ring = RING_THEN_PART.removeprefix(head)
    gcode = head + (ring + PART if order == "ring first" else PART + ring)
    pick = skip.gcode_footprint(
        io.BytesIO(gcode.encode()), frozenset({1, 2}), (0.0, 0.0, 60.0, 60.0)
    )
    rows = skip.run_rows(pick)
    # A tap at the part's centre (30, 30 mm) hits the part, as clients read the rows.
    row, x, hit = rows[60], 0, 0
    for i in range(0, len(row), 2):
        x += row[i + 1]
        if x > 60:
            hit = row[i]
            break
    assert hit == 2
    assert (pick == 2).sum() >= 20 * 20               # the 10 mm part, interior included
    assert pick[30 * 2 - 30, 30 * 2 - 30] == 1           # (15, 45) mm: the ring's filled opening


def test_bed_comes_from_printable_area():
    with zipfile.ZipFile(FIXTURES / "multi3.gcode.3mf") as zf:
        assert skip._bed(zf) == (0.0, 0.0, 256.0, 256.0)
