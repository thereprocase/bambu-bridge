"""Optional on-demand H.264 camera; TLS terminates before the loopback RTSP backend.

Orca's Windows Live555 plugin accepts RTP/AVP inside TLS, but cannot decode
MediaMTX 1.21's additional SRTP negotiation. Never expose the plain backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import signal
import sys
from pathlib import Path
from typing import Any

import structlog

from bambu_bridge.adaptive_video import AdaptiveVideo


def configuration(code: str) -> dict[str, Any]:
    return {
        "logLevel": "error",
        "rtsp": True,
        "rtspEncryption": "no",
        "rtspTransports": ["tcp"],
        "rtspAddress": "127.0.0.1:18554",
        "rtmp": False,
        "hls": True,
        "hlsAddress": "127.0.0.1:18888",
        "hlsAlwaysRemux": False,
        "hlsVariant": "lowLatency",
        "hlsSegmentDuration": "1s",
        "hlsPartDuration": "200ms",
        "hlsSegmentMaxSize": "8M",
        "hlsMuxerCloseAfter": "10s",
        "webrtc": False,
        "srt": False,
        "moq": False,
        "authInternalUsers": [
            {
                "user": "bblp",
                "pass": code,
                "permissions": [{"action": "read", "path": "streaming/live/1"}],
            },
            {
                "user": "any",
                "ips": ["127.0.0.1"],
                "permissions": [{"action": "publish", "path": "streaming/live/1"}],
            },
        ],
        "paths": {
            "streaming/live/1": {
                "runOnDemand": shlex.join([sys.executable, "-m", "bambu_bridge.video_publisher"]),
                "runOnDemandRestart": True,
                "runOnDemandStartTimeout": "20s",
                "runOnDemandCloseAfter": "5s",
            }
        },
    }


class NativeVideo:
    def __init__(self, gateway: Any):
        self.gateway = gateway
        self.process: asyncio.subprocess.Process | None = None
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task[None]] = set()
        self.config_path: Path | None = None
        self.adaptive = AdaptiveVideo(gateway)

    @property
    def ready(self) -> bool:
        return bool(self.server and self.process and self.process.returncode is None)

    def advertise(self, value: dict[str, Any]) -> dict[str, Any]:
        if not getattr(self.gateway.app.state.settings, "bridge_native_video_advertise", False):
            return value
        # Include this even in partial reports, so a crashed backend reverts to JPEG.
        if isinstance(value.get("print"), dict):
            body = value["print"]
            camera = body.setdefault("ipcam", {})
            if isinstance(camera, dict):
                camera.pop("rtsp_url", None)
                camera["liveview"] = {"local": "rtsps" if self.ready else "local", "remote": "none"}
        return value

    async def start(self) -> None:
        settings = self.gateway.app.state.settings
        if not getattr(settings, "bridge_native_video", False):
            return
        try:
            if (
                os.name != "posix"
                or not settings.bridge_http_enabled
                or not settings.bridge_api_key
            ):
                raise ValueError("Video requires a Linux host and authenticated loopback HTTP")
            code = self.gateway.saved_code()
            if not code or not Path(settings.bridge_ffmpeg_path).is_file():
                raise ValueError("Video encoder or native code unavailable")
            directory = self.gateway.store.directory / "native-video"
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)
            self.config_path = directory / "mediamtx.json"
            assert self.config_path is not None
            fd = os.open(self.config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(configuration(code), stream)
            self.config_path.chmod(0o600)
            env = {
                **os.environ,
                "BRIDGE_VIDEO_PRINTER_ID": self.gateway.config["printer_id"],
                "BRIDGE_VIDEO_API_PORT": str(settings.bridge_port),
                "BRIDGE_VIDEO_API_KEY": settings.bridge_api_key,
                "BRIDGE_VIDEO_FFMPEG": settings.bridge_ffmpeg_path,
            }
            self.process = await asyncio.create_subprocess_exec(
                settings.bridge_mediamtx_path,
                str(self.config_path),
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            for _ in range(50):
                if self.process.returncode is not None:
                    raise RuntimeError("Video backend exited")
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", 18554)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            else:
                raise TimeoutError("Video backend did not start")
            self.server = await asyncio.start_server(
                self.connected,
                self.gateway.host,
                322,
                ssl=self.gateway.context,
                ssl_handshake_timeout=10,
                limit=65536,
            )
        except Exception as exc:
            await self.close()
            structlog.get_logger().warning("camera.video_unavailable", error=type(exc).__name__)

    def connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self.relay(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream: asyncio.StreamWriter | None = None
        tasks: list[asyncio.Task[None]] = []
        try:
            self.gateway.service()  # fence deleted/replaced printer
            remote, upstream = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", 18554), 5
            )
            assert upstream is not None

            async def copy(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
                while data := await asyncio.wait_for(source.read(65536), 90):
                    self.gateway.service()
                    target.write(data)
                    await asyncio.wait_for(target.drain(), 10)

            tasks = [
                asyncio.create_task(copy(reader, upstream)),
                asyncio.create_task(copy(remote, writer)),
            ]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, TimeoutError, ValueError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            if upstream:
                upstream.close()

    async def close(self) -> None:
        await self.adaptive.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.process:
            # Kill the owned group, including on-demand publisher/encoder children.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), 5)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            await self.process.wait()
            self.process = None
        if self.config_path:
            self.config_path.unlink(missing_ok=True)
