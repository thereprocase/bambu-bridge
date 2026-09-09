"""Unit tests for the stdlib 3MF mesh parser (spec G4).

All tests are hermetic: 3MF files are synthesised in-process using
``zipfile`` + Python string formatting.  No external files, no FTPS calls.

Coverage matrix
---------------
* Minimal valid cube (8 verts, 12 tris) — happy path.
* ``<build><item transform="…">`` — transform applied to vertices.
* ``unit="inch"`` — converted to mm (25.4× scale).
* One-level ``<component>`` nesting with per-component transform.
* Bambu ``Metadata/slice_info.config`` (XML) — layer height + filaments.
* Bambu ``Metadata/project_settings.config`` (JSON) — layer height + filaments.
* Filament colour normalisation (8-digit RRGGBBAA → #RRGGBB).
* ParseError on bad ZIP.
* ParseError when 3D/3dmodel.model is absent.
* ParseError on malformed XML.
* ParseError when triangle_count exceeds the limit (monkey-patched limit).
* Empty mesh (no build items) — does not crash, returns zero verts/tris.
* Non-mesh objects (type="support") are skipped.
* Missing bbox is all-zeros when mesh is empty.
* **Production extension** — ``<component p:path="…">`` loads mesh from
  per-object model file; transform on the component is applied correctly.
* **Production extension** — ``<item p:path="…">`` on the build item
  directly references a sub-model file.
* Production path missing from archive → silently skipped (no crash).
* Production path with ``..`` traversal attempt → silently skipped.
* **Bambu 2015 namespace** — archives using the released spec namespace
  ``http://schemas.microsoft.com/3dmanufacturing/core/2015/02`` parse
  correctly (geometry_available=True, correct vertex/triangle counts).
* **Bambu .gcode.3mf sliced format** — empty ``<resources/>`` + ``<build/>``
  with ``Metadata/plate_1.json`` → ``geometry_available=False``, bbox
  populated from ``bbox_all`` field, filaments from slice_info.config.
* .gcode.3mf without plate_1.json → geometry_available=False, bbox all-zeros.
* Malformed plate_1.json → silently ignored, bbox falls back to zeros.
* plate_1.json with too-short bbox_all array → silently ignored.
* geometry_available=True for a normal mesh (normal path unchanged).
"""

from __future__ import annotations

import io
import struct
import zipfile

import pytest

from bambu_bridge.protocol.threemf import (
    ParseError,
    parse_3mf,
)

# ------------------------------------------------------------------ #
# Helpers — build tiny synthetic 3MF archives in memory
# ------------------------------------------------------------------ #

_MODEL_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<model unit="{unit}" xmlns="http://schemas.microsoft.com/3dml/2013/core">
  <resources>
{resources}
  </resources>
  <build>
{build_items}
  </build>
