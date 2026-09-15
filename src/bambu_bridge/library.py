"""Immutable print artifacts with resumable, hash-verified local storage.

Archiving has no dependency on the printer transport or job dispatcher. Upload
retries cannot start a print. SQLite transactions serialize writers across
threads/processes; incomplete files are never served as committed artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CHUNK_LIMIT = 4 * 1024 * 1024
FILE_LIMIT = 512 * 1024 * 1024
CAPTURE_LIMIT = 2 * 1024 * 1024 * 1024
IDENTIFIER = r"^[0-9a-f]{32}$"
DIGEST = r"^[0-9a-f]{64}$"


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=160)
    role: Literal["slice", "project", "original", "preview"]
    sha256: str = Field(pattern=DIGEST)
    size: int = Field(ge=1, le=FILE_LIMIT, strict=True)

    @field_validator("name")
    @classmethod
    def plain_name(cls, name: str) -> str:
        if (
            name in {".", ".."}
            or any(c in name for c in '/\\:<>"|?*')
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
        ):
            raise ValueError("Use a plain artifact filename")
        return name


class Capture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    id: str = Field(pattern=IDENTIFIER)
    title: str = Field(min_length=1, max_length=160)
    slicer_version: str = Field(min_length=1, max_length=64)
    plate: int = Field(ge=1, le=1000, strict=True)
    originals: Literal["disabled", "complete", "partial", "unavailable"] = "disabled"
    # This claim is separate from file storage. Only a future round-trip verifier
    # may mark full project recovery as verified; clients cannot assert that here.
    artifacts: tuple[Artifact, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_artifacts(self) -> Capture:
        names = [a.name for a in self.artifacts]
        if len(set(names)) != len(names):
            raise ValueError("Artifact names must be unique")
        if sum(a.size for a in self.artifacts) > CAPTURE_LIMIT:
            raise ValueError("Capture exceeds 2 GiB")
        for role in ("slice", "project", "preview"):
            if sum(a.role == role for a in self.artifacts) > 1:
                raise ValueError("Use one slice, project and preview per capture")
        sizes: dict[str, int] = {}
        for a in self.artifacts:
            if a.sha256 in sizes and sizes[a.sha256] != a.size:
                raise ValueError("Conflicting sizes for the same content hash")
            sizes[a.sha256] = a.size
        has_original = any(a.role == "original" for a in self.artifacts)
        if self.originals == "complete" and not has_original:
            raise ValueError("Complete originals requires original artifacts")
        if self.originals in {"disabled", "unavailable"} and has_original:
            raise ValueError("Original artifacts conflict with originals status")
        return self


class LibraryError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail
        super().__init__(detail)


class Attempt(BaseModel):
    """One execution of immutable slice bytes, independent of physical slot choices."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=IDENTIFIER)
    capture_id: str = Field(pattern=IDENTIFIER)
    slice_sha256: str = Field(pattern=DIGEST)
    printer_id: str = Field(min_length=1, max_length=64)
    source: Literal["native", "bridge", "replay"]
    source_id: str = Field(pattern=IDENTIFIER)
    source_revision: int | None = Field(default=None, ge=0, strict=True)
    state: Literal[
        "queued", "uploading", "submitted", "accepted", "active", "preparing", "printing", "paused",
        "completed", "failed", "canceled", "interrupted", "unknown", "ended", "not_started",
    ]
    source_state: str = Field(min_length=1, max_length=40)
    created_at: float = Field(ge=0, allow_inf_nan=False)
    started_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    finished_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    # Preserve the exact wire choices as history. Replay validates NEW choices.
    ams_mapping: tuple[int, ...] | None = Field(default=None, max_length=64)
    use_ams: bool | None = None
    error_code: str | None = Field(default=None, max_length=160)
    acknowledged: bool = False


