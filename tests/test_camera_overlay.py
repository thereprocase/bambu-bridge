"""Offline HUD truthfulness, valid JPEGs, and bounded shared-camera work."""

import asyncio
import io
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from PIL import Image

from bambu_bridge.camera_overlay import OverlayStream, bridge_line, render_frame, status_lines
from bambu_bridge.translate import SnapshotContext, translate_snapshot


def snapshot(state="RUNNING", layer=42, stamp=None):
    return translate_snapshot(
        {
            "gcode_state": state,
            "layer_num": layer,
            "total_layer_num": 275,
            "mc_percent": 15,
            "mc_remaining_time": 420,
            "nozzle_temper": 259,
            "nozzle_target_temper": 260,
            "bed_temper": 100,
            "bed_target_temper": 100,
        },
        SnapshotContext(
            printer_id="fixture",
            serial="fixture",
            friendly_name="Fixture",
            model="P1S",
            connected=True,
            last_telemetry_at=stamp or datetime.now(UTC).isoformat(),
            last_connect_attempt=None,
            last_failure_phase=None,
            cert_status="trusted",
            expected_fingerprint=None,
        ),
    )


def jpeg():
    out = io.BytesIO()
    Image.new("RGB", (1280, 720), "#354960").save(out, format="JPEG")
    return out.getvalue()


def test_measured_printer_status_and_separate_delivery():
    now = time.time()
    lines, warning = status_lines(snapshot(), [{"created": now, "state": "delivered"}], now, 0.2)
    assert "PRINTING" in lines[0] and "42/275" in lines[0] and "15%" in lines[0]
    assert "259/260 C" in lines[1] and "100/100 C" in lines[1] and "Finishes ~" in lines[1]
    assert "no start requested" in lines[2]
    assert not warning
    preparing, _ = status_lines(snapshot(layer=0), [], now, 0)
    assert "PREPARING" in preparing[0] and "15%" not in preparing[0]


@pytest.mark.parametrize(
    "start, phrase",
    [
        ("queued", "checking readiness"),
        ("sent", "awaiting acknowledgement"),
        ("accepted", "awaiting active telemetry"),
        ("running", "active print confirmed"),
        ("unknown", "do not resend"),
        ("blocked", "BBSTART_STALE_TELEMETRY"),
    ],
)
def test_bridge_stages_do_not_claim_early_success(start, phrase):
    row = {
        "created": time.time(),
        "state": "delivered",
        "start_state": start,
        "code": "BBSTART_STALE_TELEMETRY",
    }
    assert phrase in bridge_line([row], time.time())


def test_stale_disconnected_error_and_terminal_mismatch():
    data = snapshot(stamp="2020-01-01T00:00:00+00:00")
    lines, warning = status_lines(data, [], time.time(), None)
    assert warning and "STALE TELEMETRY" in lines[0]
    data["session"]["connected"] = False
    data["print_error"] = {"code": "1234"}
    lines, warning = status_lines(data, [], time.time(), None)
    assert warning and "DISCONNECTED" in lines[0] and "1234" in lines[-1]
    lines, warning = status_lines(snapshot("FINISH"), [{"start_state": "running"}], time.time(), 0)
    assert warning and "reconciliation pending" in lines[2]
    assert "FINISHED - waiting for next job" in lines[0]


@pytest.mark.parametrize(
    "source", [None, b"broken jpeg", jpeg()], ids=["missing", "corrupt", "live"]
)
def test_renderer_valid_jpeg_and_preserves_chamber(source):
    lines, warning = status_lines(snapshot(), [], time.time(), 0)
    output = render_frame(source, lines, warning)
    with Image.open(io.BytesIO(output)) as image:
        image.load()
        assert image.format == "JPEG" and image.size == (1280, 720)
        if source == jpeg():
            assert (
                max(
                    abs(a - b)
                    for a, b in zip(image.getpixel((200, 200)), (53, 73, 96), strict=False)
                )
                < 5
            )