</model>
"""

_CUBE_OBJECT = """\
    <object id="1" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0"/>
          <vertex x="10" y="0" z="0"/>
          <vertex x="10" y="10" z="0"/>
          <vertex x="0" y="10" z="0"/>
          <vertex x="0" y="0" z="10"/>
          <vertex x="10" y="0" z="10"/>
          <vertex x="10" y="10" z="10"/>
          <vertex x="0" y="10" z="10"/>
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2"/>
          <triangle v1="0" v2="2" v3="3"/>
          <triangle v1="4" v2="6" v3="5"/>
          <triangle v1="4" v2="7" v3="6"/>
          <triangle v1="0" v2="4" v3="5"/>
          <triangle v1="0" v2="5" v3="1"/>
          <triangle v1="1" v2="5" v3="6"/>
          <triangle v1="1" v2="6" v3="2"/>
          <triangle v1="2" v2="6" v3="7"/>
          <triangle v1="2" v2="7" v3="3"/>
          <triangle v1="3" v2="7" v3="4"/>
          <triangle v1="3" v2="4" v3="0"/>
        </triangles>
      </mesh>
    </object>"""


def _make_3mf(
    *,
    unit: str = "millimeter",
    resources: str = _CUBE_OBJECT,
    build_items: str = '    <item objectid="1"/>',
    extra_members: dict[str, bytes] | None = None,
) -> bytes:
    """Synthesise a minimal in-memory 3MF archive."""
    model_xml = _MODEL_TEMPLATE.format(
        unit=unit,
        resources=resources,
        build_items=build_items,
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("3D/3dmodel.model", model_xml)
        for name, data in (extra_members or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


_CONTENT_TYPES = """\
<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>"""


# ------------------------------------------------------------------ #
# Tests — happy path
# ------------------------------------------------------------------ #


def test_parse_cube_basic() -> None:
    """Minimal cube: 8 vertices, 12 triangles, mm units, no transform."""
    data = _make_3mf()
    mesh = parse_3mf(data)

    assert mesh.vertex_count == 8
    assert mesh.triangle_count == 12
    assert len(mesh.vertices) == 8 * 3
    assert len(mesh.indices) == 12 * 3

    # Vertices are array('f') — float32
    assert mesh.vertices.typecode == "f"
    assert mesh.indices.typecode == "I"


def test_parse_cube_bbox() -> None:
    """Bounding box of the unit-10 cube should be [0,0,0]..[10,10,10] mm."""
    data = _make_3mf()
    mesh = parse_3mf(data)

    assert mesh.bbox_min == pytest.approx([0.0, 0.0, 0.0], abs=1e-4)
    assert mesh.bbox_max == pytest.approx([10.0, 10.0, 10.0], abs=1e-4)


def test_parse_cube_vertex_values() -> None:
    """Spot-check that vertex values are in the float32 array in xyz triples."""
    data = _make_3mf()
    mesh = parse_3mf(data)

    # First vertex should be (0, 0, 0).
    assert mesh.vertices[0] == pytest.approx(0.0)
    assert mesh.vertices[1] == pytest.approx(0.0)
    assert mesh.vertices[2] == pytest.approx(0.0)

    # Second vertex should be (10, 0, 0).
    assert mesh.vertices[3] == pytest.approx(10.0)
    assert mesh.vertices[4] == pytest.approx(0.0)
    assert mesh.vertices[5] == pytest.approx(0.0)


def test_parse_triangle_count_and_index_count() -> None:
    """Indices array length is triangle_count * 3."""
    data = _make_3mf()
    mesh = parse_3mf(data)
    assert len(mesh.indices) == mesh.triangle_count * 3


def test_parse_no_metadata_returns_none() -> None:
    """No Bambu metadata → layer_height_mm is None, filaments is empty."""
    data = _make_3mf()
    mesh = parse_3mf(data)
    assert mesh.layer_height_mm is None
    assert mesh.filaments == []


# ------------------------------------------------------------------ #
# Tests — build-item transform
# ------------------------------------------------------------------ #


def test_build_item_translation_transform() -> None:
    """A build item with a pure translation shifts all vertices."""
    # Identity rotation + translation of (100, 200, 300)
    transform = "1 0 0  0 1 0  0 0 1  100 200 300"
    build_items = f'    <item objectid="1" transform="{transform}"/>'
    data = _make_3mf(build_items=build_items)
    mesh = parse_3mf(data)

    # Cube vertices were 0..10; after +100/+200/+300 they should be 100..110, etc.
    assert mesh.bbox_min == pytest.approx([100.0, 200.0, 300.0], abs=1e-3)
    assert mesh.bbox_max == pytest.approx([110.0, 210.0, 310.0], abs=1e-3)
    assert mesh.vertex_count == 8
    assert mesh.triangle_count == 12


def test_build_item_uniform_scale_transform() -> None:
    """A 2× uniform scale doubles the bounding box."""
    # 2× uniform scale: diagonal of rotation matrix is 2 2 2, translation 0
    transform = "2 0 0  0 2 0  0 0 2  0 0 0"
    build_items = f'    <item objectid="1" transform="{transform}"/>'
    data = _make_3mf(build_items=build_items)
    mesh = parse_3mf(data)

    assert mesh.bbox_min == pytest.approx([0.0, 0.0, 0.0], abs=1e-3)
    assert mesh.bbox_max == pytest.approx([20.0, 20.0, 20.0], abs=1e-3)


# ------------------------------------------------------------------ #
# Tests — unit conversion
# ------------------------------------------------------------------ #


def test_unit_inch_converted_to_mm() -> None:
    """unit="inch" scales all coordinates by 25.4."""
    data = _make_3mf(unit="inch")
    mesh = parse_3mf(data)

    # Cube is 10 inches on each side → 254 mm
    assert mesh.bbox_max[0] == pytest.approx(254.0, abs=1e-2)
    assert mesh.bbox_max[1] == pytest.approx(254.0, abs=1e-2)
    assert mesh.bbox_max[2] == pytest.approx(254.0, abs=1e-2)


def test_unit_centimeter_converted_to_mm() -> None:
    """unit="centimeter" scales coordinates by 10."""
    data = _make_3mf(unit="centimeter")
    mesh = parse_3mf(data)
    assert mesh.bbox_max[0] == pytest.approx(100.0, abs=1e-2)


def test_unit_meter_converted_to_mm() -> None:
    """unit="meter" scales coordinates by 1000."""
    data = _make_3mf(unit="meter")
    mesh = parse_3mf(data)
    assert mesh.bbox_max[0] == pytest.approx(10_000.0, abs=0.1)


# ------------------------------------------------------------------ #
# Tests — component nesting
# ------------------------------------------------------------------ #


_COMPONENT_RESOURCES = """\
    <object id="1" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0"/>
          <vertex x="5" y="0" z="0"/>
          <vertex x="0" y="5" z="0"/>
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2"/>
        </triangles>
      </mesh>
    </object>
    <object id="2" type="model">
      <components>
        <component objectid="1" transform="1 0 0  0 1 0  0 0 1  20 0 0"/>
      </components>
    </object>"""


def test_component_one_level_deep() -> None:
    """A build item referencing an object with a component is resolved."""
    build_items = '    <item objectid="2"/>'
    data = _make_3mf(resources=_COMPONENT_RESOURCES, build_items=build_items)
    mesh = parse_3mf(data)

    # Object 2 has one component (object 1) shifted by +20 in X.
    assert mesh.vertex_count == 3
    assert mesh.triangle_count == 1
    # X min should be 20 (component is shifted by 20).
    assert mesh.bbox_min[0] == pytest.approx(20.0, abs=1e-3)
    assert mesh.bbox_max[0] == pytest.approx(25.0, abs=1e-3)


# ------------------------------------------------------------------ #
# Tests — non-mesh objects skipped
# ------------------------------------------------------------------ #


_SUPPORT_RESOURCE = """\
    <object id="1" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0"/>
          <vertex x="1" y="0" z="0"/>
          <vertex x="0" y="1" z="0"/>
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2"/>
        </triangles>
      </mesh>
    </object>
    <object id="2" type="support">
      <mesh>
        <vertices>
          <vertex x="100" y="100" z="100"/>
          <vertex x="101" y="100" z="100"/>
          <vertex x="100" y="101" z="100"/>
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2"/>
        </triangles>
      </mesh>
    </object>"""


def test_non_mesh_support_object_skipped() -> None:
    """Objects with type='support' must NOT be included in the merged mesh."""
    build_items = '    <item objectid="1"/>\n    <item objectid="2"/>'
    data = _make_3mf(resources=_SUPPORT_RESOURCE, build_items=build_items)
    mesh = parse_3mf(data)

    # Only the model object (id=1, 3 verts, 1 tri) should be included;
    # support object (id=2) is skipped — 100/101 coordinates must not appear.
    assert mesh.vertex_count == 3
    assert mesh.bbox_max[0] < 2.0  # not 101


# ------------------------------------------------------------------ #
# Tests — Bambu metadata
# ------------------------------------------------------------------ #

_SLICE_INFO_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="layer_height" value="0.2"/>
    <filament id="1" type="PLA" color="FF0000FF" used_m="1.5" used_g="4.4"/>
    <filament id="2" type="PETG" color="00FF00FF" used_m="0.5" used_g="1.3"/>
  </plate>
</config>
"""

