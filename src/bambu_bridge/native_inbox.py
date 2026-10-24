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
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, BinaryIO, Literal
from urllib.parse import unquote, urlsplit

from bambu_bridge.accounting_manifest import encode_manifest
from bambu_bridge.protocol.job_identity import empty_idle_report

TIMING_FIELDS = (
    "received_at",
    "delivery_started_at",
    "delivered_at",
    "start_requested_at",
    "readiness_started_at",
    "ready_at",
    "acknowledged_at",
    "running_at",
    "terminal_at",
)

# Seconds without an acknowledged, active report before a dispatched start is
# marked unknown.
DISPATCH_EXPIRY = 120
# A P1S moves from the start ACK to PREPARE within a few seconds, but a status
# report already in flight when the start was published can still say IDLE.
# Evidence that a start was lost only counts after this grace period.
LOST_START_GRACE = 30
# Reserved and queued starts should leave this state within one delivery and
# claim. Revisit this bound if starts can wait for an idle printer in future.
QUEUED_EXPIRY = 1800
# Upload to file-time gap still taken as the same file: transfer time plus
# clock difference between bridge and printer.
SENT_SLACK_S = 600
READY_STATES = ("IDLE", "FINISH", "FAILED")
ACTIVE_STATES = ("PREPARE", "RUNNING", "PAUSE")
# A start sent, or being sent, whose outcome the printer's reports still decide.
TRACKED_STATES = ("dispatching", "sent", "accepted", "running", "unknown")
# A start the bridge owns on its printer: new starts and adoption wait for it.
OWNER_STATES = ("reserved", "queued", *TRACKED_STATES)
TERMINAL_STATES = ("completed", "interrupted", "resolved", "rejected", "cancelled", "blocked")


def _start_state_in(states: tuple[str, ...]) -> str:
    return "start_state IN (" + ",".join(f"'{s}'" for s in states) + ")"


TRACKED_SQL = _start_state_in(TRACKED_STATES)
OWNER_SQL = _start_state_in(OWNER_STATES)
TERMINAL_SQL = _start_state_in(TERMINAL_STATES)
REVIEW_SQL = "(state='failed' OR start_state IN ('blocked','unknown'))"
ACTIVE_SQL = f"(state IN ('receiving','stored','delivering') OR {OWNER_SQL})"
ACKABLE_SQL = f"{REVIEW_SQL} AND (start_state IS NULL OR NOT {OWNER_SQL})"


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


def command_path(command: dict[str, Any]) -> str:
    """Decode Bambu's ftp://filename form as a printer-local path, not a host."""
    value = command.get("param") if command.get("command") == "gcode_file" else command.get("url")
    if not isinstance(value, str) or not value:
        raise ValueError("BBSTART_INVALID_PATH")
    parsed = urlsplit(value)
    if parsed.scheme not in ("", "file", "ftp", "ftps"):
        raise ValueError("BBSTART_INVALID_PATH")
    path = parsed.path
    if parsed.scheme in ("ftp", "ftps") and parsed.netloc and not path:
        path = parsed.netloc
    result = logical_path(unquote(path))
    if result == "/":
        raise ValueError("BBSTART_INVALID_PATH")
    return result


def _event(row: sqlite3.Row) -> dict[str, Any]:
    return {**dict(row), "manifest": json.loads(row["manifest"])}


