import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bambu_bridge import adaptive_video
from bambu_bridge.adaptive_video import AdaptiveVideo, encoder_command


def test_vaapi_preserves_ladder_and_uses_hardware_scaling(tmp_path):
    args = encoder_command("ffmpeg", tmp_path, "/dev/dri/renderD128")
    assert args[args.index("-c:v") + 1] == "h264_vaapi"
    assert "scale_vaapi=w=640:h=360" in args[args.index("-filter_complex") + 1]
    assert args[args.index("-g") + 1] == "30"
    assert args[args.index("-bf") + 1] == "0"
    assert "-preset" not in args
    assert args[args.index("-var_stream_map") + 1] == "v:0,name:low v:1,name:medium v:2,name:high"


def test_gpu_probe_falls_back_on_failure(monkeypatch):
    import subprocess

    from bambu_bridge.video_publisher import usable_vaapi

    def failed(*args, **kwargs):
        raise subprocess.TimeoutExpired("ffmpeg", 5)

    monkeypatch.setattr(subprocess, "run", failed)
    assert not usable_vaapi("ffmpeg", "/dev/dri/renderD128")
    assert not usable_vaapi("ffmpeg", "")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    assert usable_vaapi("ffmpeg", "/dev/dri/renderD128")


@pytest.mark.parametrize("result", [1, OSError("device unavailable")])
def test_gpu_probe_failure_and_cleanup(monkeypatch, result):
    import subprocess
    from pathlib import Path

    from bambu_bridge.video_publisher import usable_vaapi

    directories = []

    def run(command, **kwargs):
        directory = Path(command[-1]).parent
        directories.append(directory)
        assert directory.is_dir()
        assert len(kwargs["input"]) == 1280 * 720 * 3
        assert "v:0,name:low v:1,name:medium v:2,name:high" in command
        assert kwargs["timeout"] == 5
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(returncode=result)

    monkeypatch.setattr(subprocess, "run", run)
    assert not usable_vaapi("ffmpeg", "")
    assert directories == []
    assert not usable_vaapi("ffmpeg", "/dev/dri/renderD128")
    assert len(directories) == 1 and not directories[0].exists()


def test_publisher_reaps_encoder_when_pipe_close_fails(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from bambu_bridge import video_publisher

    for key, value in {
        "BRIDGE_VIDEO_PRINTER_ID": "fixture",
        "BRIDGE_VIDEO_API_PORT": "8080",
        "BRIDGE_VIDEO_API_KEY": "test-credential",
        "BRIDGE_VIDEO_HLS_DIR": str(tmp_path),
        "BRIDGE_VIDEO_FFMPEG": "ffmpeg",
        "BRIDGE_VIDEO_VAAPI_DEVICE": "",
    }.items():
        monkeypatch.setenv(key, value)
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b""
    monkeypatch.setattr(video_publisher.urllib.request, "urlopen", lambda *a, **kw: response)
    child = MagicMock()
    child.stdin.close.side_effect = BrokenPipeError()
    monkeypatch.setattr(video_publisher.subprocess, "Popen", lambda *a, **kw: child)
    video_publisher.main()
    child.terminate.assert_called_once()
    child.wait.assert_called_once_with(timeout=5)


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