_PROJECT_SETTINGS_JSON = """\
{
  "layer_height": "0.15",
  "filament_colour": "AABBCCFF;112233FF",
  "filament_type": "PLA;ABS"
}
"""


def test_slice_info_config_parsed() -> None:
    """Metadata/slice_info.config provides layer height and filament info."""
    extra = {"Metadata/slice_info.config": _SLICE_INFO_XML.encode()}
    data = _make_3mf(extra_members=extra)
    mesh = parse_3mf(data)

    assert mesh.layer_height_mm == pytest.approx(0.2)
    assert len(mesh.filaments) == 2

    fil_types = {f.slot: f.type for f in mesh.filaments}
    fil_colors = {f.slot: f.color for f in mesh.filaments}
    assert fil_types[1] == "PLA"
    assert fil_types[2] == "PETG"
    # RRGGBBAA → #RRGGBB (alpha stripped)
    assert fil_colors[1] == "#FF0000"
    assert fil_colors[2] == "#00FF00"


def test_project_settings_json_parsed_as_fallback() -> None:
    """Metadata/project_settings.config provides layer height and filaments
    when slice_info is absent."""
    extra = {"Metadata/project_settings.config": _PROJECT_SETTINGS_JSON.encode()}
    data = _make_3mf(extra_members=extra)
    mesh = parse_3mf(data)

    assert mesh.layer_height_mm == pytest.approx(0.15)
    assert len(mesh.filaments) == 2

    fil_types = {f.slot: f.type for f in mesh.filaments}
    assert fil_types[1] == "PLA"
    assert fil_types[2] == "ABS"

    fil_colors = {f.slot: f.color for f in mesh.filaments}
    # AABBCCFF → #AABBCC
    assert fil_colors[1] == "#AABBCC"
    assert fil_colors[2] == "#112233"


def test_slice_info_wins_over_project_settings() -> None:
    """When both metadata sources exist, slice_info.config wins."""
    extra = {
        "Metadata/slice_info.config": _SLICE_INFO_XML.encode(),
        "Metadata/project_settings.config": _PROJECT_SETTINGS_JSON.encode(),
    }
    data = _make_3mf(extra_members=extra)
    mesh = parse_3mf(data)

    # slice_info has layer_height=0.2, project_settings has 0.15.
    assert mesh.layer_height_mm == pytest.approx(0.2)
    # slice_info has PLA/PETG; project_settings has PLA/ABS.
    types = {f.slot: f.type for f in mesh.filaments}
    assert types[2] == "PETG"  # from slice_info, not ABS from project_settings


