"""Regression coverage for stalled cameras and long-running viewers."""

from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bambu_bridge.protocol import camera
from tests.conftest import ACCESS_CODE, FakeCamera


async def test_live_view_gets_linger_without_snapshot_polling(fake_camera: FakeCamera) -> None:
    stream = camera.CameraStream("127.0.0.1", ACCESS_CODE, port=fake_camera.port, linger_s=0.3)
    try:
        async with stream.subscribe() as queue:
            await asyncio.wait_for(queue.get(), 2)
        await asyncio.sleep(0.05)
        assert stream.upstream_active
        async with stream.subscribe() as queue:
            await asyncio.wait_for(queue.get(), 2)
        assert fake_camera.total == 1
    finally:
        await stream.aclose()


def test_cached_snapshot_expires_without_new_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(camera, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    stream = camera.CameraStream("127.0.0.1", ACCESS_CODE)
    stream._on_frame(b"\xff\xd8JPEG\xff\xd9")
    assert stream.latest() is not None
    clock[0] += camera._FRAME_FRESH_S + 1
    assert stream.latest() is None


async def test_reconnect_survives_more_than_1024_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    delays: list[float] = []

    async def fail(client: camera.CameraClient) -> None:
        nonlocal attempts
        attempts += 1
        if attempts > 1100:
            client.stop()
            return
        raise ConnectionRefusedError("fixture offline")

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(camera.CameraClient, "_session", fail)
    monkeypatch.setattr(camera.asyncio, "sleep", sleep)
    client = camera.CameraClient("127.0.0.1", ACCESS_CODE, on_frame=lambda _: None)
    client._log = Mock()
    await client.run()
    assert attempts == 1101
    assert len(delays) == 1100
    assert max(delays) <= camera._BACKOFF_CAP


async def test_new_viewer_recovers_an_unexpectedly_failed_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def run(client: camera.CameraClient) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("isolated unexpected upstream failure")
        client._on_frame(b"\xff\xd8recovered\xff\xd9")
        await asyncio.Event().wait()

    monkeypatch.setattr(camera.CameraClient, "run", run)
    stream = camera.CameraStream("127.0.0.1", ACCESS_CODE)
    try:
        async with stream.subscribe():
            await asyncio.sleep(0)
            assert not stream.upstream_active
            async with stream.subscribe() as queue:
                assert await asyncio.wait_for(queue.get(), 1) == b"\xff\xd8recovered\xff\xd9"
        assert attempts == 2
    finally:
        await stream.aclose()


@pytest.mark.parametrize("partial", [b"", struct.pack("<IIII", 100, 0, 1, 0) + b"\xff\xd8"])
async def test_silent_or_partial_frame_reconnects(
    monkeypatch: pytest.MonkeyPatch, partial: bytes
) -> None:
    attempts = 0
    received = asyncio.Event()
    jpeg = b"\xff\xd8recovered\xff\xd9"

    class Writer:
        def write(self, data: bytes) -> None:
            assert len(data) == 80

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    async def connect(*args: object, **kwargs: object) -> tuple[asyncio.StreamReader, Writer]:
        nonlocal attempts
        attempts += 1
        reader = asyncio.StreamReader()
        reader.feed_data(
            partial if attempts == 1 else struct.pack("<IIII", len(jpeg), 0, 1, 0) + jpeg
        )
        return reader, Writer()

    def frame(data: bytes) -> None:
        assert data == jpeg
        received.set()

    monkeypatch.setattr(camera.asyncio, "open_connection", connect)
    monkeypatch.setattr(camera, "_FRAME_TIMEOUT_S", 0.03)
    monkeypatch.setattr(camera, "_BACKOFF_START", 0.01)
    client = camera.CameraClient("127.0.0.1", ACCESS_CODE, on_frame=frame)
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(received.wait(), 1)
        assert attempts == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
