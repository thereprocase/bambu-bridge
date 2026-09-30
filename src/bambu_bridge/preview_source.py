"""Acquire the active print source independently of its launch surface."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from bambu_bridge.preview_assets import GeometryAssets
from bambu_bridge.protocol.ftps import FtpsTransfer
from bambu_bridge.service.viz_cache import VizFillError, find_3mf
from bambu_bridge.source_assets import SourceAssets
from bambu_bridge.turntable_overlay import Shape, job_key


class PreviewSources:
    """Single flight acquisition with verified local uploads and printer fallback."""

    def __init__(
        self,
        directory: Path,
        service: Callable[[], Any],
        local: Callable[[dict[str, Any]], bytes | None],
        port: Callable[[], int],
        shared: Callable[[str, FtpsTransfer, str], Awaitable[bytes]] | None = None,
    ):
        self.assets = SourceAssets(directory)
        self.geometry = GeometryAssets(directory)
        self.directory = self.assets.directory
        self.shared = shared
        self.service, self.local, self.port = service, local, port
        self.inflight: dict[tuple[str, str, int, str], asyncio.Task[Shape | None]] = {}
        self.lock = asyncio.Semaphore(1)

    async def load(self, snapshot: dict[str, Any]) -> Shape | None:
        raw = snapshot.get("_raw", {})
        filename = source_name(raw)
        plate = selected_plate(raw)
        key = str(snapshot.get("printer_id") or ""), filename, plate, job_key(snapshot)
        task = self.inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._load(snapshot, filename, plate))
            self.inflight[key] = task

            def finished(done: asyncio.Task[Shape | None]) -> None:
                if self.inflight.get(key) is done:
                    del self.inflight[key]
                if not done.cancelled():
                    done.exception()  # Retrieve failures even if every viewer left.

            task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self.inflight.get(key) is task:
                del self.inflight[key]

    async def _load(self, snapshot: dict[str, Any], filename: str, plate: int) -> Shape | None:
        async with self.lock:
            identity = job_key(snapshot)
            reference = "job:" + identity if identity else ""
            data = await asyncio.to_thread(self.assets.read, reference) if reference else None
            if data is None:
                data = await asyncio.to_thread(self.local, snapshot)
            if data is None:
                service = self.service()
                if service.serial != snapshot.get("printer_id"):
                    raise ValueError("Preview printer changed")
                ftps = FtpsTransfer(service.ip, service.access_code, port=self.port())
                if self.shared:
                    try:
                        data = await asyncio.wait_for(
                            self.shared(service.serial, ftps, filename), 120
                        )
                    except VizFillError as exc:
                        if exc.kind == "not_found":
                            raise FileNotFoundError(
                                "Print source unavailable on printer storage"
                            ) from exc
                        raise OSError("Print source transfer failed") from exc
                else:
                    location = await find_3mf(ftps, filename)
                    if location is None:
                        raise FileNotFoundError("Print source unavailable on printer storage")
                    remote_dir, name = location
                    data = await ftps.download_bytes(name, remote_dir=remote_dir)
            # Keep content-addressed sources independently of upload retention.
            digest = await asyncio.to_thread(self.assets.put, reference, data)
            return await self.geometry.acquire(self.assets.directory / digest, plate)

    async def close(self) -> None:
        tasks = list(self.inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.inflight.clear()

    async def prewarm(self, data: bytes, plate: int = 1) -> Shape | None:
        async with self.lock:
            digest = await asyncio.to_thread(self.assets.put, "upload", data)
            return await self.geometry.acquire(self.assets.directory / digest, plate)


def selected_plate(raw: dict[str, Any]) -> int:
    """Prefer the selected archive member supplied by printer telemetry."""
    for field in ("param", "gcode_file"):
        match = re.search(r"(?:^|/)plate_(\d+)\.gcode$", str(raw.get(field) or ""))
        if match:
            return max(1, int(match.group(1)))
    value = raw.get("plate_idx")
    try:
        return max(1, int(value)) if value is not None else 1
    except (TypeError, ValueError):
        return 1


def source_name(raw: dict[str, Any]) -> str:
    """P1S can report the inner plate member separately from the archive name."""
    name = str(raw.get("gcode_file") or "")
    if not name or re.search(r"(?:^|/)Metadata/plate_\d+\.gcode$", name):
        return str(raw.get("subtask_name") or raw.get("task_name") or name)
    return name
