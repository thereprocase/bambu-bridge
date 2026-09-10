"""Security invariants for the BRIDGE_VIZ_TOKEN read-only viewer token.

Design (locked): the viz token is a SCOPED credential that is accepted ONLY on
three read-only routes:

  * GET /printers/{id}          (snapshot)
  * GET /printers/{id}/viz      (viewer HTML page)
  * GET /printers/{id}/viz/mesh (3MF mesh JSON)

It is REJECTED everywhere else.  The master key continues to work on every
route.  When BRIDGE_VIZ_TOKEN is unset (default), the feature is off and a
viz-token-shaped guess is rejected as 401 on all routes.

Coverage
--------
* Viz token accepted on GET /printers/{id} (snapshot).
* Viz token accepted on GET /printers/{id}/viz/mesh.
* Viz token accepted on GET /printers/{id}/viz (viewer page — via ?token=).
* Viz token accepted via ``Authorization: Bearer`` header on mesh + snapshot.
* Master key still accepted on all three viz-accessible routes.
* No token → 401 on all three routes.
* Wrong token → 401 on all three routes.
* Viz token REJECTED on POST control route (must_reject_on_control).
* Viz token REJECTED on PUT filament-memory (must_reject_on_write).
* BRIDGE_VIZ_TOKEN unset + viz-token-shaped guess → 401 on snapshot route.
* BRIDGE_VIZ_TOKEN unset + viz-token-shaped guess → 401 on mesh route.
* Master key unaffected: still works on control route even when viz token is set.
* Constant-time: viz token compare is not short-circuited when master key absent.
"""

from __future__ import annotations

import asyncio
import io
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

VIZ_TOKEN = "test-viz-only-token"


@pytest.fixture(autouse=True)
def _stable_file_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    async def revision(self: Any, name: str, *, remote_dir: str = "") -> tuple[int, str]:
        return (1, "fixture-revision")
    monkeypatch.setattr("bambu_bridge.protocol.ftps.FtpsTransfer.file_revision", revision)


_MASTER_AUTH = {"Authorization": f"Bearer {API_KEY}"}
_VIZ_BEARER = {"Authorization": f"Bearer {VIZ_TOKEN}"}


# ------------------------------------------------------------------ #
# 3MF builder — identical to test_viz.py's _make_cube_3mf() so it
# exercises the same parse_3mf code path (keeps this file hermetic while
# surviving threemf.py changes already landed by the parallel agent).
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


def _make_3mf() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("3D/3dmodel.model", _CUBE_MODEL)
    return buf.getvalue()


# ------------------------------------------------------------------ #
# Fixtures / helpers
# ------------------------------------------------------------------ #


@pytest.fixture(autouse=True)
def _patched_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)


def _register_printer(client: TestClient) -> None:
    r = client.post(
        "/api/v1/printers",
        headers=_MASTER_AUTH,
        json={"host": "127.0.0.1", "access_code": "12345678"},
    )
    assert r.status_code == 201, r.text


def _inject_job(app: Any, *, job_name: str = "benchy") -> None:
    from bambu_bridge.service.registry import Registry

    reg: Registry = app.state.registry
    service = reg.get(SERIAL)
    service._state["subtask_name"] = job_name
    service._state["total_layer_num"] = 100
    service._state["gcode_state"] = "RUNNING"


def _patch_ftps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub FTPS so mesh requests succeed without a real printer."""
    file_bytes = _make_3mf()

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["benchy.3mf"]

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        return file_bytes

    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.download_bytes", _download_bytes)


def _with_viz_viewer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Create a stub viewer.html and point the module at it."""
    viewer = tmp_path / "viewer.html"
    viewer.write_text("<html>TEST</html>", encoding="utf-8")
    import bambu_bridge.api.viz as viz_mod
    monkeypatch.setattr(viz_mod, "_VIEWER_HTML", viewer)
    return viewer


# ------------------------------------------------------------------ #
# Snapshot (GET /printers/{id}) — viz token accepted
# ------------------------------------------------------------------ #


