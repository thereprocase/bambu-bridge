"""Durable inventory source assignments, separate from physical printer state.

A global revision makes moves and replacement atomic across phones. This store
owns its connection; transactions must not share the jobs database connection.
No assignment here authorizes a printer write or confirms a physical reload.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

import aiosqlite


class AssignmentConflict(Exception):
    """The caller must refresh before changing assignments again."""


@dataclass(frozen=True)
class SpoolAssignment:
    printer_id: str
    source: str  # hardware-qualified AMS tray identity, or external
    spool_id: int
    assigned_at: float = 0
    observed_identity: str | None = None
    wire_slot: int | None = None
    # "owner" from the app; "rfid_new" or "rfid_known" when a tag seated it.
    origin: str = "owner"
    # What the slot physically held when assigned (inventory_sources.same_spool):
    # its tag key and chemistry. None on assignments stored before these existed,
    # until the seating pass stamps them. Kept in assignment_seats, never in the
    # payload, so an older bridge still loads the payload.
    spool_key: str | None = None
    tray_type: str | None = None

    def __post_init__(self) -> None:
        if not self.printer_id.strip() or not self.source.strip():
            raise ValueError("printer and source are required")
        if (
            isinstance(self.spool_id, bool)
            or not isinstance(self.spool_id, int)
            or self.spool_id <= 0
        ):
            raise ValueError("spool_id must be a positive integer")


@dataclass(frozen=True)
class AssignmentSnapshot:
    provider_id: str
    revision: int
    assignments: tuple[SpoolAssignment, ...]


class SpoolAssignmentStore:
    """Persist a provider namespace and an atomically replaced assignment set."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path) -> SpoolAssignmentStore:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        try:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS spool_assignments ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                "provider_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL)"
            )
            await conn.execute(
                "INSERT OR IGNORE INTO spool_assignments VALUES (1, ?, 0, '[]')",
                (str(uuid4()),),
            )
            # Which spool last sat behind each physical identity (an RFID tag or a
            # declared slot), so a returning spool can be offered again.
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS assignment_history ("
                "observed_identity TEXT NOT NULL, source TEXT NOT NULL, "
                "spool_id INTEGER NOT NULL, assigned_at REAL NOT NULL, "
                "PRIMARY KEY(observed_identity, source))"
            )
            # spool_key and tray_type per assignment, matched by assigned_at so a
            # row an older bridge left behind never applies to a later assignment.
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS assignment_seats ("
                "printer_id TEXT NOT NULL, source TEXT NOT NULL, assigned_at REAL NOT NULL, "
                "spool_key TEXT NOT NULL, tray_type TEXT NOT NULL, "
                "PRIMARY KEY(printer_id, source))"
            )
            await conn.commit()
        except BaseException:
            await conn.close()
            raise
        return cls(conn)

    async def bind_provider(self, locator_hash: str) -> None:
        """Fail closed on an inventory URL change; replacement needs explicit relinking."""
        async with self._lock:
            try:
                await self._conn.execute("BEGIN IMMEDIATE")
                await self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS inventory_provider ("
                    "singleton INTEGER PRIMARY KEY CHECK(singleton=1), locator_hash TEXT NOT NULL)"
                )
                await self._conn.execute(
                    "INSERT OR IGNORE INTO inventory_provider VALUES(1, ?)", (locator_hash,)
                )
                async with self._conn.execute(
                    "SELECT locator_hash FROM inventory_provider WHERE singleton=1"
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None or row[0] != locator_hash:
                    raise AssignmentConflict(
                        "inventory provider changed; explicit relinking required"
                    )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise

    async def close(self) -> None:
        await self._conn.close()

    async def last_spools(self) -> dict[tuple[str, str], tuple[int, float]]:
        """(observed_identity, source) -> (spool_id, assigned_at), newest per pair."""
        async with self._conn.execute(
            "SELECT observed_identity, source, spool_id, assigned_at FROM assignment_history"
        ) as cursor:
            rows = await cursor.fetchall()
        return {(row[0], row[1]): (int(row[2]), float(row[3])) for row in rows}

    async def snapshot(self) -> AssignmentSnapshot:
        # Under the writer's lock: the payload and its seats are read as one state.
        async with self._lock:
            async with self._conn.execute(
                "SELECT provider_id, revision, payload FROM spool_assignments WHERE singleton=1"
            ) as cursor:
                row = await cursor.fetchone()
            async with self._conn.execute(
                "SELECT printer_id, source, assigned_at, spool_key, tray_type FROM assignment_seats"
            ) as cursor:
                seats = {(r[0], r[1]): (r[2], r[3], r[4]) for r in await cursor.fetchall()}
        if row is None:
            raise RuntimeError("missing inventory state")
        fields = set(SpoolAssignment.__dataclass_fields__)
        assignments = []
        for value in json.loads(row[2]):
            # Fields a newer bridge may add are ignored rather than fatal.
            assignment = SpoolAssignment(**{k: v for k, v in value.items() if k in fields})
            seat = seats.get((assignment.printer_id, assignment.source))
            if seat is not None and seat[0] == assignment.assigned_at:
                assignment = dataclasses.replace(assignment, spool_key=seat[1], tray_type=seat[2])
            assignments.append(assignment)
        return AssignmentSnapshot(row[0], row[1], tuple(assignments))

    async def replace(
        self,
        assignments: tuple[SpoolAssignment, ...],
        *,
        provider_id: str,
        expected_revision: int,
    ) -> AssignmentSnapshot:
        """Replace or move assignments; stale clients never overwrite newer state."""
        sources = {(a.printer_id, a.source) for a in assignments}
        if len(sources) != len(assignments):
            raise ValueError("each physical source can hold only one spool")
        if len({a.spool_id for a in assignments}) != len(assignments):
            raise ValueError("a spool can be assigned to only one source")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError("expected_revision must be nonnegative")
        ordered = tuple(sorted(assignments, key=lambda a: (a.printer_id, a.source)))
        payload = json.dumps(
            [
                {k: v for k, v in asdict(a).items() if k not in ("spool_key", "tray_type")}
                for a in ordered
            ],
            separators=(",", ":"),
        )
        async with self._lock:
            try:
                cursor = await self._conn.execute(
                    "UPDATE spool_assignments SET payload=?, revision=revision+1 "
                    "WHERE singleton=1 AND provider_id=? AND revision=?",
                    (payload, provider_id, expected_revision),
                )
                changed = cursor.rowcount
                await cursor.close()
                if changed != 1:
                    raise AssignmentConflict("provider identity or assignment revision changed")
                await self._conn.execute("DELETE FROM assignment_seats")
                await self._conn.executemany(
                    "INSERT INTO assignment_seats VALUES (?, ?, ?, ?, ?)",
                    [
                        (a.printer_id, a.source, a.assigned_at, a.spool_key, a.tray_type or "")
                        for a in ordered
                        if a.spool_key is not None
                    ],
                )
                for a in ordered:
                    if a.observed_identity:
                        await self._conn.execute(
                            "INSERT INTO assignment_history VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(observed_identity, source) DO UPDATE SET "
                            "spool_id=excluded.spool_id, assigned_at=excluded.assigned_at "
                            "WHERE excluded.assigned_at >= assigned_at",
                            (a.observed_identity, a.source, a.spool_id, a.assigned_at),
                        )
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise
        # Return this committed revision, rather than a later concurrent writer's snapshot.
        return AssignmentSnapshot(provider_id, expected_revision + 1, ordered)
