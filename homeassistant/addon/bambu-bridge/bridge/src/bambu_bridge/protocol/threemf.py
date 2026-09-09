"""Stdlib-only 3MF mesh parser (spec G4).

Reads a ``.3mf`` ZIP, extracts the primary 3D model from
``3D/3dmodel.model``, merges all ``<object>`` meshes with ``<build>``
transform applied (row-major 3x4), and returns a :class:`Mesh3MF`.

Supported:
* All units the 3MF spec defines — all converted to millimeters.
* ``<build><item>`` transforms (3x4 row-major, homogeneous row dropped).
* One level of ``<component>`` nesting with optional per-component transforms.
* **3MF Production extension** — ``<component p:path="…">`` and
  ``<item p:path="…">`` that reference per-object model files inside the
  same ZIP archive.  Any attribute whose XML local-name is ``path`` is
  treated as the Production-extension file reference, regardless of which
  namespace prefix the slicer chose.  Missing referenced members are
  skipped silently (partial geometry beats none).
* **Both 3MF core namespaces** — the 2013 draft namespace
  ``http://schemas.microsoft.com/3dml/2013/core`` and the released spec
  namespace ``http://schemas.microsoft.com/3dmanufacturing/core/2015/02``
  (used by Bambu Studio).  The namespace is auto-detected from the root
  element tag at parse time, so no hard-coded constant is needed.
* Bambu metadata from ``Metadata/slice_info.config`` (XML key/value) or
  ``Metadata/project_settings.config`` (JSON) — defensive, absent is fine.
* **Bambu sliced-output format**: ``.gcode.3mf`` files produced by Bambu
  Studio contain NO mesh geometry — the ``<resources/>`` and ``<build/>``
  elements are intentionally empty.  The parser detects this, sets
  ``geometry_available=False``, and populates ``bbox_min``/``bbox_max``
  from ``Metadata/plate_1.json`` (the 2D build-plate footprint stored as
  ``[x_min, y_min, x_max, y_max]``).

Limits (guarded before/after parse):
* Decompressed model XML > 80 MB raises :class:`ParseError`.
* Merged triangle count > 600 000 raises :class:`ParseError`.

Non-mesh objects (e.g. ``<object type="support">``) are skipped silently.
Malformed XML in the root model raises :class:`ParseError`; malformed XML
in a referenced sub-model is skipped silently.
"""

from __future__ import annotations

import array
import contextlib
import json
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any

# ------------------------------------------------------------------ #
# Constants
# ------------------------------------------------------------------ #

# Known 3MF core namespace URIs.  The 2013 draft is used by some older
# slicers and by the synthetic test fixtures in this repo; the 2015 URI
# is used by Bambu Studio and is the released 3MF specification.
_NS_CORE_2013 = "http://schemas.microsoft.com/3dml/2013/core"
_NS_CORE_2015 = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_KNOWN_CORE_NS = {_NS_CORE_2013, _NS_CORE_2015}

# 3MF unit scale → millimeters (3MF spec §3.4)
_UNIT_TO_MM: dict[str, float] = {
    "micron": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "inch": 25.4,
    "foot": 304.8,
    "meter": 1000.0,
}

_MAX_XML_BYTES = 80 * 1024 * 1024   # 80 MB decompressed model XML guard
_MAX_TRIANGLES = 600_000


# ------------------------------------------------------------------ #
# Error
# ------------------------------------------------------------------ #