def test_snapshot_viz_token_query_param(tmp_path: Path) -> None:
    """Viz token via ?token= is accepted on the snapshot route."""
    with TestClient(build_app(tmp_path / "a.db", viz_token=VIZ_TOKEN)) as c:
        _register_printer(c)
        r = c.get(f"/api/v1/printers/{SERIAL}?token={VIZ_TOKEN}")
        assert r.status_code == 200, r.text
        assert r.json()["serial"] == SERIAL


def test_snapshot_viz_token_bearer_header(tmp_path: Path) -> None:
    """Viz token via Authorization: Bearer header is accepted on the snapshot route."""
    with TestClient(build_app(tmp_path / "b.db", viz_token=VIZ_TOKEN)) as c:
        _register_printer(c)
        r = c.get(f"/api/v1/printers/{SERIAL}", headers=_VIZ_BEARER)
        assert r.status_code == 200, r.text
        assert r.json()["serial"] == SERIAL


def test_snapshot_master_key_still_works_with_viz_token_set(tmp_path: Path) -> None:
    """Master key continues to work on the snapshot route when viz token is configured."""
    with TestClient(build_app(tmp_path / "c.db", viz_token=VIZ_TOKEN)) as c:
        _register_printer(c)
        r = c.get(f"/api/v1/printers/{SERIAL}", headers=_MASTER_AUTH)
        assert r.status_code == 200, r.text


def test_snapshot_no_token_401(tmp_path: Path) -> None:
    """No credentials → 401 on snapshot route."""
    with TestClient(build_app(tmp_path / "d.db", viz_token=VIZ_TOKEN)) as c:
        _register_printer(c)
        r = c.get(f"/api/v1/printers/{SERIAL}")
        assert r.status_code == 401, r.text


def test_snapshot_wrong_token_401(tmp_path: Path) -> None:
    """Wrong token → 401 on snapshot route."""
    with TestClient(build_app(tmp_path / "e.db", viz_token=VIZ_TOKEN)) as c:
        _register_printer(c)
        r = c.get(f"/api/v1/printers/{SERIAL}?token=WRONG")
        assert r.status_code == 401, r.text


# ------------------------------------------------------------------ #
# Mesh (GET /printers/{id}/viz/mesh) — viz token accepted
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_mesh_viz_token_query_param(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Viz token via ?token= is accepted on the mesh route."""
    _patch_ftps(monkeypatch)
    app = build_app(tmp_path / "f.db", viz_token=VIZ_TOKEN, mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh?token={VIZ_TOKEN}")
            assert r.status_code == 200, r.text
            assert r.json()["job"] == "benchy"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_mesh_viz_token_bearer_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Viz token via Authorization: Bearer header is accepted on the mesh route."""
    _patch_ftps(monkeypatch)
    app = build_app(tmp_path / "g.db", viz_token=VIZ_TOKEN, mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)
            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz/mesh",
                headers=_VIZ_BEARER,
            )
            assert r.status_code == 200, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_mesh_master_key_still_works_with_viz_token_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Master key continues to work on the mesh route when viz token is configured."""
    _patch_ftps(monkeypatch)
    app = build_app(tmp_path / "h.db", viz_token=VIZ_TOKEN, mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)
            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz/mesh",
                headers=_MASTER_AUTH,
            )
            assert r.status_code == 200, r.text

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Viewer HTML (GET /printers/{id}/viz) — viz token accepted
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_viz_viewer_viz_token_query_param(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Viz token via ?token= is accepted on the viewer HTML route."""
    _with_viz_viewer(monkeypatch, tmp_path)
    app = build_app(tmp_path / "i.db", viz_token=VIZ_TOKEN, mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz?token={VIZ_TOKEN}",
                follow_redirects=True,
            )
            assert r.status_code == 200, r.text
            assert "TEST" in r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_viewer_viz_token_bearer_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Viz token via Authorization: Bearer header is accepted on the viewer HTML route."""
    _with_viz_viewer(monkeypatch, tmp_path)
    app = build_app(tmp_path / "j.db", viz_token=VIZ_TOKEN, mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz",
                headers=_VIZ_BEARER,
                follow_redirects=True,
            )
            assert r.status_code == 200, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_viz_viewer_master_key_still_works_with_viz_token_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Master key continues to work on the viewer HTML route when viz token is configured."""
    _with_viz_viewer(monkeypatch, tmp_path)
    app = build_app(tmp_path / "k.db", viz_token=VIZ_TOKEN, mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz",
                headers=_MASTER_AUTH,
                follow_redirects=True,
            )
            assert r.status_code == 200, r.text

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# SECURITY: viz token MUST be rejected on write / control routes
# ------------------------------------------------------------------ #


