"""MediaMTX on-demand worker. Credentials stay in memory, never in FFmpeg argv."""

from __future__ import annotations

import os
import subprocess
import urllib.request
from urllib.parse import quote


def encoder_command(executable: str) -> list[str]:
    return [
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
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-threads",
        "2",
        "-g",
        "60",
        "-bf",
        "0",
        "-f",
        "rtsp",
        "-rtsp_transport",
        "tcp",
        "rtsp://127.0.0.1:18554/streaming/live/1",
    ]


def main() -> None:
    printer = quote(os.environ["BRIDGE_VIDEO_PRINTER_ID"], safe="")
    port = int(os.environ["BRIDGE_VIDEO_API_PORT"])
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/printers/{printer}/camera/video.rgb",
        headers={"Authorization": "Bearer " + os.environ["BRIDGE_VIDEO_API_KEY"]},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        process = subprocess.Popen(
            encoder_command(os.environ["BRIDGE_VIDEO_FFMPEG"]),
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert process.stdin
            while data := response.read(65536):
                process.stdin.write(data)
        finally:
            if process.stdin:
                process.stdin.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
