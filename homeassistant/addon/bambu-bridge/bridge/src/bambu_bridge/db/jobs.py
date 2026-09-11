"""SQLite persistence (spec 9). aiosqlite, one shared connection.

A bridge serves a handful of printers and writes infrequently (job/event
rows). A single connection guarded by WAL is simpler and entirely sufficient
than a pool; WAL also lets ``deploy/backup.sh`` ``.backup`` the live file.

``printers`` repo (M2); ``JobRepo`` / ``EventRepo`` (M4).
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any

import aiosqlite
import structlog
from pydantic import BaseModel

log = structlog.get_logger(__name__)


class JobState(StrEnum):
    """Job state machine states (contract §7.4, PR B remap).

    The flow:

        queued → uploading → submitted → preparing → printing → completed
                                 │           │          │
                                 └───────────┴─→ failed ┘
                                             └─→ canceled

    `submitted` records a dispatch attempt, not acknowledgement or proof
    that printing began. `preparing` covers the heat-soak + bed-leveling
    window where `gcode_state == RUNNING` but `layer_num == 0`. `printing`
    is gated on `layer_num > 0`. (`canceled` is US single-l; deliberate
    inconsistency with `completed` — see contract §7.4 note.)
    """

    QUEUED = "queued"
    UPLOADING = "uploading"
    SUBMITTED = "submitted"
    PREPARING = "preparing"
    PRINTING = "printing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def terminal(self) -> bool:
        return self in (
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELED,
        )


class Printer(BaseModel):
    """A registered printer row (spec 9 `printers`)."""

    id: str  # serial — primary key
    friendly_name: str
    ip: str
    access_code: str
    model: str | None = None
    added_at: int
    last_seen_at: int | None = None
    # TOFU: sha256(DER) of the printer's TLS leaf cert (REPORT §1 / contract
    # §3.6). Stored on register; compared on every connect. Mismatch ->
    # 403 printer_cert_changed (post-firmware-update re-trust prompt).
    cert_fingerprint: str | None = None
    # Installed nozzle type: "hardened_steel" allows 300 °C; NULL/"stainless_steel"
    # defaults to 280 °C (the safe stainless ceiling).  Set via
    # POST /printers/{id}/nozzle or learned from set_accessories MQTT reports.
    nozzle_type: str | None = None


class Job(BaseModel):
    """A print job row (spec 9 `jobs`)."""

    id: str
    printer_id: str
    file_name: str
    file_path: str | None = None
    state: JobState
    progress_pct: float | None = None
    layer_current: int | None = None
    layer_total: int | None = None
    queued_at: int
    started_at: int | None = None
    finished_at: int | None = None
    duration_s: int | None = None
    filament_used_g: float | None = None
    error_code: str | None = None
    metadata_json: str | None = None


class EventRecord(BaseModel):
    """One persisted event (spec 9 `events`). ts is unix epoch **ms**."""

    id: int
    printer_id: str
    job_id: str | None
    ts: int
    event_type: str
    payload_json: str | None = None
    severity: str | None = None
    dismissed_at: int | None = None

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json) if self.payload_json else {}


_MIGRATIONS = (
    # Contract §3.6 / REPORT §1 — TOFU cert fingerprint.
    "ALTER TABLE printers ADD COLUMN cert_fingerprint TEXT",
    # Contract §8.4 — per-event severity + dismiss tracking for the
    # NotificationsScreen. Both nullable so existing rows survive.
    "ALTER TABLE events ADD COLUMN severity TEXT",
    "ALTER TABLE events ADD COLUMN dismissed_at INTEGER",
    # Wave-1 controls: nozzle_type lets the API gate 300 °C only for
    # hardened-steel nozzles.  NULL = unknown = stainless fallback (280 °C).
    "ALTER TABLE printers ADD COLUMN nozzle_type TEXT",
    # G3 — filament memory table. CREATE TABLE IF NOT EXISTS is idempotent, but
    # the schema.sql path runs only on new databases; on existing ones we need
    # the explicit CREATE here so the table appears after an upgrade.
    """CREATE TABLE IF NOT EXISTS filament_memory (
  printer_id    TEXT NOT NULL,
  slot          INTEGER NOT NULL,
  make          TEXT,
  model         TEXT,
  profile       TEXT,
  tray_type_seen TEXT,
  updated_at    INTEGER NOT NULL,
  PRIMARY KEY (printer_id, slot)
)""",
    # §10.1 SlicedDateMemo persistence — survives restarts; keyed by
    # (filename, size_bytes) so a re-sliced file with the same name is never
    # stale.  learned_at is ISO-8601 UTC for pruning old rows by age.
    # sliced_at is ISO-8601 UTC (the timestamp embedded in the archive itself).
    """CREATE TABLE IF NOT EXISTS sliced_dates (
  filename    TEXT NOT NULL,
  size_bytes  INTEGER NOT NULL,
  sliced_at   TEXT NOT NULL,
  learned_at  TEXT NOT NULL,
  PRIMARY KEY (filename, size_bytes)
)""",
)

# Idempotent UPDATEs applied alongside ALTERs. Unlike the ALTER list these run
# UNGUARDED (no try/except): each is safe to re-run only because it matches a
# value the new code never emits, so on already-migrated data it is a 0-row
# no-op rather than an error.
_MIGRATION_UPDATES = (
    # Contract §7.4 PR B: JobState `started` → `submitted` rename. Existing
    # rows on `bambu-bridge-print4-proven` use the old name; new rows write
    # the new name. The rename is purely cosmetic (no logic gated on this
    # value on the wire), but a mixed-name DB confuses /jobs?state= filter
    # callers — sweep once at startup.
    "UPDATE jobs SET state='submitted' WHERE state='started'",
)


class Database:
    """Owns the aiosqlite connection and applies the schema on connect."""

    def __init__(self, path: str) -> None:
        self._path = path
        # Dedicated transaction connections must also see test in-memory DBs.
        self.connection_uri = (
            f"file:bridge-{uuid.uuid4().hex}?mode=memory&cache=shared"
            if path == ":memory:" else path
        )
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() not called")
        return self._conn

    async def connect(self) -> None:
        if self._path not in (":memory:", ""):
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.connection_uri, uri=True)
        self._conn.row_factory = aiosqlite.Row
        # Assert WAL + FK enforcement on every connection (spec 9).
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        schema = resources.files("bambu_bridge.db").joinpath("schema.sql").read_text()
        await self._conn.executescript(schema)
        # ALTER TABLE migrations — additive only; ignore "duplicate column".
        # SQLite has no `ADD COLUMN IF NOT EXISTS`, so try/except is the idiom.
        for ddl in _MIGRATIONS:
            try:
                await self._conn.execute(ddl)
            except aiosqlite.OperationalError as exc:  # noqa: PERF203
                if "duplicate column" not in str(exc).lower():
                    raise
        # Data migrations — UPDATEs that are idempotent by construction
        # (matching a value that the new code never emits, so re-runs are
        # 0-row no-ops).
        for ddl in _MIGRATION_UPDATES:
            await self._conn.execute(ddl)
        await self._conn.commit()
        log.info("db.connected", path=self._path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


class PrinterRepo:
    """CRUD for registered printers."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(self, printer: Printer) -> Printer:
        await self._db.conn.execute(
            """INSERT INTO printers
               (id, friendly_name, ip, access_code, model, added_at,
                last_seen_at, cert_fingerprint, nozzle_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                printer.id,
                printer.friendly_name,
                printer.ip,
                printer.access_code,
                printer.model,
                printer.added_at,
                printer.last_seen_at,
                printer.cert_fingerprint,
                printer.nozzle_type,
            ),
        )
        await self._db.conn.commit()
        log.info("db.printer_added", printer_id=printer.id)
        return printer

    async def get(self, printer_id: str) -> Printer | None:
        async with self._db.conn.execute(
            "SELECT * FROM printers WHERE id = ?", (printer_id,)
        ) as cur:
            row = await cur.fetchone()
        return Printer(**dict(row)) if row else None

    async def list(self) -> list[Printer]:
        async with self._db.conn.execute(
            "SELECT * FROM printers ORDER BY added_at"
        ) as cur:
            rows = await cur.fetchall()
        return [Printer(**dict(r)) for r in rows]

    async def update(
        self,
        printer_id: str,
        *,
        friendly_name: str | None = None,
        ip: str | None = None,
        access_code: str | None = None,
    ) -> Printer | None:
        """PATCH the mutable fields (spec 6 PATCH /printers/{id})."""
        sets: list[str] = []
        params: list[object] = []
        for col, val in (
            ("friendly_name", friendly_name),
            ("ip", ip),
            ("access_code", access_code),
        ):
            if val is not None:
                sets.append(f"{col} = ?")
                params.append(val)
        if sets:
            params.append(printer_id)
            await self._db.conn.execute(
                f"UPDATE printers SET {', '.join(sets)} WHERE id = ?", params
            )
            await self._db.conn.commit()
        return await self.get(printer_id)

    async def delete(self, printer_id: str) -> bool:
        cur = await self._db.conn.execute(
            "DELETE FROM printers WHERE id = ?", (printer_id,)
        )
        await self._db.conn.commit()
        return cur.rowcount > 0

    async def touch_last_seen(self, printer_id: str, ts: int | None = None) -> None:
        """Bump last_seen_at on a successful MQTT receive (spec 9)."""
        await self._db.conn.execute(
            "UPDATE printers SET last_seen_at = ? WHERE id = ?",
            (ts if ts is not None else int(time.time()), printer_id),
        )
        await self._db.conn.commit()

    async def update_cert_fingerprint(
        self, printer_id: str, fingerprint: str
    ) -> None:
        """TOFU re-trust path (contract §3.6 / §4.5 ``POST /trust``)."""
        await self._db.conn.execute(
            "UPDATE printers SET cert_fingerprint = ? WHERE id = ?",
            (fingerprint, printer_id),
        )
        await self._db.conn.commit()
        log.info("db.printer_cert_updated", printer_id=printer_id)


_JOB_COLS = (
    "id, printer_id, file_name, file_path, state, progress_pct, layer_current, "
    "layer_total, queued_at, started_at, finished_at, duration_s, "
    "filament_used_g, error_code, metadata_json"
)

# Job states that mean "the printer is (or should be) actively working on this
# run" — used by latest_active_started_at to recover a print's start time after
# a bridge restart. SUBMITTED is the first state that carries a started_at.
_LIVE_JOB_STATES = (
    JobState.SUBMITTED,
    JobState.PREPARING,
    JobState.PRINTING,
)


class JobRepo:
    """CRUD + filtered history for print jobs."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, job: Job) -> Job:
        await self._db.conn.execute(
            f"INSERT INTO jobs ({_JOB_COLS}) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job.id,
                job.printer_id,
                job.file_name,
                job.file_path,
                job.state.value,
                job.progress_pct,
                job.layer_current,
                job.layer_total,
                job.queued_at,
                job.started_at,
                job.finished_at,
                job.duration_s,
                job.filament_used_g,
                job.error_code,
                job.metadata_json,
            ),
        )
        await self._db.conn.commit()
        return job

    async def get(self, job_id: str) -> Job | None:
        async with self._db.conn.execute(
            f"SELECT {_JOB_COLS} FROM jobs WHERE id = ?", (job_id,)
        ) as cur:
            row = await cur.fetchone()
        return Job(**dict(row)) if row else None

    async def update(self, job_id: str, **fields: Any) -> Job | None:
        """Patch arbitrary columns (state, progress, timestamps, …)."""
        if not fields:
            return await self.get(job_id)
        cols = ", ".join(f"{k} = ?" for k in fields)
        params: list[Any] = [
            v.value if isinstance(v, JobState) else v for v in fields.values()
        ]
        params.append(job_id)
        await self._db.conn.execute(
            f"UPDATE jobs SET {cols} WHERE id = ?", params
        )
        await self._db.conn.commit()
        return await self.get(job_id)

    async def latest_active_started_at(self, printer_id: str) -> int | None:
        """Most recent non-terminal job's ``started_at`` epoch for a printer.

        Restart-recovery hook (Sauron started_at bridge): after a deploy the
        bridge may come up to find the printer already RUNNING with no
        in-memory start time. The job that was driving that print is still in
        a live state (submitted/preparing/printing) in jobs.db — its
        ``started_at`` (stamped at SUBMITTED) is the honest origin.

        Returns the epoch-seconds int, or ``None`` when there is no live job
        row with a start time (e.g. the print was started from the printer's
        own screen/SD card — those never create a job row, so the bridge has
        no record of when it began and must leave started_at unknown rather
        than fabricate ``now()``).

        Ordered by ``started_at DESC`` so the freshest live print wins if more
        than one row is somehow non-terminal.
        """
        placeholders = ", ".join("?" for _ in _LIVE_JOB_STATES)
        async with self._db.conn.execute(
            f"SELECT started_at FROM jobs "
            f"WHERE printer_id = ? AND state IN ({placeholders}) "
            "AND started_at IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (printer_id, *[s.value for s in _LIVE_JOB_STATES]),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return int(row[0])

    async def list(
        self,
        *,
        printer_id: str | None = None,
        state: JobState | None = None,
        since: int | None = None,
        until: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Job]:
        where: list[str] = []
        params: list[Any] = []
        if printer_id is not None:
            where.append("printer_id = ?")
            params.append(printer_id)
        if state is not None:
            where.append("state = ?")
            params.append(state.value)
        if since is not None:
            where.append("queued_at >= ?")
            params.append(since)
        if until is not None:
            where.append("queued_at <= ?")
            params.append(until)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        params.extend([limit, offset])
        async with self._db.conn.execute(
            f"SELECT {_JOB_COLS} FROM jobs{clause} "
            "ORDER BY queued_at DESC LIMIT ? OFFSET ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [Job(**dict(r)) for r in rows]


class EventRepo:
    """Append-only event log (spec 8: persist every transition)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(
        self,
        *,
        printer_id: str,
        event_type: str,
        job_id: str | None = None,
        payload: dict[str, Any] | None = None,
        ts_ms: int | None = None,
        severity: str | None = None,
    ) -> int:
        """Insert one event; returns the new row id (used by the API
        layer when building the §8.4 contract envelope)."""
        cur = await self._db.conn.execute(
            "INSERT INTO events "
            "(printer_id, job_id, ts, event_type, payload_json, severity) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                printer_id,
                job_id,
                ts_ms if ts_ms is not None else int(time.time() * 1000),
                event_type,
                json.dumps(payload) if payload is not None else None,
                severity,
            ),
        )
        await self._db.conn.commit()
        return int(cur.lastrowid or 0)

    _SELECT_COLS = (
        "id, printer_id, job_id, ts, event_type, payload_json, "
        "severity, dismissed_at"
    )

    async def list_for_job(self, job_id: str) -> list[EventRecord]:
        async with self._db.conn.execute(
            f"SELECT {self._SELECT_COLS} "
            "FROM events WHERE job_id = ? ORDER BY ts, id",
            (job_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [EventRecord(**dict(r)) for r in rows]

    async def list_for_printer(
        self,
        printer_id: str,
        *,
        limit: int = 100,
        since_ms: int | None = None,
        until_ms: int | None = None,
        severity: str | None = None,
        include_dismissed: bool = True,
    ) -> list[EventRecord]:
        """Filtered per-printer feed (contract §8.4).

        - `since_ms` / `until_ms` filter inclusively by unix-epoch ms.
        - `severity` filters to one bucket (info/warn/error).
        - `include_dismissed=False` skips rows with non-NULL dismissed_at,
          so the APK's "active" view drops what the user cleared.
        """
        where = ["printer_id = ?"]
        params: list[Any] = [printer_id]
        if since_ms is not None:
            where.append("ts >= ?")
            params.append(since_ms)
        if until_ms is not None:
            where.append("ts <= ?")
            params.append(until_ms)
        if severity is not None:
            where.append("severity = ?")
            params.append(severity)
        if not include_dismissed:
            where.append("dismissed_at IS NULL")
        params.append(limit)
        async with self._db.conn.execute(
            f"SELECT {self._SELECT_COLS} FROM events "
            f"WHERE {' AND '.join(where)} ORDER BY ts DESC, id DESC LIMIT ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
        return [EventRecord(**dict(r)) for r in rows]

    async def dismiss(self, event_id: int, *, ts_ms: int | None = None) -> bool:
        """Mark one event dismissed. Idempotent — re-dismissing rewrites the
        timestamp; the row stays exactly one row. Returns True if the row
        existed."""
        cur = await self._db.conn.execute(
            "UPDATE events SET dismissed_at = ? WHERE id = ?",
            (ts_ms if ts_ms is not None else int(time.time() * 1000), event_id),
        )
        await self._db.conn.commit()
        return cur.rowcount > 0

    async def dismiss_all_for_printer(
        self, printer_id: str, *, ts_ms: int | None = None
    ) -> int:
        """Bulk-dismiss every undismissed event for a printer. Returns the
        number of rows touched (the §8.6 "Clear all" implementation)."""
        cur = await self._db.conn.execute(
            "UPDATE events SET dismissed_at = ? "
            "WHERE printer_id = ? AND dismissed_at IS NULL",
            (ts_ms if ts_ms is not None else int(time.time() * 1000), printer_id),
        )
        await self._db.conn.commit()
        return int(cur.rowcount)

    async def get(self, event_id: int) -> EventRecord | None:
        async with self._db.conn.execute(
            f"SELECT {self._SELECT_COLS} FROM events WHERE id = ?",
            (event_id,),
        ) as cur:
            row = await cur.fetchone()
        return EventRecord(**dict(row)) if row else None


# Push by default for the meaningful end-of-job events (spec 13).
DEFAULT_NOTIFY_EVENTS = ("print_completed", "print_failed", "filament_runout")


class NotificationPrefs(BaseModel):
    """Per-printer push config. ``events is None`` => the default set."""

    printer_id: str
    enabled: bool = True
    events: list[str] | None = None

    def wants(self, event_name: str) -> bool:
        if not self.enabled:
            return False
        active = self.events if self.events is not None else DEFAULT_NOTIFY_EVENTS
        return event_name in active


class NotificationPrefsRepo:
    """Optional overrides; absence means "enabled, default events"."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, printer_id: str) -> NotificationPrefs:
        async with self._db.conn.execute(
            "SELECT printer_id, enabled, events_json FROM notification_prefs "
            "WHERE printer_id = ?",
            (printer_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return NotificationPrefs(printer_id=printer_id)
        d = dict(row)
        events = json.loads(d["events_json"]) if d["events_json"] else None
        return NotificationPrefs(
            printer_id=printer_id, enabled=bool(d["enabled"]), events=events
        )

    async def set(self, prefs: NotificationPrefs) -> NotificationPrefs:
        await self._db.conn.execute(
            "INSERT INTO notification_prefs (printer_id, enabled, events_json) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(printer_id) DO UPDATE SET "
            "enabled = excluded.enabled, events_json = excluded.events_json",
            (
                prefs.printer_id,
                int(prefs.enabled),
                json.dumps(prefs.events) if prefs.events is not None else None,
            ),
        )
        await self._db.conn.commit()
        return prefs


# --------------------------------------------------------------------------- #
# Print queue + spool inventory (contract §16 → v0 per autopilot directive)
# --------------------------------------------------------------------------- #


class QueueItem(BaseModel):
    """A queued print waiting to be started.

    Items here are staged 3MFs on the printer's storage; starting an item
    submits it via JobManager and removes it from the queue.
    """

    id: str
    printer_id: str
    file_path: str
    file_name: str
    ams_mapping: list[int] | None = None
    position: int
    added_at: int
    notes: str | None = None


class Spool(BaseModel):
    """A cabinet/off-AMS spool the user tracks manually.

    Distinct from AMS-loaded trays (reported by the printer in ams.tray[]);
    these are spools in the operator's filament cabinet.
    """

    id: str
    name: str
    material: str
    color_hex: str
    brand: str | None = None
    total_g: float | None = None
    remaining_g: float | None = None
    notes: str | None = None
    added_at: int


_QUEUE_COLS = (
    "id, printer_id, file_path, file_name, ams_mapping_json, position, added_at, notes"
)


class QueueRepo:
    """User-orderable print queue, scoped per printer."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(
        self,
        *,
        item_id: str,
        printer_id: str,
        file_path: str,
        file_name: str,
        ams_mapping: list[int] | None,
        added_at: int,
        notes: str | None = None,
    ) -> QueueItem:
        async with self._db.conn.execute(
            "SELECT COALESCE(MAX(position) + 1, 0) FROM print_queue "
            "WHERE printer_id = ?",
            (printer_id,),
        ) as cur:
            row = await cur.fetchone()
        position = int(row[0]) if row and row[0] is not None else 0
        await self._db.conn.execute(
            f"INSERT INTO print_queue ({_QUEUE_COLS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id,
                printer_id,
                file_path,
                file_name,
                json.dumps(ams_mapping) if ams_mapping is not None else None,
                position,
                added_at,
                notes,
            ),
        )
        await self._db.conn.commit()
        got = await self.get(item_id)
        assert got is not None
        return got

    async def get(self, item_id: str) -> QueueItem | None:
        async with self._db.conn.execute(
            f"SELECT {_QUEUE_COLS} FROM print_queue WHERE id = ?", (item_id,)
        ) as cur:
            row = await cur.fetchone()
        return self._row(row) if row else None

    async def list_for(self, printer_id: str) -> list[QueueItem]:
        async with self._db.conn.execute(
            f"SELECT {_QUEUE_COLS} FROM print_queue WHERE printer_id = ? "
            "ORDER BY position ASC",
            (printer_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [self._row(r) for r in rows]

    async def reorder(self, item_id: str, new_position: int) -> QueueItem | None:
        """Move ``item_id`` to ``new_position`` within its printer's queue.

        Other items shift to fill the gap. ``new_position`` is clamped to
        ``[0, len-1]`` so callers can pass ``0`` for send-to-front or a large
        number for send-to-back without measuring first.
        """
        item = await self.get(item_id)
        if item is None:
            return None
        items = await self.list_for(item.printer_id)
        ids = [i.id for i in items if i.id != item_id]
        clamped = max(0, min(new_position, len(ids)))
        ids.insert(clamped, item_id)
        for idx, iid in enumerate(ids):
            await self._db.conn.execute(
                "UPDATE print_queue SET position = ? WHERE id = ?", (idx, iid)
            )
        await self._db.conn.commit()
        return await self.get(item_id)

    async def delete(self, item_id: str) -> QueueItem | None:
        item = await self.get(item_id)
        if item is None:
            return None
        await self._db.conn.execute(
            "DELETE FROM print_queue WHERE id = ?", (item_id,)
        )
        # Renumber remaining items so positions stay dense.
        remaining = await self.list_for(item.printer_id)
        for idx, q in enumerate(remaining):
            if q.position != idx:
                await self._db.conn.execute(
                    "UPDATE print_queue SET position = ? WHERE id = ?",
                    (idx, q.id),
                )
        await self._db.conn.commit()
        return item

    @staticmethod
    def _row(row: Any) -> QueueItem:
        d = dict(row)
        ams_raw = d.pop("ams_mapping_json", None)
        d["ams_mapping"] = json.loads(ams_raw) if ams_raw else None
        return QueueItem(**d)


_SPOOL_COLS = (
    "id, name, material, color_hex, brand, total_g, remaining_g, notes, added_at"
)


class SpoolRepo:
    """Off-AMS cabinet inventory (user-managed)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(self, spool: Spool) -> Spool:
        await self._db.conn.execute(
            f"INSERT INTO spool_inventory ({_SPOOL_COLS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                spool.id,
                spool.name,
                spool.material,
                spool.color_hex,
                spool.brand,
                spool.total_g,
                spool.remaining_g,
                spool.notes,
                spool.added_at,
            ),
        )
        await self._db.conn.commit()
        return spool

    async def get(self, spool_id: str) -> Spool | None:
        async with self._db.conn.execute(
            f"SELECT {_SPOOL_COLS} FROM spool_inventory WHERE id = ?",
            (spool_id,),
        ) as cur:
            row = await cur.fetchone()
        return Spool(**dict(row)) if row else None

    async def list(self) -> list[Spool]:
        async with self._db.conn.execute(
            f"SELECT {_SPOOL_COLS} FROM spool_inventory ORDER BY added_at ASC"
        ) as cur:
            rows = await cur.fetchall()
        return [Spool(**dict(r)) for r in rows]

    async def update(self, spool_id: str, **fields: Any) -> Spool | None:
        if not fields:
            return await self.get(spool_id)
        cols = ", ".join(f"{k} = ?" for k in fields)
        params: list[Any] = list(fields.values())
        params.append(spool_id)
        await self._db.conn.execute(
            f"UPDATE spool_inventory SET {cols} WHERE id = ?", params
        )
        await self._db.conn.commit()
        return await self.get(spool_id)

    async def delete(self, spool_id: str) -> bool:
        cur = await self._db.conn.execute(
            "DELETE FROM spool_inventory WHERE id = ?", (spool_id,)
        )
        await self._db.conn.commit()
        return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Filament memory (G3)
# --------------------------------------------------------------------------- #


class FilamentMemory(BaseModel):
    """One slot's user-supplied filament label (make/model/profile).

    Bound to the tray type seen when the label was written. The bridge
    invalidates this row when a slot's tray_type changes to a *different*
    non-empty value — indicating a spool swap to a different material.
    """

    slot: int
    make: str | None = None
    model: str | None = None
    profile: str | None = None
    tray_type_seen: str | None = None
    updated_at: int = 0  # unix epoch seconds


class FilamentMemoryRepo:
    """CRUD for per-slot filament memory (G3)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_all(self, printer_id: str) -> dict[int, FilamentMemory]:
        """All slots with a stored label for *printer_id*.

        Returns a mapping of ``physical_slot → FilamentMemory``. Empty dict
        when no rows exist.
        """
        async with self._db.conn.execute(
            "SELECT slot, make, model, profile, tray_type_seen, updated_at "
            "FROM filament_memory WHERE printer_id = ?",
            (printer_id,),
        ) as cur:
            rows = await cur.fetchall()
        return {
            int(r["slot"]): FilamentMemory(
                slot=int(r["slot"]),
                make=r["make"],
                model=r["model"],
                profile=r["profile"],
                tray_type_seen=r["tray_type_seen"],
                updated_at=int(r["updated_at"]),
            )
            for r in rows
        }

    async def upsert(
        self,
        printer_id: str,
        slot: int,
        *,
        make: str | None,
        model: str | None,
        profile: str | None,
        tray_type_seen: str | None,
    ) -> FilamentMemory:
        """Insert or replace the label for one slot."""
        now = int(time.time())
        await self._db.conn.execute(
            "INSERT INTO filament_memory "
            "(printer_id, slot, make, model, profile, tray_type_seen, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(printer_id, slot) DO UPDATE SET "
            "make = excluded.make, model = excluded.model, "
            "profile = excluded.profile, tray_type_seen = excluded.tray_type_seen, "
            "updated_at = excluded.updated_at",
            (printer_id, slot, make, model, profile, tray_type_seen, now),
        )
        await self._db.conn.commit()
        return FilamentMemory(
            slot=slot,
            make=make,
            model=model,
            profile=profile,
            tray_type_seen=tray_type_seen,
            updated_at=now,
        )

    async def delete(self, printer_id: str, slot: int) -> bool:
        """Forget the label for one slot. Returns True if a row was removed."""
        cur = await self._db.conn.execute(
            "DELETE FROM filament_memory WHERE printer_id = ? AND slot = ?",
            (printer_id, slot),
        )
        await self._db.conn.commit()
        return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Sliced-date persistence (§10.1 SlicedDateMemo backing store)
# --------------------------------------------------------------------------- #

# Maximum rows kept in sliced_dates; pruned by learned_at when exceeded.
_SLICED_DATES_MAX = 2000


class SlicedDateRepo:
    """Persistent store for (filename, size_bytes) → sliced_at mappings.

    Backs :class:`bambu_bridge.protocol.ftps.SlicedDateMemo`; the memo now
    writes through here on every ``put`` so the cache survives bridge restarts.

    Row count is capped at :data:`_SLICED_DATES_MAX`.  When the cap is reached,
    the ``_SLICED_DATES_MAX // 10`` oldest rows (by ``learned_at``) are pruned
    so we don't hit the cap on every subsequent insert.

    All timestamps are stored as ISO-8601 UTC strings (``YYYY-MM-DDTHH:MM:SSZ``)
    — the same format used on the wire by the files listing API.
    """

    _FMT = "%Y-%m-%dT%H:%M:%SZ"

    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def _fmt(dt: datetime) -> str:
        return dt.strftime(SlicedDateRepo._FMT)

    @staticmethod
    def _parse(s: str) -> datetime:
        return datetime.strptime(s, SlicedDateRepo._FMT).replace(tzinfo=UTC)

    async def put(self, filename: str, size_bytes: int, sliced_at: datetime) -> None:
        """Upsert a (filename, size_bytes) → sliced_at mapping.

        Uses ``INSERT OR REPLACE`` so a re-sliced file with the same name and
        size updates the existing row rather than creating a duplicate.  After
        the upsert, prunes oldest rows if the table exceeds the cap.
        """
        now = datetime.now(tz=UTC)
        await self._db.conn.execute(
            "INSERT OR REPLACE INTO sliced_dates (filename, size_bytes, sliced_at, learned_at) "
            "VALUES (?, ?, ?, ?)",
            (filename, size_bytes, self._fmt(sliced_at), self._fmt(now)),
        )
        await self._db.conn.commit()
        await self._prune_if_needed()

    async def _prune_if_needed(self) -> None:
        """Prune oldest rows when the table exceeds ``_SLICED_DATES_MAX``."""
        async with self._db.conn.execute(
            "SELECT COUNT(*) FROM sliced_dates"
        ) as cur:
            row = await cur.fetchone()
        count = int(row[0]) if row else 0
        if count <= _SLICED_DATES_MAX:
            return
        # Delete the oldest _SLICED_DATES_MAX // 10 rows to amortise pruning.
        batch = _SLICED_DATES_MAX // 10
        await self._db.conn.execute(
            "DELETE FROM sliced_dates WHERE (filename, size_bytes) IN "
            "(SELECT filename, size_bytes FROM sliced_dates "
            "ORDER BY learned_at ASC LIMIT ?)",
            (batch,),
        )
        await self._db.conn.commit()

    async def get(self, filename: str, size_bytes: int) -> datetime | None:
        """Return the sliced_at for ``(filename, size_bytes)``, or ``None``."""
        async with self._db.conn.execute(
            "SELECT sliced_at FROM sliced_dates WHERE filename = ? AND size_bytes = ?",
            (filename, size_bytes),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        try:
            return self._parse(str(row[0]))
        except (ValueError, IndexError):
            return None

    async def latest_by_names(self, names: list[str]) -> dict[str, datetime]:
        """Return the most-recently-learned sliced_at per filename for *names*.

        For each filename, returns the entry with the maximum ``sliced_at``
        across all stored size variants (multiple size entries = multiple slices
        of the same-named file; the newest one wins).

        Returns a dict mapping filename → sliced_at for every name that has at
        least one stored entry.  Names with no entry are omitted.

        Uses a single SQL query with an IN clause and MAX aggregation — no
        Python-side loop over the full table.
        """
        if not names:
            return {}
        placeholders = ", ".join("?" for _ in names)
        async with self._db.conn.execute(
            f"SELECT filename, MAX(sliced_at) FROM sliced_dates "
            f"WHERE filename IN ({placeholders}) GROUP BY filename",
            names,
        ) as cur:
            rows = await cur.fetchall()
        result: dict[str, datetime] = {}
        for row in rows:
            fname = str(row[0])
            with contextlib.suppress(ValueError, IndexError):
                result[fname] = self._parse(str(row[1]))
        return result
