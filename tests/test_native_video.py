import asyncio
import contextlib
import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image

from bambu_bridge.camera_overlay import OverlayStream, render_frame
from bambu_bridge.native_video import NativeVideo, configuration
from bambu_bridge.video_publisher import remux_command


@pytest.mark.parametrize(
    "peer,enabled,printer",
    [
        ("192.168.1.2", True, "printer"),
        ("127.0.0.1", False, "printer"),
        ("127.0.0.1", True, "wrong-printer"),
    ],
)
async def test_rgb_route_rejects_remote_disabled_or_wrong_printer(peer, enabled, printer):
    from bambu_bridge.api.camera import camera_video

    gateway = SimpleNamespace(config={"printer_id": "printer"})
    request = SimpleNamespace(
        client=SimpleNamespace(host=peer),
        app=SimpleNamespace(
            state=SimpleNamespace(
                native_gateway=gateway, settings=SimpleNamespace(bridge_native_video=enabled)
            )
        ),
    )
    with pytest.raises(HTTPException) as error:
        await camera_video(printer, request)
    assert error.value.status_code == 404


def test_backend_only_exposes_loopback_and_separates_publish_permission():
    config = configuration("test-code")
    assert config["rtspAddress"] == "127.0.0.1:18554"
    assert config["rtspTransports"] == ["tcp"]
    read, publish = config["authInternalUsers"]
    assert read["user"] == "bblp" and read["pass"] == "test-code"
    assert read["permissions"] == [{"action": "read", "path": "streaming/live/1"}]
    assert publish["ips"] == ["127.0.0.1"]
    assert publish["permissions"] == [{"action": "publish", "path": "streaming/live/1"}]
    assert config["paths"]["streaming/live/1"]["runOnDemandCloseAfter"] == "5s"


def test_orca_remuxes_shared_high_stream_without_reencoding():
    args = remux_command("ffmpeg", "/tmp/bridge-hls-test/high.m3u8")
    assert args[args.index("-c:v") + 1] == "copy"
    assert args[args.index("-i") + 1] == "/tmp/bridge-hls-test/high.m3u8"
    assert args[-1] == "rtsp://127.0.0.1:18554/streaming/live/1"


def test_advertisement_falls_back_after_backend_exit_and_removes_source_url():
    gateway = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(settings=SimpleNamespace(bridge_native_video_advertise=True))
        )
    )
    video = NativeVideo(gateway)
    video.server = object()
    video.process = SimpleNamespace(returncode=None)

    def report():
        return {
            "print": {
                "gcode_state": "RUNNING",
                "ipcam": {"rtsp_url": "rtsps://physical-printer", "resolution": "720p"},
            }
        }

    result = video.advertise(report())
    assert result["print"]["ipcam"]["liveview"]["local"] == "rtsps"
    assert "rtsp_url" not in result["print"]["ipcam"]
    assert result["print"]["gcode_state"] == "RUNNING"
    assert result["print"]["ipcam"]["resolution"] == "720p"
    video.process.returncode = 1
    assert video.advertise(report())["print"]["ipcam"]["liveview"]["local"] == "local"
    gateway.app.state.settings.bridge_native_video_advertise = False
    original = report()
    assert video.advertise(original) == original


def test_rgb_size_is_stable_even_when_source_camera_changes_size():
    for size in [(640, 480), (1280, 720)]:
        stream = io.BytesIO()
        Image.new("RGB", size).save(stream, "JPEG")
        assert len(render_frame(stream.getvalue(), ["Test"], False, rgb=True)) == 1280 * 720 * 3
    assert len(render_frame(None, ["Lost"], True, rgb=True)) == 1280 * 720 * 3


async def test_video_animates_without_new_camera_frames_but_camera_still_expires(monkeypatch):
    from bambu_bridge import camera_overlay

    raw = asyncio.Queue()

    @contextlib.asynccontextmanager
    async def subscribe():
        yield raw

    service = SimpleNamespace(camera=SimpleNamespace(subscribe=subscribe), snapshot=lambda: {})
    stream = OverlayStream(lambda: service, lambda: [], video=True)
    rendered = []

    def render(frame, *args, **kwargs):
        rendered.append((frame, args[-1]))
        assert kwargs == {"rgb": True}
        return b"fresh" if frame else b"stale"

    monkeypatch.setattr(camera_overlay, "render_frame", render)
    monkeypatch.setattr(camera_overlay, "STALE_FRAME_S", 0.12)
    raw.put_nowait(b"camera")
    async with stream.subscribe() as queue:
        values = [await asyncio.wait_for(queue.get(), 1) for _ in range(7)]
        assert values[0] == b"fresh" and values[-1] == b"stale"
        assert rendered[-1][1] - rendered[0][1] >= 0.17
        raw.put_nowait(b"reconnected")
        assert await asyncio.wait_for(queue.get(), 1) == b"fresh"
    assert stream._task is None