class ParseError(ValueError):
    """Typed error raised when a 3MF cannot be parsed.

    ``message`` is a human-readable string suitable for an API response.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ------------------------------------------------------------------ #
# Output dataclasses
# ------------------------------------------------------------------ #


@dataclass
class FilamentInfo:
    slot: int
    type: str | None = None
    color: str | None = None  # "#RRGGBB" or None


@dataclass
class Mesh3MF:
    """Parsed + merged 3MF mesh in millimeter coordinates."""

    # Flat arrays: vertices xyz (float32) and triangle vertex indices (uint32).
    vertices: array.array[float]  # array('f')
    indices: array.array[int]     # array('I')

    vertex_count: int
    triangle_count: int

    # Bounding box in mm.
    bbox_min: list[float]   # [x, y, z]
    bbox_max: list[float]   # [x, y, z]

    # True when vertex/triangle data is present (full 3MF design file).
    # False when the archive is a Bambu-style sliced .gcode.3mf that carries
    # gcode only — in that case bbox is populated from plate_1.json (2D
    # footprint; z is zero) and vertices/indices are empty.
    geometry_available: bool = True

    # Bambu metadata — all optional.
    layer_height_mm: float | None = None
    filaments: list[FilamentInfo] = field(default_factory=list)


# ------------------------------------------------------------------ #
# Transform helpers
# ------------------------------------------------------------------ #

# A 3MF transform is a 3×4 row-major matrix stored in a space-separated string:
#   m00 m01 m02 m10 m11 m12 m20 m21 m22 m30 m31 m32
# (the homogeneous bottom row is omitted; it is implicitly [0 0 0 1]).
#
# Applied as: P' = P * M + t
# where M = [[m00 m01 m02]
#             [m10 m11 m12]
#             [m20 m21 m22]]
# and   t = [m30 m31 m32]
#
# Row-vector convention: p'_i = Σ_j p_j * M[j,i] + t[i]

_IDENTITY_TRANSFORM = (
    1.0, 0.0, 0.0,
    0.0, 1.0, 0.0,
    0.0, 0.0, 1.0,
    0.0, 0.0, 0.0,
)  # tuple of 12 floats: m00..m32


def _parse_transform(attr: str | None) -> tuple[float, ...] | None:
    """Parse a 3MF ``transform`` attribute string to 12 floats, or None."""
    if not attr:
        return None
    parts = attr.split()
    if len(parts) != 12:
        return None
    try:
        return tuple(float(x) for x in parts)
    except ValueError:
        return None


def _apply_transform(
    x: float,
    y: float,
    z: float,
    t: tuple[float, ...],
) -> tuple[float, float, float]:
    """Apply a 3MF 3×4 row-major transform ``t`` to vertex (x, y, z).

    Row-vector convention: p' = p * M + translation
    t = (m00 m01 m02 m10 m11 m12 m20 m21 m22 m30 m31 m32)
    """
    m00, m01, m02 = t[0], t[1], t[2]
    m10, m11, m12 = t[3], t[4], t[5]
    m20, m21, m22 = t[6], t[7], t[8]
    tx, ty, tz    = t[9], t[10], t[11]
    xp = x * m00 + y * m10 + z * m20 + tx
    yp = x * m01 + y * m11 + z * m21 + ty
    zp = x * m02 + y * m12 + z * m22 + tz
    return xp, yp, zp


def _compose_transforms(
    outer: tuple[float, ...],
    inner: tuple[float, ...],
) -> tuple[float, ...]:
    """Compose two 3×4 row-major transforms: result = apply inner then outer.

    result[p] = outer(inner(p))
    In row-vector form: (p * Mi + ti) * Mo + to = p * (Mi * Mo) + (ti * Mo + to)
    """
    def _m(t: tuple[float, ...]) -> list[list[float]]:
        return [
            [t[0], t[1], t[2]],
            [t[3], t[4], t[5]],
            [t[6], t[7], t[8]],
        ]

    def _tr(t: tuple[float, ...]) -> list[float]:
        return [t[9], t[10], t[11]]

    mi = _m(inner)
    mo = _m(outer)
    ti = _tr(inner)
    to_r = _tr(outer)

    # Combined rotation: mi * mo
    cm = [[0.0, 0.0, 0.0] for _ in range(3)]
    for r in range(3):
        for c in range(3):
            cm[r][c] = sum(mi[r][k] * mo[k][c] for k in range(3))

    # Combined translation: ti * mo + to
    ct = [
        sum(ti[k] * mo[k][c] for k in range(3)) + to_r[c]
        for c in range(3)
    ]

    return (
        cm[0][0], cm[0][1], cm[0][2],
        cm[1][0], cm[1][1], cm[1][2],
        cm[2][0], cm[2][1], cm[2][2],
        ct[0], ct[1], ct[2],
    )


# ------------------------------------------------------------------ #
# Production extension path helpers
# ------------------------------------------------------------------ #


def _path_attr(el: ET.Element) -> str | None:
    """Return the value of any attribute whose XML local-name is ``path``.

    The 3MF Production extension uses namespace
    ``http://schemas.microsoft.com/3dmanufacturing/production/2015/06``
    (conventional prefix ``p:``), but slicers may use any prefix or even
    omit the namespace declaration.  ElementTree stores attributes in Clark
    notation ``{ns}localname``; we match on the local-name portion only so
    ``p:path``, ``production:path``, and bare ``path`` all resolve.
    """
    for k, v in el.attrib.items():
        local = k.rsplit("}", 1)[-1] if "}" in k else k
        if local == "path":
            return v
    return None


def _normalise_zip_path(raw: str, archive_names: set[str]) -> str | None:
    """Resolve a Production-extension path reference to a ZIP member name.

    3MF Production paths start with ``/`` (absolute from the package root).
    ZIP member names do NOT start with ``/``.  Strip the leading slash and
    check the member exists.  Reject paths containing ``..`` (traversal
    guard).  Return the normalised member name, or ``None`` if not found /
    unsafe.
    """
    # Reject traversal attempts.
    if ".." in raw:
        return None
    # Strip leading slash(es).
    name = raw.lstrip("/")
    if not name:
        return None
    if name in archive_names:
        return name
    return None


# ------------------------------------------------------------------ #
# Sub-model loader (Production extension)
# ------------------------------------------------------------------ #

# Type alias: the object table maps object id → (verts, tris).
_ObjTable = dict[str, tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]]


def _ns_from_root(root: ET.Element) -> str:
    """Derive the Clark-notation namespace prefix ``{uri}`` from a model root tag.

    Accepts any tag of the form ``{namespace}model`` (ElementTree Clark
    notation).  If the tag has no namespace (legacy or malformed), returns
    an empty string so bare element names still match.
    """
    tag = root.tag
    if tag.startswith("{"):
        # '{namespace_uri}localname' — extract '{namespace_uri}'
        return tag[: tag.index("}") + 1]
    return ""


def _load_sub_model(
    zf: zipfile.ZipFile,
    member: str,
    scale: float,
    archive_names: set[str],
) -> _ObjTable:
    """Load a referenced sub-model XML from the archive; return its object table.

    Failures (missing member, XML error, size) are swallowed — Production
    references that can't be loaded produce an empty contribution, not a
    ParseError.  This intentionally matches the spec guidance that
    implementations should degrade gracefully on partial archives.
    """
    try:
        info = zf.getinfo(member)
        if info.file_size > _MAX_XML_BYTES:
            return {}
        xml_bytes = zf.read(member)
        sub_root = ET.fromstring(xml_bytes)
    except Exception:  # noqa: BLE001
        return {}

    ns = _ns_from_root(sub_root)
    return _extract_object_meshes(sub_root, scale, ns)


# ------------------------------------------------------------------ #
# Object mesh extraction (shared between root and sub-models)
# ------------------------------------------------------------------ #


def _extract_object_meshes(root: ET.Element, scale: float, ns: str) -> _ObjTable:
    """Build a map of object ``id`` → (vertices_mm, triangles).

    ``ns`` is the Clark-notation namespace prefix string, e.g.
    ``'{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}'``.
    An empty string is accepted for namespace-less (legacy) XML.

    Reads only direct ``<mesh>`` children.  Objects with ``<components>``
    but no ``<mesh>`` are stored with empty geometry; their components are
    resolved at build-item time (so the caller has the full picture of
    both the root and any sub-model the component references).

    Non-model objects (type="support", "solidsupport", …) are skipped.
    """
    resources = root.find(f"{ns}resources")
    if resources is None:
        return {}

    objects: _ObjTable = {}

    for obj in resources.findall(f"{ns}object"):
        obj_id = obj.get("id", "")
        obj_type = obj.get("type", "model")

        # Skip non-model objects (support, solidsupport, etc.)
        if obj_type not in ("model", ""):
            continue

        verts: list[tuple[float, float, float]] = []
        tris: list[tuple[int, int, int]] = []

        mesh_el = obj.find(f"{ns}mesh")
        if mesh_el is not None:
            vertices_el = mesh_el.find(f"{ns}vertices")
            triangles_el = mesh_el.find(f"{ns}triangles")

            if vertices_el is not None:
                for v in vertices_el.findall(f"{ns}vertex"):
                    try:
                        vx = float(v.get("x", "0")) * scale
                        vy = float(v.get("y", "0")) * scale
                        vz = float(v.get("z", "0")) * scale
                    except ValueError:
                        continue
                    verts.append((vx, vy, vz))

            if triangles_el is not None:
                for tri in triangles_el.findall(f"{ns}triangle"):
                    try:
                        v1 = int(tri.get("v1", "0"))
                        v2 = int(tri.get("v2", "0"))
                        v3 = int(tri.get("v3", "0"))
                    except ValueError:
                        continue
                    tris.append((v1, v2, v3))

        objects[obj_id] = (verts, tris)

    return objects


# ------------------------------------------------------------------ #
# Component resolution (supports Production p:path references)
# ------------------------------------------------------------------ #


def _resolve_components(
    obj_el: ET.Element,
    root_objects: _ObjTable,
    zf: zipfile.ZipFile,
    scale: float,
    archive_names: set[str],
    ns: str,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Resolve ``<components>`` for one object element; return merged verts/tris.

    ``ns`` is the Clark-notation namespace prefix string for the root model.

    For each ``<component>``:
    * If it carries a ``p:path`` (or any ``*:path``) attribute, the
      referenced objectid is looked up in that sub-model's file; otherwise
      the root object table is used.
    * The component's own ``transform`` is applied on top.
    * Missing or unloadable sub-models produce no geometry (skipped silently).
    """
    merged_verts: list[tuple[float, float, float]] = []
    merged_tris: list[tuple[int, int, int]] = []

    components_el = obj_el.find(f"{ns}components")
    if components_el is None:
        return merged_verts, merged_tris

    for comp in components_el.findall(f"{ns}component"):
        ref_id = comp.get("objectid", "")
        comp_transform = _parse_transform(comp.get("transform"))

        # Determine which object table to look up ref_id in.
        p_path = _path_attr(comp)
        if p_path is not None:
            member = _normalise_zip_path(p_path, archive_names)
            if member is None:
                continue  # path unsafe or not found — skip silently
            sub_objects = _load_sub_model(zf, member, scale, archive_names)
            obj_table = sub_objects
        else:
            obj_table = root_objects

        if ref_id not in obj_table:
            continue
        ref_verts, ref_tris = obj_table[ref_id]

        base_idx = len(merged_verts)

        for vx, vy, vz in ref_verts:
            if comp_transform is not None:
                vx, vy, vz = _apply_transform(vx, vy, vz, comp_transform)
            merged_verts.append((vx, vy, vz))

        for t1, t2, t3 in ref_tris:
            merged_tris.append((base_idx + t1, base_idx + t2, base_idx + t3))

    return merged_verts, merged_tris


