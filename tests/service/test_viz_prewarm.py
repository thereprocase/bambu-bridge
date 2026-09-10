"""Tests for the viz cache pre-warm path (service/viz_cache.py + jobs.py wiring).

Contract invariants tested:

1. print_started triggers a warm task that fills both caches (no HTTP request).
2. Failure path: FTPS download raises → caches remain empty, no propagation.
3. Debounce: second schedule_prewarm while first task is in flight does not
   double-fetch (FTPS fetch count stays 2 for mesh+toolpath, not 4).
4. Endpoint behaviour unchanged when cache pre-warmed: GET /viz/toolpath
   returns cached=True without a second FTPS fetch.
5. fill_mesh / fill_toolpath return False (not raise) on download error.
6. fill_mesh / fill_toolpath return True and skip FTPS when already cached.
7. No viz_cache wired (viz_cache=None): JobManager works normally.
8. Inflight guard is cleared after a successful warm.
9. fill_toolpath fills the sliced-date memo (sliced .gcode.3mf has no mesh;
   fill_mesh is never called so memo must be filled in the toolpath path).
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bambu_bridge.service.viz_cache import VizCache
from tests.conftest import (
    API_KEY,
    SERIAL,
    build_app,
    patch_discovery_ok,
)

_AUTH = {"Authorization": f"Bearer {API_KEY}"}

# --------------------------------------------------------------------------- #
# Synthetic archive fixtures
# --------------------------------------------------------------------------- #

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
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2"/>
          <triangle v1="0" v2="2" v3="3"/>
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1"/></build>
</model>
"""


def _make_gcode_archive(gcode: str = _SQUARE_GCODE) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("Metadata/plate_1.gcode", gcode)
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("3D/3dmodel.model", _CUBE_MODEL)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _register_printer(client: TestClient) -> None:
    r = client.post(
        "/api/v1/printers",
        headers=_AUTH,
        json={"host": "127.0.0.1", "access_code": "12345678", "friendly_name": "T"},
    )
    assert r.status_code == 201, r.text


def _inject_job(app: Any, *, job_name: str = "benchy.gcode.3mf") -> None:
    from bambu_bridge.service.registry import Registry

    reg: Registry = app.state.registry
    service = reg.get(SERIAL)
    service._state["subtask_name"] = job_name
    service._state["gcode_state"] = "RUNNING"


# --------------------------------------------------------------------------- #
# Test 1 — schedule_prewarm fills both caches (no HTTP request needed)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_prewarm_fills_both_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """schedule_prewarm → warm task populates mesh + toolpath caches."""
    archive = _make_gcode_archive()
    fetch_count = 0

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["benchy.gcode.3mf"]

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        nonlocal fetch_count
        fetch_count += 1
        return archive

    monkeypatch.setattr("bambu_bridge.service.viz_cache.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr(
        "bambu_bridge.service.viz_cache.FtpsTransfer.download_bytes", _download_bytes
    )
    monkeypatch.setattr("bambu_bridge.service.viz_cache._PREWARM_DELAY_S", 0.0)

    vc = VizCache(ftps_port=990)
    vc.schedule_prewarm(SERIAL, "127.0.0.1", "12345678", "benchy.gcode.3mf")

    # Give the background task time to run.
    await asyncio.sleep(0.2)

    mesh_hit = vc.lookup_mesh(SERIAL, "benchy.gcode.3mf")
    tp_hit = vc.lookup_toolpath(SERIAL, "benchy.gcode.3mf")
    assert mesh_hit is not None, "mesh cache should be populated after pre-warm"
    assert tp_hit is not None, "toolpath cache should be populated after pre-warm"
    # Two fill calls — one for mesh, one for toolpath — each downloads once.
    assert fetch_count == 2, f"expected 2 FTPS fetches (mesh+toolpath), got {fetch_count}"


