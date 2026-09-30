"""Shared bounded rotation sequences; geometry is rendered once per angle."""

from __future__ import annotations

import hashlib
import math
import tempfile
import threading
import zipfile
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from bambu_bridge.turntable_overlay import Shape, render_panel

FRAME_COUNT = 360
ROTATION_SECONDS = 60.0
MAX_COMPRESSED_BYTES = 16 * 1024 * 1024
DECODED_FRAMES = 8
MAX_SEQUENCES = 2
MAX_DISK_BYTES = 128 * 1024 * 1024
RENDER_VERSION = "cel-bodies-aa-v6"
_WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="spinner")


class Rotation:
    """Progressively build a revolution; retain a small decoded working set."""

    def __init__(
        self, shape: Shape, width: int, height: int, font_size: int, path: Path | None = None
    ):
        self.shape = shape
        self.size = width, height
        self.font_size = font_size
        self.path = path
        self.frames: dict[int, bytes] = {}
        self.decoded: OrderedDict[int, Image.Image] = OrderedDict()
        self.bytes = 0
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.error: str | None = None
        # A first frame is immediately available while the background worker
        # fills coarse angular coverage, followed by intermediate positions.
        if not self._load_cache():
            self._render(0)
        self.future = _WORKER.submit(self._build)

    def _render(self, index: int) -> bool:
        panel = render_panel(
            self.shape, index * ROTATION_SECONDS / FRAME_COUNT, *self.size, self.font_size
        )
        data = zlib.compress(panel.tobytes(), level=1)
        with self.lock:
            if self.bytes + len(data) > MAX_COMPRESSED_BYTES:
                self.error = "Frame cache capacity reached"
                return False
            self.frames[index] = data
            self.bytes += len(data)
        return True

    def _build(self) -> None:
        try:
            for step in (30, 10, 2, 1):
                for index in range(0, FRAME_COUNT, step):
                    if self.cancelled.is_set():
                        return
                    if index not in self.frames and not self._render(index):
                        return
            self._save_cache()
        except Exception as exc:
            self.error = type(exc).__name__

    def _load_cache(self) -> bool:
        if self.path is None or not self.path.exists():
            return False
        try:
            if self.path.stat().st_size > MAX_COMPRESSED_BYTES + 1024 * 1024:
                return False
            with zipfile.ZipFile(self.path) as archive:
                names = [str(index) for index in range(FRAME_COUNT)]
                if sorted(archive.namelist()) != sorted(names):
                    return False
                frames = {}
                total = 0
                expected = self.size[0] * self.size[1] * 4
                for index, name in enumerate(names):
                    info = archive.getinfo(name)
                    total += info.file_size
                    if total > MAX_COMPRESSED_BYTES:
                        return False
                    data = archive.read(name)
                    decoder = zlib.decompressobj()
                    raw = decoder.decompress(data, expected + 1)
                    if len(raw) != expected or not decoder.eof:
                        return False
                    frames[index] = data
            self.frames, self.bytes = frames, total
            self.path.touch()
            return True
        except (OSError, ValueError, zipfile.BadZipFile, zlib.error):
            return False

    def _save_cache(self) -> None:
        if self.path is None or self.cancelled.is_set() or len(self.frames) != FRAME_COUNT:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(
            dir=self.path.parent, prefix="rotation-", delete=False
        ) as f:
            temporary = Path(f.name)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
                for index, data in self.frames.items():
                    archive.writestr(str(index), data)
            temporary.replace(self.path)
            files = sorted(
                self.path.parent.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            total = 0
            for item in files:
                total += item.stat().st_size
                if total > MAX_DISK_BYTES and item != self.path:
                    item.unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)

    def panel(self, seconds: float) -> Image.Image:
        position = (seconds % ROTATION_SECONDS) * FRAME_COUNT / ROTATION_SECONDS
        index = math.floor(position) % FRAME_COUNT
        with self.lock:
            next_index = (index + 1) % FRAME_COUNT
            interpolate = index in self.frames and next_index in self.frames
            if index not in self.frames:
                index = min(
                    self.frames,
                    key=lambda candidate: min(
                        (candidate - index) % FRAME_COUNT, (index - candidate) % FRAME_COUNT
                    ),
                )

            def decode(key: int) -> Image.Image:
                if key in self.decoded:
                    panel = self.decoded.pop(key)
                else:
                    panel = Image.frombytes("RGBA", self.size, zlib.decompress(self.frames[key]))
                self.decoded[key] = panel
                while len(self.decoded) > DECODED_FRAMES:
                    self.decoded.popitem(last=False)
                return panel

            panel = decode(index)
            if interpolate and position % 1:
                panel = Image.blend(panel, decode(next_index), position % 1)
            return panel

    def close(self) -> None:
        self.cancelled.set()
        self.future.cancel()


class SpinnerFrames:
    """Share immutable assets across video viewers and repeated geometry loads."""

    def __init__(self) -> None:
        self.sequences: OrderedDict[tuple[str, int, int, int], Rotation] = OrderedDict()
        self.lock = threading.Lock()
        self.fingerprints: OrderedDict[int, tuple[Shape, str]] = OrderedDict()
        self.directory: Path | None = None

    def configure(self, directory: Path) -> None:
        if directory != self.directory:
            self.close()
            self.directory = directory

    def panel(
        self, shape: Shape, seconds: float, width: int, height: int, font_size: int
    ) -> Image.Image:
        with self.lock:
            fingerprint = self.fingerprints.pop(id(shape), None)
            if fingerprint is None:
                fingerprint = (
                    shape,
                    shape.content_id or hashlib.sha256(repr(shape).encode()).hexdigest(),
                )
            self.fingerprints[id(shape)] = fingerprint
            while len(self.fingerprints) > MAX_SEQUENCES * 2:
                self.fingerprints.popitem(last=False)
            key = fingerprint[1], width, height, font_size
            sequence = self.sequences.pop(key, None)
            if sequence is None:
                while len(self.sequences) >= MAX_SEQUENCES:
                    _, oldest = self.sequences.popitem(last=False)
                    oldest.close()
                asset_key = hashlib.sha256(
                    repr((RENDER_VERSION, FRAME_COUNT, key)).encode()
                ).hexdigest()
                path = self.directory / (asset_key + ".zip") if self.directory else None
                sequence = Rotation(shape, width, height, font_size, path)
            self.sequences[key] = sequence
        return sequence.panel(seconds)

    def close(self) -> None:
        with self.lock:
            for sequence in self.sequences.values():
                sequence.close()
            self.sequences.clear()
            self.fingerprints.clear()


SPINNER_FRAMES = SpinnerFrames()