# ------------------------------------------------------------------ #
# Bambu metadata
# ------------------------------------------------------------------ #


def _parse_color(raw: str) -> str | None:
    """Normalise a Bambu colour string to ``#RRGGBB``.

    Bambu uses RRGGBBAA (8 hex digits) in slice_info; strip the alpha.
    Also accept bare 6-digit hex. Return None if unrecognised.
    """
    raw = raw.strip().lstrip("#")
    if len(raw) == 8:
        return "#" + raw[:6].upper()
    if len(raw) == 6:
        return "#" + raw.upper()
    return None


def _extract_plate_bbox(
    zf: zipfile.ZipFile,
) -> tuple[list[float], list[float]] | None:
    """Try to extract a 2D build-plate footprint from ``Metadata/plate_1.json``.

    Bambu Studio sliced ``.gcode.3mf`` archives include a ``plate_1.json``
    file whose ``bbox_all`` key holds ``[x_min, y_min, x_max, y_max]`` in
    millimeters.  This is the only geometric information available in such
    archives (the model XML carries no mesh vertices).

    Returns ``(bbox_min, bbox_max)`` where both are ``[x, y, z]`` with
    ``z = 0.0``, or ``None`` if the file is absent or malformed.
    """
    names = set(zf.namelist())
    if "Metadata/plate_1.json" not in names:
        return None
    try:
        raw = zf.read("Metadata/plate_1.json")
        data: Any = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(data, dict):
            return None
        bbox_all = data.get("bbox_all")
        if not isinstance(bbox_all, list) or len(bbox_all) < 4:
            return None
        x_min = float(bbox_all[0])
        y_min = float(bbox_all[1])
        x_max = float(bbox_all[2])
        y_max = float(bbox_all[3])
        return [x_min, y_min, 0.0], [x_max, y_max, 0.0]
    except Exception:  # noqa: BLE001
        return None


