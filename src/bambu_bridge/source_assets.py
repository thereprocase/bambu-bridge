"""Bounded, content-verified source assets and opaque persistent references."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MAX_SOURCE_BYTES = 120 * 1024 * 1024
MAX_CACHE_BYTES = 256 * 1024 * 1024


class SourceAssets:
    def __init__(self, directory: Path) -> None:
        self.directory = directory / "preview-sources"

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.directory / "references.sqlite3", timeout=30)
        try:
            with db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS refs (key TEXT PRIMARY KEY, digest TEXT NOT NULL)"
                )
                yield db
        finally:
            db.close()

    @staticmethod
    def _key(reference: str) -> str:
        return hashlib.sha256(reference.encode()).hexdigest()

    def read(self, reference: str) -> bytes | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT digest FROM refs WHERE key=?", (self._key(reference),)
            ).fetchone()
        if row is None:
            return None
        if len(row[0]) != 64 or any(c not in "0123456789abcdef" for c in row[0]):
            return None
        path = self.directory / str(row[0])
        try:
            if path.stat().st_size > MAX_SOURCE_BYTES:
                return None
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        if hashlib.sha256(data).hexdigest() != row[0]:
            with self._connect() as db:
                db.execute("DELETE FROM refs WHERE digest=?", (row[0],))
            return None
        try:
            os.utime(path, None)
        except FileNotFoundError:
            return None
        return data

    def put(self, reference: str, data: bytes) -> str:
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError("Preview source exceeds size limit")
        digest = hashlib.sha256(data).hexdigest()
        with self._connect() as db:
            path = self.directory / digest
            intact = False
            if path.exists():
                with path.open("rb") as source:
                    intact = hashlib.file_digest(source, "sha256").hexdigest() == digest
            if not intact:
                with tempfile.NamedTemporaryFile(
                    dir=self.directory, prefix="source-", delete=False
                ) as f:
                    temporary = Path(f.name)
                    try:
                        f.write(data)
                        f.flush()
                    except BaseException:
                        temporary.unlink(missing_ok=True)
                        raise
                temporary.replace(path)
            path.touch()
            db.execute("INSERT OR REPLACE INTO refs VALUES (?,?)", (self._key(reference), digest))
            db.execute(
                "DELETE FROM refs WHERE rowid NOT IN "
                "(SELECT rowid FROM refs ORDER BY rowid DESC LIMIT 4096)"
            )
            files = sorted(
                (
                    p
                    for p in self.directory.iterdir()
                    if len(p.name) == 64 and all(c in "0123456789abcdef" for c in p.name)
                ),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            total = 0
            for item in files:
                total += item.stat().st_size
                if total > MAX_CACHE_BYTES and item != path:
                    item.unlink(missing_ok=True)
                    db.execute("DELETE FROM refs WHERE digest=?", (item.name,))
        return digest
