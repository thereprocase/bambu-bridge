"""Desktop-owned artifact custody. No slicer internals or printer commands."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, BinaryIO

from library_plugin import CHUNK, FILE_LIMIT, Outbox, atomic_json


def identity(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Invalid local receipt")
    return value


def filename(value: str) -> str:
    if (
        not value
        or len(value) > 160
        or value in {".", ".."}
        or value.endswith((".", " "))
        or any(c in value for c in '/\\:<>"|?*')
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or value.split(".")[0].upper()
        in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(10)),
            *(f"LPT{i}" for i in range(10)),
        }
    ):
        raise ValueError("Use an ordinary filename of at most 160 characters")
    return value


def copy_bytes(source: BinaryIO, destination: Path, limit: int) -> dict[str, Any]:
    digest, size = hashlib.sha256(), 0
    with destination.open("xb") as output:
        while data := source.read(CHUNK):
            size += len(data)
            if size > limit:
                raise ValueError("Local storage or file size limit reached")
            digest.update(data)
            output.write(data)
        if not size:
            raise ValueError("Empty files cannot be archived")
        output.flush()
        os.fsync(output.fileno())
    return {"size": size, "sha256": digest.hexdigest()}


def check_slice(path: Path, plate: int) -> None:
    """Check container/plate identity; print safety is the bridge's later preflight."""
    if type(plate) is not int or not 1 <= plate <= 1000:
        raise ValueError("Invalid plate index")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (
                len(names) > 4096
                or len(names) != len(set(names))
                or sum(entry.file_size for entry in entries) > 2 * 1024**3
                or any(entry.flag_bits & 1 for entry in entries)
                or any(
                    entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    for entry in entries
                )
            ):
                raise ValueError("Unsupported or oversized sliced archive")
            target = f"Metadata/plate_{plate}.gcode"
            if target not in names or archive.getinfo(target).file_size < 1:
                raise ValueError("The selected plate is absent from this sliced file")
    except zipfile.BadZipFile as exc:
        raise ValueError("Expected a sliced .gcode.3mf file") from exc