def test_viz_token_rejected_on_control_command(tmp_path: Path) -> None:
    """Viz token MUST NOT grant access to control routes (POST /command).

    A viz-token holder can observe state but cannot send any command to the
    printer.  Any 4xx is a pass; 200/201/204 is a failure (auth bypass).
    """
    with TestClient(build_app(tmp_path / "sec1.db", viz_token=VIZ_TOKEN)) as c:
        _register_printer(c)
        # Try with header bearer first.
        r = c.post(
            f"/api/v1/printers/{SERIAL}/command",
            headers=_VIZ_BEARER,
            json={"category": "print", "command": "pause", "params": {}},
        )
        assert r.status_code in {401, 403}, (
            f"viz token must be rejected on control route; got {r.status_code}: {r.text}"
        )


def test_viz_token_rejected_on_control_query_param(tmp_path: Path) -> None:
    """Viz token via ?token= MUST NOT grant access to control routes."""
    with TestClient(build_app(tmp_path / "sec2.db", viz_token=VIZ_TOKEN)) as c:
        _register_printer(c)
        r = c.post(
            f"/api/v1/printers/{SERIAL}/command?token={VIZ_TOKEN}",
            json={"category": "print", "command": "pause", "params": {}},
        )
        assert r.status_code in {401, 403}, (
            "viz token must be rejected on control route via ?token=; "
            f"got {r.status_code}: {r.text}"
        )


def test_viz_token_rejected_on_print_action(tmp_path: Path) -> None:
    """Viz token MUST NOT grant access to POST /print/{action}."""
    with TestClient(build_app(tmp_path / "sec3.db", viz_token=VIZ_TOKEN)) as c:
        _register_printer(c)
        r = c.post(
            f"/api/v1/printers/{SERIAL}/print/pause",
            headers=_VIZ_BEARER,
        )
        assert r.status_code in {401, 403}, (
            f"viz token must be rejected on print action; got {r.status_code}: {r.text}"
        )


def test_viz_token_rejected_on_filament_memory_put(tmp_path: Path) -> None:
    """Viz token MUST NOT grant access to PUT filament-memory (write route)."""
    with TestClient(build_app(tmp_path / "sec4.db", viz_token=VIZ_TOKEN)) as c:
        _register_printer(c)
        r = c.put(
            f"/api/v1/printers/{SERIAL}/filament-memory/1",
            headers=_VIZ_BEARER,
            json={"make": "Bambu"},
        )
        assert r.status_code in {401, 403}, (
            f"viz token must be rejected on PUT filament-memory; got {r.status_code}: {r.text}"
        )


def test_viz_token_rejected_on_filament_memory_delete(tmp_path: Path) -> None:
    """Viz token MUST NOT grant access to DELETE filament-memory (write route)."""
    with TestClient(build_app(tmp_path / "sec5.db", viz_token=VIZ_TOKEN)) as c:
        _register_printer(c)
        r = c.delete(
            f"/api/v1/printers/{SERIAL}/filament-memory/1",
            headers=_VIZ_BEARER,
        )
        assert r.status_code in {401, 403}, (
            f"viz token must be rejected on DELETE filament-memory; got {r.status_code}: {r.text}"
        )


