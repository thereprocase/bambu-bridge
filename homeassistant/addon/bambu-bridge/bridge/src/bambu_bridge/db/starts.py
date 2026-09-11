"""Durable start ownership; independent connections own whole transactions.

No printer I/O belongs in this module. Unknown dispatch retains ownership;
elapsed time is never evidence that a physical command was not delivered.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

from bambu_bridge.db.jobs import Database


class StartConflict(Exception):
    """A start cannot safely be admitted or its identity was reused."""


class StartRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.db.connection_uri, uri=True, timeout=10) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def get(self, operation_id: str) -> dict[str, Any] | None:
        async with self.db.conn.execute(
            "SELECT * FROM start_operations WHERE id=?", (operation_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def active(self, printer_id: str) -> dict[str, Any] | None:
        async with self.db.conn.execute(
            "SELECT * FROM start_operations WHERE printer_id=? AND holds_printer=1",
            (printer_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    @staticmethod
    def fingerprint(printer_id: str, payload: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps([printer_id, payload], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    async def claim(
        self,
        operation_id: str,
        printer_id: str,
        payload: dict[str, Any],
        *,
        queue_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Replay before busy checks; claim + job + queue consumption are atomic."""
        fingerprint = self.fingerprint(printer_id, payload)
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT * FROM start_operations WHERE id=?", (operation_id,)
            ) as cursor:
                existing = await cursor.fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise StartConflict("Request identity already used with different inputs")
                return dict(existing), False
            async with conn.execute(
                "SELECT 1 FROM start_operations WHERE printer_id=? AND holds_printer=1 "
                "UNION ALL SELECT 1 FROM jobs WHERE printer_id=? "
                "AND state NOT IN ('completed','failed','canceled') LIMIT 1",
                (printer_id, printer_id),
            ) as cursor:
                if await cursor.fetchone():
                    raise StartConflict("A print or unresolved start already owns this printer")
            if queue_id:
                async with conn.execute(
                    "SELECT * FROM print_queue WHERE id=? AND printer_id=?",
                    (queue_id, printer_id),
                ) as cursor:
                    queued = await cursor.fetchone()
                if queued is None:
                    raise StartConflict("Queue item no longer exists")
                mapping = (
                    json.loads(queued["ams_mapping_json"]) if queued["ams_mapping_json"] else None
                )
                if (
                    queued["file_name"] != payload["file_name"]
                    or queued["file_path"] != payload["file_path"]
                    or mapping != payload.get("ams_mapping")
                ):
                    raise StartConflict("Queue item changed before claim")
            now = int(time.time())
            job_id = uuid.uuid4().hex
            await conn.execute(
                "INSERT INTO jobs (id,printer_id,file_name,state,queued_at,metadata_json) "
                "VALUES (?,?,?,'queued',?,?)",
                (
                    job_id,
                    printer_id,
                    payload["file_name"],
                    now,
                    json.dumps({"start_operation_id": operation_id}),
                ),
            )
            await conn.execute(
                "INSERT INTO start_operations "
                "(id,printer_id,fingerprint,payload_json,job_id,queue_id,state,"
                "created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,'accepted',?,?)",
                (
                    operation_id,
                    printer_id,
                    fingerprint,
                    json.dumps(payload),
                    job_id,
                    queue_id,
                    now,
                    now,
                ),
            )
            await conn.execute(
                "INSERT INTO events (printer_id,job_id,ts,event_type,payload_json) "
                "VALUES (?,?,?,'job_created',?)",
                (printer_id, job_id, now * 1000, json.dumps({"state": "queued"})),
            )
            if queue_id:
                await conn.execute("DELETE FROM print_queue WHERE id=?", (queue_id,))
                async with conn.execute(
                    "SELECT id FROM print_queue WHERE printer_id=? ORDER BY position,id",
                    (printer_id,),
                ) as cursor:
                    remaining = await cursor.fetchall()
                for position, row in enumerate(remaining):
                    await conn.execute(
                        "UPDATE print_queue SET position=? WHERE id=?", (position, row["id"])
                    )
        operation = await self.get(operation_id)
        assert operation is not None
        return operation, True

    async def transition(
        self,
        operation_id: str,
        expected: tuple[str, ...],
        state: str,
        *,
        release: bool = False,
        reason: str | None = None,
    ) -> bool:
        """Compare-and-swap also fences workers that lost ownership on recovery."""
        async with self.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE start_operations SET state=?,updated_at=?,revision=revision+1,"
                "holds_printer=?,reason=? WHERE id=? AND holds_printer=1 AND state IN ("
                + ",".join("?" for _ in expected)
                + ")",
                (state, int(time.time()), int(not release), reason, operation_id, *expected),
            )
            return cursor.rowcount == 1

    async def recover(self) -> None:
        """No automatic continuation after process restart, including pre-dispatch.

        Historical live jobs without operation rows remain blocking via claim().
        Recovered dispatches retain their reservations for explicit reconciliation.
        """
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE jobs SET state='canceled',finished_at=?,"
                "error_code='restart_before_dispatch' "
                "WHERE id IN (SELECT job_id FROM start_operations WHERE holds_printer=1 "
                "AND state IN ('accepted','validating','staging'))",
                (int(time.time()),),
            )
            await conn.execute(
                "UPDATE start_operations SET state='canceled_before_dispatch',holds_printer=0,"
                "reason='restart_before_dispatch',revision=revision+1,updated_at=? "
                "WHERE holds_printer=1 AND state IN ('accepted','validating','staging')",
                (int(time.time()),),
            )
            await conn.execute(
                "UPDATE start_operations SET state='outcome_unknown',reason='server_restarted',"
                "revision=revision+1,updated_at=? WHERE holds_printer=1",
                (int(time.time()),),
            )

    async def resolve_unknown(self, operation_id: str, revision: int) -> None:
        """Explicit operator reconciliation, not evidence of physical failure."""
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT job_id FROM start_operations WHERE id=? AND revision=? "
                "AND state='outcome_unknown' AND holds_printer=1",
                (operation_id, revision),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise StartConflict("Operation changed; read it again before resolving")
            now = int(time.time())
            await conn.execute(
                "UPDATE start_operations SET state='resolved_unknown',holds_printer=0,"
                "revision=revision+1,updated_at=?,reason='operator_confirmed_idle' WHERE id=?",
                (now, operation_id),
            )
            await conn.execute(
                "UPDATE jobs SET state='canceled',finished_at=?,"
                "error_code='tracking_resolved_unknown' WHERE id=?",
                (now, row["job_id"]),
            )
            await conn.execute(
                "INSERT INTO events (job_id,printer_id,ts,event_type,payload_json) "
                "SELECT job_id,printer_id,?,'start_resolved',? FROM start_operations WHERE id=?",
                (
                    now * 1000,
                    '{"physical_outcome":"unknown","operator_confirmed_idle":true}',
                    operation_id,
                ),
            )