class LibraryStore:
    def __init__(
        self, root: Path, *, quota: int = 20 * 1024**3, _restoring: bool = False
    ):
        self.root, self.quota = root.resolve(), quota
        if (self.root / "INCOMPLETE").exists() and not _restoring:
            raise LibraryError(503, "This library is an incomplete backup or restore")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("blobs", "uploads"):
            (self.root / name).mkdir(exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS captures (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, manifest TEXT NOT NULL,
                    created REAL NOT NULL, finalized REAL, deleted REAL);
                CREATE TABLE IF NOT EXISTS artifacts (
                    capture TEXT NOT NULL REFERENCES captures(id), name TEXT NOT NULL,
                    hash TEXT NOT NULL, size INTEGER NOT NULL, offset INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(capture, name));
                CREATE TABLE IF NOT EXISTS blobs (
                    hash TEXT PRIMARY KEY, size INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS artifact_hash ON artifacts(hash);
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, capture TEXT NOT NULL REFERENCES captures(id),
                    source TEXT NOT NULL, source_id TEXT NOT NULL,
                    document TEXT NOT NULL, observed REAL NOT NULL,
                    UNIQUE(source, source_id));
                CREATE INDEX IF NOT EXISTS attempt_capture ON attempts(capture);
                CREATE TABLE IF NOT EXISTS attempt_events (
                    id INTEGER PRIMARY KEY, attempt TEXT NOT NULL REFERENCES attempts(id),
                    observed REAL NOT NULL, state TEXT NOT NULL, document TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS attempt_event_order ON attempt_events(attempt,id);
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.root / "library.sqlite3", timeout=30)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            yield db

    def _capture(self, db: sqlite3.Connection, cid: str, owner: str | None) -> sqlite3.Row:
        row = db.execute("SELECT * FROM captures WHERE id=?", (cid,)).fetchone()
        if row is None or row["deleted"] or (owner is not None and row["owner"] != owner):
            raise LibraryError(404, "Capture not found")
        return cast(sqlite3.Row, row)

    def create(self, owner: str, capture: Capture) -> dict[str, Any]:
        manifest = capture.model_dump_json()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM captures WHERE id=?", (capture.id,)).fetchone()
            if old is not None:
                if old["deleted"] or old["owner"] != owner or old["manifest"] != manifest:
                    raise LibraryError(409, "Capture ID already has a different manifest")
            else:
                # Reserve declared bytes, including partial uploads. Count each
                # capture separately for a conservative upper bound under retries.
                reserved = db.execute("SELECT COALESCE(SUM(size),0) FROM artifacts").fetchone()[0]
                if reserved + sum(a.size for a in capture.artifacts) > self.quota:
                    raise LibraryError(507, "Library quota reached; review retained captures")
                db.execute(
                    "INSERT INTO captures VALUES(?,?,?,?,NULL,NULL)",
                    (capture.id, owner, manifest, time.time()),
                )
                db.executemany(
                    "INSERT INTO artifacts(capture,name,hash,size) VALUES(?,?,?,?)",
                    [(capture.id, a.name, a.sha256, a.size) for a in capture.artifacts],
                )
        return self.get(capture.id, owner)

    def get(self, cid: str, owner: str | None = None) -> dict[str, Any]:
        with self.connect() as db:
            row = self._capture(db, cid, owner)
            result: dict[str, Any] = json.loads(row["manifest"])
            result.update(created=row["created"], finalized=row["finalized"])
            status = {}
            for a in db.execute("SELECT * FROM artifacts WHERE capture=?", (cid,)):
                blob = db.execute("SELECT size FROM blobs WHERE hash=?", (a["hash"],)).fetchone()
                stored = bool(blob and blob["size"] == a["size"])
                status[a["name"]] = {
                    "stored": stored,
                    "offset": a["size"] if stored else a["offset"],
                }
            result["uploads"] = status
            result["state"] = "stored" if row["finalized"] else "pending"
            result["project_roundtrip_verified"] = False
            result["attempts"] = [
                json.loads(a["document"])
                for a in db.execute("SELECT document FROM attempts WHERE capture=?", (cid,))
            ]
            return result

    def capture_state(self, cid: str) -> str:
        """Internal reconciliation helper: tombstones prevent automatic resurrection."""
        with self.connect() as db:
            row = db.execute("SELECT deleted,finalized FROM captures WHERE id=?", (cid,)).fetchone()
            if row is None:
                return "missing"
            if row["deleted"] is not None:
                return "deleted"
            return "stored" if row["finalized"] is not None else "pending"

    def record_attempt(self, attempt: Attempt) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            capture = self._capture(db, attempt.capture_id, None)
            if capture["finalized"] is None:
                raise LibraryError(409, "Finalize the slice before attaching a print attempt")
            manifest = Capture.model_validate_json(capture["manifest"])
            if not any(
                a.role == "slice" and a.sha256 == attempt.slice_sha256 for a in manifest.artifacts
            ):
                raise LibraryError(409, "Attempt bytes do not match this capture's slice")
            old = db.execute("SELECT document FROM attempts WHERE id=?", (attempt.id,)).fetchone()
            document = attempt.model_dump_json()
            if old:
                previous = Attempt.model_validate_json(old["document"])
                identity = (
                    "capture_id", "slice_sha256", "printer_id", "source", "source_id",
                    "created_at", "ams_mapping", "use_ams",
                )
                if any(getattr(previous, key) != getattr(attempt, key) for key in identity):
                    raise LibraryError(
                        409, "An execution's identity and chosen mapping are immutable"
                    )
                if (
                    previous.source_revision is not None
                    and (attempt.source_revision is None
                         or attempt.source_revision <= previous.source_revision)
                ):
                    return  # a slower observer cannot overwrite a newer durable receipt
                if old["document"] == document:
                    return
            else:
                existing = db.execute(
                    "SELECT id FROM attempts WHERE source=? AND source_id=?",
                    (attempt.source, attempt.source_id),
                ).fetchone()
                if existing:
                    raise LibraryError(409, "This source execution is already recorded")
            now = time.time()
            db.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "document=excluded.document,observed=excluded.observed",
                (attempt.id, attempt.capture_id, attempt.source, attempt.source_id, document, now),
            )
            db.execute(
                "INSERT INTO attempt_events(attempt,observed,state,document) VALUES(?,?,?,?)",
                (attempt.id, now, attempt.state, document),
            )

    def attempt_events(self, cid: str, attempt_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            self._capture(db, cid, None)
            row = db.execute(
                "SELECT id FROM attempts WHERE id=? AND capture=?", (attempt_id, cid)
            ).fetchone()
            if row is None:
                raise LibraryError(404, "Print attempt not found")
            return [
                {"observed_at": e["observed"], "attempt": json.loads(e["document"])}
                for e in db.execute(
                    "SELECT observed,document FROM attempt_events WHERE attempt=? ORDER BY id",
                    (attempt_id,),
                )
            ]

    def append(self, cid: str, name: str, offset: int, data: bytes, owner: str) -> dict[str, Any]:
        if not data or len(data) > CHUNK_LIMIT or offset < 0:
            raise LibraryError(413, "Use nonempty chunks up to 4 MiB and a nonnegative offset")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            capture = self._capture(db, cid, owner)
            a = db.execute(
                "SELECT * FROM artifacts WHERE capture=? AND name=?", (cid, name)
            ).fetchone()
            if a is None:
                raise LibraryError(404, "Artifact not declared")
            if capture["finalized"]:
                raise LibraryError(409, "Finalized captures are immutable")
            committed = self.root / "blobs" / a["hash"]
            if committed.is_file() and committed.stat().st_size == a["size"]:
                with committed.open("rb") as source:
                    valid = hashlib.file_digest(source, "sha256").hexdigest() == a["hash"]
                if valid:
                    # Recover a file rename whose DB commit was interrupted, or
                    # deduplicate a blob uploaded by another capture.
                    db.execute("INSERT OR IGNORE INTO blobs VALUES(?,?)", (a["hash"], a["size"]))
                    db.execute(
                        "UPDATE artifacts SET offset=? WHERE capture=? AND name=?",
                        (a["size"], cid, name),
                    )
                    db.commit()
                    return self.get(cid, owner)
            if offset != a["offset"] or offset + len(data) > a["size"]:
                raise LibraryError(409, "Offset mismatch; query capture status before retrying")
            # Filenames are never filesystem paths. Hash the manifest key for the
            # per-capture staging name; the content hash names committed blobs.
            staging = (
                self.root / "uploads" / hashlib.sha256((cid + "\0" + name).encode()).hexdigest()
            )
            with staging.open("r+b" if staging.exists() else "w+b") as target:
                target.seek(0, 2)
                if target.tell() < offset:
                    raise LibraryError(409, "Staging data is missing; cancel and recapture")
                target.truncate(offset)  # discard an uncommitted tail after a crash
                target.seek(offset)
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            end = offset + len(data)
            if end == a["size"]:
                with staging.open("rb") as source:
                    digest = hashlib.file_digest(source, "sha256").hexdigest()
                if digest != a["hash"]:
                    # Keep DB offset unchanged. The client can correct/retry the
                    # last chunk; earlier corruption needs cancel + new capture.
                    raise LibraryError(422, "Artifact checksum does not match the manifest")
                destination = self.root / "blobs" / digest
                # The existing-file fast path above already verified healthy
                # blobs. A same-name damaged blob may be repaired only with
                # bytes that independently match its immutable content hash.
                os.replace(staging, destination)
                sync_directory(destination.parent)
                db.execute("INSERT OR IGNORE INTO blobs VALUES(?,?)", (digest, end))
            db.execute("UPDATE artifacts SET offset=? WHERE capture=? AND name=?", (end, cid, name))
        return self.get(cid, owner)

    def finalize(self, cid: str, owner: str) -> dict[str, Any]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._capture(db, cid, owner)
            for a in db.execute("SELECT * FROM artifacts WHERE capture=?", (cid,)):
                row = db.execute("SELECT size FROM blobs WHERE hash=?", (a["hash"],)).fetchone()
                path = self.root / "blobs" / a["hash"]
                if row is None or row["size"] != a["size"] or not path.is_file():
                    raise LibraryError(409, "Upload every declared artifact before finalizing")
                with path.open("rb") as source:
                    if hashlib.file_digest(source, "sha256").hexdigest() != a["hash"]:
                        raise LibraryError(422, "Stored artifact failed integrity verification")
            db.execute(
                "UPDATE captures SET finalized=COALESCE(finalized,?) WHERE id=?", (time.time(), cid)
            )
        return self.get(cid, owner)

    def list(
        self,
        *,
        limit: int = 50,
        before: float | None = None,
        before_id: str = "",
        owner: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id FROM captures WHERE deleted IS NULL "
                "AND (created<? OR (created=? AND id<?)) "
                "AND (? IS NULL OR owner=?) ORDER BY created DESC,id DESC LIMIT ?",
                (
                    before if before is not None else float("inf"),
                    before,
                    before_id,
                    owner,
                    owner,
                    min(max(limit, 1), 100),
                ),
            ).fetchall()
        return [self.get(row["id"], owner) for row in rows]

    def download(self, cid: str, name: str) -> tuple[Path, str]:
        with self.connect() as db:
            capture = self._capture(db, cid, None)
            a = db.execute(
                "SELECT * FROM artifacts WHERE capture=? AND name=?", (cid, name)
            ).fetchone()
            if not capture["finalized"] or a is None:
                raise LibraryError(404, "Stored artifact not found")
            path = self.root / "blobs" / a["hash"]
            if not path.is_file() or path.stat().st_size != a["size"]:
                raise LibraryError(409, "Stored artifact is missing or damaged")
            with path.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != a["hash"]:
                    raise LibraryError(409, "Stored artifact is damaged")
            return path, a["hash"]

    def usage(self) -> dict[str, int]:
        with self.connect() as db:
            return {
                "stored_bytes": db.execute("SELECT COALESCE(SUM(size),0) FROM blobs").fetchone()[0],
                "reserved_bytes": db.execute(
                    "SELECT COALESCE(SUM(size),0) FROM artifacts"
                ).fetchone()[0],
                "quota_bytes": self.quota,
                "captures": db.execute(
                    "SELECT COUNT(*) FROM captures WHERE deleted IS NULL"
                ).fetchone()[0],
            }

    def delete(self, cid: str) -> None:
        """Explicit deletion only. Unreferenced blob cleanup is a separate action."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._capture(db, cid, None)
            names = [
                row[0] for row in db.execute("SELECT name FROM artifacts WHERE capture=?", (cid,))
            ]
            db.execute("DELETE FROM artifacts WHERE capture=?", (cid,))
            db.execute(
                "DELETE FROM attempt_events WHERE attempt IN "
                "(SELECT id FROM attempts WHERE capture=?)", (cid,),
            )
            db.execute("DELETE FROM attempts WHERE capture=?", (cid,))
            db.execute("UPDATE captures SET deleted=?,manifest='{}' WHERE id=?", (time.time(), cid))
        for name in names:
            (
                self.root / "uploads" / hashlib.sha256((cid + "\0" + name).encode()).hexdigest()
            ).unlink(missing_ok=True)

    def collect_unreferenced(self) -> int:
        removed = 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            referenced = {row[0] for row in db.execute("SELECT DISTINCT hash FROM artifacts")}
            known = {row[0] for row in db.execute("SELECT hash FROM blobs")}
            # Include orphaned atomic renames whose transaction never committed.
            known.update(p.name for p in (self.root / "blobs").iterdir() if p.is_file())
            for digest in known - referenced:
                if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    continue
                (self.root / "blobs" / digest).unlink(missing_ok=True)
                db.execute("DELETE FROM blobs WHERE hash=?", (digest,))
                removed += 1
        return removed

    def verify(self) -> dict[str, Any]:
        problems: list[str] = []
        with self.connect() as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = not db.execute("PRAGMA foreign_key_check").fetchall()
            problems.extend(
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT a.hash FROM artifacts a JOIN captures c ON c.id=a.capture "
                    "LEFT JOIN blobs b ON b.hash=a.hash WHERE c.finalized IS NOT NULL "
                    "AND (b.hash IS NULL OR b.size!=a.size)"
                )
            )
            for row in db.execute("SELECT hash,size FROM blobs"):
                path = self.root / "blobs" / row["hash"]
                if not path.is_file() or path.stat().st_size != row["size"]:
                    problems.append(row["hash"])
                    continue
                with path.open("rb") as source:
                    if hashlib.file_digest(source, "sha256").hexdigest() != row["hash"]:
                        problems.append(row["hash"])
        return {
            "ok": integrity == "ok" and foreign_keys and not problems,
            "database": integrity,
            "foreign_keys": foreign_keys,
            "damaged": problems,
        }


def sync_directory(path: Path) -> None:
    """Make blob renames durable before SQLite commits on the Linux server."""
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