def _extract_bambu_metadata(
    zf: zipfile.ZipFile,
) -> tuple[float | None, list[FilamentInfo]]:
    """Try to extract layer_height and filament info from Bambu metadata members.

    Tries ``Metadata/slice_info.config`` (XML) first, then falls back to
    ``Metadata/project_settings.config`` (JSON). Both are optional; failures
    are suppressed and the function returns (None, []) defensively.
    """
    layer_height: float | None = None
    filaments: list[FilamentInfo] = []

    names = set(zf.namelist())

    # ---- slice_info.config (XML) ---------------------------------------- #
    if "Metadata/slice_info.config" in names:
        try:
            raw_xml = zf.read("Metadata/slice_info.config")
            root = ET.fromstring(raw_xml.decode("utf-8", errors="replace"))

            # Extract layer height from plate metadata.
            for meta in root.iter("metadata"):
                k = meta.get("key", "")
                v = meta.get("value", "")
                if k == "layer_height":
                    with contextlib.suppress(ValueError):
                        layer_height = float(v)

            # Extract filament elements: <filament id="..." type="..." color="..." .../>
            for i, fil_el in enumerate(root.iter("filament")):
                fil_type = fil_el.get("type") or None
                color_raw = fil_el.get("color", "")
                color = _parse_color(color_raw) if color_raw else None
                # id attr is 1-based filament index
                try:
                    slot = int(fil_el.get("id", str(i)))
                except ValueError:
                    slot = i
                filaments.append(FilamentInfo(slot=slot, type=fil_type, color=color))
        except Exception:  # noqa: BLE001 — metadata absent/malformed is fine
            pass

    # ---- project_settings.config (JSON) — fallback or supplement --------- #
    if "Metadata/project_settings.config" in names and not filaments:
        try:
            raw_json = zf.read("Metadata/project_settings.config")
            cfg: Any = json.loads(raw_json.decode("utf-8", errors="replace"))

            if isinstance(cfg, dict):
                # layer_height key
                if layer_height is None:
                    lh_val = cfg.get("layer_height")
                    if lh_val is not None:
                        with contextlib.suppress(ValueError, TypeError):
                            layer_height = float(lh_val)

                # filament_colour: list or semicolon-separated string
                colors_raw: list[str] = []
                fc = cfg.get("filament_colour")
                if isinstance(fc, list):
                    colors_raw = [str(c) for c in fc]
                elif isinstance(fc, str):
                    colors_raw = [c.strip() for c in fc.split(";") if c.strip()]

                # filament_type: same shape
                types_raw: list[str] = []
                ft = cfg.get("filament_type")
                if isinstance(ft, list):
                    types_raw = [str(t) for t in ft]
                elif isinstance(ft, str):
                    types_raw = [t.strip() for t in ft.split(";") if t.strip()]

                for i, color_raw in enumerate(colors_raw):
                    color = _parse_color(color_raw)
                    fil_type = types_raw[i] if i < len(types_raw) else None
                    filaments.append(FilamentInfo(slot=i + 1, type=fil_type, color=color))

        except Exception:  # noqa: BLE001 — metadata absent/malformed is fine
            pass

    return layer_height, filaments


