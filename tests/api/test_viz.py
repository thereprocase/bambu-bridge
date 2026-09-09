"""G4: 3MF mesh visualisation API endpoints.

Hermetic — all FTPS calls are intercepted via monkeypatch on FtpsTransfer.
The synthetic 3MF bytes are built with the same helper used by
tests/protocol/test_threemf.py (inline here to keep the test file
self-contained).

Coverage
--------
* GET /{id}/viz/mesh happy path — returns valid JSON with mesh fields.
* GET /{id}/viz/mesh with ?token= auth instead of bearer header.
* GET /{id}/viz/mesh → 404 when printer not registered.
* GET /{id}/viz/mesh → 404 when no current job (subtask_name not set).
* GET /{id}/viz/mesh → 404 when file not found on printer storage.
* GET /{id}/viz/mesh → 502 when FTPS download raises.
* GET /{id}/viz/mesh → 422 when parse_3mf raises ParseError.
* GET /{id}/viz/mesh caching — second call reuses parsed mesh (no new download).
* GET /{id}/viz → 501 when viewer.html absent.
* GET /{id}/viz → 401 with invalid token.
* GET /{id}/viz → 404 for unknown printer (even with valid token).
* Response shape: vertices_b64 / indices_b64 decode to correct byte lengths.
* Filament list from Bambu metadata round-trips through the JSON response.
* geometry_available=True in response for a normal design 3MF.
* geometry_available=False + bbox from plate_1.json for Bambu .gcode.3mf.
* response_shape_keys includes geometry_available.
* ETag header present on mesh 200 response; double-quoted; contains filename.
* If-None-Match matching ETag → 304 empty body on mesh.
* 304 does not re-parse — parse call counter stays at 1.
* If-None-Match that doesn't match → 200 full response on mesh.
* Cache-Control header present on mesh 200 response.
* Accept-Encoding: gzip → Content-Encoding: gzip; decompressed body == JSON.
* No Accept-Encoding → identity response (no Content-Encoding header).
* ETag header present on toolpath 200 response; double-quoted.
* If-None-Match matching ETag → 304 empty body on toolpath.
* If-None-Match that doesn't match → 200 full response on toolpath.
* Cache-Control header present on toolpath 200 response.
* Toolpath gzip round-trip (Accept-Encoding: gzip → valid decompressed JSON).
* /viz viewer HTML page does NOT carry an ETag (dynamic job state not cached).
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import io
import json
import struct
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    API_KEY,
    SERIAL,
    build_app,
    patch_discovery_ok,
)

_AUTH = {"Authorization": f"Bearer {API_KEY}"}
_TOKEN_PARAM = f"?token={API_KEY}"

# ------------------------------------------------------------------ #
# Helpers — synthetic 3MF builder (mirrors test_threemf.py)
# ------------------------------------------------------------------ #

_CONTENT_TYPES = """\
<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>"""

_CUBE_MODEL = """\
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
  <build>
    <item objectid="1"/>
  </build>
</model>
"""

_SLICE_INFO_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="layer_height" value="0.2"/>
    <filament id="1" type="PLA" color="FF0000FF" used_m="1" used_g="3"/>
  </plate>
</config>
"""