def test_corrupt_metadata_does_not_crash() -> None:
    """Malformed metadata is silently ignored; mesh is still returned."""
    extra = {
        "Metadata/slice_info.config": b"<not valid xml",
        "Metadata/project_settings.config": b"not json {{{",
    }
    data = _make_3mf(extra_members=extra)
    mesh = parse_3mf(data)  # must not raise
    assert mesh.vertex_count == 8
    assert mesh.layer_height_mm is None
    assert mesh.filaments == []


# ------------------------------------------------------------------ #
# Tests — error paths
# ------------------------------------------------------------------ #


def test_parse_error_on_bad_zip() -> None:
    """Raw garbage bytes raise ParseError (not a ZIP)."""
    with pytest.raises(ParseError, match="bad ZIP"):
        parse_3mf(b"THIS IS NOT A ZIP FILE AT ALL")


def test_parse_error_on_empty_bytes() -> None:
    """Empty bytes raise ParseError."""
    with pytest.raises(ParseError):
        parse_3mf(b"")


def test_parse_error_missing_model_member() -> None:
    """A ZIP without 3D/3dmodel.model raises ParseError."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("some_other_file.txt", "hello")
    with pytest.raises(ParseError, match="3D/3dmodel.model"):
        parse_3mf(buf.getvalue())


def test_parse_error_malformed_xml() -> None:
    """A 3MF with invalid XML in the model raises ParseError."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model><unclosed")
    with pytest.raises(ParseError, match="malformed XML"):
        parse_3mf(buf.getvalue())


def test_parse_error_triangle_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mesh exceeding the triangle limit raises ParseError with a message."""
    import bambu_bridge.protocol.threemf as mod  # noqa: PLC0415

    monkeypatch.setattr(mod, "_MAX_TRIANGLES", 11)  # cube has 12 tris

    data = _make_3mf()
    with pytest.raises(ParseError, match="triangles"):
        parse_3mf(data)


# ------------------------------------------------------------------ #
# Tests — empty mesh
# ------------------------------------------------------------------ #


def test_empty_build_section_no_crash() -> None:
    """A model with an empty <build> section returns a zero-count mesh."""
    # No build items → merged_verts is empty.
    empty_model = """\
<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dml/2013/core">
  <resources/>
  <build/>
</model>
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("3D/3dmodel.model", empty_model)
    mesh = parse_3mf(buf.getvalue())

    assert mesh.vertex_count == 0
    assert mesh.triangle_count == 0
    assert mesh.bbox_min == [0.0, 0.0, 0.0]
    assert mesh.bbox_max == [0.0, 0.0, 0.0]
    assert len(mesh.vertices) == 0
    assert len(mesh.indices) == 0


# ------------------------------------------------------------------ #
# Tests — output array properties
# ------------------------------------------------------------------ #


def test_output_arrays_are_correct_typecodes() -> None:
    """vertices is array('f') and indices is array('I')."""
    data = _make_3mf()
    mesh = parse_3mf(data)
    assert mesh.vertices.typecode == "f"
    assert mesh.indices.typecode == "I"


def test_vertices_bytes_are_float32_le() -> None:
    """tobytes() from a little-endian float32 array matches struct.pack('<f')."""
    data = _make_3mf()
    mesh = parse_3mf(data)
    raw_bytes = mesh.vertices.tobytes()
    # On any platform, first 4 bytes should decode as the first X coordinate.
    (first_val,) = struct.unpack_from("<f", raw_bytes, 0)
    assert first_val == pytest.approx(mesh.vertices[0], abs=1e-6)


# ------------------------------------------------------------------ #
# Tests — six-digit colour normalisation
# ------------------------------------------------------------------ #


_SLICE_INFO_6HEX = """\
<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="layer_height" value="0.2"/>
    <filament id="1" type="PLA" color="FF0000" used_m="1" used_g="3"/>
  </plate>
</config>
"""


def test_six_digit_color_normalised() -> None:
    """Six-digit hex colour (without alpha) is returned as #RRGGBB."""
    extra = {"Metadata/slice_info.config": _SLICE_INFO_6HEX.encode()}
    data = _make_3mf(extra_members=extra)
    mesh = parse_3mf(data)
    assert mesh.filaments[0].color == "#FF0000"


# ------------------------------------------------------------------ #
# Tests — multiple build items merged
# ------------------------------------------------------------------ #