# --------------------------------------------------------------------------- #
# Test 2 — failure path swallowed, caches remain empty
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_prewarm_failure_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """FTPS download raises → caches stay empty, no exception propagated."""
    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["badfile.gcode.3mf"]

    async def _download_fail(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        raise OSError("FTPS refused")

    monkeypatch.setattr("bambu_bridge.service.viz_cache.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr(
        "bambu_bridge.service.viz_cache.FtpsTransfer.download_bytes", _download_fail
    )
    monkeypatch.setattr("bambu_bridge.service.viz_cache._PREWARM_DELAY_S", 0.0)
    monkeypatch.setattr("bambu_bridge.service.viz_cache._PREWARM_RETRY_S", 0.0)

    vc = VizCache(ftps_port=990)
    vc.schedule_prewarm(SERIAL, "127.0.0.1", "12345678", "badfile.gcode.3mf")

    # Allow both the initial attempt and the retry to complete.
    await asyncio.sleep(0.3)

    assert vc.lookup_mesh(SERIAL, "badfile.gcode.3mf") is None
    assert vc.lookup_toolpath(SERIAL, "badfile.gcode.3mf") is None
    # Guard key must be cleared after failure (not permanently stuck).
    assert (SERIAL, "badfile.gcode.3mf") not in vc.inflight_keys


# --------------------------------------------------------------------------- #
# Test 3 — debounce: second schedule_prewarm while in-flight does not double-fetch
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_prewarm_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two rapid schedule_prewarm calls for the same key result in one warm (2 fetches)."""
    archive = _make_gcode_archive()
    fetch_count = 0

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["benchy.gcode.3mf"]

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        nonlocal fetch_count
        fetch_count += 1
        return archive

    monkeypatch.setattr("bambu_bridge.service.viz_cache.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr(
        "bambu_bridge.service.viz_cache.FtpsTransfer.download_bytes", _download_bytes
    )
    # Non-zero delay ensures the second call arrives while the first is still
    # in _inflight (sleeping).
    monkeypatch.setattr("bambu_bridge.service.viz_cache._PREWARM_DELAY_S", 0.05)

    vc = VizCache(ftps_port=990)
    # Both calls happen before the first task wakes from its 50 ms sleep.
    vc.schedule_prewarm(SERIAL, "127.0.0.1", "12345678", "benchy.gcode.3mf")
    vc.schedule_prewarm(SERIAL, "127.0.0.1", "12345678", "benchy.gcode.3mf")

    await asyncio.sleep(0.3)

    # Only one warm ran: 2 FTPS fetches (mesh + toolpath), NOT 4.
    assert fetch_count == 2, (
        f"expected 2 FTPS fetches (one warm, mesh+toolpath), got {fetch_count}"
    )


# --------------------------------------------------------------------------- #
# Test 4 — endpoint uses pre-warmed cache (no second FTPS fetch)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_endpoint_uses_prewarmed_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /viz/toolpath returns cached=True without an FTPS fetch when pre-warmed.

    Uses a single TestClient context so the VizCache populated by fill_mesh /
    fill_toolpath is the same object the endpoint reads.  fill_* is called via
    run_coroutine_threadsafe so we can await it from inside the to_thread body.
    """
    archive = _make_gcode_archive()
    fetch_count = 0

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["benchy.gcode.3mf"]

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        nonlocal fetch_count
        fetch_count += 1
        return archive

    monkeypatch.setattr("bambu_bridge.service.viz_cache.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr(
        "bambu_bridge.service.viz_cache.FtpsTransfer.download_bytes", _download_bytes
    )
    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr("bambu_bridge.api.viz.FtpsTransfer.download_bytes", _download_bytes)
    patch_discovery_ok(monkeypatch, serial=SERIAL)

    app = build_app(tmp_path / "prewarm_ep.db", mqtt_port=1)
    loop = asyncio.get_running_loop()
    check_results: dict[str, Any] = {}

    async def _fill(vc: VizCache) -> None:
        await vc.fill_mesh(SERIAL, "127.0.0.1", "12345678", "benchy.gcode.3mf")
        await vc.fill_toolpath(SERIAL, "127.0.0.1", "12345678", "benchy.gcode.3mf")

    def run_all() -> None:
        with TestClient(app) as c:
            _register_printer(c)
            _inject_job(app, job_name="benchy.gcode.3mf")

            # Pre-warm via the service-layer fill functions (same as schedule_prewarm).
            vc: VizCache = app.state.viz_cache_obj
            asyncio.run_coroutine_threadsafe(_fill(vc), loop).result(timeout=10)

            fetch_before = fetch_count

            r = c.get(f"/api/v1/printers/{SERIAL}/viz/toolpath", headers=_AUTH)
            check_results["status"] = r.status_code
            check_results["cached"] = r.json().get("cached")
            check_results["fetch_before"] = fetch_before
            check_results["fetch_after"] = fetch_count

    await asyncio.to_thread(run_all)

    assert check_results["status"] == 200, f"endpoint returned {check_results['status']}"
    assert check_results["cached"] is True, "endpoint should return cached=True"
    assert check_results["fetch_after"] == check_results["fetch_before"], (
        f"endpoint triggered extra FTPS fetch: {check_results}"
    )


# --------------------------------------------------------------------------- #
# Test 5 — fill_mesh / fill_toolpath return False on download error, not raise
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fill_returns_false_on_download_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fill_mesh and fill_toolpath return False (not raise) when download fails."""
    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["x.gcode.3mf"]

    async def _fail_download(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        raise ConnectionError("refused")

    monkeypatch.setattr("bambu_bridge.service.viz_cache.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr(
        "bambu_bridge.service.viz_cache.FtpsTransfer.download_bytes", _fail_download
    )

    vc = VizCache(ftps_port=990)
    ok_mesh = await vc.fill_mesh(SERIAL, "127.0.0.1", "code", "x.gcode.3mf")
    ok_tp = await vc.fill_toolpath(SERIAL, "127.0.0.1", "code", "x.gcode.3mf")

    assert ok_mesh is False, "fill_mesh must return False on download error"
    assert ok_tp is False, "fill_toolpath must return False on download error"


# --------------------------------------------------------------------------- #
# Test 6 — fill_* return True immediately when cache already populated
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fill_skips_download_when_already_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fill_mesh / fill_toolpath skip FTPS when the filename is already cached."""
    archive = _make_gcode_archive()
    fetch_count = 0

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["cached.gcode.3mf"]

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        nonlocal fetch_count
        fetch_count += 1
        return archive

    monkeypatch.setattr("bambu_bridge.service.viz_cache.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr(
        "bambu_bridge.service.viz_cache.FtpsTransfer.download_bytes", _download_bytes
    )

    vc = VizCache(ftps_port=990)

    # Prime the cache.
    ok1_mesh = await vc.fill_mesh(SERIAL, "127.0.0.1", "code", "cached.gcode.3mf")
    ok1_tp = await vc.fill_toolpath(SERIAL, "127.0.0.1", "code", "cached.gcode.3mf")
    assert ok1_mesh is True
    assert ok1_tp is True
    assert fetch_count == 2  # one download per fill path

    # Second fill — must skip FTPS.
    ok2_mesh = await vc.fill_mesh(SERIAL, "127.0.0.1", "code", "cached.gcode.3mf")
    ok2_tp = await vc.fill_toolpath(SERIAL, "127.0.0.1", "code", "cached.gcode.3mf")
    assert ok2_mesh is True
    assert ok2_tp is True
    assert fetch_count == 2, "no additional FTPS fetches when already cached"


# --------------------------------------------------------------------------- #
# Test 7 — no viz_cache wired: JobManager works normally (no crash)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_job_manager_works_without_viz_cache(tmp_path: Path) -> None:
    """JobManager with viz_cache=None does not crash on print_started."""
    from bambu_bridge.db.jobs import Database, EventRepo, JobRepo
    from bambu_bridge.service.events import Event, EventBus
    from bambu_bridge.service.jobs import JobManager

    class _FakeRegistry:
        def get(self, serial: str) -> None:  # noqa: ARG002
            raise RuntimeError("should not be called")

    class _FakePrinterService:
        serial = SERIAL
        bus = EventBus()

    db = Database(str(tmp_path / "noviz.db"))
    await db.connect()
    manager = JobManager(
        JobRepo(db),
        EventRepo(db),
        _FakeRegistry(),  # type: ignore[arg-type]
        viz_cache=None,
    )
    svc = _FakePrinterService()
    await manager.attach(svc)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    # Fire print_started — must not raise.
    svc.bus.publish(
        Event("event", {"subtask_name": "test.gcode.3mf"}, name="print_started")
    )
    await asyncio.sleep(0.1)

    await manager.shutdown()
    await db.close()


# --------------------------------------------------------------------------- #
# Test 8 — inflight guard cleared after successful warm
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_inflight_guard_cleared_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a successful pre-warm the inflight guard is removed."""
    archive = _make_gcode_archive()

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["x.gcode.3mf"]

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        return archive

    monkeypatch.setattr("bambu_bridge.service.viz_cache.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr(
        "bambu_bridge.service.viz_cache.FtpsTransfer.download_bytes", _download_bytes
    )
    monkeypatch.setattr("bambu_bridge.service.viz_cache._PREWARM_DELAY_S", 0.0)

    vc = VizCache(ftps_port=990)
    vc.schedule_prewarm(SERIAL, "127.0.0.1", "code", "x.gcode.3mf")
    # Guard key is in-flight immediately.
    assert (SERIAL, "x.gcode.3mf") in vc.inflight_keys

    # Wait for completion.
    await asyncio.sleep(0.2)
    assert (SERIAL, "x.gcode.3mf") not in vc.inflight_keys


# --------------------------------------------------------------------------- #
# Test 9 — fill_toolpath fills the sliced-date memo
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fill_toolpath_fills_sliced_date_memo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fill_toolpath writes the sliced timestamp to the SlicedDateMemo.

    Sliced Bambu .gcode.3mf files carry no mesh geometry; the viewer hits
    /viz/toolpath directly and fill_mesh is never called for them.  The
    sliced-date memo must therefore be populated from the toolpath download
    path so the file-listing sort order is correct after a cold start.
    """
    from datetime import datetime

    from bambu_bridge.protocol.ftps import SlicedDateMemo

    archive = _make_gcode_archive()

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:  # noqa: ARG001
        return ["cold.gcode.3mf"]

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = ""  # noqa: ARG001
    ) -> bytes:
        return archive

    monkeypatch.setattr("bambu_bridge.service.viz_cache.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr(
        "bambu_bridge.service.viz_cache.FtpsTransfer.download_bytes", _download_bytes
    )

    # Use a real SlicedDateMemo (no DB repo) so aput writes to the LRU.
    memo = SlicedDateMemo()

    vc = VizCache(ftps_port=990, sliced_date_memo=memo)
    ok = await vc.fill_toolpath(SERIAL, "127.0.0.1", "code", "cold.gcode.3mf")

    assert ok is True, "fill_toolpath must succeed"
    # The archive has a valid ZIP entry date_time (year ≥ 2020 because it was
    # just created).  extract_sliced_at_from_bytes should find it.
    sliced_at = memo.get("cold.gcode.3mf", len(archive))
    assert sliced_at is not None, (
        "fill_toolpath must populate the sliced-date memo so file-listing "
        "sort order is correct even when fill_mesh was never called"
    )
    assert isinstance(sliced_at, datetime), f"expected datetime, got {type(sliced_at)}"


