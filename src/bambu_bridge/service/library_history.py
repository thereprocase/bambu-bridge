"""Observe durable native receipts without participating in printer dispatch.

Only the native inbox's immutable UUID and verified payload bytes establish a
capture association. Names and approximate timestamps never join a print.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from bambu_bridge.library import CHUNK_LIMIT, Artifact, Attempt, Capture, LibraryError, LibraryStore

if TYPE_CHECKING:
    from bambu_bridge.native_inbox import NativeInbox

log = structlog.get_logger(__name__)
OWNER = "bridge-native-archive"
STATES = {
    "reserved": "queued",
    "queued": "queued",
    "dispatching": "submitted",
    # Native custody receipts collapse PREPARE/RUNNING/PAUSE. Preserve that
    # uncertainty instead of claiming filament has started depositing.
    "sent": "submitted",
    "accepted": "accepted",
    "running": "active",
    "completed": "completed",
    "interrupted": "interrupted",
    "unknown": "unknown",
    "cancelled": "canceled",
    "rejected": "failed",
    "blocked": "not_started",
    "resolved": "ended",
}


def identity(kind: str, source_id: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, "bambu-bridge/library/" + kind + "/" + source_id).hex


def native_attempt(row: dict[str, Any], capture_id: str) -> Attempt:
    command = json.loads(row["command"])["print"]
    source_state = row.get("start_state") or row["state"]
    state = STATES.get(source_state, "unknown")
    if row["state"] == "failed" and source_state in ("queued", "reserved"):
        state = "not_started"
    if row.get("code") == "BBSTART_NOT_DISPATCHED":
        state = "not_started"
    return Attempt.model_validate(
        {
            "id": identity("native-attempt", row["id"]),
            "capture_id": capture_id,
            "slice_sha256": row["sha256"],
            "printer_id": row["printer"],
            "source": "replay" if row.get("replay_request_id") else "native",
            "source_id": row["id"],
            "source_revision": row.get("revision"),
            "state": state,
            "source_state": source_state,
            "created_at": row["created"]
            if row.get("replay_request_id")
            else row.get("start_requested_at") or row["created"],
            "started_at": row.get("running_at"),
            "finished_at": row.get("terminal_at"),
            "ams_mapping": command.get("ams_mapping"),
            "use_ams": command.get("use_ams"),
            "start_options": {
                key: command[key]
                for key in (
                    "bed_type",
                    "bed_leveling",
                    "flow_cali",
                    "vibration_cali",
                    "layer_inspect",
                    "timelapse",
                )
                if key in command
                and (
                    type(command[key]) is bool
                    or (
                        key == "bed_type"
                        and isinstance(command[key], str)
                        and len(command[key]) <= 64
                    )
                )
            },
            "error_code": row.get("code")
            if state in ("failed", "unknown", "not_started")
            else None,
            "acknowledged": bool(row.get("acknowledged")),
        }
    )


def archive_native(store: LibraryStore, inbox: NativeInbox, row: dict[str, Any]) -> bool:
    replay = (
        store.replay_request(row["replay_request_id"]) if row.get("replay_request_id") else None
    )
    if row.get("replay_request_id") and replay is None:
        raise LibraryError(409, "Replay receipt has no library request")
    cid = replay["capture_id"] if replay else identity("native-capture", row["id"])
    state = store.capture_state(cid)
    if state == "deleted":
        return False  # explicit owner deletion must survive reconciliation
    if replay and state != "stored":
        raise LibraryError(409, "Replay capture is unavailable")
    if state != "stored":
        if not row.get("retained"):
            return False  # an expired historical cache is not a recoverable slice
        command = json.loads(row["command"])["print"]
        plate = re.fullmatch(r"Metadata/plate_([1-9][0-9]*)\.gcode", str(command.get("param", "")))
        if command.get("command") != "project_file" or not plate:
            raise LibraryError(422, "Native print receipt does not identify a sliced 3MF plate")
        number = int(plate[1])
        artifact = Artifact(
            name=f"plate-{number}.gcode.3mf",
            role="slice",
            sha256=row["sha256"],
            size=row["bytes"],
        )
        title = str(row["logical"]).rsplit("/", 1)[-1]
        title = "".join(c for c in title if ord(c) >= 32 and ord(c) != 127)[:160] or "Native print"
        capture = Capture(
            id=cid,
            title=title,
            slicer_version="Not recorded",
            plate=number,
            originals="unavailable",
            artifacts=(artifact,),
        )
        with inbox.open_verified(row["id"]) as source:
            status = store.create(OWNER, capture)
            upload = status["uploads"][artifact.name]
            if not upload["stored"]:
                offset = upload["offset"]
                source.seek(offset)
                while chunk := source.read(CHUNK_LIMIT):
                    store.append(cid, artifact.name, offset, chunk, OWNER)
                    offset += len(chunk)
            store.finalize(cid, OWNER)
    store.record_attempt(native_attempt(row, cid))
    return True


class LibraryHistory:
    def __init__(self, store: LibraryStore, inbox: Callable[[], NativeInbox | None]):
        self.store, self.inbox = store, inbox
        self.stop = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self.last_scan: float | None = None
        self.last_error: dict[str, Any] | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self.run(), name="library:history")

    async def close(self) -> None:
        self.stop.set()
        if self.task:
            await self.task
            self.task = None

    async def scan(self) -> None:
        inbox = self.inbox()
        if inbox is None:
            return
        cursor = 0
        errors = 0
        while not self.stop.is_set():
            rows = await asyncio.to_thread(inbox.archive_page, cursor)
            if not rows:
                break
            for row in rows:
                if self.stop.is_set():
                    return
                cursor = row["archive_cursor"]
                try:
                    await asyncio.to_thread(archive_native, self.store, inbox, row)
                except Exception as exc:
                    errors += 1
                    # No original paths, commands, keys or printer credentials.
                    self.last_error = {"source_id": row["id"], "code": type(exc).__name__}
                    log.warning("library.native_archive_failed", **self.last_error)
            await asyncio.sleep(0)
        self.last_scan = time.time()
        if not errors:
            self.last_error = None

    async def run(self) -> None:
        while not self.stop.is_set():
            try:
                await self.scan()
            except Exception as exc:
                self.last_error = {"code": type(exc).__name__}
                log.warning("library.history_scan_failed", **self.last_error)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=30)