_TWO_OBJECT_RESOURCES = """\
    <object id="1" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0"/>
          <vertex x="1" y="0" z="0"/>
          <vertex x="0" y="1" z="0"/>
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2"/>
        </triangles>
      </mesh>
    </object>
    <object id="2" type="model">
      <mesh>
        <vertices>
          <vertex x="50" y="0" z="0"/>
          <vertex x="51" y="0" z="0"/>
          <vertex x="50" y="1" z="0"/>
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2"/>
        </triangles>
      </mesh>
    </object>"""


def test_two_build_items_merged() -> None:
    """Two build items produce a merged mesh with summed counts."""
    build_items = "    <item objectid=\"1\"/>\n    <item objectid=\"2\"/>"
    data = _make_3mf(resources=_TWO_OBJECT_RESOURCES, build_items=build_items)
    mesh = parse_3mf(data)

    assert mesh.vertex_count == 6
    assert mesh.triangle_count == 2
    # bbox should span both objects
    assert mesh.bbox_min[0] == pytest.approx(0.0, abs=1e-3)
    assert mesh.bbox_max[0] == pytest.approx(51.0, abs=1e-3)


def test_second_object_indices_rebased() -> None:
    """Triangle indices for the second object are offset by vertex_count of the first."""
    build_items = "    <item objectid=\"1\"/>\n    <item objectid=\"2\"/>"
    data = _make_3mf(resources=_TWO_OBJECT_RESOURCES, build_items=build_items)
    mesh = parse_3mf(data)

    # Object 1 has 3 verts (indices 0,1,2). Object 2's triangle should use 3,4,5.
    tri2_v1 = mesh.indices[3]  # first index of second triangle
    tri2_v2 = mesh.indices[4]
    tri2_v3 = mesh.indices[5]
    assert {tri2_v1, tri2_v2, tri2_v3} == {3, 4, 5}


# ------------------------------------------------------------------ #
# Helpers — Production-extension archive builders
# ------------------------------------------------------------------ #

# The Bambu Studio Production-extension layout:
#
#   3D/3dmodel.model          — root; contains build items and component stubs
#   3D/Objects/object_1.model — per-object model; contains the actual <mesh>
#
# Root 3dmodel.model example:
#   <resources>
#     <object id="1" type="model">
#       <components>
#         <component objectid="1"
#                    p:path="/3D/Objects/object_1.model"
#                    transform="1 0 0 0 1 0 0 0 1 50 0 0"/>
#       </components>
#     </object>
#   </resources>
#   <build>
#     <item objectid="1"/>
#   </build>
#
# object_1.model:
#   <model xmlns="http://schemas.microsoft.com/3dml/2013/core">
#     <resources>
#       <object id="1" type="model">
#         <mesh> … </mesh>
#       </object>
#     </resources>
#   </model>

# The cube mesh fragment — reused across Production tests.
_CUBE_MESH_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dml/2013/core">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0"/>
          <vertex x="10" y="0" z="0"/>
          <vertex x="10" y="10" z="0"/>
          <vertex x="0" y="10" z="0"/>
          <vertex x="0" y="0" z="10"/>
          <vertex x="10" y="0" z="10"/>
          <vertex x="10" y="10" z="10"/>
          <vertex x="0" y="10" z="10"/>
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2"/>
          <triangle v1="0" v2="2" v3="3"/>
          <triangle v1="4" v2="6" v3="5"/>
          <triangle v1="4" v2="7" v3="6"/>
          <triangle v1="0" v2="4" v3="5"/>
          <triangle v1="0" v2="5" v3="1"/>
          <triangle v1="1" v2="5" v3="6"/>
          <triangle v1="1" v2="6" v3="2"/>
          <triangle v1="2" v2="6" v3="7"/>
          <triangle v1="2" v2="7" v3="3"/>
          <triangle v1="3" v2="7" v3="4"/>
          <triangle v1="3" v2="4" v3="0"/>
        </triangles>
      </mesh>
    </object>
  </resources>
</model>
"""

# Production extension namespace (used in root model XML).
_NS_PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"


def _make_production_3mf(
    *,
    component_transform: str | None = None,
    item_uses_path: bool = False,
    sub_model_path: str = "/3D/Objects/object_1.model",
    sub_model_name: str = "3D/Objects/object_1.model",
    sub_model_bytes: bytes | None = None,
    include_sub_model: bool = True,
) -> bytes:
    """Build a Bambu-style Production-extension 3MF.

    Two layouts are supported via ``item_uses_path``:

    ``False`` (default) — Bambu Studio layout:
        root model's build item → root object with <components> →
        each component has p:path pointing at the sub-model.

    ``True`` — build item itself carries p:path:
        <item objectid="1" p:path="/3D/Objects/object_1.model"/>
        Root model's <resources> is empty (no object stubs needed).

    ``component_transform`` is the space-separated 3MF transform string
    placed on the ``<component>`` (only used when ``item_uses_path=False``).
    """
    comp_transform_attr = (
        f' transform="{component_transform}"' if component_transform else ""
    )
    sub_bytes = sub_model_bytes if sub_model_bytes is not None else _CUBE_MESH_XML.encode()

    if item_uses_path:
        # Build item carries the p:path directly; root resources can be empty.
        root_model = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter"
    xmlns="http://schemas.microsoft.com/3dml/2013/core"
    xmlns:p="{_NS_PROD}">
  <resources/>
  <build>
    <item objectid="1" p:path="{sub_model_path}"/>
  </build>
</model>
"""
    else:
        # Bambu layout: root object references sub-model via <component p:path>.
        root_model = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter"
    xmlns="http://schemas.microsoft.com/3dml/2013/core"
    xmlns:p="{_NS_PROD}">
  <resources>
    <object id="1" type="model">
      <components>
        <component objectid="1"
            p:path="{sub_model_path}"{comp_transform_attr}/>
      </components>
    </object>
  </resources>
  <build>
    <item objectid="1"/>
  </build>