# --------------------------------------------------------------------------- #
# Test 10 — VizCache LRU eviction (cap of 3, oldest evicted, re-insert no-grow)
# --------------------------------------------------------------------------- #


def _toolpath_stub() -> Any:
    """A minimal GcodeToolpath value for cache-mechanics tests."""
    import array

    from bambu_bridge.protocol.gcode_path import GcodeToolpath

    return GcodeToolpath(
        positions=array.array("f"),
        segment_count=0,
        bbox_min=[0.0, 0.0, 0.0],
        bbox_max=[0.0, 0.0, 0.0],
    )


def test_viz_cache_lru_eviction() -> None:
    """The toolpath cache holds at most _CACHE_MAX (3) entries, oldest-evicted.

    Inserting four distinct keys leaves exactly three; the first-inserted key
    is the one evicted.  Re-inserting an existing key must NOT grow the cache
    (it refreshes recency in place) — guarding the dict-ordered LRU semantics.
    """
    from bambu_bridge.service.viz_cache import _CACHE_MAX

    assert _CACHE_MAX == 3, "test assumes a cap of 3"

    vc = VizCache(ftps_port=990)
    keys = [(SERIAL, f"f{i}.gcode.3mf", 100 + i) for i in range(4)]

    for k in keys:
        vc.put_toolpath(k, _toolpath_stub())

    # Cap honoured: only the last three keys remain.
    assert len(vc.toolpath_cache) == _CACHE_MAX, (
        f"cache must cap at {_CACHE_MAX}, got {len(vc.toolpath_cache)}"
    )
    # Oldest (first-inserted) key was evicted.
    assert keys[0] not in vc.toolpath_cache, "oldest key must be evicted"
    for k in keys[1:]:
        assert k in vc.toolpath_cache, f"recent key {k} must survive"

    # Re-inserting an already-present key does not grow the cache.
    before = len(vc.toolpath_cache)
    vc.put_toolpath(keys[1], _toolpath_stub())
    assert len(vc.toolpath_cache) == before, (
        "re-inserting an existing key must not grow the cache"
    )
    # And it refreshes recency: keys[1] is now most-recent, so the next NEW
    # insert evicts keys[2] (now the oldest), not keys[1].
    new_key = (SERIAL, "f_new.gcode.3mf", 999)
    vc.put_toolpath(new_key, _toolpath_stub())
    assert len(vc.toolpath_cache) == _CACHE_MAX
    assert keys[2] not in vc.toolpath_cache, (
        "re-inserted key must have refreshed recency so keys[2] evicts first"
    )
    assert keys[1] in vc.toolpath_cache, "refreshed key must survive the next eviction"


