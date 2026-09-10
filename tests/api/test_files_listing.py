"""API-level tests for the enriched file-listing endpoint.

Tests:
* Enriched response shape: files is a list of objects with timestamp fields.
* Backward-compat: file_names flat list is still present.
* Sort ordering: newest-first when timestamps are available.
* Opportunistic sliced-date memo is consulted when populated.
* sort_basis field reflects which tier was used.
* Files without any timestamp get sort_basis=="none" and appear last.
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    ACCESS_CODE,
    API_KEY,
    SERIAL,
    build_app,
    patch_discovery_ok,
)

_AUTH = {"Authorization": f"Bearer {API_KEY}"}


def _dt(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _make_3mf_with_zip_timestamp(
    slice_info_time: tuple[int, int, int, int, int, int] | None = None,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info_3d = zipfile.ZipInfo("3D/3dmodel.model")
        info_3d.date_time = (2020, 1, 1, 0, 0, 0)
        zf.writestr(
            info_3d,
            '<?xml version="1.0"?><model xmlns="http://schemas.microsoft.com/'
            '3dmanufacturing/core/2015/02"><resources/><build/></model>',
        )
        if slice_info_time is not None:
            info_si = zipfile.ZipInfo("Metadata/slice_info.config")
            info_si.date_time = slice_info_time
            zf.writestr(info_si, "<config/>")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _patched_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)


@pytest.mark.asyncio
async def test_listing_returns_enriched_objects(
    tmp_path: Path, ftps_server: tuple[int, Path]
) -> None:
    """The files endpoint returns a list of objects with name + timestamp fields."""
    port, storage = ftps_server
    app = build_app(tmp_path / "enr.db", mqtt_port=1, ftps_port=port)

    (storage / "a.gcode.3mf").write_bytes(b"A")

    def run() -> None:
        with TestClient(app) as c:
            c.post(
                "/api/v1/printers",
                headers=_AUTH,
                json={"host": "127.0.0.1", "access_code": ACCESS_CODE, "friendly_name": "F"},
            )
            r = c.get(f"/api/v1/printers/{SERIAL}/files", headers=_AUTH)
            assert r.status_code == 200
            data = r.json()
            # files is a list of objects.
            assert isinstance(data["files"], list)
            # At least one entry.
            file_entry = next(e for e in data["files"] if e["name"] == "a.gcode.3mf")
            # Required fields present.
            assert "name" in file_entry
            assert "sliced_at" in file_entry
            assert "modified_at" in file_entry
            assert "created_at" in file_entry
            assert "sort_basis" in file_entry

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_listing_backward_compat_file_names(
    tmp_path: Path, ftps_server: tuple[int, Path]
) -> None:
    """The legacy flat file_names list is still present in the response."""
    port, storage = ftps_server
    app = build_app(tmp_path / "bc.db", mqtt_port=1, ftps_port=port)
    (storage / "compat.gcode.3mf").write_bytes(b"C")

    def run() -> None:
        with TestClient(app) as c:
            c.post(
                "/api/v1/printers",
                headers=_AUTH,
                json={"host": "127.0.0.1", "access_code": ACCESS_CODE, "friendly_name": "F"},
            )
            r = c.get(f"/api/v1/printers/{SERIAL}/files", headers=_AUTH)
            assert r.status_code == 200
            data = r.json()
            assert "file_names" in data
            assert "compat.gcode.3mf" in data["file_names"]

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_listing_sorted_newest_first_by_modified(
    tmp_path: Path, ftps_server: tuple[int, Path]
) -> None:
    """Files are returned sorted newest-first when mtime is available."""
    port, storage = ftps_server
    app = build_app(tmp_path / "sorted.db", mqtt_port=1, ftps_port=port)

    # Write three files and manipulate their mtimes so we have a known order.
    for fname, mtime in [
        ("older.gcode.3mf", 1_000_000),
        ("newest.gcode.3mf", 3_000_000),
        ("middle.gcode.3mf", 2_000_000),
    ]:
        p = storage / fname
        p.write_bytes(b"X")
        import os
        os.utime(str(p), (mtime, mtime))

    def run() -> None:
        with TestClient(app) as c:
            c.post(
                "/api/v1/printers",
                headers=_AUTH,
                json={"host": "127.0.0.1", "access_code": ACCESS_CODE, "friendly_name": "F"},
            )
            r = c.get(f"/api/v1/printers/{SERIAL}/files", headers=_AUTH)
            assert r.status_code == 200
            names = r.json()["file_names"]
            # Filter to only the files we seeded (the storage dir may have
            # directories like 'model', 'cache').
            seeded = [n for n in names if n.endswith(".gcode.3mf")]
            assert seeded[0] == "newest.gcode.3mf"
            assert seeded[1] == "middle.gcode.3mf"
            assert seeded[2] == "older.gcode.3mf"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_listing_sliced_at_from_memo(
    tmp_path: Path, ftps_server: tuple[int, Path]
) -> None:
    """sliced_at is returned from the memo when the bridge has cached it."""
    port, storage = ftps_server
    app = build_app(tmp_path / "memo.db", mqtt_port=1, ftps_port=port)

    # Seed the file.
    data = _make_3mf_with_zip_timestamp(slice_info_time=(2026, 5, 19, 7, 37, 44))
    (storage / "known.gcode.3mf").write_bytes(data)

    def run() -> None:
        with TestClient(app) as c:
            c.post(
                "/api/v1/printers",
                headers=_AUTH,
                json={"host": "127.0.0.1", "access_code": ACCESS_CODE, "friendly_name": "F"},
            )
            # Manually populate the sliced-date memo on app.state.
            sliced_at = datetime(2026, 5, 19, 7, 37, 44, tzinfo=UTC)
            app.state.sliced_date_memo.put(
                "known.gcode.3mf", len(data), sliced_at
            )

            r = c.get(f"/api/v1/printers/{SERIAL}/files", headers=_AUTH)
            assert r.status_code == 200
            file_entry = next(
                e for e in r.json()["files"] if e["name"] == "known.gcode.3mf"
            )
            assert file_entry["sliced_at"] == "2026-05-19T07:37:44Z"
            assert file_entry["sort_basis"] == "sliced"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_listing_undated_files_sort_last(
    tmp_path: Path, ftps_server: tuple[int, Path]
) -> None:
    """When MLSD/LIST returns timestamps, undated files appear after dated ones."""
    port, storage = ftps_server
    app = build_app(tmp_path / "undated.db", mqtt_port=1, ftps_port=port)

    # We can't easily create a file with no mtime via the filesystem — instead
    # we mock the list_dir_with_timestamps to return a mix.
    from bambu_bridge.protocol import ftps as ftps_mod
    from bambu_bridge.protocol.ftps import FileEntry

    async def _mock_listing(self: ftps_mod.FtpsTransfer, remote_dir: str = "") -> list[FileEntry]:  # noqa: ARG001
        return [
            FileEntry("undated.gcode.3mf"),
            FileEntry("dated.gcode.3mf", modified_at=_dt(2026, 6, 1)),
        ]

    def run() -> None:
        with TestClient(app) as c:
            c.post(
                "/api/v1/printers",
                headers=_AUTH,
                json={"host": "127.0.0.1", "access_code": ACCESS_CODE, "friendly_name": "F"},
            )
            with patch.object(
                ftps_mod.FtpsTransfer,
                "list_dir_with_timestamps",
                _mock_listing,
            ):
                r = c.get(f"/api/v1/printers/{SERIAL}/files", headers=_AUTH)
            assert r.status_code == 200
            names = r.json()["file_names"]
            assert names[0] == "dated.gcode.3mf"
            assert names[1] == "undated.gcode.3mf"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_listing_sort_basis_none_for_undated(
    tmp_path: Path, ftps_server: tuple[int, Path]
) -> None:
    """sort_basis is 'none' for files with no available timestamp."""
    port, storage = ftps_server
    app = build_app(tmp_path / "sbasis.db", mqtt_port=1, ftps_port=port)

    from bambu_bridge.protocol import ftps as ftps_mod
    from bambu_bridge.protocol.ftps import FileEntry

    async def _mock_listing(self: ftps_mod.FtpsTransfer, remote_dir: str = "") -> list[FileEntry]:  # noqa: ARG001
        return [FileEntry("no_ts.gcode.3mf")]

    def run() -> None:
        with TestClient(app) as c:
            c.post(
                "/api/v1/printers",
                headers=_AUTH,
                json={"host": "127.0.0.1", "access_code": ACCESS_CODE, "friendly_name": "F"},
            )
            with patch.object(
                ftps_mod.FtpsTransfer,
                "list_dir_with_timestamps",
                _mock_listing,
            ):
                r = c.get(f"/api/v1/printers/{SERIAL}/files", headers=_AUTH)
            assert r.status_code == 200
            file_entry = r.json()["files"][0]
            assert file_entry["sort_basis"] == "none"
            assert file_entry["sliced_at"] is None
            assert file_entry["modified_at"] is None
            assert file_entry["created_at"] is None

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_delete_default_dir_targets_root(
    tmp_path: Path, ftps_server: tuple[int, Path]
) -> None:
    """DELETE without ?dir removes from the persistent root (download symmetry).

    Complements the /cache delete test in test_files.py: the default directory
    must address the same root file that a default download would read, and an
    unknown dir is rejected with 422 just like download.
    """
    port, storage = ftps_server
    app = build_app(tmp_path / "del_root.db", mqtt_port=1, ftps_port=port)

    (storage / "root_only.3mf").write_bytes(b"ROOT-BYTES")

    def run() -> None:
        with TestClient(app) as c:
            c.post(
                "/api/v1/printers",
                headers=_AUTH,
                json={"host": "127.0.0.1", "access_code": ACCESS_CODE, "friendly_name": "F"},
            )

            # Default download reads the root file.
            got = c.get(
                f"/api/v1/printers/{SERIAL}/files/root_only.3mf", headers=_AUTH
            )
            assert got.status_code == 200, got.text
            assert got.content == b"ROOT-BYTES"

            # Default delete removes that same root file.
            d = c.delete(
                f"/api/v1/printers/{SERIAL}/files/root_only.3mf", headers=_AUTH
            )
            assert d.status_code == 200, d.text

            # Unknown dir rejected, mirroring download validation.
            bad = c.delete(
                f"/api/v1/printers/{SERIAL}/files/root_only.3mf?dir=model",
                headers=_AUTH,
            )
            assert bad.status_code == 422, bad.text

    await asyncio.to_thread(run)

    assert not (storage / "root_only.3mf").exists(), (
        "default delete must remove the root file"
    )


@pytest.mark.asyncio
async def test_listing_dir_field_preserved(
    tmp_path: Path, ftps_server: tuple[int, Path]
) -> None:
    """The 'dir' field in the response matches the requested directory."""
    port, storage = ftps_server
    app = build_app(tmp_path / "dirfield.db", mqtt_port=1, ftps_port=port)

    def run() -> None:
        with TestClient(app) as c:
            c.post(
                "/api/v1/printers",
                headers=_AUTH,
                json={"host": "127.0.0.1", "access_code": ACCESS_CODE, "friendly_name": "F"},
            )
            r = c.get(f"/api/v1/printers/{SERIAL}/files?dir=cache", headers=_AUTH)
            assert r.status_code == 200
            assert r.json()["dir"] == "cache"

    await asyncio.to_thread(run)
