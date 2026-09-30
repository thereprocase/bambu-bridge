"""Versioned, bounded parsed geometry; damaged assets are rebuilt from source."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import math
import os
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

from bambu_bridge.turntable_overlay import Shape, archive_shape

GEOMETRY_VERSION = "bodies-lod-v6"
MAX_ASSET_BYTES = 32 * 1024 * 1024
MAX_DISK_BYTES = 96 * 1024 * 1024


class GeometryAssets:
    def __init__(self, directory: Path):
        self.directory = directory / "preview-geometry"

    async def acquire(self, source: Path, plate: int) -> Shape | None:
        """Parse in a disposable process so a dense job releases its working heap."""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "bambu_bridge.preview_worker",
            str(self.directory.parent),
            str(source),
            str(plate),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=120)
            if process.returncode or len(output) > MAX_ASSET_BYTES:
                raise ValueError("Preview geometry worker failed")
            obj = json.loads(output)
            if obj is None:
                return None
            for key in ("segments", "faces", "ink_edges"):
                obj[key] = tuple(tuple(row) for row in obj[key])
            obj["face_parts"] = tuple(obj["face_parts"])
            return Shape(**obj)
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()

    def load(self, data: bytes, plate: int) -> Shape | None:
        import io

        identity = hashlib.sha256(
            f"{GEOMETRY_VERSION}:{plate}:".encode() + hashlib.sha256(data).digest()
        ).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / (identity + ".gz")
        try:
            if path.stat().st_size <= MAX_ASSET_BYTES:
                with gzip.open(path, "rb") as f:
                    raw = f.read(MAX_ASSET_BYTES + 1)
                if len(raw) <= MAX_ASSET_BYTES:
                    obj = json.loads(raw)
                    for field, width, limit in (
                        ("segments", 6, 6000),
                        ("faces", 15, 100_000),
                        ("ink_edges", 4, 100_000),
                    ):
                        rows = obj[field]
                        if len(rows) > limit or any(len(row) != width for row in rows):
                            raise ValueError("Invalid geometry dimensions")
                        obj[field] = tuple(tuple(row) for row in rows)
                    if any(
                        not math.isfinite(n) or abs(n) > 2000
                        for row in (*obj["faces"], *obj["segments"])
                        for n in row
                    ):
                        raise ValueError("Invalid geometry coordinates")
                    obj["face_parts"] = tuple(obj["face_parts"])
                    if not (0 < obj["radius"] <= 2000 and 0 <= obj["height"] <= 2000):
                        raise ValueError("Invalid geometry dimensions")
                    if any(type(part) is not int or part < 0 for part in obj["face_parts"]):
                        raise ValueError("Invalid geometry component")
                    if len(obj["face_parts"]) != len(obj["faces"]) or len(obj["ink_edges"]) != len(
                        obj["faces"]
                    ):
                        raise ValueError("Invalid geometry attributes")
                    if obj["content_id"] != identity:
                        raise ValueError("Geometry identity changed")
                    cached = Shape(**obj)
                    path.touch()
                    return cached
        except (OSError, ValueError, TypeError, KeyError, EOFError):
            pass
        shape = archive_shape(io.BytesIO(data), plate)
        if shape is None:
            return None
        shape = replace(shape, content_id=identity)
        raw = json.dumps(asdict(shape), separators=(",", ":")).encode()
        if len(raw) <= MAX_ASSET_BYTES:
            with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as f:
                temporary = Path(f.name)
            try:
                with gzip.open(temporary, "wb", compresslevel=1) as f:
                    f.write(raw)
                temporary.replace(path)
                total = 0
                for item in sorted(
                    self.directory.glob("*.gz"), key=lambda p: p.stat().st_mtime, reverse=True
                ):
                    total += item.stat().st_size
                    if total > MAX_DISK_BYTES and item != path:
                        item.unlink(missing_ok=True)
            finally:
                temporary.unlink(missing_ok=True)
        return shape
