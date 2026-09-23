from __future__ import annotations

import io
import math
import zipfile

import pytest
from PIL import Image

from bambu_bridge.protocol.gcode_path import parse_gcode_toolpath
from bambu_bridge.turntable_overlay import Shape, archive_shape, draw_shape, job_key, projection

GCODE = (
    b"G90\nM83\nG1 X100 Y100 Z1\n; FEATURE: Outer wall\nG1 X150 E1\nG1 Y150 E1\n"
    b"; FEATURE: Sparse infill\nG1 X120 Y120 E1\n; FEATURE: Outer wall\nG1 X100 E1\n"
)


def test_feature_filter_preserves_modal_position_through_other_moves() -> None:
    full = parse_gcode_toolpath(GCODE)
    outer = parse_gcode_toolpath(GCODE, features=frozenset({"Outer wall"}))
    assert outer.segment_count == 3
    assert full.segment_count == 4
    assert list(outer.positions[-6:]) == [120, 120, 1, 100, 120, 1]


def test_archive_preview_keeps_physical_plate_placement() -> None:
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", GCODE)
    source.seek(0)
    shape = archive_shape(source)
    assert shape is not None
    assert shape.segments[0] == (-28, -28, 1, 22, -28, 1)
    assert shape.radius == pytest.approx(math.sqrt(2) * 128)


def test_zoom_and_elevation_are_constant_through_full_rotation() -> None:
    shape = Shape(((100, 100, 0, 100, 100, 120),), math.sqrt(2) * 128, 120)
    heights = []
    for degree in range(0, 360, 5):
        project = projection(shape, 244, 186, math.radians(degree))
        floor = project(0, 0, 0)
        top = project(0, 0, 120)
        heights.append(floor[1] - top[1])
        for x, y, z in [(-128, -128, 0), (128, 128, 0), (100, 100, 120)]:
            px, py = project(x, y, z)
            assert 0 <= px <= 244
            assert 20 <= py <= 186
    assert max(heights) - min(heights) < 1e-9


def test_one_rpm_returns_same_image_without_zoom() -> None:
    shape = Shape(((80, 90, 0, 100, 90, 40),), math.sqrt(2) * 128, 40)

    def frame(seconds: float) -> bytes:
        canvas = Image.new("RGB", (1280, 720))
        draw_shape(canvas, shape, seconds)
        return canvas.tobytes()

    assert frame(0) == frame(60)
    assert frame(0) != frame(15)


def test_interrupted_idle_job_does_not_keep_old_shape() -> None:
    assert job_key({"_raw": {"gcode_state": "RUNNING", "gcode_file": "new.3mf"}}) == "new.3mf"
    assert job_key({"_raw": {"gcode_state": "IDLE", "gcode_file": "old.3mf"}}) == ""


@pytest.mark.asyncio
async def test_cache_discards_late_geometry_after_job_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from bambu_bridge.turntable_overlay import ShapeCache

    gate = asyncio.Event()
    shape = Shape((), 180, 20)

    async def slow_loader(*args: object) -> Shape:
        await gate.wait()
        return shape

    monkeypatch.setattr(asyncio, "to_thread", slow_loader)
    cache = ShapeCache(lambda snapshot: shape)
    old = {"_raw": {"gcode_state": "RUNNING", "gcode_file": "old.3mf"}}
    idle = {"_raw": {"gcode_state": "IDLE", "gcode_file": ""}}
    assert cache.update(old) is None
    await asyncio.sleep(0)
    assert cache.update(idle) is None
    gate.set()
    await asyncio.sleep(0)
    assert cache.update(idle) is None
    assert cache.task is None


@pytest.mark.parametrize("relative", [True, False])
def test_parallel_walls_remain_separate_across_travel(relative: bool) -> None:
    mode = "M83" if relative else "M82"
    end_e = 1 if relative else 2
    code = (
        f"G90\n{mode}\nG1 X0 Y0 Z1\n; FEATURE: Outer wall\nG1 X10 E1\nG1 X20 Y10\nG1 X30 E{end_e}\n"
    )
    path = parse_gcode_toolpath(code, features=frozenset({"Outer wall"}))
    assert path.segment_count == 2
    assert list(path.positions) == [0, 0, 1, 10, 0, 1, 20, 10, 1, 30, 10, 1]


def test_retraced_wall_is_not_collapsed_into_zero_length() -> None:
    code = "M83\nG1 X0 Y0 Z1\n; FEATURE: Outer wall\nG1 X10 E1\nG1 X0 E1\n"
    assert parse_gcode_toolpath(code).segment_count == 2


def test_preview_type_allowlist_includes_skins_but_not_print_helpers() -> None:
    code = "M83\nG1 X100 Y100 Z1\n"
    for i, feature in enumerate(
        [
            "Outer wall",
            "Top surface",
            "Bottom surface",
            "Sparse infill",
            "Brim",
            "Support",
            "Custom",
            "Travel",
        ]
    ):
        code += f"; FEATURE: {feature}\nG1 X{100+i*10} Y{100+i*10}\nG1 X{105+i*10} E1\n"
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", code)
    source.seek(0)
    shape = archive_shape(source)
    assert shape is not None and len(shape.segments) == 3
    assert {segment[0] for segment in shape.segments} == {-28, -18, -8}


