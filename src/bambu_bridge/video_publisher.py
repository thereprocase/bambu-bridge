"""MediaMTX on-demand worker. Credentials stay in memory, never in FFmpeg argv."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import quote

from bambu_bridge.adaptive_video import encoder_command


def usable_vaapi(executable: str, device: str) -> bool:
    """Probe the complete three-rendition pipeline with the worker's permissions."""
    if not device:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="bridge-video-probe-") as directory:
            result = subprocess.run(
                encoder_command(executable, Path(directory), device),
                input=bytes(1280 * 720 * 3),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def main() -> None:
    printer = quote(os.environ["BRIDGE_VIDEO_PRINTER_ID"], safe="")
    port = int(os.environ["BRIDGE_VIDEO_API_PORT"])
    directory = os.environ.get("BRIDGE_VIDEO_HLS_DIR")
    if not directory:
        remux(printer, port)
        return
    executable = os.environ["BRIDGE_VIDEO_FFMPEG"]
    device = os.environ.get("BRIDGE_VIDEO_VAAPI_DEVICE", "")
    command = encoder_command(
        executable, Path(directory), device if usable_vaapi(executable, device) else None
    )
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/printers/{printer}/camera/video.rgb",
        headers={"Authorization": "Bearer " + os.environ["BRIDGE_VIDEO_API_KEY"]},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert process.stdin
            while data := response.read(65536):
                process.stdin.write(data)
        finally:
            if process.stdin:
                # An encoder exit can leave buffered bytes in the pipe. Finish
                # reaping the child even when flushing those bytes fails.
                with contextlib.suppress(BrokenPipeError):
                    process.stdin.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def remux_command(executable: str, playlist: str) -> list[str]:
    return [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-re",
        "-live_start_index",
        "-2",
        "-i",
        playlist,
        "-an",
        "-c:v",
        "copy",
        "-f",
        "rtsp",
        "-rtsp_transport",
        "tcp",
        "rtsp://127.0.0.1:18554/streaming/live/1",
    ]


def remux(printer: str, port: int) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/printers/{printer}/camera/video.lease",
        headers={"Authorization": "Bearer " + os.environ["BRIDGE_VIDEO_API_KEY"]},
    )

    def lease() -> str:
        with urllib.request.urlopen(request, timeout=12) as response:
            result = str(json.load(response)["playlist"])
        path = Path(result)
        if not path.is_absolute() or path.name != "high.m3u8":
            raise ValueError("Invalid local video lease")
        return result

    playlist = lease()
    process = subprocess.Popen(
        remux_command(os.environ["BRIDGE_VIDEO_FFMPEG"], playlist), stderr=subprocess.DEVNULL
    )
    try:
        while True:
            try:
                process.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                if lease() != playlist:
                    break  # encoder generation changed; MediaMTX restarts this reader
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__ == "__main__":
    main()
