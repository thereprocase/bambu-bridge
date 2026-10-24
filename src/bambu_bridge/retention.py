"""Age limits for regenerable preview caches and a count limit for inbox snapshots.

The preview caches (source archives, parsed geometry, spinner frames) already
cap their size on write; this removes entries nobody has read for a while, so
a quiet printer does not keep a full cache forever. Every entry is rebuilt on
demand from the inbox payload or the printer, so pruning never loses data.
Reads touch mtime, which keeps the active job's entries young.

Native-inbox snapshots are operator backups, not caches: only the count is
limited, newest kept.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

_SOURCE = re.compile(r"[0-9a-f]{64}")
_GEOMETRY = re.compile(r"[0-9a-f]{64}\.gz")
_FRAMES = re.compile(r"[0-9a-f]{64}\.zip")
_BACKUP = re.compile(r"[0-9a-f]{32}\.zip")


@dataclass(frozen=True, slots=True)
class Pruned:
    files: int = 0
    bytes: int = 0

    def __add__(self, other: Pruned) -> Pruned:
        return Pruned(self.files + other.files, self.bytes + other.bytes)


def _remove(paths: list[Path]) -> Pruned:
    files = size = 0
    for path in paths:
        try:
            length = path.stat().st_size
            path.unlink()
        except FileNotFoundError:
            continue
        files += 1
        size += length
    return Pruned(files, size)


def _stale(directory: Path, pattern: re.Pattern[str], cutoff: float) -> list[Path]:
    if not directory.is_dir():
        return []
    stale = []
    for path in directory.iterdir():
        if not pattern.fullmatch(path.name) or path.is_symlink():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                stale.append(path)
        except FileNotFoundError:
            continue
    return stale


def prune_preview_caches(directory: Path, max_age_s: float, *, now: float | None = None) -> Pruned:
    """Remove preview cache entries not read or written within ``max_age_s``."""
    if max_age_s <= 0:
        return Pruned()
    cutoff = (time.time() if now is None else now) - max_age_s
    sources = directory / "preview-sources"
    stale_sources = _stale(sources, _SOURCE, cutoff)
    if stale_sources:
        # Drop the references first so a concurrent read misses cleanly
        # instead of resolving to a file that is about to disappear.
        db = sqlite3.connect(sources / "references.sqlite3", timeout=30)
        try:
            with db:
                db.executemany(
                    "DELETE FROM refs WHERE digest=?", [(p.name,) for p in stale_sources]
                )
        except sqlite3.OperationalError:
            pass  # no references table yet; the files are unreachable anyway
        finally:
            db.close()
    result = _remove(stale_sources)
    result += _remove(_stale(directory / "preview-geometry", _GEOMETRY, cutoff))
    result += _remove(_stale(directory / "preview-frames", _FRAMES, cutoff))
    return result


def prune_native_backups(directory: Path, keep: int) -> Pruned:
    """Keep the newest ``keep`` native-inbox snapshots; 0 keeps all."""
    root = directory / "native-backups"
    if keep <= 0 or not root.is_dir():
        return Pruned()
    backups = []
    for path in root.iterdir():
        if _BACKUP.fullmatch(path.name) and not path.is_symlink():
            try:
                backups.append((path.stat().st_mtime, path))
            except FileNotFoundError:
                continue
    backups.sort(reverse=True)
    return _remove([path for _, path in backups[keep:]])


def prune_state(directory: Path, *, preview_max_age_days: int, native_backups_keep: int) -> Pruned:
    caches = prune_preview_caches(directory, preview_max_age_days * 86400)
    backups = prune_native_backups(directory, native_backups_keep)
    if caches.files or backups.files:
        log.info(
            "retention.pruned",
            preview_files=caches.files,
            preview_bytes=caches.bytes,
            native_backups=backups.files,
            native_backup_bytes=backups.bytes,
        )
    return caches + backups