def test_cel_ribbons_merge_stacked_walls_without_filling_travel_gap() -> None:
    from bambu_bridge.turntable_overlay import exterior_faces

    faces = exterior_faces(
        [
            100,
            100,
            0.2,
            110,
            100,
            0.2,
            100,
            100,
            0.4,
            110,
            100,
            0.4,
            120,
            100,
            0.4,
            130,
            100,
            0.4,
        ],
        [],
    )
    assert len(faces) == 2
    assert faces[0][2] == 0
    assert faces[0][8] == 0.4
    assert faces[0][3] == -18
    assert faces[1][0] == -8


def test_cel_lighting_uses_exactly_four_bands() -> None:
    from bambu_bridge.turntable_overlay import BANDS, BASE, cel_color

    colors = {cel_color((1.0, 0.0, 0.0), math.radians(i)) for i in range(360)}
    allowed = {(*(round(c * b) for c in BASE), 255) for b in BANDS}
    assert colors <= allowed
    assert len(colors) >= 3


def test_archive_builds_colored_surfaces_without_infill() -> None:
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", GCODE)
    source.seek(0)
    shape = archive_shape(source)
    assert shape is not None and len(shape.faces) == 3


def test_over_budget_plate_keeps_whole_top_outline_within_budget() -> None:
    # 200 layers of a 40-segment ring whose radius creeps 0.01 mm per layer: nothing
    # stacks, so the exact merge would need 8000 ribbons (over MAX_FACES).
    from bambu_bridge.turntable_overlay import MAX_FACES, exterior_faces

    def ring(layer: int) -> list[tuple[float, float]]:
        r = 20 + 0.01 * layer
        return [
            (100 + r * math.cos(math.tau * i / 40), 100 + r * math.sin(math.tau * i / 40))
            for i in range(41)
        ]

    walls: list[float] = []
    for layer in range(1, 201):
        z = round(layer * 0.2, 4)
        pts = ring(layer)
        for (x, y), (u, v) in zip(pts, pts[1:], strict=False):
            walls += [x, y, z, u, v, z]
    faces = exterior_faces(walls, [])
    assert 0 < len(faces) <= MAX_FACES
    top = max(f[8] for f in faces)
    assert abs(top - 40.0) < 1e-6

    # every edge of the finished top outline is present (faces[::stride] dropped most)
    def edge(a: tuple[float, float], b: tuple[float, float]) -> tuple[tuple[float, float], ...]:
        pa, pb = (round(a[0], 3), round(a[1], 3)), (round(b[0], 3), round(b[1], 3))
        return (min(pa, pb), max(pa, pb))

    edges = {
        edge((f[0] + 128, f[1] + 128), (f[3] + 128, f[4] + 128))
        for f in faces
        if abs(f[8] - top) < 1e-6
    }
    pts = ring(200)
    assert all(edge(a, b) in edges for a, b in zip(pts, pts[1:], strict=False))


def test_frames_are_rendered_once_per_angle_step() -> None:
    from bambu_bridge.turntable_overlay import _panel

    shape = Shape(((80, 90, 0, 100, 90, 40),), math.sqrt(2) * 128, 40)
    first = _panel(shape, 244, 186, 7)
    assert _panel(shape, 244, 186, 7) is first
    assert _panel(shape, 244, 186, 8) is not first


def test_rest_job_shape_is_fetched_from_printer_storage() -> None:
    # REST POST /jobs keeps no local copy (the bytes are dropped after FTPS upload),
    # so the camera turntable must fetch the job archive back like the 3D view does.
    from types import SimpleNamespace

    from bambu_bridge.native_gateway import NativeGateway

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("Metadata/plate_1.gcode", GCODE)
    name = "Masonry-Keys.gcode.3mf"

    class FakeFtps:
        async def list_dir(self, remote_dir: str = "") -> list[str]:
            return [name]

        async def download_bytes(self, filename: str, *, remote_dir: str = "") -> bytes:
            assert filename == name
            return archive.getvalue()

    printer = SimpleNamespace(ip="10.0.0.9", access_code="x")
    state = SimpleNamespace(
        viz_cache_obj=SimpleNamespace(_make_ftps=lambda ip, code: FakeFtps()),
        registry=SimpleNamespace(get=lambda pid: printer if pid == "P1" else None),
    )
    gateway = SimpleNamespace(inbox=None, app=SimpleNamespace(state=state))
    gateway._load_sd_shape = lambda snap, fn: NativeGateway._load_sd_shape(gateway, snap, fn)
    snapshot = {"printer_id": "P1", "_raw": {"gcode_file": name, "gcode_state": "RUNNING"}}
    shape = NativeGateway.load_camera_shape(gateway, snapshot)
    assert shape is not None and shape.faces
    # unknown printer or non-archive file: no shape, no crash
    assert NativeGateway.load_camera_shape(gateway, {**snapshot, "printer_id": "P2"}) is None
    other = {**snapshot, "_raw": {"gcode_file": "cali.gcode", "gcode_state": "RUNNING"}}
    assert NativeGateway.load_camera_shape(gateway, other) is None