class NativeInbox:
    def __init__(self, directory: Path, *, budget: int = 512 * 1024 * 1024):
        self.directory = directory / "native-inbox"
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.database = self.directory / "inbox.sqlite3"
        self.budget = budget
        self._lock = None
        # Called from observe()'s thread after a tracked print's layer advance
        # is committed; a wake hint only, so it must be cheap and thread-safe.
        self.on_layer: Callable[[], None] | None = None
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS uploads (
                id TEXT PRIMARY KEY, printer TEXT NOT NULL, logical TEXT NOT NULL,
                remote TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT, state TEXT NOT NULL, code TEXT,
                command TEXT, start_state TEXT, created INTEGER NOT NULL)
            """)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounting_reviews (
                    attempt_id TEXT PRIMARY KEY REFERENCES uploads(id),
                    reason TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accounting_manifests (
                    attempt_id TEXT PRIMARY KEY REFERENCES uploads(id),
                    manifest TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accounting_shadow_events (
                    attempt_id TEXT PRIMARY KEY REFERENCES uploads(id),
                    outcome TEXT NOT NULL, manifest TEXT NOT NULL,
                    delivery_state TEXT NOT NULL DEFAULT 'shadow', created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS accounting_events_pending
                    ON accounting_shadow_events(created_at) WHERE delivery_state='shadow';
                CREATE TABLE IF NOT EXISTS accounting_meta (key TEXT PRIMARY KEY, value);
                -- What each print's spools were charged so far (service/usage_ledger).
                CREATE TABLE IF NOT EXISTS usage_ledger (
                    attempt_id TEXT NOT NULL REFERENCES uploads(id), spool_id INTEGER NOT NULL,
                    counted_g REAL NOT NULL DEFAULT 0, written_g REAL NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'open', sent_before_g REAL, sent_delta_g REAL,
                    last_write_at REAL, reason TEXT, record TEXT, remaining_g REAL,
                    first_layer INTEGER, last_layer INTEGER,
                    PRIMARY KEY(attempt_id, spool_id)
                );
                -- What a print's debit could not settle, for the review queue.
                CREATE TABLE IF NOT EXISTS usage_shortfalls (
                    attempt_id TEXT PRIMARY KEY REFERENCES uploads(id),
                    shortfalls TEXT NOT NULL, updated_at REAL NOT NULL
                );
                -- Grams moved from one spool to another after the fact (service/usage_review).
                CREATE TABLE IF NOT EXISTS usage_moves (
                    id INTEGER PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES uploads(id),
                    from_spool INTEGER NOT NULL, to_spool INTEGER NOT NULL, grams REAL NOT NULL,
                    state TEXT NOT NULL, from_written_before REAL NOT NULL,
                    started_at REAL NOT NULL, finished_at REAL
                );
                -- How a held print was closed by hand (service/usage_review).
                CREATE TABLE IF NOT EXISTS usage_resolutions (
                    attempt_id TEXT PRIMARY KEY REFERENCES uploads(id), action TEXT NOT NULL,
                    charges TEXT NOT NULL, resolved_at REAL NOT NULL
                );
            """)
            # A print that ran and then failed or was cancelled ends as 'resolved'
            # (BBSTART_ENDED); it used filament up to the layer it reached, so it
            # is an interrupted attempt for accounting. An owner resolving a stuck
            # row is not. Same name as before, so an older build that creates it
            # only if missing keeps this definition.
            db.executescript("""
                DROP TRIGGER IF EXISTS accounting_terminal_shadow;
                CREATE TRIGGER accounting_terminal_shadow
                AFTER UPDATE OF start_state ON uploads
                WHEN (NEW.start_state IN ('completed','interrupted')
                      OR (NEW.start_state='resolved' AND NEW.code='BBSTART_ENDED'))
                  AND OLD.start_state IS NOT NEW.start_state
                BEGIN
                    INSERT OR IGNORE INTO accounting_shadow_events
                        (attempt_id,outcome,manifest,created_at)
                    SELECT NEW.id,
                           CASE NEW.start_state WHEN 'completed' THEN 'completed'
                                ELSE 'interrupted' END,
                           manifest,unixepoch()
                    FROM accounting_manifests WHERE attempt_id=NEW.id;
                END;
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(uploads)")}
            for name, definition in {
                "dispatch_seq": "TEXT",
                "acknowledged": "INTEGER NOT NULL DEFAULT 0",
                "seen_active": "INTEGER NOT NULL DEFAULT 0",
                "dispatched_at": "INTEGER",
                "retained": "INTEGER NOT NULL DEFAULT 1",
                "kind": "TEXT NOT NULL DEFAULT 'upload'",
                "client_peer": "TEXT",
                "revision": "INTEGER NOT NULL DEFAULT 0",
                "replay_request_id": "TEXT",
                "review_ack_at": "INTEGER",
                # Highest printer layer_num seen while this attempt was printing.
                "layer_reached": "INTEGER",
                **{name: "REAL" for name in TIMING_FIELDS},
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE uploads ADD COLUMN {name} {definition}")
            # The usage ledger grew after it was first deployed; an inbox from an
            # earlier release has the table without the later columns.
            ledger_columns = {row[1] for row in db.execute("PRAGMA table_info(usage_ledger)")}
            for name, definition in {
                "reason": "TEXT",
                "record": "TEXT",
                "remaining_g": "REAL",
                "first_layer": "INTEGER",
                "last_layer": "INTEGER",
            }.items():
                if name not in ledger_columns:
                    db.execute(f"ALTER TABLE usage_ledger ADD COLUMN {name} {definition}")
            # Capture milestones atomically with state changes, not on every
            # telemetry packet. Existing receipts retain NULL for unobserved times.
            db.execute("DROP TRIGGER IF EXISTS upload_timing_update")
            rules = {
                "received_at": "NEW.state='stored' AND OLD.state IS NOT NEW.state",
                "delivery_started_at": "NEW.state='delivering' AND OLD.state IS NOT NEW.state",
                "delivered_at": "NEW.state='delivered' AND OLD.state IS NOT NEW.state",
                "start_requested_at": (
                    "NEW.start_state='queued' " "AND OLD.start_state IS NOT NEW.start_state"
                ),
                "acknowledged_at": (
                    "NEW.acknowledged=1 " "AND OLD.acknowledged IS NOT NEW.acknowledged"
                ),
                "running_at": (
                    "NEW.start_state='running' " "AND OLD.start_state IS NOT NEW.start_state"
                ),
                "terminal_at": (
                    "NEW.start_state IN ('completed','interrupted','resolved',"
                    "'rejected','cancelled','blocked') "
                    "AND OLD.start_state IS NOT NEW.start_state"
                ),
            }
            now = "ROUND((julianday('now')-2440587.5)*86400.0,3)"
            assignments = ",".join(
                f"{name}=CASE WHEN {condition} THEN COALESCE({name},{now}) ELSE {name} END"
                for name, condition in rules.items()
            )
            db.execute(
                "CREATE TRIGGER IF NOT EXISTS upload_timing_update "
                "AFTER UPDATE OF state,start_state,acknowledged,seen_active ON uploads BEGIN "
                f"UPDATE uploads SET {assignments},revision=revision+1 WHERE id=NEW.id; END"
            )
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
    def connect(self) -> Iterator[sqlite3.Connection]:
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
            # A failed delivery cannot be claimed; release legacy owners before
            # the ordinary reserved-start restart cancellation below.
            db.execute(
                "UPDATE uploads SET start_state='blocked' "
                "WHERE state='failed' AND start_state IN ('reserved','queued')"
            )
            db.execute(
                "UPDATE uploads SET start_state='cancelled',code='BBSTART_NOT_DISPATCHED' "
                "WHERE start_state='reserved'"
            )

    def reserve(
        self,
        printer: str,
        name: str,
        maximum: int,
        *,
        peer: str | None = None,
        replay_request_id: str | None = None,
        replay_command: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        identifier = replay_request_id or uuid.uuid4().hex
        if bool(replay_request_id) != bool(replay_command):
            raise ValueError("A replay reservation requires its immutable start command")
        self.payload(identifier)  # validate identity before it participates in a remote path
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
                "INSERT INTO uploads (id,printer,logical,remote,bytes,state,created,client_peer,"
                "replay_request_id,command,start_state) "
                "VALUES (?,?,?,?,?,'receiving',unixepoch(),?,?,?,?)",
                (
                    identifier,
                    printer,
                    logical,
                    remote,
                    maximum,
                    peer,
                    replay_request_id,
                    json.dumps(replay_command, sort_keys=True) if replay_command else None,
                    "reserved" if replay_request_id else None,
                ),
            )
        return self.get(identifier)

    def queue_replay(self, identifier: str) -> None:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE uploads SET start_state='queued' WHERE id=? AND replay_request_id=? "
                "AND state IN ('stored','delivered') AND start_state='reserved'",
                (identifier, identifier),
            ).rowcount
            if changed != 1:
                raise ValueError("BBREPLAY_NOT_READY")

    def abandon_replay(self, identifier: str) -> None:
        """Release an unsubmitted replay after transfer failure; never touch a sent start."""
        with self.connect() as db:
            db.execute(
                "UPDATE uploads SET state='failed',start_state='cancelled',"
                "code='BBSTART_NOT_DISPATCHED' "
                "WHERE id=? AND replay_request_id=? AND start_state IN ('reserved','queued')",
                (identifier, identifier),
            )

    def get(self, identifier: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM uploads WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise ValueError("Unknown upload")
            return dict(row)

    def unresolved_starts(self, printer: str) -> list[dict[str, Any]]:
        """Starts sent, or being sent, to the printer whose outcome is still open."""
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM uploads WHERE printer=? AND {TRACKED_SQL}", (printer,)
            ).fetchall()
        return [dict(row) for row in rows]

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

    def archive_page(self, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Read-only, stable receipt enumeration for the independent print library."""
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT rowid AS archive_cursor,* FROM uploads WHERE rowid>? "
                    "AND kind IN ('upload','observed') AND sha256 IS NOT NULL "
                    "AND command IS NOT NULL "
                    "ORDER BY rowid LIMIT ?",
                    (after, min(max(limit, 1), 100)),
                )
            ]

    async def receive(
        self, reader: asyncio.StreamReader, row: dict[str, Any], maximum: int
    ) -> dict[str, Any]:
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

    def _seal(self, handle: BinaryIO, identifier: str, total: int, digest: str) -> None:
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
            # claim_start requires delivered, so failed rows must not retain
            # the printer's start mutex even when they are not library replays.
            db.execute(
                "UPDATE uploads SET state='failed',code=?,review_ack_at=NULL,"
                "start_state=CASE WHEN start_state IN "
                "('reserved','queued') THEN 'blocked' ELSE start_state END,"
                "bytes=CASE WHEN sha256 IS NULL THEN ? ELSE bytes END WHERE id=?",
                (code, size, identifier),
            )

    def open_verified(self, identifier: str) -> BinaryIO:
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
                    f"SELECT 1 FROM uploads WHERE printer=? AND {ACTIVE_SQL} LIMIT 1", (printer,)
                ).fetchone()
                is not None
            )

    def hold_start(
        self, printer: str, payload: dict[str, Any], *, peer: str | None = None
    ) -> dict[str, Any] | None:
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
        logical = command_path(command)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM uploads WHERE printer=? AND logical=? "
                "AND kind='upload' "
                "AND (client_peer=? OR client_peer IS NULL) "
                "ORDER BY created DESC,rowid DESC LIMIT 1",
                (printer, logical, peer),
            ).fetchone()
            if row is None:
                if db.execute(
                    "SELECT 1 FROM uploads WHERE printer=? AND logical=? AND kind='upload' LIMIT 1",
                    (printer, logical),
                ).fetchone():
                    raise ValueError("BBSTART_UPLOAD_OWNER_MISMATCH")
                return None
            prior = db.execute(
                "SELECT id FROM uploads WHERE printer=? AND command=? AND id<>? "
                "AND start_state NOT IN ('completed','interrupted',"
                "'rejected','cancelled','resolved') LIMIT 1",
                (printer, encoded, row["id"]),
            ).fetchone()
            if prior is not None:
                raise ValueError("BBSTART_GENERATION_AMBIGUOUS")
            owner = db.execute(
                f"SELECT id FROM uploads WHERE printer=? AND id<>? AND {OWNER_SQL} LIMIT 1",
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

    def review_accounting(self, identifier: str, reason: str) -> None:
        """Record why a shadow attempt has no attributable usage estimate."""
        if not re.fullmatch(r"[a-z0-9_]{1,80}", reason):
            raise ValueError("ACCOUNTING_REVIEW_REASON")
        with self.connect() as db:
            if not db.execute("SELECT id FROM uploads WHERE id=?", (identifier,)).fetchone():
                raise ValueError("ACCOUNTING_ATTEMPT_MISSING")
            db.execute(
                "INSERT OR IGNORE INTO accounting_reviews VALUES (?,?,unixepoch())",
                (identifier, reason),
            )

    def printing_attempts(
        self, printer: str | None = None
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Attempts printing now, oldest first, each with its frozen manifest."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT u.*, m.manifest AS frozen FROM uploads u "
                "JOIN accounting_manifests m ON m.attempt_id=u.id "
                "WHERE u.start_state='running' AND (? IS NULL OR u.printer=?) ORDER BY u.created",
                (printer, printer),
            ).fetchall()
        return [(attempt, json.loads(attempt.pop("frozen"))) for attempt in map(dict, rows)]

    def printing_attempt(self, printer: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """The attempt printing now on this printer (the latest) and its manifest, if any."""
        found = self.printing_attempts(printer)
        return found[-1] if found else None

    def freeze_accounting(self, identifier: str, manifest: dict[str, Any]) -> None:
        """Reserve context in the canonical attempt DB before dispatch, never after.

        Identical recovery is idempotent even after dispatch. Different context
        cannot replace a reservation. Callers still need a qualified assignment
        snapshot and a dispatch policy; this method never enables charging.
        """
        encoded = encode_manifest(manifest)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT manifest FROM accounting_manifests WHERE attempt_id=?", (identifier,)
            ).fetchone()
            if prior:
                if prior["manifest"] != encoded:
                    raise ValueError("ACCOUNTING_MANIFEST_CONFLICT")
                return
            row = db.execute("SELECT * FROM uploads WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise ValueError("ACCOUNTING_ATTEMPT_MISSING")
            if row["start_state"] not in (None, "reserved", "queued"):
                raise ValueError("ACCOUNTING_ALREADY_DISPATCHED")
            if row["kind"] == "upload" and row["sha256"] != manifest["artifact_sha256"]:
                raise ValueError("ACCOUNTING_ARTIFACT_CHANGED")
            db.execute(
                "INSERT INTO accounting_manifests VALUES (?,?,unixepoch())", (identifier, encoded)
            )

    def accounting_page(
        self, limit: int = 50, offset: int = 0, delivery_state: str | None = None
    ) -> list[dict[str, Any]]:
        """Recent prints and what happened to their debit; manifest fields, never manifests.

        `delivery_state` narrows the page to prints in that state, so the held
        ones can be listed and counted in one call.
        """
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("ACCOUNTING_PAGE_INVALID")
        if delivery_state not in (None, "shadow", "delivered", "review"):
            raise ValueError("ACCOUNTING_DELIVERY_STATE")
        with self.connect() as db:
            rows = db.execute(
                "SELECT u.id,u.printer,u.start_state,e.outcome,e.delivery_state,r.reason,"
                "json_extract(u.command,'$.print.subtask_name') AS title,"
                "u.created AS created_at,e.created_at AS ended_at,s.shortfalls AS shortfalls,"
                "x.action AS resolved_action,x.resolved_at AS resolved_at,"
                "(SELECT json_group_array(json_object('spool_id',l.spool_id,'grams',l.written_g,"
                "'layers',CASE WHEN l.first_layer IS NULL THEN NULL"
                " ELSE json_array(l.first_layer,l.last_layer) END))"
                " FROM usage_ledger l WHERE l.attempt_id=u.id AND l.written_g>0) AS charged,"
                "json_extract(m.manifest,'$.planned_grams') AS planned_grams,"
                "json_extract(m.manifest,'$.analysis_issues') AS analysis_issues,"
                "json_extract(m.manifest,'$.artifact_sha256') AS artifact_sha256,"
                "json_extract(m.manifest,'$.plate') AS plate,"
                "json_extract(m.manifest,'$.provider_id') AS provider_id,"
                "json_extract(m.manifest,'$.assignment_revision') AS assignment_revision,"
                "json_extract(m.manifest,'$.parser_version') AS parser_version "
                "FROM uploads u LEFT JOIN accounting_manifests m ON m.attempt_id=u.id "
                "LEFT JOIN accounting_shadow_events e ON e.attempt_id=u.id "
                "LEFT JOIN accounting_reviews r ON r.attempt_id=u.id "
                "LEFT JOIN usage_shortfalls s ON s.attempt_id=u.id "
                "LEFT JOIN usage_resolutions x ON x.attempt_id=u.id "
                "WHERE (m.attempt_id IS NOT NULL OR r.attempt_id IS NOT NULL) "
                "AND (? IS NULL OR e.delivery_state=?) "
                "ORDER BY u.created DESC,u.id LIMIT ? OFFSET ?",
                (delivery_state, delivery_state, limit, offset),
            ).fetchall()
        return [
            {
                "attempt_id": row["id"],
                "printer_id": row["printer"],
                "title": row["title"] or "",
                "created_at": row["created_at"],
                "ended_at": row["ended_at"],
                "capture_id": None,
                "start_state": row["start_state"],
                "outcome": row["outcome"],
                "delivery_state": row["delivery_state"],
                # What Spoolman was charged, and what could not be settled.
                "charged": json.loads(row["charged"] or "[]"),
                "shortfalls": json.loads(row["shortfalls"] or "[]"),
                # How the owner closed it, when they did; null for the automatic ones.
                "resolution": {"action": row["resolved_action"], "resolved_at": row["resolved_at"]}
                if row["resolved_action"]
                else None,
                "mode": "counted",
                "automatic_debit_eligible": False,
                "review_reason": row["reason"],
                "planned_grams": row["planned_grams"],
                "artifact_sha256": row["artifact_sha256"],
                "plate": row["plate"],
                "parser_version": row["parser_version"],
                "provider_id": row["provider_id"],
                "assignment_revision": row["assignment_revision"],
                # Installed apps map over both; the manifest no longer lists sources.
                "sources": [],
                "analysis_issues": json.loads(row["analysis_issues"] or "[]"),
            }
            for row in rows
        ]

    def review_count(self) -> int:
        """Prints waiting for the owner's word on their debit."""
        with self.connect() as db:
            return int(
                db.execute(
                    "SELECT COUNT(*) FROM accounting_shadow_events WHERE delivery_state='review'"
                ).fetchone()[0]
            )

    def accounting_review(self, identifier: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT reason FROM accounting_reviews WHERE attempt_id=?", (identifier,)
            ).fetchone()
        return str(row["reason"]) if row else None

    def set_delivery_state(self, identifier: str, state: str) -> None:
        if state not in ("shadow", "delivered", "review"):
            raise ValueError("ACCOUNTING_DELIVERY_STATE")
        with self.connect() as db:
            db.execute(
                "UPDATE accounting_shadow_events SET delivery_state=? WHERE attempt_id=?",
                (state, identifier),
            )

    def accounting_event(self, identifier: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT e.*,u.printer AS printer_id FROM accounting_shadow_events e "
                "JOIN uploads u ON u.id=e.attempt_id WHERE e.attempt_id=?",
                (identifier,),
            ).fetchone()
        return _event(row) if row else None

    def deduct_since(self) -> float:
        """When automatic deduction was first switched on; later restarts keep it."""
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO accounting_meta VALUES ('deduct_since', unixepoch())")
            return float(
                db.execute("SELECT value FROM accounting_meta WHERE key='deduct_since'").fetchone()[
                    0
                ]
            )

    def pending_accounting_events(self, since: float) -> list[dict[str, Any]]:
        """Finished prints not yet debited, from `since` on; reads nothing else."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM accounting_shadow_events "
                "WHERE delivery_state='shadow' AND created_at>=? ORDER BY created_at,attempt_id",
                (float(since),),
            ).fetchall()
        return [_event(row) for row in rows]

    def accounting_events(self) -> list[dict[str, Any]]:
        """Internal shadow evidence; no provider writes or API publication."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM accounting_shadow_events ORDER BY created_at,attempt_id"
            ).fetchall()
        return [_event(row) for row in rows]

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
            original = urlsplit(payload["print"]["url"])
            if original.scheme in ("ftp", "ftps") and original.netloc and not original.path:
                payload["print"]["url"] = original.scheme + "://" + row["remote"].lstrip("/")
            else:
                payload["print"]["url"] = "file:///sdcard" + row["remote"]
            if "file" in payload["print"]:
                payload["print"]["file"] = posixpath.basename(row["remote"])
        else:
            prefix = "/sdcard" if payload["print"]["param"].startswith("/sdcard/") else ""
            payload["print"]["param"] = prefix + row["remote"]
        payload["print"]["sequence_id"] = sequence
        return dict(payload)

    def dispatched(self, identifier: str, state: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE uploads SET start_state=?,code=? WHERE id=? AND start_state='dispatching'",
                (state, "BBSTART_SENT" if state == "sent" else "BBSTART_UNKNOWN", identifier),
            )

    def has_active_print(self, printer: str) -> bool:
        """The bridge owns a start on this printer, so adoption would be refused."""
        with self.connect() as db:
            return bool(
                db.execute(
                    f"SELECT 1 FROM uploads WHERE printer=? AND {OWNER_SQL} LIMIT 1", (printer,)
                ).fetchone()
            )

    def sent_file(self, name: str, printer: str, written: float | None = None) -> bool:
        """The bridge uploaded this file to this printer; the library archives that upload.

        `written` is the printer's time for the file: one written more than
        SENT_SLACK_S after the bridge's upload has been replaced since.
        """
        with self.connect() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM uploads WHERE kind='upload' AND remote=? AND printer=? "
                    "AND (? IS NULL OR created>=?) LIMIT 1",
                    ("/" + name, printer, written, (written or 0) - SENT_SLACK_S),
                ).fetchone()
            )

    def adopt_running_print(
        self,
        printer: str,
        remote: str,
        command: dict[str, Any],
        manifest: dict[str, Any],
        *,
        sha256: str,
        started_at: float | None = None,
    ) -> str | None:
        """Track a print the bridge did not send, from the file the printer runs.

        The row starts as running and acknowledged, so observe() follows it to
        FINISH or a stop like any other attempt. `started_at` is when the print
        began, if the bridge saw it; refills are counted from then. None when
        the bridge already tracks a print on this printer.
        """
        encoded = encode_manifest(manifest)
        identifier = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                f"SELECT 1 FROM uploads WHERE printer=? AND {OWNER_SQL}", (printer,)
            ).fetchone():
                return None
            db.execute(
                "INSERT INTO uploads (id,printer,logical,remote,bytes,sha256,state,code,command,"
                "start_state,created,dispatched_at,acknowledged,seen_active,retained,kind) "
                "VALUES (?,?,?,?,0,?,'external','BBSTART_OBSERVED',?,'running',unixepoch(),"
                "COALESCE(?,unixepoch()),1,1,0,'observed')",
                (
                    identifier,
                    printer,
                    remote,
                    remote,
                    sha256,
                    json.dumps({"print": command}),
                    int(started_at) if started_at else None,
                ),
            )
            db.execute(
                "INSERT INTO accounting_manifests VALUES (?,?,unixepoch())", (identifier, encoded)
            )
        return identifier

    def claim_external(
        self, printer: str, payload: dict[str, Any], *, reserved: bool = False
    ) -> str:
        """Fence app/raw starts against deferred native starts at the common wire boundary."""
        identifier = uuid.uuid4().hex
        sequence = uuid.uuid4().hex
        command = payload["print"]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        path = "/" if reserved else command_path(command)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute(
                f"SELECT id FROM uploads WHERE printer=? AND {OWNER_SQL} LIMIT 1", (printer,)
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
        remote = command_path(command)
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

    def observe(self, printer: str, report: dict[str, Any], snapshot: dict[str, Any]) -> bool:
        """Only fresh report edges advance lifecycle; snapshots alone never prove a start."""
        incoming = report.get("print", {})
        if not isinstance(incoming, dict):
            return False
        changed = advanced = False
        current = snapshot.get("print", {})
        filename = str(current.get("gcode_file", ""))
        # Some prints name the archive only in `file` (gcode_file is the plate member).
        running = posixpath.basename(str(current.get("file", "")))
        state = incoming.get("gcode_state")
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM uploads WHERE printer=? AND {TRACKED_SQL}", (printer,)
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
                # Only for a start the printer acknowledged, or one adopted from
                # its own report: an external remote is a reusable name.
                if (
                    running
                    and running == posixpath.basename(row["remote"])
                    and (acknowledged or row["kind"] == "observed")
                ):
                    matches = True
                # P1S reports can name the archive through subtask_name while
                # gcode_file names the inner Metadata/plate_N.gcode member.
                # Require the exact dispatch ACK before using a reusable name.
                command = json.loads(row["command"] or "{}").get("print", {})
                task_name = command.get("subtask_name")
                if acknowledged and task_name and current.get("subtask_name") == task_name:
                    matches = True
                layer = row["layer_reached"]
                if matches and acknowledged and current.get("gcode_state") in ("RUNNING", "PAUSE"):
                    # Only a fresh report: the merged state can still hold the
                    # previous print's last layer.
                    with contextlib.suppress(TypeError, ValueError):
                        seen = int(incoming.get("layer_num") or 0)
                        if seen > (layer or 0):
                            layer = seen
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
                if active and acknowledged and empty_idle_report(incoming, current):
                    start_state, code = "interrupted", "BBSTART_INTERRUPTED"
                if row["kind"] == "observed" and start_state == "running":
                    # Rows adopted from the printer's report; end them on any sign
                    # the print is over, counting the layers it reached:
                    # another file began (the end went unseen while the bridge was down),
                    reprinted = bool(
                        not matches and incoming.get("file") and state in ACTIVE_STATES
                    )
                    # the same file began again (a start or full report whose layer
                    # count went back; a lone layer delta never ends a print),
                    with contextlib.suppress(KeyError, TypeError, ValueError):
                        if int(incoming["layer_num"]) < (row["layer_reached"] or 0):
                            reprinted = reprinted or state in ACTIVE_STATES
                    # or it ended before the adoption was written.
                    ended = current.get("gcode_state") in READY_STATES
                    if reprinted or ended:
                        finished = matches and current.get("gcode_state") == "FINISH"
                        start_state = "completed" if finished else "resolved"
                        code = "BBSTART_COMPLETED" if finished else "BBSTART_ENDED"
                        layer = row["layer_reached"]  # never the next print's layer
                # A dispatched start that was never seen active must not fence
                # the printer forever. Starts are published at QoS 0 on a clean
                # session and never retransmitted, so one the printer is not
                # acting on cannot arrive later from the bridge. Release it only
                # on a fresh report that contradicts it, never on elapsed time
                # alone, and never replay it.
                age = time.time() - row["dispatched_at"] if row["dispatched_at"] else 0
                if not active and start_state in ("sent", "accepted", "unknown"):
                    if age >= LOST_START_GRACE and empty_idle_report(incoming, current):
                        # Explicit empty identity: the printer restarted and
                        # holds no job, e.g. powered off right after the ACK.
                        start_state, code = "interrupted", "BBSTART_LOST"
                    elif age >= DISPATCH_EXPIRY and state in READY_STATES:
                        # Still reporting a ready state well after dispatch:
                        # the start either never began or already ended.
                        start_state, code = "resolved", "BBSTART_AUTO_RESOLVED"
                if layer != row["layer_reached"]:
                    # Progress only; not a lifecycle change.
                    db.execute("UPDATE uploads SET layer_reached=? WHERE id=?", (layer, row["id"]))
                    advanced = True
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
                    changed = True
        if advanced and self.on_layer is not None:
            # A failed wake only delays the debit to its worker's next poll.
            with contextlib.suppress(Exception):
                self.on_layer()
        return changed

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
        """Mark starts unacknowledged for DISPATCH_EXPIRY unknown; returns them as now stored."""
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "UPDATE uploads SET start_state='unknown',code='BBSTART_UNKNOWN',"
                    "review_ack_at=NULL WHERE start_state IN ('dispatching','sent','accepted') "
                    "AND dispatched_at < unixepoch()-? RETURNING *",
                    (DISPATCH_EXPIRY,),
                ).fetchall()
            ]

    def expire_queued(self) -> list[dict[str, Any]]:
        """Release stale starts that never reached dispatch; returns them as now stored.

        A cached upload can be old when armed, so use its start-request time
        when available; reserved rows and legacy receipts fall back to created.
        """
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "UPDATE uploads SET start_state='blocked',code='BBSTART_QUEUE_EXPIRED',"
                    "review_ack_at=NULL WHERE start_state IN ('reserved','queued') "
                    "AND dispatched_at IS NULL "
                    "AND COALESCE(start_requested_at,created) < unixepoch()-? RETURNING *",
                    (QUEUED_EXPIRY,),
                ).fetchall()
            ]

    def recoverable(self, printer: str) -> bool:
        """True when a fresh status report could let observe() release a lost start."""
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM uploads WHERE printer=? AND seen_active=0 "
                    "AND start_state IN ('sent','accepted','unknown') "
                    "AND dispatched_at <= unixepoch()-? LIMIT 1",
                    (printer, LOST_START_GRACE),
                ).fetchone()
                is not None
            )

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
                "WHERE id=? AND start_state IN ('reserved','queued')",
                (identifier,),
            ).rowcount
            if changed != 1:
                raise ValueError("Start is not pending; a dispatched print needs printer controls")

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
                "UPDATE uploads SET state='stored',code='BBFTP_STORED',review_ack_at=NULL "
                "WHERE id=?",
                (identifier,),
            )

    def discard(self, identifier: str) -> None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM uploads WHERE id=?", (identifier,)).fetchone()
            if (
                not row
                or row["state"] in ("receiving", "stored", "delivering")
                or row["start_state"] in OWNER_STATES
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
                f"state='delivered' AND (start_state IS NULL OR {TERMINAL_SQL})",
                (int(time.time()) - 604800,),
            ).fetchall()
            for row in rows:
                self.payload(row["id"]).unlink(missing_ok=True)
                db.execute("UPDATE uploads SET retained=0 WHERE id=?", (row["id"],))
            db.execute(
                "DELETE FROM uploads WHERE retained=0 AND created<unixepoch()-7776000 "
                f"AND (start_state IS NULL OR {TERMINAL_SQL})"
            )

    def cancel_queued(self, printer: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE uploads SET start_state='cancelled',code='BBSTART_CANCELLED' "
                "WHERE printer=? AND start_state='queued'",
                (printer,),
            )

    def block_start(self, identifier: str, code: str = "BBSTART_NOT_IDLE") -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE uploads SET start_state='blocked',code=?,review_ack_at=NULL "
                "WHERE id=? AND start_state='queued'",
                (code, identifier),
            )

    def mark_readiness(self, identifier: str, *, ready: bool = False) -> None:
        column = "ready_at" if ready else "readiness_started_at"
        with self.connect() as db:
            db.execute(
                f"UPDATE uploads SET {column}=? WHERE id=? AND start_state='queued'",
                (time.time(), identifier),
            )

    def status(
        self,
        *,
        printer: str | None = None,
        limit: int = 100,
        offset: int = 0,
        view: Literal["all", "review", "active"] = "all",
    ) -> list[dict[str, Any]]:
        view_sql = {
            "all": "1",
            "review": f"review_ack_at IS NULL AND {REVIEW_SQL}",
            "active": ACTIVE_SQL,
        }[view]
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,printer,logical,remote,kind,retained,"
                    "sha256 IS NOT NULL AS complete,"
                    "CASE WHEN state='receiving' THEN 0 ELSE bytes END AS bytes,"
                    "state,code,start_state,created,dispatched_at,review_ack_at,"
                    f"CASE WHEN review_ack_at IS NULL AND {REVIEW_SQL} "
                    "THEN 1 ELSE 0 END AS needs_review,"
                    + ",".join(TIMING_FIELDS)
                    + f" FROM uploads WHERE (? IS NULL OR printer=?) AND ({view_sql}) ORDER BY "
                    f"COALESCE({OWNER_SQL},0) DESC,"
                    "created DESC,rowid DESC LIMIT ? OFFSET ?",
                    (printer, printer, limit, offset),
                )
            ]

    def queue_overview(self, printer: str) -> dict[str, Any]:
        with self.connect() as db:
            owner = db.execute(
                f"SELECT id,logical,start_state FROM uploads WHERE printer=? AND {OWNER_SQL} "
                "ORDER BY created DESC,rowid DESC LIMIT 1",
                (printer,),
            ).fetchone()
            review_count = db.execute(
                f"SELECT COUNT(*) FROM uploads WHERE printer=? AND review_ack_at IS NULL "
                f"AND {REVIEW_SQL}",
                (printer,),
            ).fetchone()[0]
            acknowledgeable_count = db.execute(
                f"SELECT COUNT(*) FROM uploads WHERE printer=? AND review_ack_at IS NULL "
                f"AND {ACKABLE_SQL}",
                (printer,),
            ).fetchone()[0]
            return {
                "start_owner": owner["id"] if owner else None,
                "start_owner_state": owner["start_state"] if owner else None,
                "start_owner_logical": owner["logical"] if owner else None,
                "review_count": review_count,
                "acknowledgeable_count": acknowledgeable_count,
            }

    def acknowledge_past_reviews(self, printer: str) -> int:
        """Hide past review warnings without changing starts or deleting evidence."""
        with self.connect() as db:
            return db.execute(
                "UPDATE uploads SET review_ack_at=unixepoch() WHERE printer=? "
                f"AND review_ack_at IS NULL AND {ACKABLE_SQL}",
                (printer,),
            ).rowcount