</model>
"""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("3D/3dmodel.model", root_model)
        if include_sub_model:
            zf.writestr(sub_model_name, sub_bytes)
    return buf.getvalue()


# ------------------------------------------------------------------ #
# Tests — Production extension: component p:path
# ------------------------------------------------------------------ #


def test_production_component_path_loads_sub_model() -> None:
    """A <component p:path="…"> loads the cube from the sub-model file."""
    data = _make_production_3mf()
    mesh = parse_3mf(data)

    # The cube in object_1.model: 8 verts, 12 tris, 10 mm side.
    assert mesh.vertex_count == 8
    assert mesh.triangle_count == 12
    assert mesh.bbox_min == pytest.approx([0.0, 0.0, 0.0], abs=1e-3)
    assert mesh.bbox_max == pytest.approx([10.0, 10.0, 10.0], abs=1e-3)


def test_production_component_path_with_transform() -> None:
    """A <component p:path="…" transform="…"> applies the transform to the
    sub-model's geometry before merging."""
    # Pure translation of +50 along X.
    t = "1 0 0  0 1 0  0 0 1  50 0 0"
    data = _make_production_3mf(component_transform=t)
    mesh = parse_3mf(data)

    assert mesh.vertex_count == 8
    assert mesh.triangle_count == 12
    # Cube was 0..10 mm; shifted +50 in X → 50..60.
    assert mesh.bbox_min[0] == pytest.approx(50.0, abs=1e-3)
    assert mesh.bbox_max[0] == pytest.approx(60.0, abs=1e-3)
    # Y and Z unchanged.
    assert mesh.bbox_min[1] == pytest.approx(0.0, abs=1e-3)
    assert mesh.bbox_max[2] == pytest.approx(10.0, abs=1e-3)


def test_production_component_path_namespace_agnostic() -> None:
    """The p:path attribute is matched by local-name 'path' regardless of
    which namespace prefix the slicer used (or even no namespace at all)."""
    # Use a different namespace prefix (not the standard p:).
    alt_ns = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
    root_model = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter"
    xmlns="http://schemas.microsoft.com/3dml/2013/core"
    xmlns:prod="{alt_ns}">
  <resources>
    <object id="1" type="model">
      <components>
        <component objectid="1" prod:path="/3D/Objects/object_1.model"/>
      </components>
    </object>
  </resources>
  <build>
    <item objectid="1"/>
  </build>
</model>
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("3D/3dmodel.model", root_model)
        zf.writestr("3D/Objects/object_1.model", _CUBE_MESH_XML)
    data = buf.getvalue()

    mesh = parse_3mf(data)
    assert mesh.vertex_count == 8
    assert mesh.triangle_count == 12


# ------------------------------------------------------------------ #
# Tests — Production extension: item p:path
# ------------------------------------------------------------------ #


def test_production_item_path_loads_sub_model() -> None:
    """A <item p:path="…" objectid="1"> on the build item loads geometry
    from the referenced file directly (no root-level object stub needed)."""
    data = _make_production_3mf(item_uses_path=True)
    mesh = parse_3mf(data)

    assert mesh.vertex_count == 8
    assert mesh.triangle_count == 12
    assert mesh.bbox_max == pytest.approx([10.0, 10.0, 10.0], abs=1e-3)


# ------------------------------------------------------------------ #
# Tests — Production extension: graceful degradation
# ------------------------------------------------------------------ #


def test_production_missing_sub_model_skipped_silently() -> None:
    """A <component p:path="…"> whose target file is absent in the ZIP
    produces zero geometry but does NOT raise ParseError."""
    data = _make_production_3mf(include_sub_model=False)
    # Must not raise; returns an empty (or near-empty) mesh.
    mesh = parse_3mf(data)
    assert mesh.vertex_count == 0
    assert mesh.triangle_count == 0