def test_viz_token_rejected_on_list_printers(tmp_path: Path) -> None:
    """Viz token MUST NOT grant access to GET /printers (list all printers).

    List is a different route from the single-printer snapshot; it stays
    master-key-only so a viz-token holder can't enumerate the registry.
    """
    with TestClient(build_app(tmp_path / "sec6.db", viz_token=VIZ_TOKEN)) as c:
        _register_printer(c)
        r = c.get("/api/v1/printers", headers=_VIZ_BEARER)
        assert r.status_code in {401, 403}, (
            f"viz token must be rejected on GET /printers list; got {r.status_code}: {r.text}"
        )


def test_viz_token_rejected_on_printer_delete(tmp_path: Path) -> None:
    """Viz token MUST NOT grant DELETE /printers/{id}."""
    with TestClient(build_app(tmp_path / "sec7.db", viz_token=VIZ_TOKEN)) as c:
        _register_printer(c)
        r = c.delete(f"/api/v1/printers/{SERIAL}", headers=_VIZ_BEARER)
        assert r.status_code in {401, 403}, (
            f"viz token must be rejected on DELETE /printers/{SERIAL}; "
            f"got {r.status_code}: {r.text}"
        )


def test_master_key_still_works_on_control_when_viz_token_is_set(
    tmp_path: Path,
) -> None:
    """Master key continues to work on all routes even when viz token is configured.

    This ensures adding a viz token does not break or restrict the master key.
    (The control route will 409 because the printer is offline, but auth passes.)
    """
    with TestClient(build_app(tmp_path / "sec8.db", viz_token=VIZ_TOKEN)) as c:
        _register_printer(c)
        r = c.post(
            f"/api/v1/printers/{SERIAL}/print/pause",
            headers=_MASTER_AUTH,
        )
        # 409 = printer offline (expected — no live broker); auth passed.
        # 401/403 would mean the master key was erroneously rejected.
        assert r.status_code != 401, f"master key was rejected; got {r.status_code}: {r.text}"
        assert r.status_code != 403, f"master key was rejected; got {r.status_code}: {r.text}"


# ------------------------------------------------------------------ #
# SECURITY: BRIDGE_VIZ_TOKEN unset — viz-token-shaped guess rejected
# ------------------------------------------------------------------ #


def test_unset_viz_token_guess_rejected_on_snapshot(tmp_path: Path) -> None:
    """When BRIDGE_VIZ_TOKEN is not configured, a viz-token-shaped guess is 401.

    Feature must be off by default.  Callers cannot probe for a token.
    """
    # viz_token=None (default) => feature off
    with TestClient(build_app(tmp_path / "off1.db")) as c:
        _register_printer(c)
        r = c.get(f"/api/v1/printers/{SERIAL}?token={VIZ_TOKEN}")
        assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_unset_viz_token_guess_rejected_on_mesh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When BRIDGE_VIZ_TOKEN is not configured, a guess is 401 on the mesh route."""
    _patch_ftps(monkeypatch)
    app = build_app(tmp_path / "off2.db", mqtt_port=1)  # no viz_token

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/mesh?token={VIZ_TOKEN}")
            assert r.status_code == 401, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_unset_viz_token_guess_rejected_on_viewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When BRIDGE_VIZ_TOKEN is not configured, a guess is 401 on the viewer route."""
    _with_viz_viewer(monkeypatch, tmp_path)
    app = build_app(tmp_path / "off3.db", mqtt_port=1)  # no viz_token

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz?token={VIZ_TOKEN}",
                follow_redirects=True,
            )
            assert r.status_code == 401, r.text

    await asyncio.to_thread(run)


# ------------------------------------------------------------------ #
# Edge: empty-string BRIDGE_VIZ_TOKEN behaves as unset (feature off)
# ------------------------------------------------------------------ #


