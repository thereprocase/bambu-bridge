"""M5: camera HTTP endpoints — snapshot + MJPEG stream, media auth."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bambu_bridge.api.camera import camera_stream, mjpeg_part
from bambu_bridge.db.jobs import Database, PrinterRepo
from bambu_bridge.service.registry import Registry
from tests.conftest import (
    ACCESS_CODE,
    API_KEY,
    SERIAL,
    FakeCamera,
    build_app,
    patch_discovery_ok,
)

_AUTH = {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture(autouse=True)
def _patched_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)


def _register(c: TestClient) -> None:
    assert (
        c.post(
            "/api/v1/printers",
            headers=_AUTH,
            json={
                "host": "127.0.0.1",
                "access_code": ACCESS_CODE,
                "friendly_name": "Cam",
            },
        ).status_code
        == 201
    )


@pytest.mark.asyncio
async def test_snapshot_returns_jpeg(
    tmp_path: Path, fake_camera: FakeCamera
) -> None:
    app = build_app(tmp_path / "cam.db", mqtt_port=1, camera_port=fake_camera.port)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            # token via query (browser <img> can't set a header)
            r = c.get(f"/api/v1/printers/{SERIAL}/camera/snapshot.jpg?token={API_KEY}")
            assert r.status_code == 200, r.text
            assert r.headers["content-type"] == "image/jpeg"
            assert r.content.startswith(b"\xff\xd8")

    await asyncio.to_thread(run)


def test_mjpeg_part_framing() -> None:
    part = mjpeg_part(b"\xff\xd8JPEG\xff\xd9")
    assert part.startswith(b"--frame\r\n")
    assert b"Content-Type: image/jpeg\r\n" in part
    assert b"Content-Length: 8\r\n\r\n" in part
    assert part.endswith(b"\xff\xd8JPEG\xff\xd9\r\n")


@pytest.mark.asyncio
async def test_stream_endpoint_yields_framed_jpegs(
    database: Database, fake_camera: FakeCamera
) -> None:
    """Drive the endpoint's StreamingResponse generator directly.

    The Starlette TestClient can't cleanly tear down an infinite streaming
    response, so we exercise the real endpoint + multiplexer without an HTTP
    transport: build a registry, call the endpoint, consume one part, then
    aclose() the body iterator (== client disconnect).
    """
    registry = Registry(PrinterRepo(database), camera_port=fake_camera.port)
    await registry.add(
        serial=SERIAL,
        ip="127.0.0.1",
        access_code=ACCESS_CODE,
        friendly_name="Cam",
    )
    try:
        resp = await camera_stream(SERIAL, registry)
        assert resp.media_type == "multipart/x-mixed-replace; boundary=frame"
        body = resp.body_iterator
        try:
            async with asyncio.timeout(5):
                part = await body.__anext__()
            assert part.startswith(b"--frame\r\n")
            assert b"Content-Type: image/jpeg" in part
            assert b"\xff\xd8" in part and part.endswith(b"\r\n")
        finally:
            await body.aclose()  # == browser tab closed
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_camera_auth_and_404(tmp_path: Path) -> None:
    app = build_app(tmp_path / "cam3.db")

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            assert (
                c.get(f"/api/v1/printers/{SERIAL}/camera/snapshot.jpg").status_code
                == 401
            )
            assert (
                c.get(
                    "/api/v1/printers/none/camera/snapshot.jpg", headers=_AUTH
                ).status_code
                == 404
            )

    await asyncio.to_thread(run)
