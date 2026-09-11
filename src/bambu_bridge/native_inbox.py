"""Durable native-upload custody. No printer work on the receiving path.

Private payloads and command bodies live beside the private pairing store.
An immutable printer filename prevents a later upload replacing a queued job.
Dispatch is deliberately at-most-once: a crash after dispatch begins requires
operator reconciliation, never automatic replay of a physical start.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import posixpath
import sqlite3
import time
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
        self._lock = None
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS uploads (
                id TEXT PRIMARY KEY, printer TEXT NOT NULL, logical TEXT NOT NULL,
                remote TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT, state TEXT NOT NULL, code TEXT,
                command TEXT, start_state TEXT, created INTEGER NOT NULL)
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(uploads)")}
            for name, definition in {
                "dispatch_seq": "TEXT",
                "acknowledged": "INTEGER NOT NULL DEFAULT 0",
                "seen_active": "INTEGER NOT NULL DEFAULT 0",
                "dispatched_at": "INTEGER",
                "retained": "INTEGER NOT NULL DEFAULT 1",
                "kind": "TEXT NOT NULL DEFAULT 'upload'",
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE uploads ADD COLUMN {name} {definition}")
        self.database.chmod(0o600)

    def acquire(self) -> None:
        handle = (self.directory / "worker.lock").open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            handle.close()
            raise
        self._lock = handle

    def close(self) -> None:
        if self._lock:
            self._lock.close()
            self._lock = None

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
            db.execute(
                "UPDATE uploads SET start_state='cancelled',code='BBSTART_NOT_DISPATCHED' "
                "WHERE start_state='reserved'"
            )

    def reserve(self, printer: str, name: str, maximum: int) -> dict[str, Any]:
        identifier = uuid.uuid4().hex
        logical = logical_path(name)
        suffix = ".gcode.3mf" if logical.endswith(".3mf") else posixpath.splitext(logical)[1]
        if not suffix or len(suffix) > 16:
            suffix = ".bin"
        remote = posixpath.join(posixpath.dirname(logical), f"beluga-{identifier}{suffix}")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            used, count = db.execute(
                "SELECT COALESCE(SUM(bytes),0), COUNT(*) FROM uploads WHERE retained=1"
            ).fetchone()
            if used + maximum > self.budget or count >= 128:
                raise OSError("Inbox capacity exhausted")
            db.execute(
                "INSERT INTO uploads (id,printer,logical,remote,bytes,state,created) "
                "VALUES (?,?,?,?,?,'receiving',unixepoch())",
                (identifier, printer, logical, remote, maximum),
            )
        return self.get(identifier)

    def get(self, identifier: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM uploads WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise ValueError("Unknown upload")
            return dict(row)

    def lookup(self, printer: str, path: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM uploads WHERE printer=? AND logical=? AND kind='upload' "
                "AND state!='deleted' "
                "ORDER BY created DESC,rowid DESC LIMIT 1",
                (printer, path),
            ).fetchone()
            return dict(row) if row else None

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
                row["received_bytes"] = total
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
        path = self.payload(identifier)
        size = path.stat().st_size if path.exists() else 0
        with self.connect() as db:
            db.execute(
                "UPDATE uploads SET state='failed',code=?,"
                "bytes=CASE WHEN sha256 IS NULL THEN ? ELSE bytes END WHERE id=?",
                (code, size, identifier),
            )

    def open_verified(self, identifier: str):
        row = self.get(identifier)
        source = self.payload(identifier).open("rb")
        try:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
            if digest != row["sha256"] or source.tell() != row["bytes"]:
                raise InboxError("BBDELIVERY_INTEGRITY")
            source.seek(0)
            return source
        except BaseException:
            source.close()
            raise

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

    def unresolved(self, printer: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM uploads WHERE printer=? AND "
                    "(state IN ('receiving','stored','delivering') OR start_state IN "
                    "('reserved','queued','dispatching','sent','accepted','running','unknown')) "
                    "LIMIT 1",
                    (printer,),
                ).fetchone()
                is not None
            )

    def hold_start(self, printer: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        command = payload.get("print")
        if not isinstance(command, dict) or command.get("command") not in (
            "project_file",
            "gcode_file",
        ):
            return None
        url = command.get("url") if command["command"] == "project_file" else command.get("param")
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
                "SELECT id FROM uploads WHERE printer=? AND command=? AND id<>? "
                "AND start_state NOT IN ('completed','rejected','cancelled','resolved') LIMIT 1",
                (printer, encoded, row["id"]),
            ).fetchone()
            if prior is not None:
                raise ValueError("BBSTART_GENERATION_AMBIGUOUS")
            owner = db.execute(
                "SELECT id FROM uploads WHERE printer=? AND id<>? "
                "AND start_state IN "
                "('reserved','queued','dispatching','sent','accepted','running','unknown') "
                "LIMIT 1",
                (printer, row["id"]),
            ).fetchone()
            if owner is not None:
                raise ValueError("BBSTART_UNRESOLVED")
            if row["start_state"] in ("unknown", "blocked", "cancelled"):
                raise ValueError(row["code"] or "BBSTART_REVIEW_REQUIRED")
            if row["command"] is not None and row["command"] != encoded:
                raise ValueError("BBSTART_CONFLICT")
            if row["state"] not in ("stored", "delivering", "delivered"):
                raise ValueError(row["code"] or "BBSTART_NOT_STORED")
            db.execute(
                "UPDATE uploads SET command=?,start_state=COALESCE(start_state,'queued') "
                "WHERE id=?",
                (encoded, row["id"]),
            )
        return self.get(row["id"])

    def claim_start(self, identifier: str) -> dict[str, Any] | None:
        sequence = uuid.uuid4().hex
        with self.connect() as db:
            changed = db.execute(
                "UPDATE uploads SET start_state='dispatching',dispatch_seq=?,"
                "dispatched_at=unixepoch() "
                "WHERE id=? AND state='delivered' AND start_state='queued'",
                (sequence, identifier),
            ).rowcount
        if not changed:
            return None
        row = self.get(identifier)
        payload = json.loads(row["command"])
        if payload["print"]["command"] == "project_file":
            payload["print"]["url"] = "file:///sdcard" + row["remote"]
        else:
            prefix = "/sdcard" if payload["print"]["param"].startswith("/sdcard/") else ""
            payload["print"]["param"] = prefix + row["remote"]
        payload["print"]["sequence_id"] = sequence
        return payload

    def dispatched(self, identifier: str, state: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE uploads SET start_state=?,code=? WHERE id=? AND start_state='dispatching'",
                (state, "BBSTART_SENT" if state == "sent" else "BBSTART_UNKNOWN", identifier),
            )

    def claim_external(
        self, printer: str, payload: dict[str, Any], *, reserved: bool = False
    ) -> str:
        """Fence app/raw starts against deferred native starts at the common wire boundary."""
        identifier = uuid.uuid4().hex
        sequence = uuid.uuid4().hex
        command = payload["print"]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        path = logical_path(
            unquote(urlsplit(str(command.get("url", command.get("param", "")))).path)
        )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute(
                "SELECT id FROM uploads WHERE printer=? AND start_state IN "
                "('reserved','queued','dispatching','sent','accepted','running','unknown') LIMIT 1",
                (printer,),
            ).fetchone()
            if owner:
                raise ValueError("BBSTART_UNRESOLVED")
            db.execute(
                "INSERT INTO uploads (id,printer,logical,remote,bytes,state,code,command,"
                "start_state,created,dispatch_seq,dispatched_at,retained,kind) "
                "VALUES (?,?,?,?,0,'external','BBSTART_DISPATCHING',?,?,unixepoch(),"
                "?,unixepoch(),0,'external')",
                (
                    identifier,
                    printer,
                    path,
                    path,
                    encoded,
                    "reserved" if reserved else "dispatching",
                    sequence,
                ),
            )
        payload["print"] = {**command, "sequence_id": sequence}
        return identifier

    def activate_reserved(self, identifier: str, payload: dict[str, Any]) -> None:
        command = payload["print"]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        remote = logical_path(unquote(urlsplit(str(command.get("url", ""))).path))
        sequence = uuid.uuid4().hex
        with self.connect() as db:
            changed = db.execute(
                "UPDATE uploads SET start_state='dispatching',command=?,logical=?,remote=?,"
                "dispatch_seq=?,dispatched_at=unixepoch() WHERE id=? AND start_state='reserved'",
                (encoded, remote, remote, sequence, identifier),
            ).rowcount
            if changed != 1:
                raise ValueError("BBSTART_ALREADY_DISPATCHED")
        payload["print"] = {**command, "sequence_id": sequence}

    def abandon_reserved(self, identifier: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE uploads SET start_state='cancelled',code='BBSTART_NOT_DISPATCHED' "
                "WHERE id=? AND start_state='reserved'",
                (identifier,),
            )

    def observe(self, printer: str, report: dict[str, Any], snapshot: dict[str, Any]) -> None:
        """Only fresh report edges advance lifecycle; snapshots alone never prove a start."""
        incoming = report.get("print", {})
        if not isinstance(incoming, dict):
            return
        current = snapshot.get("print", {})
        filename = str(current.get("gcode_file", ""))
        state = incoming.get("gcode_state")
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM uploads WHERE printer=? AND start_state IN "
                "('dispatching','sent','accepted','running','unknown')",
                (printer,),
            ).fetchall()
            for row in rows:
                start_state, code = row["start_state"], row["code"]
                acknowledged, active = row["acknowledged"], row["seen_active"]
                if (
                    incoming.get("command") in ("project_file", "gcode_file")
                    and row["dispatch_seq"]
                    and str(incoming.get("sequence_id", "")) == row["dispatch_seq"]
                ):
                    result = str(incoming.get("result", "")).upper()
                    if result in ("SUCCESS", "OK"):
                        acknowledged = 1
                        if start_state in ("dispatching", "sent", "unknown"):
                            start_state, code = "accepted", "BBSTART_ACCEPTED"
                    elif result in ("FAIL", "FAILED", "ERROR"):
                        start_state, code = "rejected", "BBSTART_REJECTED"
                # Native targets are immutable UUID-named files. Do not claim a
                # foreign print from only a generic RUNNING/FINISH observation.
                matches = bool(filename) and (
                    logical_path(filename) == row["remote"]
                    or posixpath.basename(filename) == posixpath.basename(row["remote"])
                )
                if matches and state in ("PREPARE", "RUNNING", "PAUSE"):
                    active = 1
                    if row["kind"] == "upload":
                        # A fresh active report naming our unique immutable
                        # object is stronger evidence than a possibly lost ack.
                        acknowledged = 1
                    if acknowledged:
                        start_state, code = "running", "BBSTART_RUNNING"
                if matches and active and acknowledged and state in ("FINISH", "FAILED", "IDLE"):
                    start_state = "completed" if state == "FINISH" else "resolved"
                    code = "BBSTART_COMPLETED" if state == "FINISH" else "BBSTART_ENDED"
                if (start_state, code, acknowledged, active) != (
                    row["start_state"],
                    row["code"],
                    row["acknowledged"],
                    row["seen_active"],
                ):
                    db.execute(
                        "UPDATE uploads SET start_state=?,code=?,acknowledged=?,seen_active=? "
                        "WHERE id=?",
                        (start_state, code, acknowledged, active, row["id"]),
                    )

    def translated_report(self, printer: str, report: dict[str, Any]) -> dict[str, Any]:
        incoming = report.get("print", {})
        if not isinstance(incoming, dict) or incoming.get("command") not in (
            "project_file",
            "gcode_file",
        ):
            return report
        with self.connect() as db:
            row = db.execute(
                "SELECT command FROM uploads WHERE printer=? AND dispatch_seq=?",
                (printer, str(incoming.get("sequence_id", ""))),
            ).fetchone()
        if row:
            original = json.loads(row["command"])["print"]
            return {
                **report,
                "print": {**incoming, "sequence_id": original.get("sequence_id", "0")},
            }
        return report

    def expire_dispatch(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM uploads WHERE start_state IN ('dispatching','sent','accepted') "
                    "AND dispatched_at < unixepoch()-120"
                )
            ]
            db.execute(
                "UPDATE uploads SET start_state='unknown',code='BBSTART_UNKNOWN' "
                "WHERE start_state IN ('dispatching','sent','accepted') "
                "AND dispatched_at < unixepoch()-120"
            )
            return rows

    def resolve(self, identifier: str) -> None:
        """Owner-confirmed non-running resolution; caller must verify fresh idle state."""
        with self.connect() as db:
            row = db.execute("SELECT start_state FROM uploads WHERE id=?", (identifier,)).fetchone()
            if not row or row[0] not in (
                "unknown",
                "blocked",
                "cancelled",
                "rejected",
                "accepted",
                "sent",
                "running",
            ):
                raise ValueError("Upload is not eligible for manual resolution")
            db.execute(
                "UPDATE uploads SET start_state='resolved',code='BBSTART_OWNER_RESOLVED' "
                "WHERE id=?",
                (identifier,),
            )

    def cancel(self, identifier: str) -> None:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE uploads SET start_state='cancelled',code='BBSTART_CANCELLED' "
                "WHERE id=? AND start_state='queued'",
                (identifier,),
            ).rowcount
            if changed != 1:
                raise ValueError("Start is not queued; a dispatched print needs printer controls")

    def retry_delivery(self, identifier: str) -> None:
        row = self.get(identifier)
        if (
            row["state"] != "failed"
            or not row["sha256"]
            or row["start_state"] not in (None, "queued", "cancelled", "blocked")
        ):
            raise ValueError("Only a complete, undispatched upload can be retried")
        with self.connect() as db:
            db.execute(
                "UPDATE uploads SET state='stored',code='BBFTP_STORED' WHERE id=?", (identifier,)
            )

    def discard(self, identifier: str) -> None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM uploads WHERE id=?", (identifier,)).fetchone()
            if (
                not row
                or row["state"] in ("receiving", "stored", "delivering")
                or row["start_state"]
                in ("reserved", "queued", "dispatching", "sent", "accepted", "running", "unknown")
            ):
                raise ValueError("Resolve or cancel the pending job before discarding")
            self.payload(identifier).unlink(missing_ok=True)
            db.execute(
                "UPDATE uploads SET retained=0,code='BBFTP_LOCAL_COPY_DISCARDED' WHERE id=?",
                (identifier,),
            )

    def deleted(self, identifier: str) -> None:
        self.discard(identifier)
        with self.connect() as db:
            db.execute(
                "UPDATE uploads SET state='deleted',code='BBFTP_FILE_DELETED' WHERE id=?",
                (identifier,),
            )

    def prune(self) -> None:
        """Remove only this inbox's redundant, terminal local cache after seven days."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT id FROM uploads WHERE retained=1 AND created<? AND "
                "state='delivered' AND (start_state IS NULL OR start_state IN "
                "('completed','resolved','rejected','cancelled','blocked'))",
                (int(time.time()) - 604800,),
            ).fetchall()
            for row in rows:
                self.payload(row["id"]).unlink(missing_ok=True)
                db.execute("UPDATE uploads SET retained=0 WHERE id=?", (row["id"],))
            db.execute(
                "DELETE FROM uploads WHERE retained=0 AND created<unixepoch()-7776000 "
                "AND (start_state IS NULL OR start_state IN "
                "('completed','resolved','rejected','cancelled','blocked'))"
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
                    "SELECT id,CASE WHEN state='receiving' THEN 0 ELSE bytes END AS bytes,"
                    "state,code,start_state FROM uploads ORDER BY "
                    "COALESCE(start_state IN "
                    "('reserved','queued','dispatching','sent','accepted','running','unknown'),0) "
                    "DESC,"
                    "created DESC,rowid DESC LIMIT 100"
                )
            ]
