"""Portable native-inbox snapshots with restore staged for manual review."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from contextlib import closing
from pathlib import Path
from typing import IO, Any

from bambu_bridge.native_inbox import NativeInbox

VERSION = 1
IDENTIFIER = re.compile(r"[0-9a-f]{32}")
PAYLOAD = re.compile(r"[0-9a-f]{32}\.payload")
MAX_BACKUP_BYTES = 2 * 1024 * 1024 * 1024


def backups_root(directory: Path) -> Path:
    root = directory / "native-backups"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def backup_path(directory: Path, identifier: str) -> Path:
    if not IDENTIFIER.fullmatch(identifier):
        raise ValueError("Invalid backup identity")
    return backups_root(directory) / f"{identifier}.zip"


def _add_file(archive: zipfile.ZipFile, name: str, source: Path) -> dict[str, Any]:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"Backup source is missing: {name}")
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as reader, archive.open(name, "w") as writer:
        while chunk := reader.read(1024 * 1024):
            writer.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    return {"size": size, "sha256": digest.hexdigest()}


def create_backup(inbox: NativeInbox, directory: Path, printer: str) -> dict[str, Any]:
    """Snapshot committed receipts and complete immutable payloads together."""
    root = backups_root(directory)
    identifier = uuid.uuid4().hex
    partial = root / f".{identifier}.partial"
    target = backup_path(directory, identifier)
    try:
        with tempfile.TemporaryDirectory(prefix="native-snapshot-", dir=root) as temp:
            database = Path(temp) / "inbox.sqlite3"
            fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as output:
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
                    files: dict[str, dict[str, Any]] = {}
                    with inbox.connect() as lock:
                        lock.execute("BEGIN IMMEDIATE")
                        with (
                            closing(sqlite3.connect(inbox.database)) as source,
                            closing(sqlite3.connect(database)) as destination,
                        ):
                            source.backup(destination)
                        files["inbox.sqlite3"] = _add_file(archive, "inbox.sqlite3", database)
                        for row in lock.execute(
                            "SELECT id,bytes,sha256 FROM uploads "
                            "WHERE retained=1 AND sha256 IS NOT NULL"
                        ):
                            name = f"{row['id']}.payload"
                            entry = _add_file(archive, name, inbox.payload(row["id"]))
                            if entry["size"] != row["bytes"] or entry["sha256"] != row["sha256"]:
                                raise ValueError(f"Stored upload failed verification: {row['id']}")
                            files[name] = entry
                    report = {
                        "version": VERSION,
                        "printer_id": printer,
                        "created": int(time.time()),
                        "files": files,
                    }
                    archive.writestr("snapshot.json", json.dumps(report, separators=(",", ":")))
                output.flush()
                os.fsync(output.fileno())
        os.replace(partial, target)
        return {"id": identifier, "created": report["created"], "bytes": target.stat().st_size}
    finally:
        partial.unlink(missing_ok=True)


def list_backups(directory: Path) -> list[dict[str, Any]]:
    result = []
    for path in backups_root(directory).glob("*.zip"):
        if IDENTIFIER.fullmatch(path.stem) and path.is_file() and not path.is_symlink():
            stat = path.stat()
            result.append({"id": path.stem, "created": int(stat.st_mtime), "bytes": stat.st_size})
    return sorted(result, key=lambda row: row["created"], reverse=True)


def _copy_verified(source: IO[bytes], target: Path, expected: dict[str, Any]) -> None:
    digest = hashlib.sha256()
    size = 0
    with target.open("xb") as output:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > expected["size"]:
                raise ValueError("Backup member exceeds its manifest size")
            digest.update(chunk)
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if size != expected["size"] or digest.hexdigest() != expected["sha256"]:
        raise ValueError("Backup member failed checksum verification")


def stage_backup(
    directory: Path, identifier: str, printer: str, *, source_path: Path | None = None
) -> Path:
    """Verify a snapshot and make every pending start require operator review."""
    source = source_path or backup_path(directory, identifier)
    if not source.is_file() or source.is_symlink() or source.stat().st_size > MAX_BACKUP_BYTES:
        raise ValueError("Backup is unavailable or too large")
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        if len(names) > 1024 or len(names) != len(set(names)) or "snapshot.json" not in names:
            raise ValueError("Invalid backup members")
        if archive.getinfo("snapshot.json").file_size > 1024 * 1024:
            raise ValueError("Backup manifest is too large")
        report = json.loads(archive.read("snapshot.json"))
        files = report.get("files")
        if (
            report.get("version") != VERSION
            or report.get("printer_id") != printer
            or not isinstance(files, dict)
            or "inbox.sqlite3" not in files
            or set(names) != set(files) | {"snapshot.json"}
        ):
            raise ValueError("Backup format or printer does not match")
        total = 0
        for name, entry in files.items():
            if name != "inbox.sqlite3" and not PAYLOAD.fullmatch(name):
                raise ValueError("Invalid backup member name")
            if not isinstance(entry, dict) or type(entry.get("size")) is not int:
                raise ValueError("Invalid backup member metadata")
            size, digest = entry["size"], entry.get("sha256")
            if size < 0 or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Invalid backup member metadata")
            total += size
            if total > MAX_BACKUP_BYTES or archive.getinfo(name).file_size != size:
                raise ValueError("Backup contents exceed the allowed size")
        stage = Path(tempfile.mkdtemp(prefix="native-restore-", dir=directory))
        try:
            inbox_dir = stage / "native-inbox"
            inbox_dir.mkdir(mode=0o700)
            for name, entry in files.items():
                with archive.open(name) as reader:
                    _copy_verified(reader, inbox_dir / name, entry)
            database = inbox_dir / "inbox.sqlite3"
            with closing(sqlite3.connect(database)) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Backup database failed integrity check")
                rows = db.execute(
                    "SELECT id FROM uploads WHERE retained=1 AND sha256 IS NOT NULL"
                ).fetchall()
                if any(f"{row[0]}.payload" not in files for row in rows):
                    raise ValueError("Backup is missing a retained payload")
                # A restore must never replay a physical print or resume an
                # interrupted transfer without an explicit operator action.
                db.execute(
                    "UPDATE uploads SET state='failed',code='BBRESTORE_REVIEW' "
                    "WHERE state IN ('receiving','stored','delivering')"
                )
                db.execute(
                    "UPDATE uploads SET start_state='blocked',code='BBRESTORE_REVIEW' "
                    "WHERE start_state IN ('reserved','queued')"
                )
                db.execute(
                    "UPDATE uploads SET start_state='unknown',code='BBRESTORE_REVIEW' "
                    "WHERE start_state IN ('dispatching','sent','accepted','running')"
                )
                db.commit()
            return inbox_dir
        except Exception:
            shutil.rmtree(stage)
            raise
