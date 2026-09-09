"""M5: camera auth packet, frame parsing, and one-upstream multiplexing."""

from __future__ import annotations

import asyncio

import pytest

from bambu_bridge.protocol.camera import CameraStream, build_auth_packet
from tests.conftest import ACCESS_CODE, FakeCamera


def test_auth_packet_layout() -> None:
    pkt = build_auth_packet("bblp", "12345678")
    assert len(pkt) == 80
    assert pkt[16:48] == b"bblp".ljust(32, b"\x00")
    assert pkt[48:80] == b"12345678".ljust(32, b"\x00")


def test_camera_tls_context_is_pinned_to_tls_1_2() -> None:
    """Last of the three protocol TLS contexts pinned to 1.2 — same
    rationale as mqtt gotcha #8 / ftps."""
    import ssl

    from bambu_bridge.protocol.tls import insecure_tls_context

    ctx = insecure_tls_context()
    assert ctx.minimum_version is ssl.TLSVersion.TLSv1_2
    assert ctx.maximum_version is ssl.TLSVersion.TLSv1_2
    assert ctx.verify_mode is ssl.CERT_NONE
    assert ctx.check_hostname is False


async def _first_frame(stream: CameraStream, timeout: float = 5.0) -> bytes:
    async with stream.subscribe() as q:
        async with asyncio.timeout(timeout):
            return await q.get()


@pytest.mark.asyncio
async def test_stream_delivers_frames_and_buffers_latest(
    fake_camera: FakeCamera,
) -> None:
    # linger_s=0: verify original eager-teardown behaviour (last-subscriber-gone
    # => upstream stops immediately, not after the linger window).
    stream = CameraStream("127.0.0.1", ACCESS_CODE, port=fake_camera.port, linger_s=0.0)
    async with stream.subscribe() as q:
        frame = await asyncio.wait_for(q.get(), timeout=5)
        assert frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9")
        assert stream.upstream_active is True
        assert stream.latest() is not None
    # Last subscriber gone -> upstream auto-closes (spec 5.3).
    await asyncio.sleep(0.1)
    assert stream.upstream_active is False
    async with asyncio.timeout(3):
        while fake_camera.active != 0:
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_two_subscribers_share_one_upstream(
    fake_camera: FakeCamera,
) -> None:
    stream = CameraStream("127.0.0.1", ACCESS_CODE, port=fake_camera.port)
    async with stream.subscribe() as q1, stream.subscribe() as q2:
        f1 = await asyncio.wait_for(q1.get(), timeout=5)
        f2 = await asyncio.wait_for(q2.get(), timeout=5)
        assert f1.startswith(b"\xff\xd8")
        assert f2.startswith(b"\xff\xd8")
    # Exactly one printer-side connection was ever open.
    assert fake_camera.max_active == 1
    assert fake_camera.total == 1


@pytest.mark.asyncio
async def test_wait_for_frame_opens_short_lived_upstream(
    fake_camera: FakeCamera,
) -> None:
    stream = CameraStream("127.0.0.1", ACCESS_CODE, port=fake_camera.port)
    frame = await stream.wait_for_frame(timeout=5)
    assert frame is not None and frame.startswith(b"\xff\xd8")


@pytest.mark.asyncio
async def test_two_sequential_snapshots_share_one_upstream(
    fake_camera: FakeCamera,
) -> None:
    """Two sequential wait_for_frame calls within the linger window must use
    exactly one upstream connect — the second call reuses the live session and
    the buffered frame.  This is the core fix for the per-snapshot TCP churn.
    """
    # linger_s=5 — well above the loop latency, well below the test timeout.
    stream = CameraStream("127.0.0.1", ACCESS_CODE, port=fake_camera.port, linger_s=5.0)

    frame1 = await stream.wait_for_frame(timeout=5)
    assert frame1 is not None and frame1.startswith(b"\xff\xd8")

    # The upstream must still be alive (linger window active).
    assert stream.upstream_active, "upstream should remain alive during linger window"

    frame2 = await stream.wait_for_frame(timeout=5)
    assert frame2 is not None and frame2.startswith(b"\xff\xd8")

    # Exactly one printer-side connection for both calls combined.
    assert fake_camera.total == 1, (
        f"expected 1 upstream connect across two snapshots, got {fake_camera.total}"
    )


@pytest.mark.asyncio
async def test_continuous_polling_holds_single_upstream(
    fake_camera: FakeCamera,
) -> None:
    """Continuous 1fps-style polling must hold exactly ONE upstream for the
    entire polling period, even though each individual snapshot call may
    return a buffered frame without entering subscribe().

    This is the regression test for the live-deployment bug where buffer-hit
    snapshot calls did NOT extend the linger deadline, causing a reconnect
    every ~linger_s seconds under steady polling.

    Setup:  linger_s=0.15, poll_interval=0.05s, total_duration=0.40s
            (total > linger_s, so the old code would have torn down and
            reconnected ~2-3 times; the fixed code keeps one connection).
    """
    linger_s = 0.15
    poll_interval = 0.05  # faster than linger — simulates 1/0.05 = 20fps client
    total_polls = 8       # 8 × 0.05s = 0.40s total > linger_s

    stream = CameraStream(
        "127.0.0.1", ACCESS_CODE, port=fake_camera.port, linger_s=linger_s
    )

    frames_received = 0
    for _ in range(total_polls):
        frame = await stream.wait_for_frame(timeout=5)
        assert frame is not None and frame.startswith(b"\xff\xd8")
        frames_received += 1
        await asyncio.sleep(poll_interval)

    assert frames_received == total_polls

    # Exactly one upstream connection across the entire polling run.
    assert fake_camera.total == 1, (
        f"expected 1 upstream connect across {total_polls} polls "
        f"(linger={linger_s}s, interval={poll_interval}s), got {fake_camera.total}"
    )

    # After polling stops, the upstream should still be alive (within linger).
    assert stream.upstream_active, "upstream should be alive immediately after last poll"

    # Wait longer than linger_s — now the upstream must tear down.
    await asyncio.sleep(linger_s + 0.15)
    assert not stream.upstream_active, "upstream must stop after idle linger expires"


@pytest.mark.asyncio
async def test_upstream_teardown_fires_after_linger_expiry(
    fake_camera: FakeCamera,
) -> None:
    """After linger_s elapses with no subscriber the upstream must stop.

    Uses a very short linger so the test completes quickly without being
    sensitive to scheduler jitter.
    """
    stream = CameraStream("127.0.0.1", ACCESS_CODE, port=fake_camera.port, linger_s=0.1)

    frame = await stream.wait_for_frame(timeout=5)
    assert frame is not None

    # During the linger window the upstream is still up.
    assert stream.upstream_active

    # Wait for linger + a small margin to let the teardown task run.
    await asyncio.sleep(0.4)

    assert not stream.upstream_active, "upstream must have stopped after linger expiry"
    # The printer's connection counter should be back to zero.
    async with asyncio.timeout(3):
        while fake_camera.active != 0:
            await asyncio.sleep(0.02)
    assert fake_camera.active == 0


@pytest.mark.asyncio
async def test_linger_zero_stops_upstream_immediately(
    fake_camera: FakeCamera,
) -> None:
    """linger_s=0 reproduces the original eager-teardown behaviour."""
    stream = CameraStream("127.0.0.1", ACCESS_CODE, port=fake_camera.port, linger_s=0.0)

    frame = await stream.wait_for_frame(timeout=5)
    assert frame is not None

    # With zero linger the upstream is torn down as soon as the subscribe()
    # context exits — give the event loop one tick to settle.
    await asyncio.sleep(0.1)
    assert not stream.upstream_active