# ------------------------------------------------------------------ #
# Public entry point
# ------------------------------------------------------------------ #


def parse_3mf(data: bytes) -> Mesh3MF:
    """Parse a 3MF file from raw bytes; return a merged :class:`Mesh3MF`.

    Raises :class:`ParseError` on:
    * Not a valid ZIP (including empty input).
    * Missing ``3D/3dmodel.model`` member.
    * Decompressed model XML > 80 MB.
    * Merged triangle count > 600 000.
    * Malformed XML in the root model.

    Non-mesh objects and unresolvable Production-extension references are
    silently skipped — partial geometry is always preferred to failure.
    """
    # ---- Open as ZIP ---------------------------------------------------- #
    try:
        zf = zipfile.ZipFile(  # noqa: SIM115 — explicit close below
            __import__("io").BytesIO(data)
        )
    except zipfile.BadZipFile as exc:
        raise ParseError(f"Not a valid 3MF file (bad ZIP): {exc}") from exc

    with zf:
        archive_names: set[str] = set(zf.namelist())

        # ---- Locate root model entry ------------------------------------- #
        model_name: str | None = None
        for candidate in ("3D/3dmodel.model", "3d/3dmodel.model"):
            if candidate in archive_names:
                model_name = candidate
                break
        if model_name is None:
            # Case-insensitive fallback
            lower_map = {n.lower(): n for n in archive_names}
            model_name = lower_map.get("3d/3dmodel.model")
        if model_name is None:
            raise ParseError(
                "3MF is missing 3D/3dmodel.model — not a valid 3MF or unsupported layout."
            )

        # ---- Size guard (decompressed) ----------------------------------- #
        info = zf.getinfo(model_name)
        if info.file_size > _MAX_XML_BYTES:
            raise ParseError(
                f"3D model XML is {info.file_size // (1024 * 1024)} MB "
                f"(limit is {_MAX_XML_BYTES // (1024 * 1024)} MB)."
            )

        # ---- Read and parse root model XML ------------------------------ #
        try:
            xml_bytes = zf.read(model_name)
        except Exception as exc:
            raise ParseError(f"Failed to read 3D/3dmodel.model: {exc}") from exc

        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            raise ParseError(f"3D/3dmodel.model contains malformed XML: {exc}") from exc

        # ---- Namespace auto-detection ----------------------------------- #
        # Derive the Clark-notation prefix from the root element tag so the
        # parser handles both the 2013 draft namespace and the 2015 release
        # namespace used by Bambu Studio (and any other future variant).
        ns = _ns_from_root(root)

        # ---- Unit scale ------------------------------------------------- #
        unit = root.get("unit", "millimeter")
        scale = _UNIT_TO_MM.get(unit, 1.0)

        # ---- Parse root object meshes ------------------------------------ #
        root_objects = _extract_object_meshes(root, scale, ns)

        # Keep a map from id → Element for component resolution.
        resources_el = root.find(f"{ns}resources")
        obj_el_map: dict[str, ET.Element] = {}
        if resources_el is not None:
            for obj_el in resources_el.findall(f"{ns}object"):
                oid = obj_el.get("id", "")
                obj_el_map[oid] = obj_el

        # ---- Process build items ---------------------------------------- #
        merged_verts: list[tuple[float, float, float]] = []
        merged_tris: list[tuple[int, int, int]] = []

        build_el = root.find(f"{ns}build")
        if build_el is not None:
            for item in build_el.findall(f"{ns}item"):
                item_transform = _parse_transform(item.get("transform"))

                # Production extension: <item p:path="…" objectid="…">
                # The objectid refers to an object in the referenced file,
                # not in the root model.
                item_path = _path_attr(item)
                if item_path is not None:
                    member = _normalise_zip_path(item_path, archive_names)
                    if member is None:
                        continue  # unsafe or missing — skip silently
                    item_objects = _load_sub_model(zf, member, scale, archive_names)
                    obj_id = item.get("objectid", "")
                    if obj_id not in item_objects:
                        continue
                    item_verts, item_tris = item_objects[obj_id]
                    _emit_verts_tris(
                        item_verts, item_tris, item_transform,
                        merged_verts, merged_tris,
                    )
                    continue

                # Standard path: objectid refers to root model.
                obj_id = item.get("objectid", "")
                if obj_id not in root_objects:
                    continue

                obj_verts, obj_tris = root_objects[obj_id]

                # If the root object has no direct mesh, check for components
                # (including Production p:path references inside them).
                if not obj_verts and obj_id in obj_el_map:
                    obj_verts, obj_tris = _resolve_components(
                        obj_el_map[obj_id],
                        root_objects,
                        zf,
                        scale,
                        archive_names,
                        ns,
                    )

                _emit_verts_tris(
                    obj_verts, obj_tris, item_transform,
                    merged_verts, merged_tris,
                )

        # If build element is absent (malformed but permitted), include all
        # root objects directly — last-resort fallback.
        if build_el is None or not merged_verts:
            for obj_verts, obj_tris in root_objects.values():
                _emit_verts_tris(
                    obj_verts, obj_tris, None,
                    merged_verts, merged_tris,
                )

        # ---- Triangle count guard --------------------------------------- #
        triangle_count = len(merged_tris)
        if triangle_count > _MAX_TRIANGLES:
            raise ParseError(
                f"Mesh has {triangle_count:,} triangles (limit is "
                f"{_MAX_TRIANGLES:,}). Simplify the model before uploading."
            )

        vertex_count = len(merged_verts)

        # ---- Build flat arrays ----------------------------------------- #
        vertices_arr: array.array[float] = array.array("f")
        for vx, vy, vz in merged_verts:
            vertices_arr.append(vx)
            vertices_arr.append(vy)
            vertices_arr.append(vz)

        indices_arr: array.array[int] = array.array("I")
        for t1, t2, t3 in merged_tris:
            indices_arr.append(t1)
            indices_arr.append(t2)
            indices_arr.append(t3)

        # ---- Bounding box ---------------------------------------------- #
        geometry_available: bool
        if merged_verts:
            xs = [v[0] for v in merged_verts]
            ys = [v[1] for v in merged_verts]
            zs = [v[2] for v in merged_verts]
            bbox_min: list[float] = [min(xs), min(ys), min(zs)]
            bbox_max: list[float] = [max(xs), max(ys), max(zs)]
            geometry_available = True
        else:
            # No mesh geometry — typical for Bambu .gcode.3mf sliced output.
            # Fall back to plate_1.json 2D build-plate footprint if available.
            plate_bbox = _extract_plate_bbox(zf)
            if plate_bbox is not None:
                bbox_min, bbox_max = plate_bbox
            else:
                bbox_min = [0.0, 0.0, 0.0]
                bbox_max = [0.0, 0.0, 0.0]
            geometry_available = False

        # ---- Bambu metadata -------------------------------------------- #
        layer_height, filaments = _extract_bambu_metadata(zf)

    return Mesh3MF(
        vertices=vertices_arr,
        indices=indices_arr,
        vertex_count=vertex_count,
        triangle_count=triangle_count,
        bbox_min=bbox_min,
        bbox_max=bbox_max,
        geometry_available=geometry_available,
        layer_height_mm=layer_height,
        filaments=filaments,
    )


# ------------------------------------------------------------------ #
# Internal helper — emit vertices + triangles into accumulator lists
# ------------------------------------------------------------------ #


def _emit_verts_tris(
    src_verts: list[tuple[float, float, float]],
    src_tris: list[tuple[int, int, int]],
    transform: tuple[float, ...] | None,
    merged_verts: list[tuple[float, float, float]],
    merged_tris: list[tuple[int, int, int]],
) -> None:
    """Append ``src_verts`` (with optional transform) and re-indexed
    ``src_tris`` into the running accumulator lists."""
    base_idx = len(merged_verts)
    for vx, vy, vz in src_verts:
        if transform is not None:
            vx, vy, vz = _apply_transform(vx, vy, vz, transform)
        merged_verts.append((vx, vy, vz))
    for t1, t2, t3 in src_tris:
        merged_tris.append((base_idx + t1, base_idx + t2, base_idx + t3))