def test_production_path_traversal_skipped_silently() -> None:
    """A p:path containing '..' is rejected silently (traversal guard)."""
    data = _make_production_3mf(
        sub_model_path="/../etc/passwd",
        # Still include a file at the normalised name — but the guard must
        # reject the path before any lookup attempt.
        sub_model_name="etc/passwd",
    )
    mesh = parse_3mf(data)
    assert mesh.vertex_count == 0
    assert mesh.triangle_count == 0


def test_production_malformed_sub_model_skipped_silently() -> None:
    """If the sub-model XML is malformed, that component is silently skipped."""
    bad_xml = b"<model><unclosed"
    data = _make_production_3mf(sub_model_bytes=bad_xml)
    mesh = parse_3mf(data)
    assert mesh.vertex_count == 0
    assert mesh.triangle_count == 0


# ------------------------------------------------------------------ #
# Helpers — Bambu sliced-output (.gcode.3mf) archive builder
# ------------------------------------------------------------------ #

# Structure observed from weather_station_reflector.gcode.3mf:
#
#   3D/3dmodel.model      (779 bytes, empty resources + build)
#   Metadata/plate_1.json (631 bytes, bbox_all + bbox_objects)
#   Metadata/slice_info.config (XML, filament + layer_height)
#   Metadata/plate_1.gcode (large gcode, not parsed)
#   [Content_Types].xml
#   _rels/.rels
#
# The model XML uses the released 3MF namespace (2015/02), not the 2013
# draft namespace used in the synthetic test fixtures above.

_NS_BAMBU_2015 = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"

_GCODE_3MF_MODEL = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US"
    xmlns="{_NS_BAMBU_2015}"
    xmlns:BambuStudio="http://schemas.bambulab.com/package/2021">
 <metadata name="Application">BambuStudio-2.3.2</metadata>
 <resources>
 </resources>
 <build/>
</model>
"""

# plate_1.json bbox_all mirrors observed values from the real file.
# Format: [x_min, y_min, x_max, y_max] in millimeters.
_PLATE_1_JSON_FULL = """\
{
  "bbox_all": [52.601566, 36.950956, 191.329822, 206.967427],
  "bbox_objects": [
    {
      "area": 3537.67,
      "bbox": [109.380454, 125.018336, 191.329822, 206.967427],
      "id": 667,
      "layer_height": 0.2,
      "name": "weather_station_reflector.stl"
    }
  ]
}
"""

_SLICE_INFO_GCODE_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="layer_height" value="0.2"/>
    <filament id="1" type="PLA" color="FF5500FF" used_m="2.1" used_g="6.2"/>
  </plate>
</config>
"""


