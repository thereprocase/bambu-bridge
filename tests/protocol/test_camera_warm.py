"""Warm camera stream: settle frames after a cold connect, keep-warm holds, and
the printer service holding the stream only while a print is in flight."""

from __future__ import annotations

import asyncio

import pytest

from bambu_bridge.protocol.camera import CameraStream
from bambu_bridge.protocol.models import GcodeState
from bambu_bridge.service.printer import PrinterService
from tests.conftest import ACCESS_CODE, SERIAL, FakeCamera


@pytest.mark.asyncio
async def test_settle_frames_drops_the_first_frames_of_a_session(fake_camera: FakeCamera) -> None:
    stream = CameraStream("127.0.0.1", ACCESS_CODE, port=fake_camera.port, settle_frames=3)
    async with stream.subscribe() as queue:
        first = await asyncio.wait_for(queue.get(), timeout=5)
    # FakeCamera numbers its frames from 0; frames 0-2 are the replayed buffer.
    assert b"frame-3" in first
    await stream.aclose()


@pytest.mark.asyncio
async def test_keep_warm_holds_upstream_past_linger_then_releases(fake_camera: FakeCamera) -> None:
    stream = CameraStream("127.0.0.1", ACCESS_CODE, port=fake_camera.port, linger_s=0.2)
    stream.keep_warm(True)
    stream.keep_warm(True)  # idempotent: still one holder, one upstream
    async with asyncio.timeout(5):
        while stream.latest() is None:
            await asyncio.sleep(0.02)
    await asyncio.sleep(0.5)  # well past linger_s
    assert stream.warm and stream.upstream_active
    # A snapshot while warm is served from the live stream without a new connect.
    assert await stream.wait_for_frame(timeout=2) is not None
    assert fake_camera.total == 1

    stream.keep_warm(False)
    async with asyncio.timeout(5):
        while stream.upstream_active:
            await asyncio.sleep(0.05)
    assert not stream.warm
    await stream.aclose()


@pytest.mark.asyncio
async def test_aclose_releases_a_warm_hold(fake_camera: FakeCamera) -> None:
    stream = CameraStream("127.0.0.1", ACCESS_CODE, port=fake_camera.port)
    stream.keep_warm(True)
    async with asyncio.timeout(5):
        while not stream.upstream_active:
            await asyncio.sleep(0.02)
    await stream.aclose()
    assert not stream.warm
    async with asyncio.timeout(3):
        while fake_camera.active != 0:
            await asyncio.sleep(0.02)


def _service(port: int, keep_warm: str) -> PrinterService:
    return PrinterService(
        SERIAL,
        "127.0.0.1",
        ACCESS_CODE,
        friendly_name="Workshop P1S",
        model="P1S",
        camera_port=port,
        camera_linger_s=0.1,
        camera_keep_warm=keep_warm,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "state", "warm"),
    [
        ("printing", GcodeState.RUNNING, True),
        ("printing", GcodeState.PREPARE, True),
        ("printing", GcodeState.PAUSE, True),
        ("printing", GcodeState.FINISH, False),
        ("printing", GcodeState.IDLE, False),
        ("always", GcodeState.IDLE, True),
        ("off", GcodeState.RUNNING, False),
    ],
)
async def test_service_holds_camera_warm_by_print_state(
    fake_camera: FakeCamera, mode: str, state: GcodeState, warm: bool
) -> None:
    service = _service(fake_camera.port, mode)
    service._gcode_state = state
    service._update_camera_warmth()
    try:
        assert (service._camera is not None and service._camera.warm) is warm
    finally:
        if service._camera is not None:
            await service._camera.aclose()


@pytest.mark.asyncio
async def test_service_releases_hold_when_print_finishes(fake_camera: FakeCamera) -> None:
    service = _service(fake_camera.port, "printing")
    service._gcode_state = GcodeState.RUNNING
    service._update_camera_warmth()
    assert service._camera is not None and service._camera.warm
    service._gcode_state = GcodeState.FINISH
    service._update_camera_warmth()
    assert not service._camera.warm
    await service._camera.aclose()