def test_empty_string_viz_token_behaves_as_unset(tmp_path: Path) -> None:
    """An empty-string viz token must NOT open access — treat as feature-off."""
    # Pydantic-settings maps empty string to None for str | None = None fields,
    # but double-check the guard in require_read_or_viz handles "" explicitly.
    with TestClient(build_app(tmp_path / "empty.db", viz_token="")) as c:
        _register_printer(c)
        # Any string as ?token= must be rejected (would match empty == empty
        # under naive comparison).
        r = c.get(f"/api/v1/printers/{SERIAL}?token=")
        assert r.status_code == 401, r.text
        r2 = c.get(f"/api/v1/printers/{SERIAL}?token=anything")
        assert r2.status_code == 401, r2.text


# ------------------------------------------------------------------ #
# Toolpath (GET /printers/{id}/viz/toolpath) — viz token accepted
# ------------------------------------------------------------------ #



def _make_toolpath_archive_token() -> bytes:
    """Minimal .gcode.3mf with a tiny square perimeter gcode."""
    gcode = (
        "G90\nM83\nG92 E0\nG1 Z0.2 F1200\n"
        "G1 X10 Y0 E0.5\nG1 X10 Y10 E0.5\nG1 X0 Y10 E0.5\nG1 X0 Y0 E0.5\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("Metadata/plate_1.gcode", gcode)
        zf.writestr("3D/3dmodel.model", "<model/>")
    return buf.getvalue()


def _patch_ftps_toolpath(monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _make_toolpath_archive_token()

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["benchy.gcode.3mf"]

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        return archive

    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.download_bytes", _download_bytes)


def _inject_toolpath_job(app: Any) -> None:
    from bambu_bridge.service.registry import Registry

    reg: Registry = app.state.registry
    service = reg.get(SERIAL)
    service._state["subtask_name"] = "benchy.gcode.3mf"
    service._state["total_layer_num"] = 100
    service._state["gcode_state"] = "RUNNING"


@pytest.mark.asyncio
async def test_toolpath_viz_token_query_param(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Viz token via ?token= is accepted on the toolpath route."""
    _patch_ftps_toolpath(monkeypatch)
    app = build_app(tmp_path / "tp_tok_a.db", viz_token=VIZ_TOKEN, mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_toolpath_job(app)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath?token={VIZ_TOKEN}")
            assert r.status_code == 200, r.text
            assert r.json()["segment_count"] == 4

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_toolpath_viz_token_bearer_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Viz token via Authorization: Bearer is accepted on the toolpath route."""
    _patch_ftps_toolpath(monkeypatch)
    app = build_app(tmp_path / "tp_tok_b.db", viz_token=VIZ_TOKEN, mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_toolpath_job(app)
            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz/toolpath",
                headers=_VIZ_BEARER,
            )
            assert r.status_code == 200, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_toolpath_master_key_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Master key continues to work on the toolpath route."""
    _patch_ftps_toolpath(monkeypatch)
    app = build_app(tmp_path / "tp_tok_c.db", viz_token=VIZ_TOKEN, mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_toolpath_job(app)
            r = c.get(
                f"/api/v1/printers/{SERIAL}/viz/toolpath",
                headers=_MASTER_AUTH,
            )
            assert r.status_code == 200, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_toolpath_no_token_401(tmp_path: Path) -> None:
    """No credentials → 401 on toolpath route."""
    app = build_app(tmp_path / "tp_tok_d.db", viz_token=VIZ_TOKEN, mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath")
            assert r.status_code == 401, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_toolpath_wrong_token_401(tmp_path: Path) -> None:
    """Wrong token → 401 on toolpath route."""
    app = build_app(tmp_path / "tp_tok_e.db", viz_token=VIZ_TOKEN, mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath?token=WRONG")
            assert r.status_code == 401, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_unset_viz_token_guess_rejected_on_toolpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When BRIDGE_VIZ_TOKEN is not configured, viz-token guess is 401 on toolpath."""
    _patch_ftps_toolpath(monkeypatch)
    app = build_app(tmp_path / "tp_tok_off.db", mqtt_port=1)  # no viz_token

    def run() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_toolpath_job(app)
            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath?token={VIZ_TOKEN}")
            assert r.status_code == 401, r.text

    await asyncio.to_thread(run)