async def test_shared_worker_updates_during_camera_loss_and_releases(monkeypatch):
    from bambu_bridge import camera_overlay

    raw = asyncio.Queue()
    count = 0

    @asynccontextmanager
    async def subscribe():
        nonlocal count
        count += 1
        try:
            yield raw
        finally:
            count -= 1

    service = SimpleNamespace(camera=SimpleNamespace(subscribe=subscribe), snapshot=snapshot)
    stream = OverlayStream(lambda: service, lambda: [])
    calls = []

    def render(frame, lines, warning, ams=None, shape=None, rotation_seconds=0):
        calls.append(frame)
        return b"live" if frame else b"unavailable"

    monkeypatch.setattr(camera_overlay, "render_frame", render)
    monkeypatch.setattr(camera_overlay, "STALE_FRAME_S", 0.1)
    raw.put_nowait(b"frame")
    async with stream.subscribe() as first, stream.subscribe() as second:
        a, b = await asyncio.wait_for(asyncio.gather(first.get(), second.get()), 3)
        assert a == b == b"live" and count == 1 and len(calls) == 1
        a, b = await asyncio.wait_for(asyncio.gather(first.get(), second.get()), 3)
        assert a == b == b"unavailable" and len(calls) == 2
        raw.put_nowait(b"new-frame")
        assert await asyncio.wait_for(first.get(), 3) == b"live"
        assert second.qsize() == 1
    assert count == 0 and stream._task is None


async def test_service_fence_closes_overlay_without_old_frames():
    @asynccontextmanager
    async def subscribe():
        yield asyncio.Queue()

    original = SimpleNamespace(camera=SimpleNamespace(subscribe=subscribe), snapshot=snapshot)
    calls = 0

    def service():
        nonlocal calls
        calls += 1
        return original if calls == 1 else object()

    stream = OverlayStream(service, lambda: [])
    async with stream.subscribe() as queue:
        assert await asyncio.wait_for(queue.get(), 2) is None


def test_hms_and_unknown_fields_stay_visible():
    data = snapshot()
    data["hms"] = [{"hex": "0700_8000", "stale": False}]
    lines, warning = status_lines(data, [], time.time(), None)
    assert warning and "HMS 0700_8000" in lines[-1]
    lines, warning = status_lines({}, [], time.time(), None)
    assert warning and "DISCONNECTED" in lines[0]
    assert "--/-- C" in lines[1] and "Telemetry age --s" in lines[-1]


async def test_camera_rate_is_not_capped_by_status_refresh(monkeypatch):
    from bambu_bridge import camera_overlay

    raw = asyncio.Queue()

    @asynccontextmanager
    async def subscribe():
        yield raw

    calls = 0

    def state():
        nonlocal calls
        calls += 1
        return snapshot()

    service = SimpleNamespace(camera=SimpleNamespace(subscribe=subscribe), snapshot=state)
    stream = OverlayStream(lambda: service, lambda: [])
    monkeypatch.setattr(
        camera_overlay, "render_frame", lambda frame, *args: frame
    )
    async with stream.subscribe() as queue:
        for i in range(5):
            frame = str(i).encode()
            raw.put_nowait(frame)
            assert await asyncio.wait_for(queue.get(), 0.5) == frame
        assert calls == 1


async def test_no_duplicate_live_frames_between_camera_arrivals(monkeypatch):
    from bambu_bridge import camera_overlay

    raw = asyncio.Queue()

    @asynccontextmanager
    async def subscribe():
        yield raw

    service = SimpleNamespace(camera=SimpleNamespace(subscribe=subscribe), snapshot=snapshot)
    monkeypatch.setattr(
        camera_overlay, "render_frame", lambda frame, *args: frame
    )
    stream = OverlayStream(lambda: service, lambda: [])
    async with stream.subscribe() as queue:
        raw.put_nowait(b"live")
        assert await asyncio.wait_for(queue.get(), 0.5) == b"live"
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(queue.get(), 1.2)


@pytest.mark.parametrize(
    "stamp, remaining, expected",
    [
        ("2026-09-13T12:00:00+00:00", 420, "Finishes ~3:00 PM"),
        ("2026-01-13T12:00:00+00:00", 420, "Finishes ~2:00 PM"),
        ("2026-09-14T02:00:00+00:00", 420, "Finishes ~Mon 5:00 AM"),
    ],
)
def test_completion_estimate_uses_local_timezone(stamp, remaining, expected):
    now = datetime.fromisoformat(stamp).timestamp()
    state = snapshot(stamp=stamp)
    state["job"]["remaining_min"] = remaining
    lines, _ = status_lines(state, [], now, 0, "America/New_York")
    assert expected in lines[1]
    assert "min left" not in lines[1]
