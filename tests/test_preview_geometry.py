from __future__ import annotations

from PIL import Image
import pytest

from bambu_bridge.preview_geometry import body_faces, crease_edges
from bambu_bridge.turntable_overlay import Shape, cel_color, paint_cel, part_color, projection


def test_mesh_welds_duplicate_vertices_and_keeps_disconnected_colors():
    from bambu_bridge.preview_geometry import mesh_faces

    vertices = [
        10,
        10,
        0,
        20,
        10,
        0,
        10,
        20,
        0,
        10,
        10,
        0,
        10,
        20,
        0,
        10,
        10,
        10,
        50,
        10,
        0,
        60,
        10,
        0,
        50,
        20,
        0,
    ]
    faces, parts, two_sided = mesh_faces(vertices, [0, 1, 2, 3, 4, 5, 6, 7, 8])
    assert len(faces) == 3
    assert parts == (0, 0, 1)
    assert two_sided


@pytest.mark.parametrize(
    "vertices,indices", [([float("nan"), 0, 0] * 3, [0, 1, 2]), ([0, 0, 0] * 3, [0, 1, 3])]
)
def test_mesh_rejects_invalid_geometry(vertices, indices):
    from bambu_bridge.preview_geometry import mesh_faces

    with pytest.raises(ValueError):
        mesh_faces(vertices, indices)


def test_continuous_z_keeps_endpoint_height_and_connected_color():
    from bambu_bridge.preview_geometry import continuous_faces
    from bambu_bridge.protocol.gcode_path import parse_gcode_toolpath

    data = b"M83\nG1 X100 Y100 Z0.2\nG1 X110 Z0.3 E1\nG1 Y110 Z0.4 E1\n"
    paths = parse_gcode_toolpath(data, preserve_z=True)
    assert paths.positions[5] == pytest.approx(0.3)
    faces, parts = continuous_faces(paths.positions)
    assert set(parts) == {0}
    assert max(face[i] for face in faces for i in (2, 5, 8, 11)) == pytest.approx(0.4)


@pytest.mark.parametrize("command", ["G2", "G3"])
def test_full_circle_arc_and_helix_are_preserved(command):
    from bambu_bridge.protocol.gcode_path import parse_gcode_toolpath

    paths = parse_gcode_toolpath(
        f"M83\nG1 X100 Y100 Z0.2\n{command} I10 J0 Z0.4 E1\n", arcs=True, preserve_z=True
    )
    assert paths.segment_count > 20
    assert paths.bbox_max[0] == pytest.approx(120, abs=0.06)
    assert paths.positions[-1] == pytest.approx(0.4)


def test_dense_simplification_retains_all_components():
    from bambu_bridge.preview_geometry import simplify_faces

    faces, parts = body_faces(ring(20, 20, 10, 0.2) + ring(50, 20, 10, 0.2))
    dense = tuple(face for face in faces for _ in range(1000))
    identities = tuple(part for part in parts for _ in range(1000))
    reduced, labels = simplify_faces(dense, identities, budget=100)
    assert set(labels) == {0, 1}
    assert 0 < len(reduced) <= 100


def ring(x, y, size, z):
    points = [(x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y)]
    return [value for a, b in zip(points, points[1:], strict=False) for value in (*a, z, *b, z)]


def test_disconnected_parts_have_distinct_stable_colors():
    path = ring(20, 20, 10, 0.2) + ring(60, 20, 10, 0.2)
    faces, parts = body_faces(path)
    assert set(parts) == {0, 1}
    assert part_color(0) != part_color(1)
    assert len(faces) == len(parts)
    assert body_faces(path) == (faces, parts)


def test_stacked_sections_form_one_body_with_merged_sides():
    faces, parts = body_faces(ring(20, 20, 10, 0.2) + ring(20, 20, 10, 0.4))
    assert set(parts) == {0}
    assert len(faces) == 8  # Four merged sides, two triangles at each cap.
    assert max(face[i] for face in faces for i in (2, 5, 8, 11)) == 0.4


def test_caps_preserve_holes():
    faces, parts = body_faces(ring(20, 20, 20, 0.2) + ring(25, 25, 10, 0.2))
    assert set(parts) == {0}
    caps = [face for face in faces if face[14] == 1]
    assert caps
    for face in caps:
        x = sum(face[i] + 128 for i in (0, 3, 6)) / 3
        y = sum(face[i] + 128 for i in (1, 4, 7)) / 3
        assert not (25.22 < x < 34.78 and 25.22 < y < 34.78)


def test_bridge_between_islands_unifies_connected_body():
    faces, parts = body_faces(ring(20, 20, 10, 0.2) + ring(40, 20, 10, 0.2) + ring(20, 20, 30, 0.4))
    assert faces and set(parts) == {0}


def test_vertical_gap_keeps_bodies_separate():
    faces, parts = body_faces(ring(20, 20, 10, 0.2) + ring(20, 20, 10, 5))
    assert faces and set(parts) == {0, 1}


def test_ink_marks_creases_and_leaves_cap_triangulation_plain():
    faces, _ = body_faces(ring(20, 20, 10, 0.2))
    edges = crease_edges(faces)
    assert any(any(flags) for flags in edges)
    for face, flags in zip(faces, edges, strict=True):
        if face[14] == 1:
            assert sum(flags) == 2  # Two contour edges; internal diagonal stays plain.


def test_visible_side_is_plain_and_caps_do_not_paint_over_it():
    path = []
    for layer in range(1, 201):
        path.extend(ring(108, 108, 40, round(layer * 0.2, 4)))
    faces, parts = body_faces(path)
    shape = Shape((), 180, 40, faces=faces, face_parts=parts)
    panel = Image.new("RGBA", (244, 186))
    project = projection(shape, 244, 186, 0)
    paint_cel(panel, shape, project, 0)
    expected = cel_color((0, 1, 0), 0)
    for height in (5, 10, 20, 30, 35):
        x, y = project(0, 20.22, height)
        assert panel.getpixel((round(x), round(y))) == expected