def _make_gcode_3mf(
    *,
    include_plate_json: bool = True,
    plate_json_bytes: bytes | None = None,
    include_slice_info: bool = True,
) -> bytes:
    """Build a synthetic archive that mirrors the Bambu .gcode.3mf structure.

    The root model uses the 2015 namespace and has empty <resources> and
    <build/> — exactly what Bambu Studio produces in sliced output.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("3D/3dmodel.model", _GCODE_3MF_MODEL)
        if include_plate_json:
            pj = plate_json_bytes if plate_json_bytes is not None else _PLATE_1_JSON_FULL.encode()
            zf.writestr("Metadata/plate_1.json", pj)
        if include_slice_info:
            zf.writestr("Metadata/slice_info.config", _SLICE_INFO_GCODE_XML.encode())
    return buf.getvalue()


# ------------------------------------------------------------------ #
# Tests — Bambu 2015 namespace with actual mesh geometry
# ------------------------------------------------------------------ #


def test_bambu_2015_namespace_with_geometry() -> None:
    """A 3MF that uses the 2015 'manufacturing' namespace parses correctly.

    This covers real design files opened in Bambu Studio (not sliced output).
    The namespace auto-detection must recognise the 2015 URI and locate
    <resources> and <build> under the correct Clark-notation prefix.
    """
    cube_2015 = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="{_NS_BAMBU_2015}">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0"/>
          <vertex x="5" y="0" z="0"/>
          <vertex x="0" y="5" z="0"/>
          <vertex x="0" y="0" z="5"/>
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2"/>
          <triangle v1="0" v2="1" v3="3"/>
          <triangle v1="0" v2="2" v3="3"/>
          <triangle v1="1" v2="2" v3="3"/>
        </triangles>
      </mesh>
    </object>
  </resources>
  <build>
    <item objectid="1"/>
  </build>
</model>
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("3D/3dmodel.model", cube_2015)
    mesh = parse_3mf(buf.getvalue())

    assert mesh.geometry_available is True
    assert mesh.vertex_count == 4
    assert mesh.triangle_count == 4
    assert mesh.bbox_min == pytest.approx([0.0, 0.0, 0.0], abs=1e-3)
    assert mesh.bbox_max == pytest.approx([5.0, 5.0, 5.0], abs=1e-3)


# ------------------------------------------------------------------ #
# Tests — Bambu .gcode.3mf sliced-output format (no geometry)
# ------------------------------------------------------------------ #


def test_gcode_3mf_geometry_available_false() -> None:
    """A sliced .gcode.3mf with empty model returns geometry_available=False."""
    data = _make_gcode_3mf()
    mesh = parse_3mf(data)

    assert mesh.geometry_available is False
    assert mesh.vertex_count == 0
    assert mesh.triangle_count == 0
    assert len(mesh.vertices) == 0
    assert len(mesh.indices) == 0


def test_gcode_3mf_bbox_from_plate_json() -> None:
    """plate_1.json bbox_all [xmin, ymin, xmax, ymax] populates bbox_min/max.

    Observed from weather_station_reflector.gcode.3mf:
      bbox_all = [52.601566, 36.950956, 191.329822, 206.967427]
    z is set to 0.0 because the JSON only carries a 2D footprint.
    """
    data = _make_gcode_3mf()
    mesh = parse_3mf(data)

    assert mesh.bbox_min == pytest.approx([52.601566, 36.950956, 0.0], abs=1e-3)
    assert mesh.bbox_max == pytest.approx([191.329822, 206.967427, 0.0], abs=1e-3)


def test_gcode_3mf_filaments_from_slice_info() -> None:
    """slice_info.config filament data is still extracted from .gcode.3mf."""
    data = _make_gcode_3mf()
    mesh = parse_3mf(data)

    assert mesh.layer_height_mm == pytest.approx(0.2)
    assert len(mesh.filaments) == 1
    assert mesh.filaments[0].slot == 1
    assert mesh.filaments[0].type == "PLA"
    assert mesh.filaments[0].color == "#FF5500"


def test_gcode_3mf_without_plate_json_bbox_zero() -> None:
    """If plate_1.json is absent, bbox falls back to all-zeros (no crash)."""
    data = _make_gcode_3mf(include_plate_json=False)
    mesh = parse_3mf(data)

    assert mesh.geometry_available is False
    assert mesh.bbox_min == [0.0, 0.0, 0.0]
    assert mesh.bbox_max == [0.0, 0.0, 0.0]


def test_gcode_3mf_malformed_plate_json_bbox_zero() -> None:
    """Malformed plate_1.json is silently ignored; bbox falls back to zeros."""
    data = _make_gcode_3mf(plate_json_bytes=b"not valid json {{{{")
    mesh = parse_3mf(data)

    assert mesh.geometry_available is False
    assert mesh.bbox_min == [0.0, 0.0, 0.0]
    assert mesh.bbox_max == [0.0, 0.0, 0.0]


def test_gcode_3mf_plate_json_short_bbox_all_ignored() -> None:
    """plate_1.json with a bbox_all array shorter than 4 is silently ignored."""
    short_bbox_json = b'{"bbox_all": [10.0, 20.0]}'
    data = _make_gcode_3mf(plate_json_bytes=short_bbox_json)
    mesh = parse_3mf(data)

    assert mesh.geometry_available is False
    assert mesh.bbox_min == [0.0, 0.0, 0.0]
    assert mesh.bbox_max == [0.0, 0.0, 0.0]


def test_gcode_3mf_plate_json_non_dict_ignored() -> None:
    """plate_1.json that is a JSON array (not object) is silently ignored."""
    data = _make_gcode_3mf(plate_json_bytes=b"[1, 2, 3]")
    mesh = parse_3mf(data)

    assert mesh.geometry_available is False
    assert mesh.bbox_min == [0.0, 0.0, 0.0]


def test_gcode_3mf_plate_json_non_numeric_bbox_ignored() -> None:
    """plate_1.json bbox_all with non-numeric values is silently ignored."""
    bad = b'{"bbox_all": ["a", "b", "c", "d"]}'
    data = _make_gcode_3mf(plate_json_bytes=bad)
    mesh = parse_3mf(data)

    assert mesh.geometry_available is False
    assert mesh.bbox_min == [0.0, 0.0, 0.0]


def test_gcode_3mf_2015_namespace_parsed_correctly() -> None:
    """The 2015 'manufacturing' namespace in _GCODE_3MF_MODEL is accepted
    without raising ParseError — namespace auto-detection covers it."""
    data = _make_gcode_3mf()
    # Must not raise even though the root model uses the 2015 namespace.
    mesh = parse_3mf(data)
    assert mesh.vertex_count == 0  # sliced output, no geometry


def test_normal_mesh_geometry_available_true() -> None:
    """A regular design 3MF (with actual mesh) has geometry_available=True."""
    data = _make_3mf()  # the standard cube fixture
    mesh = parse_3mf(data)

    assert mesh.geometry_available is True
    assert mesh.vertex_count == 8
    assert mesh.triangle_count == 12