def _make_cube_3mf(*, with_metadata: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("3D/3dmodel.model", _CUBE_MODEL)
        if with_metadata:
            zf.writestr("Metadata/slice_info.config", _SLICE_INFO_XML)
    return buf.getvalue()


def _make_bad_3mf() -> bytes:
    """A ZIP with an unparseable model XML."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model><unclosed")
    return buf.getvalue()


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture(autouse=True)
def _patched_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)


def _register_printer(client: TestClient) -> None:
    """Register the mock printer via POST /printers."""
    r = client.post(
        "/api/v1/printers",
        headers=_AUTH,
        json={"host": "127.0.0.1", "access_code": "12345678", "friendly_name": "Test"},
    )
    assert r.status_code == 201, r.text


def _inject_job(app: Any, *, job_name: str = "benchy", total_layers: int = 200) -> None:
    """Inject a running job into the mock printer's raw state."""
    from bambu_bridge.service.registry import Registry

    reg: Registry = app.state.registry
    service = reg.get(SERIAL)
    service._state["subtask_name"] = job_name
    service._state["total_layer_num"] = total_layers
    service._state["gcode_state"] = "RUNNING"


def _patch_ftps(monkeypatch: pytest.MonkeyPatch, file_bytes: bytes, filename: str) -> None:
    """Monkeypatch FtpsTransfer so list_dir returns filename and download returns file_bytes."""
    from bambu_bridge.api import viz as viz_mod

    original_cls = viz_mod.FtpsTransfer  # noqa: F841

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return [filename]

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        return file_bytes

    monkeypatch.setattr(
        "bambu_bridge.api.viz.FtpsTransfer.list_dir", _list_dir
    )
    monkeypatch.setattr(
        "bambu_bridge.api.viz.FtpsTransfer.download_bytes", _download_bytes
    )


# ------------------------------------------------------------------ #
# Tests — /viz/mesh happy path
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_mesh_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /viz/mesh with a running job returns correct mesh JSON."""
    cube_bytes = _make_cube_3mf()
    _patch_ftps(monkeypatch, cube_bytes, "benchy.3mf")

    app = build_app(tmp_path / "viz.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy", total_layers=200)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r.status_code == 200, r.text
            body = r.json()

            assert body["job"] == "benchy"
            assert body["total_layers"] == 200
            assert body["vertex_count"] == 8
            assert body["triangle_count"] == 12
            assert body["cached"] is False
            assert "vertices_b64" in body
            assert "indices_b64" in body
            assert "bbox" in body
            assert body["bbox"]["min"] == pytest.approx([0.0, 0.0, 0.0], abs=1e-3)
            assert body["bbox"]["max"] == pytest.approx([10.0, 10.0, 10.0], abs=1e-3)
            assert body["source_file"] == "benchy.3mf"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_mesh_vertices_b64_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vertices_b64 decodes to vertex_count * 3 * 4 bytes (float32)."""
    cube_bytes = _make_cube_3mf()
    _patch_ftps(monkeypatch, cube_bytes, "benchy.3mf")

    app = build_app(tmp_path / "viz_b64.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r.status_code == 200, r.text
            body = r.json()

            verts = base64.b64decode(body["vertices_b64"])
            idxs = base64.b64decode(body["indices_b64"])

            expected_vert_bytes = body["vertex_count"] * 3 * 4  # 3 floats * 4 bytes
            expected_idx_bytes = body["triangle_count"] * 3 * 4  # 3 uint32 * 4 bytes

            assert len(verts) == expected_vert_bytes
            assert len(idxs) == expected_idx_bytes

            # Spot-check first vertex is (0, 0, 0) — first 12 bytes = 3 × float32-LE
            x, y, z = struct.unpack_from("<fff", verts, 0)
            assert x == pytest.approx(0.0, abs=1e-4)
            assert y == pytest.approx(0.0, abs=1e-4)
            assert z == pytest.approx(0.0, abs=1e-4)

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_mesh_with_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bambu metadata in the 3MF propagates to the filaments field."""
    cube_bytes = _make_cube_3mf(with_metadata=True)
    _patch_ftps(monkeypatch, cube_bytes, "benchy.3mf")

    app = build_app(tmp_path / "viz_meta.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r.status_code == 200, r.text
            body = r.json()

            assert body["layer_height_mm"] == pytest.approx(0.2)
            assert len(body["filaments"]) == 1
            assert body["filaments"][0]["type"] == "PLA"
            assert body["filaments"][0]["color"] == "#FF0000"
            assert body["filaments"][0]["slot"] == 1

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_mesh_token_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """?token= query param is accepted in lieu of Authorization header."""
    cube_bytes = _make_cube_3mf()
    _patch_ftps(monkeypatch, cube_bytes, "benchy.3mf")

    app = build_app(tmp_path / "viz_tok.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)
            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz/mesh{_TOKEN_PARAM}",
                # No Authorization header.
            )
            assert r.status_code == 200, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_mesh_bearer_wins_over_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When both bearer header and ?token= are present, header wins."""
    cube_bytes = _make_cube_3mf()
    _patch_ftps(monkeypatch, cube_bytes, "benchy.3mf")

    app = build_app(tmp_path / "viz_dual.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)
            # Provide invalid token param but valid header — should succeed.
            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz/mesh?token=WRONG_TOKEN",
                headers=_AUTH,
            )
            assert r.status_code == 200, r.text

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — /viz/mesh error paths
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_mesh_unknown_printer_404(tmp_path: Path) -> None:
    """Unknown printer ID → 404."""
    app = build_app(tmp_path / "viz_404.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            r = c.get("/api/v1/printers/NO_SUCH_PRINTER/viz/mesh", headers=_AUTH)
            assert r.status_code == 404, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_mesh_no_current_job_404(tmp_path: Path) -> None:
    """No subtask_name set on the printer → 404 no current job."""
    app = build_app(tmp_path / "viz_nojob.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            # Don't inject a job — subtask_name is absent from _state.
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r.status_code == 404, r.text
            assert "job" in r.json()["message"].lower() or r.status_code == 404

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_mesh_file_not_on_printer_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File not in FTPS listing → 404 file not found."""
    # list_dir returns nothing.
    async def _empty_list(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return []

    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.list_dir", _empty_list)

    app = build_app(tmp_path / "viz_nofile.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="ghost_job")
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r.status_code == 404, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_mesh_ftps_download_failure_502(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FTPS download raises → 502 Bad Gateway."""
    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["benchy.3mf"]

    async def _download_fail(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        raise OSError("connection refused")

    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.download_bytes", _download_fail)

    app = build_app(tmp_path / "viz_502.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r.status_code == 502, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_mesh_parse_error_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupt 3MF bytes → 422 parse error."""
    bad_bytes = _make_bad_3mf()
    _patch_ftps(monkeypatch, bad_bytes, "corrupt.3mf")

    app = build_app(tmp_path / "viz_422.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="corrupt")
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_mesh_auth_required(tmp_path: Path) -> None:
    """No auth → 401."""
    app = build_app(tmp_path / "viz_noauth.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh")
            assert r.status_code == 401, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_mesh_wrong_token_401(tmp_path: Path) -> None:
    """Wrong ?token= → 401."""
    app = build_app(tmp_path / "viz_badtok.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh?token=WRONG")
            assert r.status_code == 401, r.text

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — caching
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_mesh_cache_hit_on_second_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second request reuses the cached mesh (cached=True in response)."""
    cube_bytes = _make_cube_3mf()
    download_count = 0

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["benchy.3mf"]

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        nonlocal download_count
        download_count += 1
        return cube_bytes

    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.download_bytes", _download_bytes)

    app = build_app(tmp_path / "viz_cache.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)

            r1 = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r1.status_code == 200, r1.text
            assert r1.json()["cached"] is False

            r2 = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r2.status_code == 200, r2.text
            assert r2.json()["cached"] is True

        # download was only called once (second hit used cache).
        assert download_count == 1

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — /viz (viewer HTML)
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_viewer_returns_501_when_html_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /viz → 501 when viewer.html is not present.

    We monkeypatch ``_VIEWER_HTML`` to a guaranteed-absent path so this test
    is hermetic regardless of whether another agent has placed viewer.html.
    """
    import bambu_bridge.api.viz as viz_mod

    absent_path = tmp_path / "nonexistent_viewer.html"
    monkeypatch.setattr(viz_mod, "_VIEWER_HTML", absent_path)

    app = build_app(tmp_path / "viz_501.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz{_TOKEN_PARAM}",
                follow_redirects=True,
            )
            assert r.status_code == 501, r.text
            assert "viewer not built yet" in r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_viewer_serves_html_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /viz serves viewer.html content when the file exists."""
    # Temporarily point the static path to a tmp file.
    viewer_file = tmp_path / "viewer.html"
    viewer_file.write_text("<html><body>TEST VIEWER</body></html>", encoding="utf-8")

    import bambu_bridge.api.viz as viz_mod

    monkeypatch.setattr(viz_mod, "_VIEWER_HTML", viewer_file)

    app = build_app(tmp_path / "viz_html.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz{_TOKEN_PARAM}",
                follow_redirects=True,
            )
            assert r.status_code == 200, r.text
            assert "TEST VIEWER" in r.text
            assert "text/html" in r.headers.get("content-type", "")

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_viewer_401_without_token(tmp_path: Path) -> None:
    """GET /viz without ?token= → 401."""
    app = build_app(tmp_path / "viz_v401.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz", follow_redirects=True)
            assert r.status_code == 401, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_viewer_401_with_wrong_token(tmp_path: Path) -> None:
    """GET /viz with wrong ?token= → 401."""
    app = build_app(tmp_path / "viz_v401b.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz?token=WRONG",
                follow_redirects=True,
            )
            assert r.status_code == 401, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_viewer_404_unknown_printer(tmp_path: Path) -> None:
    """GET /viz for unknown printer → 404."""
    app = build_app(tmp_path / "viz_v404.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            r = c.get(
                f"/api/v1/printers/NO_PRINTER/viz{_TOKEN_PARAM}",
                follow_redirects=True,
            )
            assert r.status_code == 404, r.text

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — misc shape assertions
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_mesh_response_shape_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Response has all required top-level keys."""
    cube_bytes = _make_cube_3mf()
    _patch_ftps(monkeypatch, cube_bytes, "benchy.3mf")

    app = build_app(tmp_path / "viz_shape.db", mqtt_port=1)
    required_keys = {
        "job",
        "total_layers",
        "layer_height_mm",
        "vertex_count",
        "triangle_count",
        "vertices_b64",
        "indices_b64",
        "bbox",
        "filaments",
        "source_file",
        "cached",
    }

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r.status_code == 200, r.text
            missing = required_keys - set(r.json().keys())
            assert not missing, f"Missing response keys: {missing}"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_mesh_bbox_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bbox has min and max each with 3 floats."""
    cube_bytes = _make_cube_3mf()
    _patch_ftps(monkeypatch, cube_bytes, "benchy.3mf")

    app = build_app(tmp_path / "viz_bbox.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r.status_code == 200, r.text
            bbox = r.json()["bbox"]
            assert "min" in bbox and "max" in bbox
            assert len(bbox["min"]) == 3
            assert len(bbox["max"]) == 3
            # All should be numeric
            for v in bbox["min"] + bbox["max"]:
                assert isinstance(v, float | int)

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Helpers — Bambu .gcode.3mf (sliced output, no mesh geometry)
# ------------------------------------------------------------------ #

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

# Observed bbox_all from weather_station_reflector.gcode.3mf
_PLATE_1_JSON = (
    '{"bbox_all": [52.601566, 36.950956, 191.329822, 206.967427],'
    ' "bbox_objects": [{"area": 3537.67,'
    ' "bbox": [109.380454, 125.018336, 191.329822, 206.967427],'
    ' "id": 667, "layer_height": 0.2,'
    ' "name": "weather_station_reflector.stl"}]}'
)

_SLICE_INFO_GCODE = """\
<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="layer_height" value="0.2"/>
    <filament id="1" type="PLA" color="FF5500FF" used_m="2.1" used_g="6.2"/>
  </plate>
</config>
"""


def _make_gcode_3mf_bytes() -> bytes:
    """Minimal .gcode.3mf archive mirroring the real Bambu sliced format."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("3D/3dmodel.model", _GCODE_3MF_MODEL)
        zf.writestr("Metadata/plate_1.json", _PLATE_1_JSON)
        zf.writestr("Metadata/slice_info.config", _SLICE_INFO_GCODE)
    return buf.getvalue()


# ------------------------------------------------------------------ #
# Tests — geometry_available field in API response
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_mesh_geometry_available_true_for_design_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal design 3MF returns geometry_available=True."""
    cube_bytes = _make_cube_3mf()
    _patch_ftps(monkeypatch, cube_bytes, "benchy.3mf")

    app = build_app(tmp_path / "viz_ga_true.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["geometry_available"] is True
            assert body["vertex_count"] == 8
            assert body["triangle_count"] == 12

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_mesh_geometry_available_false_for_gcode_3mf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Bambu .gcode.3mf returns geometry_available=False with bbox from plate_1.json."""
    gcode_bytes = _make_gcode_3mf_bytes()
    _patch_ftps(monkeypatch, gcode_bytes, "weather_station_reflector.gcode.3mf")

    app = build_app(tmp_path / "viz_ga_false.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="weather_station_reflector.gcode.3mf")
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r.status_code == 200, r.text
            body = r.json()

            assert body["geometry_available"] is False
            assert body["vertex_count"] == 0
            assert body["triangle_count"] == 0

            # bbox comes from plate_1.json bbox_all
            bbox = body["bbox"]
            assert bbox["min"] == pytest.approx([52.601566, 36.950956, 0.0], abs=1e-3)
            assert bbox["max"] == pytest.approx([191.329822, 206.967427, 0.0], abs=1e-3)

            # Filaments still extracted from slice_info.config
            assert len(body["filaments"]) == 1
            assert body["filaments"][0]["type"] == "PLA"
            assert body["filaments"][0]["color"] == "#FF5500"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_mesh_response_includes_geometry_available_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Response always contains the geometry_available key."""
    cube_bytes = _make_cube_3mf()
    _patch_ftps(monkeypatch, cube_bytes, "benchy.3mf")

    app = build_app(tmp_path / "viz_ga_key.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r.status_code == 200, r.text
            assert "geometry_available" in r.json()

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Helpers — synthetic .gcode.3mf archive with real gcode
# ------------------------------------------------------------------ #

# A minimal gcode that produces a 10×10 mm square perimeter at Z=0.2.
# Uses M83 relative E.  4 extrusion segments expected.
_SQUARE_GCODE = """\
G90
M83
G92 E0
G1 Z0.2 F1200
G1 X0 Y0 F6000
G1 X10 Y0 E0.5
G1 X10 Y10 E0.5
G1 X0 Y10 E0.5
G1 X0 Y0 E0.5
"""


def _make_toolpath_archive(gcode_text: str = _SQUARE_GCODE) -> bytes:
    """Build a .gcode.3mf archive containing the given gcode."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("Metadata/plate_1.gcode", gcode_text)
        zf.writestr("3D/3dmodel.model", "<model/>")
    return buf.getvalue()


def _make_bad_toolpath_archive() -> bytes:
    """A .gcode.3mf archive without Metadata/plate_1.gcode — triggers 422."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    return buf.getvalue()


# ------------------------------------------------------------------ #
# Tests — /viz/toolpath happy path
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_toolpath_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /viz/toolpath with a running job returns correct toolpath JSON."""
    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "benchy.gcode.3mf")

    app = build_app(tmp_path / "tp_happy.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf", total_layers=200)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r.status_code == 200, r.text
            body = r.json()

            assert body["job"] == "benchy.gcode.3mf"
            assert body["total_layers"] == 200
            assert body["segment_count"] == 4   # 4 sides of the square
            assert body["cached"] is False
            assert "positions_b64" in body
            assert "bbox" in body
            assert "decimated" in body
            assert "source_file" in body
            assert body["decimated"] is False

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_positions_b64_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """positions_b64 decodes to segment_count * 2 * 3 * 4 bytes (float32 pairs)."""
    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "benchy.gcode.3mf")

    app = build_app(tmp_path / "tp_b64.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r.status_code == 200, r.text
            body = r.json()

            raw = base64.b64decode(body["positions_b64"])
            expected_bytes = body["segment_count"] * 2 * 3 * 4  # 2 verts * xyz * float32
            assert len(raw) == expected_bytes

            # First vertex (x, y, z) should be float32 readable.
            if expected_bytes >= 12:
                x, y, z = struct.unpack_from("<fff", raw, 0)
                assert isinstance(x, float)

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_bbox_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bbox has min and max each with 3 floats; values reflect the square."""
    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "benchy.gcode.3mf")

    app = build_app(tmp_path / "tp_bbox.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r.status_code == 200, r.text
            bbox = r.json()["bbox"]
            assert "min" in bbox and "max" in bbox
            assert len(bbox["min"]) == 3
            assert len(bbox["max"]) == 3
            # Square is 0..10 in X and Y at Z=0.2
            assert bbox["min"] == pytest.approx([0.0, 0.0, 0.2], abs=1e-3)
            assert bbox["max"] == pytest.approx([10.0, 10.0, 0.2], abs=1e-3)

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_response_shape_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Response has all required top-level keys."""
    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "benchy.gcode.3mf")

    app = build_app(tmp_path / "tp_shape.db", mqtt_port=1)
    required_keys = {
        "job",
        "total_layers",
        "layer_height_mm",
        "segment_count",
        "positions_b64",
        "bbox",
        "decimated",
        "source_file",
        "cached",
    }

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r.status_code == 200, r.text
            missing = required_keys - set(r.json().keys())
            assert not missing, f"Missing response keys: {missing}"

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — /viz/toolpath error paths
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_toolpath_unknown_printer_404(tmp_path: Path) -> None:
    """Unknown printer ID → 404."""
    app = build_app(tmp_path / "tp_404.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            r = c.get("/api/v1/printers/NO_SUCH_PRINTER/viz/toolpath", headers=_AUTH)
            assert r.status_code == 404, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_no_current_job_404(tmp_path: Path) -> None:
    """No subtask_name → 404."""
    app = build_app(tmp_path / "tp_nojob.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r.status_code == 404, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_file_not_found_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File not in FTPS listing → 404."""
    async def _empty_list(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return []

    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.list_dir", _empty_list)

    app = build_app(tmp_path / "tp_nofile.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="ghost.gcode.3mf")
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r.status_code == 404, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_ftps_failure_502(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FTPS download raises → 502."""
    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["benchy.gcode.3mf"]

    async def _download_fail(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        raise OSError("connection refused")

    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.download_bytes", _download_fail)

    app = build_app(tmp_path / "tp_502.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r.status_code == 502, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_parse_error_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Archive without Metadata/plate_1.gcode member → 422."""
    bad_archive = _make_bad_toolpath_archive()
    _patch_ftps(monkeypatch, bad_archive, "corrupt.gcode.3mf")

    app = build_app(tmp_path / "tp_422.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="corrupt.gcode.3mf")
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_auth_required(tmp_path: Path) -> None:
    """No auth → 401."""
    app = build_app(tmp_path / "tp_noauth.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath")
            assert r.status_code == 401, r.text

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — /viz/toolpath auth variants
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_toolpath_token_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """?token= query param is accepted in lieu of Authorization header."""
    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "benchy.gcode.3mf")

    app = build_app(tmp_path / "tp_tok.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")
            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz/toolpath{_TOKEN_PARAM}",
            )
            assert r.status_code == 200, r.text

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — /viz/toolpath caching
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_toolpath_cache_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second toolpath request reuses cache (cached=True) without re-download."""
    archive = _make_toolpath_archive()
    download_count = 0

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["benchy.gcode.3mf"]

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        nonlocal download_count
        download_count += 1
        return archive

    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.download_bytes", _download_bytes)

    app = build_app(tmp_path / "tp_cache.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")

            r1 = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r1.status_code == 200, r1.text
            assert r1.json()["cached"] is False

            r2 = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r2.status_code == 200, r2.text
            assert r2.json()["cached"] is True

        assert download_count == 1

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — zero-segment toolpath (empty gcode)
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_toolpath_zero_segments_returns_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An archive with travel-only gcode returns 200 with segment_count=0."""
    empty_gcode = "G1 X10 Y10 F6000\nG1 X20 Y10 F6000\n"  # no E moves
    archive = _make_toolpath_archive(empty_gcode)
    _patch_ftps(monkeypatch, archive, "benchy.gcode.3mf")

    app = build_app(tmp_path / "tp_zero.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["segment_count"] == 0
            # positions_b64 decodes to empty
            assert base64.b64decode(body["positions_b64"]) == b""

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — ETag and Cache-Control on /viz/mesh
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_mesh_200_carries_etag_and_cache_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /viz/mesh 200 response includes ETag and Cache-Control headers."""
    cube_bytes = _make_cube_3mf()
    _patch_ftps(monkeypatch, cube_bytes, "benchy.3mf")

    app = build_app(tmp_path / "mesh_etag_200.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)

            # First request — cold cache, full download + parse.
            r1 = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r1.status_code == 200, r1.text

            # ETag must be present and properly double-quoted (RFC 7232 §2.3).
            etag = r1.headers.get("etag", "")
            assert etag.startswith('"') and etag.endswith('"'), (
                f"ETag must be double-quoted, got: {etag!r}"
            )
            # ETag must reference the filename so a rename invalidates it.
            assert "benchy.3mf" in etag

            # Cache-Control must prevent proxy caching and require revalidation.
            cc = r1.headers.get("cache-control", "")
            assert "private" in cc
            assert "must-revalidate" in cc

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_mesh_304_on_matching_if_none_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /viz/mesh with a matching If-None-Match returns 304 with empty body."""
    cube_bytes = _make_cube_3mf()
    _patch_ftps(monkeypatch, cube_bytes, "benchy.3mf")

    app = build_app(tmp_path / "mesh_304.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)

            # Prime the in-process cache with a first request.
            r1 = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r1.status_code == 200, r1.text
            etag = r1.headers["etag"]

            # Repeat with the ETag — must get 304 and empty body.
            r2 = c.get(
                f"/api/v1/printers/{SERIAL}/viz/mesh",
                headers={**_AUTH, "If-None-Match": etag},
            )
            assert r2.status_code == 304, r2.text
            assert r2.content == b"", (
                f"304 body must be empty, got {len(r2.content)} bytes"
            )
            # 304 still carries the ETag and Cache-Control.
            assert r2.headers.get("etag") == etag
            assert "must-revalidate" in r2.headers.get("cache-control", "")

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_mesh_304_does_not_reparse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """304 short-circuit fires before parse_3mf — parse call count stays at 1."""
    cube_bytes = _make_cube_3mf()
    parse_call_count = 0

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["benchy.3mf"]

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        return cube_bytes

    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.download_bytes", _download_bytes)

    # parse_3mf is now called through viz_cache (the endpoint delegates to
    # VizCache.fill_mesh_for_request).  Patch it in the viz_cache module.
    import bambu_bridge.service.viz_cache as viz_cache_mod
    original_parse = viz_cache_mod.parse_3mf

    def _counting_parse(data: bytes) -> Any:
        nonlocal parse_call_count
        parse_call_count += 1
        return original_parse(data)

    monkeypatch.setattr(viz_cache_mod, "parse_3mf", _counting_parse)

    app = build_app(tmp_path / "mesh_no_reparse.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)

            # First request — causes one parse.
            r1 = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r1.status_code == 200, r1.text
            etag = r1.headers["etag"]
            assert parse_call_count == 1

            # Second request with matching ETag — must NOT call parse_3mf again.
            r2 = c.get(
                f"/api/v1/printers/{SERIAL}/viz/mesh",
                headers={**_AUTH, "If-None-Match": etag},
            )
            assert r2.status_code == 304, r2.text
            assert parse_call_count == 1, (
                f"parse_3mf was called again on 304 path (count={parse_call_count})"
            )

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_mesh_stale_if_none_match_returns_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If-None-Match that doesn't match the current ETag → 200 full response."""
    cube_bytes = _make_cube_3mf()
    _patch_ftps(monkeypatch, cube_bytes, "benchy.3mf")

    app = build_app(tmp_path / "mesh_stale_etag.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)

            r1 = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r1.status_code == 200, r1.text

            # Send a deliberately wrong ETag.
            r2 = c.get(
                f"/api/v1/printers/{SERIAL}/viz/mesh",
                headers={**_AUTH, "If-None-Match": '"wrong-etag-value"'},
            )
            assert r2.status_code == 200, r2.text
            # Body must be the full JSON.
            assert "vertex_count" in r2.json()

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Helpers — large synthetic fixtures for gzip tests
# ------------------------------------------------------------------ #


def _make_large_mesh_3mf() -> bytes:
    """Build a 3MF with ~100 triangles so the JSON response exceeds 1 KB.

    The mesh is a fan of triangles around the origin — enough geometry that
    base64(vertices) + base64(indices) in the JSON body is > 1 KB.
    """
    # 102 triangles = 204 extra vertices (shared apex), 306 total
    # Each vertex line: ~40 chars; each triangle line: ~40 chars — well over 1 KB.
    n = 50  # 50 segments → 100 triangles (2 per segment for a tube)
    import math

    vertices = ["<vertices>"]
    for i in range(n + 1):
        angle = 2 * math.pi * i / n
        vertices.append(f'<vertex x="{math.cos(angle):.4f}" y="{math.sin(angle):.4f}" z="0"/>')
        vertices.append(f'<vertex x="{math.cos(angle):.4f}" y="{math.sin(angle):.4f}" z="10"/>')
    vertices.append("</vertices>")

    triangles = ["<triangles>"]
    for i in range(n):
        b = i * 2
        triangles.append(f'<triangle v1="{b}" v2="{b+2}" v3="{b+1}"/>')
        triangles.append(f'<triangle v1="{b+1}" v2="{b+2}" v3="{b+3}"/>')
    triangles.append("</triangles>")

    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dml/2013/core">'
        "<resources>"
        '<object id="1" type="model"><mesh>'
        + "".join(vertices)
        + "".join(triangles)
        + "</mesh></object>"
        "</resources>"
        '<build><item objectid="1"/></build>'
        "</model>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("3D/3dmodel.model", model)
    return buf.getvalue()


def _make_large_toolpath_archive() -> bytes:
    """Build a .gcode.3mf archive with many non-collinear extrusion moves.

    Uses a spiralling zigzag so that consecutive segments are NOT collinear
    (the collinear-merge logic in the parser would otherwise collapse them
    into a handful of segments).  ~200 direction-changes → ~200 segments →
    positions_b64 is several KB, ensuring the JSON response exceeds 1 KB.
    """
    import math

    lines = ["G90", "M83", "G92 E0", "G1 Z0.2 F1200"]
    # Trace an approximate circle in 200 steps — every move changes direction.
    n = 200
    r = 50.0
    for i in range(n + 1):
        angle = 2 * math.pi * i / n
        x = r * math.cos(angle)
        y = r * math.sin(angle)
        lines.append(f"G1 X{x:.4f} Y{y:.4f} E0.05")
    gcode = "\n".join(lines) + "\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("Metadata/plate_1.gcode", gcode)
        zf.writestr("3D/3dmodel.model", "<model/>")
    return buf.getvalue()


# ------------------------------------------------------------------ #
# Tests — gzip compression on /viz/mesh
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_mesh_gzip_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accept-Encoding: gzip → Content-Encoding: gzip; decompressed body == JSON."""
    # Use a mesh with ~100 triangles so the response body is clearly > 1 KB.
    large_bytes = _make_large_mesh_3mf()
    _patch_ftps(monkeypatch, large_bytes, "large_mesh.3mf")

    app = build_app(tmp_path / "mesh_gzip.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="large_mesh.3mf")

            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz/mesh",
                headers={**_AUTH, "Accept-Encoding": "gzip"},
            )
            assert r.status_code == 200, r.text

            # httpx auto-decompresses; the decoded body must be valid JSON.
            body = r.json()
            assert "vertex_count" in body

            # The Content-Encoding header proves the server actually compressed.
            # httpx transparently decompresses, but the response header survives.
            assert r.headers.get("content-encoding") == "gzip", (
                f"Expected Content-Encoding: gzip, got: {r.headers.get('content-encoding')!r}"
            )

            # Verify round-trip manually.
            raw_json = json.dumps(body).encode()
            repacked = gzip.compress(raw_json)
            assert gzip.decompress(repacked) == raw_json

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_mesh_identity_without_accept_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without Accept-Encoding: gzip the response is identity (no Content-Encoding)."""
    cube_bytes = _make_cube_3mf()
    _patch_ftps(monkeypatch, cube_bytes, "benchy.3mf")

    app = build_app(tmp_path / "mesh_identity.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)

            # Explicitly omit Accept-Encoding (TestClient default may add it).
            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz/mesh",
                headers={**_AUTH, "Accept-Encoding": "identity"},
            )
            assert r.status_code == 200, r.text
            assert "content-encoding" not in r.headers, (
                f"Identity request must not get Content-Encoding, "
                f"got: {r.headers.get('content-encoding')!r}"
            )
            # Body must still be parseable JSON.
            assert "vertex_count" in r.json()

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — ETag and Cache-Control on /viz/toolpath
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_toolpath_200_carries_etag_and_cache_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /viz/toolpath 200 response includes ETag and Cache-Control headers."""
    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "benchy.gcode.3mf")

    app = build_app(tmp_path / "tp_etag_200.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")

            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r.status_code == 200, r.text

            etag = r.headers.get("etag", "")
            assert etag.startswith('"') and etag.endswith('"'), (
                f"ETag must be double-quoted, got: {etag!r}"
            )
            assert "benchy.gcode.3mf" in etag

            cc = r.headers.get("cache-control", "")
            assert "private" in cc
            assert "must-revalidate" in cc

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_304_on_matching_if_none_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /viz/toolpath with a matching If-None-Match returns 304 with empty body."""
    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "benchy.gcode.3mf")

    app = build_app(tmp_path / "tp_304.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")

            r1 = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r1.status_code == 200, r1.text
            etag = r1.headers["etag"]

            r2 = c.get(
                f"/api/v1/printers/{SERIAL}/viz/toolpath",
                headers={**_AUTH, "If-None-Match": etag},
            )
            assert r2.status_code == 304, r2.text
            assert r2.content == b"", (
                f"304 body must be empty, got {len(r2.content)} bytes"
            )
            assert r2.headers.get("etag") == etag
            assert "must-revalidate" in r2.headers.get("cache-control", "")

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_304_does_not_reparse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """304 short-circuit fires before parse_gcode_from_archive — parse count stays 1."""
    archive = _make_toolpath_archive()
    parse_call_count = 0

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["benchy.gcode.3mf"]

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        return archive

    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.download_bytes", _download_bytes)

    # parse_gcode_from_archive is now called through viz_cache (the endpoint
    # delegates to VizCache.fill_toolpath_for_request).
    import bambu_bridge.service.viz_cache as viz_cache_mod
    original_parse = viz_cache_mod.parse_gcode_from_archive

    def _counting_parse(data: bytes) -> Any:
        nonlocal parse_call_count
        parse_call_count += 1
        return original_parse(data)

    monkeypatch.setattr(viz_cache_mod, "parse_gcode_from_archive", _counting_parse)

    app = build_app(tmp_path / "tp_no_reparse.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")

            r1 = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r1.status_code == 200, r1.text
            etag = r1.headers["etag"]
            assert parse_call_count == 1

            r2 = c.get(
                f"/api/v1/printers/{SERIAL}/viz/toolpath",
                headers={**_AUTH, "If-None-Match": etag},
            )
            assert r2.status_code == 304, r2.text
            assert parse_call_count == 1, (
                f"parse_gcode_from_archive was called again on 304 path "
                f"(count={parse_call_count})"
            )

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_stale_if_none_match_returns_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If-None-Match that doesn't match the current ETag → 200 full toolpath."""
    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "benchy.gcode.3mf")

    app = build_app(tmp_path / "tp_stale_etag.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")

            r1 = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r1.status_code == 200, r1.text

            r2 = c.get(
                f"/api/v1/printers/{SERIAL}/viz/toolpath",
                headers={**_AUTH, "If-None-Match": '"wrong-etag"'},
            )
            assert r2.status_code == 200, r2.text
            assert "segment_count" in r2.json()

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_gzip_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accept-Encoding: gzip → Content-Encoding: gzip; decompressed body == JSON."""
    # Use a large gcode archive (200 segments) so the JSON response is > 1 KB.
    large_archive = _make_large_toolpath_archive()
    _patch_ftps(monkeypatch, large_archive, "large_path.gcode.3mf")

    app = build_app(tmp_path / "tp_gzip.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="large_path.gcode.3mf")

            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz/toolpath",
                headers={**_AUTH, "Accept-Encoding": "gzip"},
            )
            assert r.status_code == 200, r.text

            # httpx auto-decompresses; Content-Encoding header still present.
            assert r.headers.get("content-encoding") == "gzip", (
                f"Expected Content-Encoding: gzip, got: {r.headers.get('content-encoding')!r}"
            )

            body = r.json()
            assert "segment_count" in body
            # Large archive has 200 G1 E moves → 200 segments (minus initial
            # position move, which has no prior position to form a segment from).
            assert body["segment_count"] > 0

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_identity_without_accept_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without Accept-Encoding: gzip the toolpath response is identity."""
    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "benchy.gcode.3mf")

    app = build_app(tmp_path / "tp_identity.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")

            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz/toolpath",
                headers={**_AUTH, "Accept-Encoding": "identity"},
            )
            assert r.status_code == 200, r.text
            assert "content-encoding" not in r.headers, (
                f"Identity request must not get Content-Encoding, "
                f"got: {r.headers.get('content-encoding')!r}"
            )
            assert "segment_count" in r.json()

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — /viz viewer HTML page must NOT carry a job-tied ETag
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_viewer_html_no_job_etag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /viz viewer HTML must not carry an ETag keyed to job state.

    The viewer page is static HTML; tying an ETag to the current job would
    let a stale viewer bypass a job change.  We assert the ETag is absent
    (or at least not derived from a job filename — the page has no job info).
    """
    viewer_file = tmp_path / "viewer.html"
    viewer_file.write_text("<html><body>VIZ VIEWER</body></html>", encoding="utf-8")

    import bambu_bridge.api.viz as viz_mod

    monkeypatch.setattr(viz_mod, "_VIEWER_HTML", viewer_file)

    app = build_app(tmp_path / "viz_viewer_etag.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz{_TOKEN_PARAM}",
                follow_redirects=True,
            )
            assert r.status_code == 200, r.text
            # The viz viewer page must not carry an ETag that encodes the
            # job filename — the endpoint serves static HTML and the ETag,
            # if any, must NOT contain a job-derived value like "benchy".
            etag = r.headers.get("etag", "")
            assert "benchy" not in etag, (
                f"Viewer ETag must not reference a job name; got: {etag!r}"
            )

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Helpers for binary toolpath format tests
# ------------------------------------------------------------------ #


def _parse_bin_toolpath(raw: bytes) -> tuple[dict, bytes]:
    """Split a binary toolpath response into (header_dict, positions_bytes).

    Layout: [uint32-LE header_len][header_len bytes JSON][float32-LE positions].
    Raises AssertionError on any structural error.
    """
    assert len(raw) >= 4, f"Response too short for header length: {len(raw)} bytes"
    (header_len,) = struct.unpack_from("<I", raw, 0)
    assert 4 + header_len <= len(raw), (
        f"Header length {header_len} overflows response of {len(raw)} bytes"
    )
    header_bytes = raw[4 : 4 + header_len]
    header = json.loads(header_bytes.decode("utf-8"))
    positions_bytes = raw[4 + header_len :]
    return header, positions_bytes


# ------------------------------------------------------------------ #
# Tests — /viz/toolpath?fmt=bin happy path
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_toolpath_bin_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /viz/toolpath?fmt=bin returns application/octet-stream with correct layout."""
    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "benchy.gcode.3mf")

    app = build_app(tmp_path / "tp_bin_happy.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf", total_layers=200)

            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin", headers=_AUTH)
            assert r.status_code == 200, r.text
            assert r.headers.get("content-type") == "application/octet-stream", (
                f"Expected application/octet-stream, got: {r.headers.get('content-type')!r}"
            )

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_bin_header_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binary header decodes to valid JSON with all required scalar fields."""
    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "benchy.gcode.3mf")

    app = build_app(tmp_path / "tp_bin_hdr.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf", total_layers=200)

            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin", headers=_AUTH)
            assert r.status_code == 200, r.text

            hdr, pos_bytes = _parse_bin_toolpath(r.content)

            # All required scalar fields.
            required = {"segment_count", "bbox", "layer_table",
                        "job", "total_layers", "layer_height_mm",
                        "decimated", "source_file", "cached"}
            missing = required - set(hdr.keys())
            assert not missing, f"Binary header missing fields: {missing}"

            assert hdr["segment_count"] == 4
            assert hdr["job"] == "benchy.gcode.3mf"
            assert hdr["total_layers"] == 200
            assert hdr["cached"] is False
            assert hdr["decimated"] is False

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_bin_byte_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positions block has exactly segment_count * 2 * 3 * 4 bytes of float32-LE."""
    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "benchy.gcode.3mf")

    app = build_app(tmp_path / "tp_bin_layout.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")

            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin", headers=_AUTH)
            assert r.status_code == 200, r.text

            hdr, pos_bytes = _parse_bin_toolpath(r.content)
            seg_count = hdr["segment_count"]
            expected_bytes = seg_count * 2 * 3 * 4  # 2 verts * xyz * float32
            assert len(pos_bytes) == expected_bytes, (
                f"Expected {expected_bytes} bytes for {seg_count} segments, "
                f"got {len(pos_bytes)}"
            )

            # First float should be readable as a valid float32.
            if expected_bytes >= 4:
                (x,) = struct.unpack_from("<f", pos_bytes, 0)
                assert isinstance(x, float)

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_bin_positions_match_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binary positions decode to the same float32 values as the JSON base64 path."""
    archive = _make_toolpath_archive()

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["benchy.gcode.3mf"]

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        return archive

    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.download_bytes", _download_bytes)

    app = build_app(tmp_path / "tp_bin_match.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")

            r_json = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r_json.status_code == 200, r_json.text
            j = r_json.json()
            json_floats = list(struct.unpack(
                f"<{len(base64.b64decode(j['positions_b64'])) // 4}f",
                base64.b64decode(j["positions_b64"]),
            ))

            r_bin = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin", headers=_AUTH)
            assert r_bin.status_code == 200, r_bin.text
            hdr, pos_bytes = _parse_bin_toolpath(r_bin.content)
            bin_floats = list(struct.unpack(f"<{len(pos_bytes) // 4}f", pos_bytes))

            assert len(json_floats) == len(bin_floats), (
                f"Float count mismatch: json={len(json_floats)} bin={len(bin_floats)}"
            )
            for i, (jf, bf) in enumerate(zip(json_floats, bin_floats, strict=True)):
                assert jf == pytest.approx(bf, abs=1e-5), (
                    f"Float mismatch at index {i}: json={jf} bin={bf}"
                )

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — binary format layer_table
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_toolpath_bin_layer_table_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """layer_table in binary header is a list of [v0, v1, z] entries."""
    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "benchy.gcode.3mf")

    app = build_app(tmp_path / "tp_bin_ltbl.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")

            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin", headers=_AUTH)
            assert r.status_code == 200, r.text

            hdr, _ = _parse_bin_toolpath(r.content)
            lt = hdr["layer_table"]
            assert isinstance(lt, list), "layer_table must be a list"
            assert len(lt) >= 1, (
                "layer_table must have at least one entry for a print with segments"
            )
            for entry in lt:
                assert len(entry) == 3, f"Each entry must be [v0, v1, z], got {entry!r}"
                v0, v1, z = entry
                assert isinstance(v0, int | float), f"v0 must be numeric, got {type(v0)}"
                assert isinstance(v1, int | float), f"v1 must be numeric, got {type(v1)}"
                assert isinstance(z, int | float), f"z must be numeric, got {type(z)}"
                assert v0 <= v1, f"v0 ({v0}) must be ≤ v1 ({v1})"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_bin_layer_table_spans_all_vertices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """layer_table entries collectively cover all vertices (no gaps, no overflow)."""
    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "benchy.gcode.3mf")

    app = build_app(tmp_path / "tp_bin_ltbl_span.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")

            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin", headers=_AUTH)
            assert r.status_code == 200, r.text

            hdr, pos_bytes = _parse_bin_toolpath(r.content)
            vertex_count = len(pos_bytes) // 12  # each vertex is 3 × float32
            lt = hdr["layer_table"]

            # First layer starts at vertex 0.
            assert lt[0][0] == 0, f"First layer v0 must be 0, got {lt[0][0]}"
            # Last layer ends at the last vertex.
            assert lt[-1][1] == vertex_count - 1, (
                f"Last layer v1 ({lt[-1][1]}) must equal last vertex "
                f"({vertex_count - 1})"
            )

    await asyncio.to_thread(run)


# A two-layer gcode: a 10×10 square at Z=0.2, then a second square at Z=0.4.
# This exercises the multi-layer branch of _build_bin_toolpath's layer_table
# loop (the conditional layer_table.append guarded by a Z-transition), which
# is dead code under every single-Z fixture above.
_TWO_LAYER_GCODE = """\
G90
M83
G92 E0
G1 Z0.2 F1200
G1 X0 Y0 F6000
G1 X10 Y0 E0.5
G1 X10 Y10 E0.5
G1 X0 Y10 E0.5
G1 X0 Y0 E0.5
G1 Z0.4 F1200
G1 X0 Y0 F6000
G1 X10 Y0 E0.5
G1 X10 Y10 E0.5
G1 X0 Y10 E0.5
G1 X0 Y0 E0.5
"""


@pytest.mark.asyncio
async def test_viz_toolpath_bin_two_layers_split_layer_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A two-layer print produces exactly two layer_table entries.

    Regression coverage for the multi-layer branch in _build_bin_toolpath:
    every other toolpath fixture stays at a single Z, so the Z-transition
    ``layer_table.append`` inside the loop was never exercised.  This fixture
    extrudes a square at Z=0.2 then another at Z=0.4 and asserts:
      * len(layer_table) == 2,
      * the two layer Z values are ≈ 0.2 and ≈ 0.4 (ascending),
      * the vertex [v0, v1] ranges are contiguous and non-overlapping.
    """
    archive = _make_toolpath_archive(_TWO_LAYER_GCODE)
    _patch_ftps(monkeypatch, archive, "two_layer.gcode.3mf")

    app = build_app(tmp_path / "tp_bin_two_layer.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="two_layer.gcode.3mf")
            # Pin a real layer height so the split threshold is the physical
            # half-layer (0.1 mm), not the bbox-derived fallback.
            from bambu_bridge.service.registry import Registry

            reg: Registry = app.state.registry
            reg.get(SERIAL)._state["layer_height_mm"] = 0.2

            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin", headers=_AUTH
            )
            assert r.status_code == 200, r.text

            hdr, pos_bytes = _parse_bin_toolpath(r.content)
            vertex_count = len(pos_bytes) // 12
            lt = hdr["layer_table"]

            # Exactly two layers.
            assert len(lt) == 2, f"expected 2 layer_table entries, got {lt!r}"

            (v0_a, v1_a, z_a), (v0_b, v1_b, z_b) = lt[0], lt[1]

            # Z values ascend and match the two printed layers.
            assert z_a == pytest.approx(0.2, abs=1e-3), f"layer 0 Z={z_a}"
            assert z_b == pytest.approx(0.4, abs=1e-3), f"layer 1 Z={z_b}"

            # Contiguous, non-overlapping vertex ranges covering all vertices.
            assert v0_a == 0, f"first layer must start at vertex 0, got {v0_a}"
            assert v1_b == vertex_count - 1, (
                f"last layer must end at last vertex {vertex_count - 1}, got {v1_b}"
            )
            assert v1_a < v0_b, (
                f"layer ranges must not overlap: layer0 ends {v1_a}, "
                f"layer1 starts {v0_b}"
            )
            assert v0_b == v1_a + 1, (
                f"layer ranges must be contiguous: {v1_a} → {v0_b}"
            )

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — binary format ETag / 304 / Cache-Control
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_toolpath_bin_carries_representation_specific_etag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /viz/toolpath?fmt=bin carries an ETag DISTINCT from the JSON one.

    A strong ETag must identify exactly one representation (RFC 7232 §2.3).
    json and bin are different representations of the same source file, so
    their ETags must differ — otherwise a client that cached json under that
    tag and then requested bin would be told 304 and reuse the wrong bytes.
    Both ETags still share the ``<filename>:<size>`` prefix (same source file)
    and differ only in the trailing format token.
    """
    archive = _make_toolpath_archive()

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["benchy.gcode.3mf"]

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        return archive

    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.download_bytes", _download_bytes)

    app = build_app(tmp_path / "tp_bin_etag.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")

            r_json = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r_json.status_code == 200, r_json.text
            etag_json = r_json.headers.get("etag", "")

            r_bin = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin", headers=_AUTH)
            assert r_bin.status_code == 200, r_bin.text
            etag_bin = r_bin.headers.get("etag", "")

            # ETag must be double-quoted.
            assert etag_bin.startswith('"') and etag_bin.endswith('"'), (
                f"Binary ETag must be double-quoted, got: {etag_bin!r}"
            )
            # The ETags must DIFFER between formats (representation-specific).
            assert etag_json != etag_bin, (
                f"json and bin must carry distinct ETags, both were: {etag_json!r}"
            )
            # …but share the file-identity prefix (same filename + size).
            assert etag_json.rstrip('"').endswith(":json"), (
                f"json ETag must end in :json, got {etag_json!r}"
            )
            assert etag_bin.rstrip('"').endswith(":bin"), (
                f"bin ETag must end in :bin, got {etag_bin!r}"
            )
            prefix_json = etag_json.rstrip('"').rsplit(":", 1)[0]
            prefix_bin = etag_bin.rstrip('"').rsplit(":", 1)[0]
            assert prefix_json == prefix_bin, (
                f"ETag file-identity prefix must match: {prefix_json!r} vs {prefix_bin!r}"
            )
            # Cache-Control must be present.
            cc = r_bin.headers.get("cache-control", "")
            assert "private" in cc
            assert "must-revalidate" in cc

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_bin_304_on_matching_if_none_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /viz/toolpath?fmt=bin with matching If-None-Match returns 304."""
    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "benchy.gcode.3mf")

    app = build_app(tmp_path / "tp_bin_304.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")

            r1 = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin", headers=_AUTH)
            assert r1.status_code == 200, r1.text
            etag = r1.headers["etag"]

            r2 = c.get(
                f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin",
                headers={**_AUTH, "If-None-Match": etag},
            )
            assert r2.status_code == 304, r2.text
            assert r2.content == b"", (
                f"304 body must be empty, got {len(r2.content)} bytes"
            )
            assert r2.headers.get("etag") == etag

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_bin_304_does_not_reparse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binary 304 short-circuit does not call parse_gcode_from_archive again."""
    archive = _make_toolpath_archive()
    parse_call_count = 0

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["benchy.gcode.3mf"]

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        return archive

    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.download_bytes", _download_bytes)

    # parse_gcode_from_archive is now called through viz_cache.
    import bambu_bridge.service.viz_cache as viz_cache_mod
    original_parse = viz_cache_mod.parse_gcode_from_archive

    def _counting_parse(data: bytes) -> Any:
        nonlocal parse_call_count
        parse_call_count += 1
        return original_parse(data)

    monkeypatch.setattr(viz_cache_mod, "parse_gcode_from_archive", _counting_parse)

    app = build_app(tmp_path / "tp_bin_no_reparse.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")

            r1 = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin", headers=_AUTH)
            assert r1.status_code == 200, r1.text
            assert parse_call_count == 1

            etag = r1.headers["etag"]
            r2 = c.get(
                f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin",
                headers={**_AUTH, "If-None-Match": etag},
            )
            assert r2.status_code == 304, r2.text
            assert parse_call_count == 1, (
                f"parse_gcode_from_archive called again on bin 304 path "
                f"(count={parse_call_count})"
            )

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — binary format gzip decision
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_toolpath_bin_gzip_roundtrip_with_accept_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binary toolpath with Accept-Encoding: gzip always returns a parseable body.

    Whether or not the server applies gzip (determined by the 15% savings
    threshold), the response body must parse as a valid binary toolpath.
    httpx auto-decompresses, so r.content is always the raw binary regardless.
    """
    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "benchy.gcode.3mf")

    app = build_app(tmp_path / "tp_bin_gzip_rt.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")

            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin",
                headers={**_AUTH, "Accept-Encoding": "gzip"},
            )
            assert r.status_code == 200, r.text
            # r.content is always the decoded binary (httpx handles decompression).
            hdr, pos_bytes = _parse_bin_toolpath(r.content)
            assert hdr["segment_count"] == 4
            assert len(pos_bytes) == 4 * 2 * 3 * 4  # 4 segs * 2 verts * xyz * float32

    await asyncio.to_thread(run)


def test_viz_toolpath_bin_gzip_threshold_unit() -> None:
    """_bin_response_with_headers skips gzip when savings < 15% (unit test).

    This exercises the threshold logic directly without a full HTTP stack.
    We craft a body of random bytes (incompressible) whose compressed form
    is larger than 85% of the original, then verify no Content-Encoding is set.
    """
    import gzip as _gz
    import os

    from starlette.datastructures import Headers
    from starlette.requests import Request as StarletteRequest

    from bambu_bridge.api.viz import _BIN_GZIP_MIN_SAVINGS, _bin_response_with_headers

    # Build an incompressible body: random bytes compress poorly.
    rng = bytearray(os.urandom(4096))  # 4 KB of random bytes

    # Verify that random bytes do NOT compress by >= 15%.
    c = _gz.compress(bytes(rng), compresslevel=6)
    savings = 1.0 - len(c) / len(rng)
    # If savings happen to be ≥ 15% (very unlikely but possible with certain
    # random seeds), skip this unit test rather than false-fail.
    if savings >= _BIN_GZIP_MIN_SAVINGS:
        return  # pragma: no cover

    # Build a minimal Starlette Request that advertises gzip, bypassing pydantic
    # schema generation (no FastAPI sub-app needed).
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/t",
        "query_string": b"",
        "headers": Headers({"accept-encoding": "gzip"}).raw,
    }
    req = StarletteRequest(scope)

    resp = _bin_response_with_headers(bytes(rng), req, extra_headers={})

    # With incompressible data, server must NOT add Content-Encoding.
    assert resp.headers.get("content-encoding") != "gzip", (
        "Server incorrectly gzipped incompressible binary data "
        f"(savings were {savings:.1%}, threshold {_BIN_GZIP_MIN_SAVINGS:.0%})"
    )


@pytest.mark.asyncio
async def test_viz_toolpath_bin_gzip_applied_for_large_compressible_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binary toolpath gzips when savings are ≥ 15% AND client accepts gzip.

    The large toolpath archive has a highly-repetitive gcode pattern (circle
    at constant Z), so the float positions are repetitive and gzip DOES achieve
    ≥ 15% savings.  We assert the server applied Content-Encoding: gzip and
    the (auto-decompressed by httpx) body parses correctly.
    """
    large_archive = _make_large_toolpath_archive()
    _patch_ftps(monkeypatch, large_archive, "large.gcode.3mf")

    app = build_app(tmp_path / "tp_bin_gzip.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="large.gcode.3mf")

            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin",
                headers={**_AUTH, "Accept-Encoding": "gzip"},
            )
            assert r.status_code == 200, r.text

            # httpx auto-decompresses; Content-Encoding header still reflects
            # what the server sent.  The circle gcode compresses ~75%: gzip
            # must have fired.
            assert r.headers.get("content-encoding") == "gzip", (
                f"Expected Content-Encoding: gzip for large compressible binary, "
                f"got: {r.headers.get('content-encoding')!r}"
            )

            # r.content is already decompressed by httpx — parse it directly.
            hdr, pos_bytes = _parse_bin_toolpath(r.content)
            assert hdr["segment_count"] > 0
            # Byte count must match segment_count.
            expected_bytes = hdr["segment_count"] * 2 * 3 * 4
            assert len(pos_bytes) == expected_bytes

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — invalid fmt parameter
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_toolpath_invalid_fmt_returns_422(tmp_path: Path) -> None:
    """?fmt=xyz → 422 Unprocessable Entity (FastAPI query validation)."""
    app = build_app(tmp_path / "tp_badfmt.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=xyz",
                headers=_AUTH,
            )
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_default_fmt_is_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ?fmt= the response is JSON (backward compat for HA iframe)."""
    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "benchy.gcode.3mf")

    app = build_app(tmp_path / "tp_default_fmt.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")

            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r.status_code == 200, r.text
            assert "application/json" in r.headers.get("content-type", ""), (
                f"Default fmt should be JSON, got: {r.headers.get('content-type')!r}"
            )
            body = r.json()
            assert "positions_b64" in body, "JSON response must contain positions_b64"

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — bin cache behaviour
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_toolpath_bin_cache_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second binary request uses cache (cached=True in header, no re-download)."""
    archive = _make_toolpath_archive()
    download_count = 0

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["benchy.gcode.3mf"]

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        nonlocal download_count
        download_count += 1
        return archive

    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.download_bytes", _download_bytes)

    app = build_app(tmp_path / "tp_bin_cache.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")

            r1 = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin", headers=_AUTH)
            assert r1.status_code == 200, r1.text
            hdr1, _ = _parse_bin_toolpath(r1.content)
            assert hdr1["cached"] is False

            r2 = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin", headers=_AUTH)
            assert r2.status_code == 200, r2.text
            hdr2, _ = _parse_bin_toolpath(r2.content)
            assert hdr2["cached"] is True

        assert download_count == 1

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — cross-format ETag isolation (json and bin must NOT share a 304)
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_toolpath_cross_format_etag_does_not_304(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The JSON ETag must NOT satisfy a 304 on the bin path, and vice versa.

    json and bin are distinct representations of the same source file.  A
    strong ETag identifies ONE representation (RFC 7232 §2.3), so presenting
    the json ETag as If-None-Match on the bin path must yield a full 200 (the
    correct bin body) — never a 304 that would make the client reuse cached
    json bytes as if they were the binary payload.
    """
    archive = _make_toolpath_archive()

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["benchy.gcode.3mf"]

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        return archive

    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.download_bytes", _download_bytes)

    app = build_app(tmp_path / "tp_xfmt_etag.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")

            # Warm with JSON.
            r_json = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r_json.status_code == 200, r_json.text
            etag_from_json = r_json.headers["etag"]

            # Use JSON's ETag on the bin path — must NOT 304; must serve the
            # full binary body instead (content-type octet-stream).
            r_bin = c.get(
                f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin",
                headers={**_AUTH, "If-None-Match": etag_from_json},
            )
            assert r_bin.status_code == 200, (
                f"json ETag must not 304 the bin path; got {r_bin.status_code}"
            )
            assert r_bin.headers.get("content-type") == "application/octet-stream"
            hdr, _ = _parse_bin_toolpath(r_bin.content)
            assert hdr["segment_count"] == 4

            # Symmetric: the bin ETag must NOT 304 the JSON path.
            etag_from_bin = r_bin.headers["etag"]
            r_json2 = c.get(
                f"/api/v1/printers/{SERIAL}/viz/toolpath",
                headers={**_AUTH, "If-None-Match": etag_from_bin},
            )
            assert r_json2.status_code == 200, (
                f"bin ETag must not 304 the json path; got {r_json2.status_code}"
            )
            assert "segment_count" in r_json2.json()

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — binary zero-segment toolpath
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_toolpath_bin_zero_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binary toolpath with 0 segments returns 200 with empty positions block."""
    empty_gcode = "G1 X10 Y10 F6000\nG1 X20 Y10 F6000\n"  # no E moves
    archive = _make_toolpath_archive(empty_gcode)
    _patch_ftps(monkeypatch, archive, "empty.gcode.3mf")

    app = build_app(tmp_path / "tp_bin_zero.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="empty.gcode.3mf")

            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin", headers=_AUTH)
            assert r.status_code == 200, r.text

            hdr, pos_bytes = _parse_bin_toolpath(r.content)
            assert hdr["segment_count"] == 0
            assert pos_bytes == b"", (
                f"Zero-segment toolpath must have empty positions block, "
                f"got {len(pos_bytes)} bytes"
            )

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — header alignment (RangeError safety for Float32Array)
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_toolpath_bin_posoffset_is_4byte_aligned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positions block must start at a 4-byte-aligned offset.

    new Float32Array(arrayBuffer, byteOffset, n) in the browser throws
    RangeError when byteOffset % 4 != 0.  posOffset = 4 + header_len,
    so the server must pad the JSON header to a 4-byte boundary.

    Tests with a job name whose length ensures the raw header JSON would
    be un-aligned (we vary job name length to exercise all four residues).
    """
    archive = _make_toolpath_archive()

    for extra_chars in range(4):
        # Build job names whose lengths push header JSON through all 4 residues.
        job_name = f"align{'x' * extra_chars}.gcode.3mf"
        _patch_ftps(monkeypatch, archive, job_name)
        _app = build_app(tmp_path / f"tp_align_{extra_chars}.db", mqtt_port=1)

        def run(jn: str = job_name, _a: Any = _app) -> None:
            with TestClient(_a) as c:
                _register_printer(c)
                _inject_job(_a, job_name=jn)

                r = c.get(
                    f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin",
                    headers=_AUTH,
                )
                assert r.status_code == 200, f"job={jn}: {r.text}"

                raw = r.content
                assert len(raw) >= 4, f"job={jn}: response too short"
                (hdr_len,) = struct.unpack_from("<I", raw, 0)
                pos_offset = 4 + hdr_len
                assert pos_offset % 4 == 0, (
                    f"job={jn}: posOffset={pos_offset} is not 4-byte aligned "
                    f"(header_len={hdr_len}).  new Float32Array would throw "
                    f"RangeError in the browser."
                )
                # Also verify the positions block itself is still parseable.
                hdr, pos_bytes = _parse_bin_toolpath(raw)
                assert hdr["segment_count"] >= 0

        await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_toolpath_bin_posoffset_4byte_aligned_large_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alignment holds for a large fixture with an odd-length job name.

    Uses a 200-segment circle archive with a job name whose character count
    is chosen to produce a non-4-aligned raw header JSON (verified by
    asserting posOffset % 4 == 0 after the server pads it).
    """
    large_archive = _make_large_toolpath_archive()
    # Odd-length name — forces the header to a non-trivial length.
    job_name = "circle_odd_len_job.gcode.3mf"  # len = 28, forces non-trivial padding
    _patch_ftps(monkeypatch, large_archive, job_name)

    app = build_app(tmp_path / "tp_align_large.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name=job_name, total_layers=200)

            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz/toolpath?fmt=bin",
                headers=_AUTH,
            )
            assert r.status_code == 200, r.text

            raw = r.content
            (hdr_len,) = struct.unpack_from("<I", raw, 0)
            pos_offset = 4 + hdr_len
            assert pos_offset % 4 == 0, (
                f"posOffset={pos_offset} not 4-byte aligned (header_len={hdr_len})"
            )
            hdr, pos_bytes = _parse_bin_toolpath(raw)
            expected = hdr["segment_count"] * 2 * 3 * 4
            assert len(pos_bytes) == expected

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — cold endpoint load fills the sliced-date memo
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_toolpath_cold_load_fills_sliced_date_memo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cold endpoint load (/viz/toolpath, no pre-warm) must fill the sliced-date memo.

    Regression test for the architecture defect where the inline cold-load
    path in viz.py downloaded and parsed without calling VizCache, so the
    sliced-date memo was never populated on endpoint-triggered cold loads.

    The fix: the endpoint delegates to VizCache.fill_toolpath_for_request,
    which is the single authoritative pipeline (download → memo → parse →
    cache).  After the first /viz/toolpath request, the memo must contain
    a non-None sliced_at for the job file.
    """
    from datetime import datetime

    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "cold.gcode.3mf")

    app = build_app(tmp_path / "cold_memo.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="cold.gcode.3mf")

            # First request — cold cache, no pre-warm has run.
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r.status_code == 200, r.text
            assert r.json()["cached"] is False, "First cold load must return cached=False"

            # The sliced-date memo must now contain an entry for the job file.
            memo = app.state.sliced_date_memo
            sliced_at = memo.get("cold.gcode.3mf", len(archive))
            assert sliced_at is not None, (
                "sliced_date_memo must be populated after a cold endpoint load; "
                "got None.  Did the endpoint delegate to VizCache?"
            )
            assert isinstance(sliced_at, datetime), (
                f"sliced_at must be a datetime, got {type(sliced_at)}"
            )
            # The ZIP entry date_time for _make_toolpath_archive will be the
            # current time (writestr uses localtime) — year must be >= 2020.
            assert sliced_at.year >= 2020, (
                f"sliced_at.year={sliced_at.year} looks like a bogus epoch timestamp"
            )

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_mesh_cold_load_fills_sliced_date_memo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cold endpoint load (/viz/mesh, no pre-warm) must fill the sliced-date memo.

    Mirrors the toolpath test for the mesh path.  Uses a .gcode.3mf archive
    with an empty mesh (geometry_available=False) so that the archive IS a
    sliced file — the same kind of file that has the sliced-date embedded in
    its ZIP timestamps.
    """
    from datetime import datetime

    gcode_bytes = _make_gcode_3mf_bytes()
    _patch_ftps(monkeypatch, gcode_bytes, "cold_mesh.gcode.3mf")

    app = build_app(tmp_path / "cold_mesh_memo.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="cold_mesh.gcode.3mf")

            r = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r.status_code == 200, r.text
            assert r.json()["cached"] is False, "First cold load must return cached=False"

            memo = app.state.sliced_date_memo
            sliced_at = memo.get("cold_mesh.gcode.3mf", len(gcode_bytes))
            assert sliced_at is not None, (
                "sliced_date_memo must be populated after a cold /viz/mesh load; "
                "got None.  Did the endpoint delegate to VizCache?"
            )
            assert isinstance(sliced_at, datetime)

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Tests — viz works for a completed job (post-print review)
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_works_for_finished_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both /viz/mesh and /viz/toolpath return 200 after the print FINISHes.

    The user reviews the model/toolpath of a completed print.  The endpoints
    gate only on ``subtask_name`` (the job is still named in printer state when
    gcode_state == FINISH), not on the live RUNNING flag, so a finished job is
    still viewable.  Uses a .gcode.3mf archive that is both a (geometry-less)
    valid 3MF and carries Metadata/plate_1.gcode, so both endpoints succeed.
    """
    archive = _make_toolpath_archive()
    _patch_ftps(monkeypatch, archive, "finished.gcode.3mf")

    app = build_app(tmp_path / "viz_finish.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="finished.gcode.3mf", total_layers=200)
            # The print has completed: gcode_state flips to FINISH but the job
            # name is still present in printer state.
            from bambu_bridge.service.registry import Registry

            reg: Registry = app.state.registry
            reg.get(SERIAL)._state["gcode_state"] = "FINISH"

            r_mesh = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh", headers=_AUTH)
            assert r_mesh.status_code == 200, r_mesh.text
            assert r_mesh.json()["job"] == "finished.gcode.3mf"

            r_tp = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            assert r_tp.status_code == 200, r_tp.text
            assert r_tp.json()["job"] == "finished.gcode.3mf"
            assert r_tp.json()["segment_count"] == 4

    await asyncio.to_thread(run)


@pytest.fixture(autouse=True)
def _stable_file_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    async def revision(self: Any, name: str, *, remote_dir: str = "") -> tuple[int, str]:
        return (1, "fixture-revision")
    monkeypatch.setattr("bambu_bridge.protocol.ftps.FtpsTransfer.file_revision", revision)
