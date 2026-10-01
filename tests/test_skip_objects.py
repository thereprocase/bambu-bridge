"""Skip Objects core: OrcaSlicer 2.4.2 PartSkipDialog / SkipPartCanvas rules.

Real fixtures (tests/fixtures/orca):

* gui_pick1.3mf — the pick map, slice_info and model_settings of a
  one-object plate sliced in the OrcaSlicer 2.4.2 GUI (identify_id 496).
* multi3.gcode.3mf — Orca 2.4.2 CLI slice of three objects with
  ``--arrange 1`` (cube, bar, frame): slice_info lists the objects. The CLI
  cannot render pick images here (no OpenGL context), so it has no map.
* sparse13.gcode.3mf — CLI slice with ``--arrange 0``: no ``<object>`` in
  slice_info, only model_settings identify_ids.
"""

from __future__ import annotations

import io
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
    assert job.pick is None


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


@pytest.mark.parametrize(
    "raw,plate",
    [
        ({"plate_idx": 3}, 3),
        ({"plate_idx": "2"}, 2),
        ({"plate_idx": 0, "gcode_file": "/data/Metadata/plate_4.gcode"}, 4),
        ({"gcode_file": "Metadata/plate_12.gcode"}, 12),
        ({"plate_idx": True}, 1),
        ({"plate_idx": "x", "gcode_file": "job.gcode.3mf"}, 1),
        ({}, 1),
    ],
)
def test_plate_index(raw, plate):
    assert skip.plate_index(raw) == plate


def test_skipped_ids_reads_s_obj():
    assert skip.skipped_ids({"s_obj": [63, True, "74", 85]}) == [63, 85]
    assert skip.skipped_ids({"s_obj": None}) == []
    assert skip.skipped_ids({}) == []


@pytest.mark.parametrize(
    "fun,supported",
    [
        (format(1 << 49, "X"), True),
        (format((1 << 49) | (1 << 31) | 0xFF, "x"), True),
        (format((1 << 50) | (1 << 48), "X"), False),
        ("", False),
        (None, False),
        ("not-hex", False),
    ],
)
def test_part_skip_support_is_fun_bit_49(fun, supported):
    raw = {} if fun is None else {"fun": fun}
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
        ({"fun": FUN, "gcode_state": "RUNNING", "print_type": "system"},
         "Calibration prints cannot skip objects"),
        ({"fun": FUN, "gcode_state": "RUNNING", "gcode_file": "/usr/auto_cali_for_user.gcode"},
         "Calibration prints cannot skip objects"),
    ],
)
def test_unavailable_reason(raw, reason):
    assert skip.unavailable_reason(raw) == reason


def _job(n: int = 3, labelled: bool = True) -> skip.SkipJob:
    return skip.SkipJob(1, labelled, tuple(skip.PartObject(i, f"o{i}") for i in range(1, n + 1)))


def test_job_reasons():
    assert skip.job_reason(_job()) is None
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
