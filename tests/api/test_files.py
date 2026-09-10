"""M4: FTPS-backed file endpoints (spec 6 "Files")."""

from __future__ import annotations

import asyncio
from pathlib import Path

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


@pytest.fixture(autouse=True)
def _patched_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)


@pytest.mark.asyncio
async def test_upload_list_delete(
    tmp_path: Path, ftps_server: tuple[int, Path]
) -> None:
    port, storage = ftps_server
    app = build_app(tmp_path / "files.db", mqtt_port=1, ftps_port=port)

    def run() -> None:
        with TestClient(app) as c:
            c.post(
                "/api/v1/printers",
                headers=_AUTH,
                json={
                    "host": "127.0.0.1",
                    "access_code": ACCESS_CODE,
                    "friendly_name": "F",
                },
            )
            up = c.post(
                f"/api/v1/printers/{SERIAL}/files",
                headers=_AUTH,
                files={"file": ("cube.3mf", b"3MF-BYTES", "application/octet-stream")},
            )
            assert up.status_code == 201, up.text
            # Root upload: P1S stores files at "/" not "/model/"
            assert up.json()["path"] == "/cube.3mf"

            listed = c.get(f"/api/v1/printers/{SERIAL}/files", headers=_AUTH)
            assert "cube.3mf" in listed.json()["file_names"]

            d = c.delete(f"/api/v1/printers/{SERIAL}/files/cube.3mf", headers=_AUTH)
            assert d.status_code == 200

    await asyncio.to_thread(run)
    # File was deleted; should not exist at the FTPS root (= storage root)
    assert not (storage / "cube.3mf").exists()


@pytest.mark.asyncio
async def test_upload_then_download_roundtrip(
    tmp_path: Path, ftps_server: tuple[int, Path]
) -> None:
    port, _ = ftps_server
    app = build_app(tmp_path / "dl.db", mqtt_port=1, ftps_port=port)
    blob = b"BENCHY-3MF-" + bytes(range(256))

    def run() -> None:
        with TestClient(app) as c:
            c.post(
                "/api/v1/printers",
                headers=_AUTH,
                json={
                    "host": "127.0.0.1",
                    "access_code": ACCESS_CODE,
                    "friendly_name": "F",
                },
            )
            assert (
                c.post(
                    f"/api/v1/printers/{SERIAL}/files",
                    headers=_AUTH,
                    files={"file": ("b.3mf", blob, "application/octet-stream")},
                ).status_code
                == 201
            )
            got = c.get(
                f"/api/v1/printers/{SERIAL}/files/b.3mf", headers=_AUTH
            )
            assert got.status_code == 200, got.text
            assert got.content == blob
            assert "attachment" in got.headers["content-disposition"]

            # cache dir is selectable; an unknown dir is rejected
            assert (
                c.get(
                    f"/api/v1/printers/{SERIAL}/files?dir=cache", headers=_AUTH
                ).json()["dir"]
                == "cache"
            )
            assert (
                c.get(
                    f"/api/v1/printers/{SERIAL}/files?dir=../etc",
                    headers=_AUTH,
                ).status_code
                == 422
            )

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_delete_with_dir_targets_same_file_as_download(
    tmp_path: Path, ftps_server: tuple[int, Path]
) -> None:
    """DELETE ?dir=cache and GET ?dir=cache address the same /cache file.

    Regression for the asymmetry where delete_file ignored ``dir`` and always
    removed from the FTPS root, so a DELETE of a /cache file silently hit root
    (and left the real /cache file in place).  We stage a file ONLY in /cache,
    confirm a download with ?dir=cache reads it, then DELETE with ?dir=cache
    and confirm the /cache file is gone while a same-named root file (if any)
    is untouched.
    """
    port, storage = ftps_server
    app = build_app(tmp_path / "del_dir.db", mqtt_port=1, ftps_port=port)

    # Stage a file ONLY under /cache, plus a same-named decoy at the root so a
    # root-targeted delete would be caught (it must NOT be removed).
    (storage / "cache" / "staged.3mf").write_bytes(b"CACHE-COPY")
    (storage / "staged.3mf").write_bytes(b"ROOT-DECOY")

    def run() -> None:
        with TestClient(app) as c:
            c.post(
                "/api/v1/printers",
                headers=_AUTH,
                json={
                    "host": "127.0.0.1",
                    "access_code": ACCESS_CODE,
                    "friendly_name": "F",
                },
            )

            # Download with ?dir=cache must read the /cache copy.
            got = c.get(
                f"/api/v1/printers/{SERIAL}/files/staged.3mf?dir=cache",
                headers=_AUTH,
            )
            assert got.status_code == 200, got.text
            assert got.content == b"CACHE-COPY", (
                "download ?dir=cache must read the /cache file, not the root decoy"
            )

            # Delete with ?dir=cache must remove the SAME /cache file.
            d = c.delete(
                f"/api/v1/printers/{SERIAL}/files/staged.3mf?dir=cache",
                headers=_AUTH,
            )
            assert d.status_code == 200, d.text
            assert d.json()["deleted"] == "staged.3mf"

            # An unknown dir is still rejected on delete (mirrors download).
            bad = c.delete(
                f"/api/v1/printers/{SERIAL}/files/staged.3mf?dir=../etc",
                headers=_AUTH,
            )
            assert bad.status_code == 422, bad.text

    await asyncio.to_thread(run)

    # The /cache file is gone; the root decoy must remain (delete honoured dir).
    assert not (storage / "cache" / "staged.3mf").exists(), (
        "delete ?dir=cache must remove the /cache file"
    )
    assert (storage / "staged.3mf").exists(), (
        "delete ?dir=cache must NOT touch the same-named root file"
    )


@pytest.mark.asyncio
async def test_files_unknown_printer_404(tmp_path: Path) -> None:
    app = build_app(tmp_path / "f404.db")

    def run() -> None:
        with TestClient(app) as c:
            r = c.get("/api/v1/printers/nope/files", headers=_AUTH)
            assert r.status_code == 404

    await asyncio.to_thread(run)
