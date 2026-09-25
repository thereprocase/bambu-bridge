"""One aligned HLS quality ladder per printer, leased only while it is watched."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)
IDLE_SECONDS = 20.0
QUALITIES = (("low", 640, 360, 350), ("medium", 960, 540, 800), ("high", 1280, 720, 1600))
RESOURCE = re.compile(
    r"(?:index|low|medium|high)\.m3u8|init_(?:low|medium|high)\.mp4|(?:low|medium|high)_\d+\.m4s"
)


def encoder_command(executable: str, directory: Path) -> list[str]:
    args = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgb24",
        "-video_size",
        "1280x720",
        "-framerate",
        "30",
        "-i",
        "pipe:0",
        "-filter_complex_threads",
        "1",
        "-filter_complex",
        "[0:v]split=3[a][b][c];[a]scale=640:360[low];[b]scale=960:540[medium];" "[c]null[high]",
    ]
    for name, _, _, _ in QUALITIES:
        args += ["-map", f"[{name}]"]
    args += [
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "main",
        "-threads",
        "2",
        "-g",
        "30",
        "-keyint_min",
        "30",
        "-sc_threshold",
        "0",
        "-bf",
        "0",
    ]
    for i, (_, _, _, bitrate) in enumerate(QUALITIES):
        args += [
            f"-b:v:{i}",
            f"{bitrate}k",
            f"-maxrate:v:{i}",
            f"{bitrate}k",
            f"-bufsize:v:{i}",
            f"{bitrate * 2}k",
        ]
    args += [
        "-f",
        "hls",
        "-hls_time",
        "1",
        "-hls_list_size",
        "6",
        "-hls_delete_threshold",
        "3",
        "-hls_segment_type",
        "fmp4",
        "-hls_flags",
        "delete_segments+independent_segments+temp_file",
        "-hls_start_number_source",
        "epoch_us",
        "-hls_fmp4_init_filename",
        "init_%v.mp4",
        "-hls_segment_filename",
        str(directory / "%v_%d.m4s"),
        "-master_pl_name",
        "index.m3u8",
        "-var_stream_map",
        "v:0,name:low v:1,name:medium v:2,name:high",
        str(directory / "%v.m3u8"),
    ]
    return args


class AdaptiveVideo:
    def __init__(self, gateway: Any):
        self.gateway = gateway
        self.lock = asyncio.Lock()
        self.process: asyncio.subprocess.Process | None = None
        self.directory: Path | None = None
        self.timer: asyncio.Task[None] | None = None
        self.last_access = 0.0

    async def read(self, resource: str) -> bytes:
        if not RESOURCE.fullmatch(resource):
            raise FileNotFoundError(resource)
        async with self.lock:
            if self.process and self.process.returncode is not None:
                await self._stop()
            if self.process is None:
                # Only playlists may wake a parked stream. Old segments cannot
                # restart encoding or be confused with a new generation.
                if not resource.endswith(".m3u8"):
                    raise FileNotFoundError(resource)
                settings = self.gateway.app.state.settings
                self.directory = Path(tempfile.mkdtemp(prefix="bridge-hls-"))
                env = {
                    **os.environ,
                    "BRIDGE_VIDEO_PRINTER_ID": self.gateway.config["printer_id"],
                    "BRIDGE_VIDEO_API_PORT": str(settings.bridge_port),
                    "BRIDGE_VIDEO_API_KEY": settings.bridge_api_key,
                    "BRIDGE_VIDEO_FFMPEG": settings.bridge_ffmpeg_path,
                    "BRIDGE_VIDEO_HLS_DIR": str(self.directory),
                }
                try:
                    self.process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "bambu_bridge.video_publisher",
                        env=env,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        start_new_session=True,
                    )
                except BaseException:
                    await self._stop()
                    raise
                self.timer = asyncio.create_task(self._park())
                log.info("camera.adaptive_started", qualities=3)
            self.last_access = time.monotonic()
            directory = self.directory
            process = self.process
        assert directory is not None and process is not None
        deadline = time.monotonic() + 10
        path = directory / resource
        while True:
            if process.returncode is not None:
                raise RuntimeError("Adaptive encoder exited")
            try:
                data = await asyncio.to_thread(path.read_bytes)
                if data and (not resource.endswith(".m3u8") or data.startswith(b"#EXTM3U")):
                    if len(data) > 9 * 1024 * 1024:
                        raise ValueError("Video segment too large")
                    return data
            except FileNotFoundError:
                if not resource.endswith(".m3u8"):
                    raise
            if time.monotonic() > deadline:
                raise TimeoutError("Adaptive encoder startup timed out")
            await asyncio.sleep(0.1)

    async def _park(self) -> None:
        try:
            while True:
                await asyncio.sleep(max(0.05, IDLE_SECONDS - (time.monotonic() - self.last_access)))
                async with self.lock:
                    if time.monotonic() - self.last_access >= IDLE_SECONDS:
                        await self._stop()
                        log.info("camera.adaptive_parked")
                        return
        except asyncio.CancelledError:
            return

    async def _stop(self) -> None:
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
        self.timer = None
        if self.process:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), 3)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            await self.process.wait()
            self.process = None
        if self.directory:
            await asyncio.to_thread(shutil.rmtree, self.directory, True)
            self.directory = None

    async def close(self) -> None:
        async with self.lock:
            await self._stop()