# --------------------------------------------------------------------------- #
# Test 11 — find_3mf falls back from persistent root to /cache
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fill_toolpath_find_3mf_cache_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fill_toolpath succeeds when the file is only in /cache, not the root.

    find_3mf searches the persistent root first, then /cache.  When the
    persistent listing is empty but the cache listing has the file, the fill
    must still locate, download, and parse it (the staged-in-cache path that
    the P1S uses for the currently-running job).
    """
    from bambu_bridge.protocol.ftps import UPLOAD_DIR_CACHE, UPLOAD_DIR_PERSISTENT

    archive = _make_gcode_archive()
    downloaded_from: list[str] = []

    async def _list_dir(self: Any, remote_dir: str) -> list[str]:
        # Persistent root is empty; the file lives only in /cache.
        if remote_dir == UPLOAD_DIR_CACHE:
            return ["staged.gcode.3mf"]
        return []

    async def _download_bytes(
        self: Any, name: str, *, remote_dir: str = UPLOAD_DIR_PERSISTENT
    ) -> bytes:
        downloaded_from.append(remote_dir)
        return archive

    monkeypatch.setattr("bambu_bridge.service.viz_cache.FtpsTransfer.list_dir", _list_dir)
    monkeypatch.setattr(
        "bambu_bridge.service.viz_cache.FtpsTransfer.download_bytes", _download_bytes
    )

    vc = VizCache(ftps_port=990)
    ok = await vc.fill_toolpath(SERIAL, "127.0.0.1", "code", "staged.gcode.3mf")

    assert ok is True, "fill_toolpath must succeed via the /cache fallback"
    assert vc.lookup_toolpath(SERIAL, "staged.gcode.3mf") is not None, (
        "toolpath cache must be populated from the /cache fallback"
    )
    # The download must have targeted /cache, not the (empty) persistent root.
    assert downloaded_from == [UPLOAD_DIR_CACHE], (
        f"download must target /cache, got {downloaded_from!r}"
    )


@pytest.fixture(autouse=True)
def _stable_file_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    async def revision(self: Any, name: str, *, remote_dir: str = "") -> tuple[int, str]:
        return (1, "fixture-revision")
    monkeypatch.setattr("bambu_bridge.protocol.ftps.FtpsTransfer.file_revision", revision)
