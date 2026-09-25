import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bambu_bridge import adaptive_video
from bambu_bridge.adaptive_video import AdaptiveVideo, encoder_command


def test_ladder_aligns_keyframes_and_bounds_disk_and_rate(tmp_path):
    args = encoder_command("ffmpeg", tmp_path)
    assert args[args.index("-g") + 1] == "30"
    assert args[args.index("-sc_threshold") + 1] == "0"
    assert args[args.index("-hls_list_size") + 1] == "6"
    assert "delete_segments+independent_segments+temp_file" in args
    assert args[args.index("-var_stream_map") + 1] == "v:0,name:low v:1,name:medium v:2,name:high"
    assert [args[args.index(f"-maxrate:v:{i}") + 1] for i in range(3)] == ["350k", "800k", "1600k"]
    assert "epoch_us" in args  # no stale segment aliases after wake


@pytest.mark.parametrize(
    "resource", ["../index.m3u8", "https://bad/index.m3u8", "init_low.mp4", "low_1.m4s"]
)
async def test_bad_paths_and_stale_segments_do_not_wake_encoder(resource):
    video = AdaptiveVideo(None)
    with pytest.raises(FileNotFoundError):
        await video.read(resource)
    assert video.process is None


async def test_concurrent_viewers_share_worker_and_idle_parks_it(monkeypatch, tmp_path):
    monkeypatch.setattr(adaptive_video, "IDLE_SECONDS", 0.1)
    monkeypatch.setattr(adaptive_video.tempfile, "mkdtemp", lambda **kw: str(tmp_path))
    process = SimpleNamespace(pid=1234567, returncode=None, wait=AsyncMock(return_value=0))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(adaptive_video.signal, "SIGKILL", 9, raising=False)
    killed = []
    monkeypatch.setattr(
        adaptive_video.os, "killpg", lambda pid, sig: killed.append(pid), raising=False
    )
    (tmp_path / "index.m3u8").write_bytes(b"#EXTM3U\nlow.m3u8\n")
    settings = SimpleNamespace(
        bridge_port=8080, bridge_api_key="private", bridge_ffmpeg_path="ffmpeg"
    )
    video = AdaptiveVideo(
        SimpleNamespace(
            config={"printer_id": "printer"},
            app=SimpleNamespace(state=SimpleNamespace(settings=settings)),
        )
    )
    try:
        replies = await asyncio.gather(*(video.read("index.m3u8") for _ in range(4)))
        assert all(b"#EXTM3U" in reply for reply in replies)
        assert spawn.await_count == 1
        assert "private" not in str(spawn.call_args.args)
        await asyncio.sleep(0.07)
        await video.read("index.m3u8")
        await asyncio.sleep(0.06)
        assert video.process is process
        await asyncio.sleep(0.1)
        assert video.process is None and video.directory is None
        assert killed
    finally:
        await video.close()
