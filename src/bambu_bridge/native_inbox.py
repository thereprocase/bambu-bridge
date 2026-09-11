"""Durable native-upload custody. No printer work on the receiving path.

Private payloads and command bodies live beside the private pairing store.
An immutable printer filename prevents a later upload replacing a queued job.
Dispatch is deliberately at-most-once: a crash after dispatch begins requires
operator reconciliation, never automatic replay of a physical start.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import posixpath
import sqlite3
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


class InboxError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def logical_path(value: str, cwd: str = "/") -> str:
    if any(c in value for c in "\r\n\0"):
        raise ValueError("Invalid path")
    path = posixpath.normpath(posixpath.join(cwd, value))
    if path.startswith("/sdcard/"):
        path = path[len("/sdcard") :]
    return "/" + path.lstrip("/")


class NativeInbox:
    def __init__(self, directory: Path, *, budget: int = 512 * 1024 * 1024):
        self.directory = directory / "native-inbox"
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.database = self.directory / "inbox.sqlite3"
        self.budget = budget
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS uploads (
                id TEXT PRIMARY KEY, printer TEXT NOT NULL, logical TEXT NOT NULL,
                remote TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT, state TEXT NOT NULL, code TEXT,
                command TEXT, start_state TEXT, created INTEGER NOT NULL)
            """)
        self.database.chmod(0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self) -> None:
        with self.connect() as db:
            db.execute("""UPDATE uploads SET state='failed', code='BBFTP_RESTART_REVIEW'
                          WHERE state IN ('receiving', 'delivering')""")
            db.execute("""UPDATE uploads SET start_state='unknown', code='BBSTART_UNKNOWN'
                          WHERE start_state='dispatching'""")

    def reserve(self, printer: str, name: str, maximum: int) -> dict[str, Any]:
        identifier = uuid.uuid4().hex
        logical = logical_path(name)
        suffix = ".gcode.3mf" if logical.endswith(".3mf") else ".bin"
        remote = posixpath.join(posixpath.dirname(logical), f"beluga-{identifier}{suffix}")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            used, count = db.execute(
                "SELECT COALESCE(SUM(bytes),0), COUNT(*) FROM uploads"
            ).fetchone()
            if used + maximum > self.budget or count >= 128:
                raise OSError("Inbox capacity exhausted")
            db.execute(
                "INSERT INTO uploads VALUES (?,?,?,?,?,NULL,'receiving',NULL,NULL,NULL,"
                "unixepoch())",
                (identifier, printer, logical, remote, maximum),
            )
        return self.get(identifier)

    def get(self, identifier: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM uploads WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise ValueError("Unknown upload")
            return dict(row)

    def payload(self, identifier: str) -> Path:
        if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
            raise ValueError("Invalid upload identity")
        return self.directory / (identifier + ".payload")

    async def receive(self, reader: asyncio.StreamReader, row: dict[str, Any], maximum: int):
        """Drain promptly into a bounded disk spool; acknowledge only after fsync+commit."""
        path = self.payload(row["id"])
        total = 0
        digest = hashlib.sha256()
        try:
            handle = await asyncio.to_thread(path.open, "xb")
        except OSError as exc:
            await asyncio.to_thread(self.fail, row["id"], "BBFTP_STORAGE_WRITE_FAILED")
            raise InboxError("BBFTP_STORAGE_WRITE_FAILED") from exc
        try:
            while chunk := await asyncio.wait_for(reader.read(256 * 1024), 60):
                total += len(chunk)
                if total > maximum:
                    raise ValueError("Transfer limit exceeded")
                digest.update(chunk)
                await asyncio.to_thread(handle.write, chunk)
            await asyncio.to_thread(self._seal, handle, row["id"], total, digest.hexdigest())
        except BaseException as exc:
            await asyncio.to_thread(handle.close)
            code = "BBFTP_RECEIVE_FAILED"
            if isinstance(exc, ValueError):
                code = "BBFTP_SIZE_LIMIT"
            elif isinstance(exc, OSError) and not isinstance(exc, ConnectionError | TimeoutError):
                code = "BBFTP_STORAGE_WRITE_FAILED"
            await asyncio.to_thread(self.fail, row["id"], code)
            if code != "BBFTP_RECEIVE_FAILED":
                raise InboxError(code) from exc
            raise
        finally:
            await asyncio.to_thread(handle.close)
        return self.get(row["id"])

    def _seal(self, handle, identifier: str, total: int, digest: str) -> None:
        handle.flush()
        os.fsync(handle.fileno())
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        with self.connect() as db:
            db.execute(
                "UPDATE uploads SET bytes=?,sha256=?,state='stored',code='BBFTP_STORED' "
                "WHERE id=? AND state='receiving'",
                (total, digest, identifier),
            )

    def fail(self, identifier: str, code: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE uploads SET state='failed',code=? WHERE id=?", (code, identifier))

    def transition(self, identifier: str, previous: str, state: str, code: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "UPDATE uploads SET state=?,code=? WHERE id=? AND state=?",
                    (state, code, identifier, previous),
                ).rowcount
                == 1
            )

    def pending(self, printer: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM uploads WHERE printer=? AND "
                    "(state='stored' OR start_state='queued') ORDER BY created,rowid",
                    (printer,),
                )
            ]

    def hold_start(self, printer: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        command = payload.get("print")
        if not isinstance(command, dict) or command.get("command") != "project_file":
            return None
        url = command.get("url")
        if not isinstance(url, str):
            return None
        parsed = urlsplit(url)
        if parsed.scheme not in ("", "file", "ftp", "ftps"):
            return None
        logical = logical_path(unquote(parsed.path))
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM uploads WHERE printer=? AND logical=? "
                "ORDER BY created DESC,rowid DESC LIMIT 1",
                (printer, logical),
            ).fetchone()
            if row is None:
                return None
            prior = db.execute(
                "SELECT id FROM uploads WHERE printer=? AND command=? AND id<>? LIMIT 1",
                (printer, encoded, row["id"]),
            ).fetchone()
            if prior is not None:
                raise ValueError("BBSTART_GENERATION_AMBIGUOUS")
            owner = db.execute(
                "SELECT id FROM uploads WHERE printer=? AND id<>? "
                "AND start_state IN ('queued','dispatching','sent','unknown') LIMIT 1",
                (printer, row["id"]),
            ).fetchone()
            if owner is not None:
                raise ValueError("BBSTART_UNRESOLVED")
            if row["start_state"] in ("unknown", "blocked", "cancelled"):
                raise ValueError(row["code"] or "BBSTART_REVIEW_REQUIRED")
            if row["command"] is not None and row["command"] != encoded:
                raise ValueError("BBSTART_CONFLICT")
            if row["state"] in ("receiving", "failed"):
                raise ValueError(row["code"] or "BBSTART_NOT_STORED")
            db.execute(
                "UPDATE uploads SET command=?,start_state=COALESCE(start_state,'queued') "
                "WHERE id=?",
                (encoded, row["id"]),
            )
        return self.get(row["id"])

    def claim_start(self, identifier: str) -> dict[str, Any] | None:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE uploads SET start_state='dispatching' "
                "WHERE id=? AND state='delivered' AND start_state='queued'",
                (identifier,),
            ).rowcount
        if not changed:
            return None
        row = self.get(identifier)
        payload = json.loads(row["command"])
        payload["print"]["url"] = "file:///sdcard" + row["remote"]
        return payload

    def dispatched(self, identifier: str, state: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE uploads SET start_state=?,code=? WHERE id=?",
                (state, "BBSTART_SENT" if state == "sent" else "BBSTART_UNKNOWN", identifier),
            )

    def cancel_queued(self, printer: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE uploads SET start_state='cancelled',code='BBSTART_CANCELLED' "
                "WHERE printer=? AND start_state='queued'",
                (printer,),
            )

    def block_start(self, identifier: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE uploads SET start_state='blocked',code='BBSTART_NOT_IDLE' "
                "WHERE id=? AND start_state='queued'",
                (identifier,),
            )

    def status(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,bytes,state,code,start_state FROM uploads "
                    "ORDER BY created DESC,rowid DESC"
                )
            ]
