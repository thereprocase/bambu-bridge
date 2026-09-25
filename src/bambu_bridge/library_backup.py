"""Consistent library-only backups; restore into a new directory without overwriting.

Run ``python -m bambu_bridge.library_backup --help``. Printer credentials and
the job dispatcher are deliberately outside this tool's scope.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from bambu_bridge.library import LibraryError, LibraryStore, sync_directory

BACKUP_VERSION = 1
MEMBER = re.compile(r"library\.sqlite3|(?:blobs|uploads)/[a-f0-9]{64}")


def copy_verified(source: Path, destination: Path, size: int, digest: str | None = None) -> str:
    """Copy exactly the committed bytes, including only a partial upload's prefix."""
    if source.is_symlink() or not source.is_file():
        raise LibraryError(409, "Backup source is missing or is a symbolic link")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    hasher = hashlib.sha256()
    with source.open("rb") as src, destination.open("xb") as dst:
        remaining = size
        while remaining:
            data = src.read(min(remaining, 4 * 1024 * 1024))
            if not data:
                raise LibraryError(409, "Backup source is shorter than the committed data")
            dst.write(data)
            hasher.update(data)
            remaining -= len(data)
        dst.flush()
        os.fsync(dst.fileno())
    actual = hasher.hexdigest()
    if digest is not None and actual != digest:
        raise LibraryError(409, "Backup checksum verification failed")
    return actual


def new_destination(source: Path, destination: Path) -> None:
    if destination.resolve().is_relative_to(source.resolve()):
        raise LibraryError(409, "Choose a destination outside the source directory")
    if destination.exists():
        raise LibraryError(409, "Destination must be a new directory")
    destination.mkdir(parents=True, mode=0o700)
    (destination / "INCOMPLETE").write_text("Backup operation has not completed.\n")


def backup(store: LibraryStore, destination: Path) -> dict[str, Any]:
    """Freeze writes while taking a SQLite snapshot and copying referenced bytes."""
    new_destination(store.root, destination)
    files: dict[str, Any] = {}
    with store.connect() as lock:
        lock.execute("BEGIN IMMEDIATE")
        # A separate read connection avoids SQLite backup waiting on its own
        # uncommitted write transaction. All library writers use the same lock.
        with (
            closing(sqlite3.connect(store.root / "library.sqlite3")) as source,
            closing(sqlite3.connect(destination / "library.sqlite3")) as target,
        ):
            source.backup(target)
        database = destination / "library.sqlite3"
        with database.open("rb") as data:
            files["library.sqlite3"] = {
                "size": database.stat().st_size,
                "sha256": hashlib.file_digest(data, "sha256").hexdigest(),
            }
        for row in lock.execute("SELECT hash,size FROM blobs"):
            name = "blobs/" + row["hash"]
            digest = copy_verified(store.root / name, destination / name, row["size"], row["hash"])
            files[name] = {"size": row["size"], "sha256": digest}
        for row in lock.execute(
            "SELECT capture,name,offset,hash FROM artifacts WHERE offset>0 "
            "AND hash NOT IN (SELECT hash FROM blobs)"
        ):
            staging = hashlib.sha256((row["capture"] + "\0" + row["name"]).encode()).hexdigest()
            name = "uploads/" + staging
            digest = copy_verified(store.root / name, destination / name, row["offset"])
            files[name] = {"size": row["offset"], "sha256": digest}
    report = {"schema_version": BACKUP_VERSION, "files": files}
    with (destination / "snapshot.json").open("x", encoding="utf-8") as out:
        json.dump(report, out, indent=2)
        out.flush()
        os.fsync(out.fileno())
    (destination / "INCOMPLETE").unlink()
    sync_directory(destination)
    return {"files": len(files), "bytes": sum(f["size"] for f in files.values())}


def restore(source: Path, destination: Path) -> LibraryStore:
    """Only an explicit new root may be restored. The active root stays untouched."""
    manifest = source / "snapshot.json"
    if (
        (source / "INCOMPLETE").exists()
        or not manifest.is_file()
        or manifest.stat().st_size > 16 * 1024 * 1024
    ):
        raise LibraryError(409, "Backup is incomplete or its manifest is invalid")
    report = json.loads(manifest.read_text(encoding="utf-8"))
    files = report.get("files", {})
    if report.get("schema_version") != BACKUP_VERSION or "library.sqlite3" not in files:
        raise LibraryError(409, "Unsupported backup manifest")
    for name, entry in files.items():
        if (
            not MEMBER.fullmatch(name)
            or not isinstance(entry["size"], int)
            or entry["size"] < 0
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            or not (source / name).resolve().is_relative_to(source.resolve())
        ):
            raise LibraryError(409, "Invalid backup file entry")
    new_destination(source, destination)
    for name, entry in files.items():
        copy_verified(source / name, destination / name, entry["size"], entry["sha256"])
    store = LibraryStore(destination, _restoring=True)
    if not store.verify()["ok"]:
        raise LibraryError(409, "Restored library failed integrity verification")
    (destination / "INCOMPLETE").unlink()
    sync_directory(destination)
    return store


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("backup", "restore"):
        command = sub.add_parser(name)
        command.add_argument("source", type=Path)
        command.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "backup":
            if not (args.source / "library.sqlite3").is_file():
                raise LibraryError(404, "Library database does not exist")
            print(json.dumps(backup(LibraryStore(args.source), args.destination)))
        else:
            print(json.dumps(restore(args.source, args.destination).verify()))
    except (LibraryError, OSError, ValueError, KeyError, TypeError) as exc:
        # An incomplete output is left intact for inspection, never overwritten.
        parser.exit(1, "Library operation failed: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