def check_project(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (
                len(names) > 4096
                or len(names) != len(set(names))
                or sum(entry.file_size for entry in entries) > 2 * 1024**3
                or any(entry.flag_bits & 1 for entry in entries)
                or "Metadata/project_settings.config" not in names
                or "3D/3dmodel.model" not in names
            ):
                raise ValueError("Select an Orca project saved with geometry and project settings")
    except zipfile.BadZipFile as exc:
        raise ValueError("Expected a saved Orca project .3mf") from exc


class InstanceLock:
    """One companion owns a data directory, including its quota and outbox."""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = (root / "instance.lock").open("a+b", buffering=0)
        try:
            self.file.seek(0, os.SEEK_END)
            if not self.file.tell():
                self.file.write(b"0")
            self.file.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("A companion is already using this data directory") from exc

    def close(self) -> None:
        self.file.close()


class Custody:
    def __init__(self, root: Path, quota: int = 4 * 1024**3):
        self.root, self.quota = root, quota
        self.lock = threading.RLock()
        for name in ("inputs", "inbox", "outbox"):
            (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.outbox = Outbox(root / "outbox", quota=quota)

    def used(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    def remember(self, source: Path, role: str) -> dict[str, Any]:
        """Snapshot a file the user explicitly selected before it can disappear."""
        if role not in {"project", "original"}:
            raise ValueError("Choose a project or original source")
        name = filename(source.name)
        if role == "project" and (
            source.suffix.lower() != ".3mf" or name.lower().endswith(".gcode.3mf")
        ):
            raise ValueError("Select a saved Orca project .3mf, not a sliced export")
        with self.lock:
            rid = uuid.uuid4().hex
            directory = self.root / "inputs" / rid
            directory.mkdir(mode=0o700)
            (directory / "files").mkdir(mode=0o700)
            try:
                before = source.stat()
                with source.open("rb") as stream:
                    details = copy_bytes(
                        stream,
                        directory / "files" / name,
                        min(FILE_LIMIT, self.quota - self.used() - 64 * 1024),
                    )
                after = source.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ino,
                ) or details["size"] != before.st_size:
                    raise ValueError("Source changed while it was being captured; save and retry")
                if role == "project":
                    check_project(directory / "files" / name)
                record = {"id": rid, "name": name, "role": role, "received": time.time(), **details}
                atomic_json(directory / "record.json", record)
                return record
            except Exception:
                # Only this operation's exact files; no recursive path deletion.
                (directory / "files" / name).unlink(missing_ok=True)
                (directory / "record.json").unlink(missing_ok=True)
                (directory / "files").rmdir()
                directory.rmdir()
                raise

    def receive(self, name: str, plate: int, source: BinaryIO) -> dict[str, Any]:
        filename(name)
        if not name.lower().endswith(".gcode.3mf"):
            raise ValueError("Upload a sliced .gcode.3mf file")
        with self.lock:
            rid = uuid.uuid4().hex
            directory = self.root / "inbox" / rid
            directory.mkdir(mode=0o700)
            (directory / "files").mkdir(mode=0o700)
            try:
                details = copy_bytes(
                    source,
                    directory / "files" / name,
                    min(FILE_LIMIT, self.quota - self.used() - 64 * 1024),
                )
                check_slice(directory / "files" / name, plate)
                record = {
                    "id": rid,
                    "name": name,
                    "plate": plate,
                    "received": time.time(),
                    "association": "unassigned",
                    **details,
                }
                atomic_json(directory / "record.json", record)
                return record
            except Exception:
                (directory / "files" / name).unlink(missing_ok=True)
                (directory / "record.json").unlink(missing_ok=True)
                (directory / "files").rmdir()
                directory.rmdir()
                raise

    def record(self, kind: str, rid: str) -> dict[str, Any]:
        if kind not in {"inputs", "inbox"}:
            raise ValueError("Invalid local record kind")
        record = json.loads(
            (self.root / kind / identity(rid) / "record.json").read_text(encoding="utf-8")
        )
        if not isinstance(record, dict):
            raise ValueError("Invalid local receipt record")
        if record["id"] != rid:
            raise ValueError("Local receipt identity changed")
        filename(record["name"])
        return record

    def rows(self, kind: str) -> list[dict[str, Any]]:
        if kind not in {"inputs", "inbox"}:
            raise ValueError("Invalid local record kind")
        with self.lock:
            return sorted(
                [
                    self.record(kind, p.name)
                    for p in (self.root / kind).iterdir()
                    if p.is_dir() and (p / "record.json").exists()
                ],
                key=lambda row: row["received"],
                reverse=True,
            )

    def freeze(
        self,
        rid: str,
        title: str,
        slicer: str,
        inputs: list[str],
        *,
        association_confirmed: bool = False,
        originals_complete: bool = False,
    ) -> str:
        """Explicitly associate snapshots; never guess by names, dates or active window."""
        with self.lock:
            row = self.record("inbox", rid)
            directory = self.root / "inbox" / rid
            pending = directory / "capture.json"
            if pending.exists():
                # Recover a crash after outbox commit but before the inbox acknowledgement.
                prior = json.loads(pending.read_text(encoding="utf-8"))
                cid = identity(prior["id"])
                if (self.outbox.root / cid / "manifest.json").exists():
                    if (
                        prior["inputs"] != inputs
                        or prior["title"] != title
                        or prior["slicer"] != slicer
                        or prior["originals_complete"] != originals_complete
                    ):
                        raise ValueError("This upload is already frozen with different choices")
                    row.update(
                        capture_id=cid, association="user_confirmed" if inputs else "slice_only"
                    )
                    atomic_json(directory / "record.json", row)
                    return cid
            if not title.strip() or len(title) > 160 or not slicer.strip() or len(slicer) > 64:
                raise ValueError("Supply a title and slicer version")
            if len(inputs) != len(set(inputs)) or len(inputs) > 63:
                raise ValueError("Choose at most 63 distinct input snapshots")
            if inputs and not association_confirmed:
                raise ValueError("Confirm these saved files belong to the selected upload")
            sources = [("slice", directory / "files" / row["name"])]
            records = [row]
            projects = originals = 0
            for key in inputs:
                record = self.record("inputs", key)
                projects += record["role"] == "project"
                originals += record["role"] == "original"
                sources.append(
                    (
                        record["role"],
                        self.root / "inputs" / identity(key) / "files" / record["name"],
                    )
                )
                records.append(record)
            if projects > 1:
                raise ValueError("Choose one saved project snapshot")
            if originals_complete and not originals:
                raise ValueError("No originals were selected")
            # Check custody bytes again; an edited local snapshot must not be relabeled.
            for (_, path), record in zip(sources, records, strict=True):
                with path.open("rb") as source:
                    if hashlib.file_digest(source, "sha256").hexdigest() != record["sha256"]:
                        raise ValueError("A local snapshot is damaged")
            if self.used() + sum(record["size"] for record in records) + 64 * 1024 > self.quota:
                raise ValueError("Local storage limit reached; existing copies are retained")
            cid = uuid.uuid4().hex
            declaration = {
                "id": cid,
                "title": title,
                "slicer": slicer,
                "inputs": inputs,
                "originals_complete": originals_complete,
            }
            atomic_json(pending, declaration)
            self.outbox.enqueue(
                title,
                slicer,
                row["plate"],
                sources,
                originals="complete"
                if originals_complete
                else "partial"
                if originals
                else "disabled",
                capture_id=cid,
            )
            row.update(capture_id=cid, association="user_confirmed" if inputs else "slice_only")
            atomic_json(directory / "record.json", row)
            return cid

    def deliver(self, cid: str, bridge: Any) -> dict[str, Any]:
        with self.lock:
            return self.outbox.deliver(cid, bridge)
