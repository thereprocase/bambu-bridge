"""Execution identity, reconciliation and recovery; no printer network required."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from bambu_bridge.library import Artifact, Attempt, Capture, LibraryError, LibraryStore
from bambu_bridge.library_backup import backup, restore
from bambu_bridge.service.library_history import LibraryHistory, archive_native, identity


def receipt(identifier: str = "a" * 32, data: bytes = b"exact submitted slice") -> dict:
    return {
        "id": identifier,
        "archive_cursor": 1,
        "printer": "FIXTURE",
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "logical": "/cache/part.gcode.3mf",
        "retained": 1,
        "state": "delivered",
        "start_state": "queued",
        "created": 100,
        "start_requested_at": 110,
        "command": json.dumps(
            {
                "print": {
                    "command": "project_file",
                    "param": "Metadata/plate_2.gcode",
                    "ams_mapping": [3, 1],
                    "use_ams": True,
                }
            }
        ),
    }


def capture_slice(store: LibraryStore, cid: str = "a" * 32) -> Capture:
    data = b"unchanged toolpath"
    capture = Capture(
        id=cid,
        title="Fixture",
        slicer_version="fixture",
        plate=1,
        artifacts=(
            Artifact(
                name="slice.3mf",
                role="slice",
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            ),
        ),
    )
    store.create("fixture", capture)
    store.append(cid, "slice.3mf", 0, data, "fixture")
    store.finalize(cid, "fixture")
    return capture


def attempt(capture: Capture, **changes) -> Attempt:
    return Attempt.model_validate(
        {
            "id": "b" * 32,
            "capture_id": capture.id,
            "slice_sha256": capture.artifacts[0].sha256,
            "printer_id": "FIXTURE",
            "source": "replay",
            "source_id": "b" * 32,
            "state": "queued",
            "source_state": "queued",
            "created_at": 123,
            "ams_mapping": [3, 1],
            "use_ams": True,
            **changes,
        }
    )


def test_repeat_prints_keep_slice_and_distinct_choices_through_backup(tmp_path: Path):
    store = LibraryStore(tmp_path / "active")
    capture = capture_slice(store)
    first = attempt(capture)
    store.record_attempt(first)
    store.record_attempt(first)  # scan/retry must not manufacture more events
    store.record_attempt(attempt(capture, state="completed", source_state="completed"))
    second = attempt(capture, id="c" * 32, source_id="c" * 32, ams_mapping=[1, 3])
    store.record_attempt(second)
    assert len(store.get(capture.id)["attempts"]) == 2
    assert len(store.attempt_events(capture.id, first.id)) == 2
    backup(store, tmp_path / "backup")
    restored = restore(tmp_path / "backup", tmp_path / "restored")
    assert restored.get(capture.id)["attempts"] == store.get(capture.id)["attempts"]
    assert restored.attempt_events(capture.id, first.id) == store.attempt_events(
        capture.id, first.id
    )
    assert restored.download(capture.id, "slice.3mf")[0].read_bytes() == b"unchanged toolpath"


def test_cannot_rebind_execution_change_mapping_or_attach_wrong_bytes(tmp_path: Path):
    store = LibraryStore(tmp_path)
    capture = capture_slice(store)
    first = attempt(capture)
    store.record_attempt(first)
    with pytest.raises(LibraryError, match="immutable"):
        store.record_attempt(attempt(capture, ams_mapping=[0, 2]))
    with pytest.raises(LibraryError, match="do not match"):
        store.record_attempt(attempt(capture, slice_sha256="f" * 64))
    with pytest.raises(LibraryError, match="already recorded"):
        store.record_attempt(attempt(capture, id="d" * 32))
    other = capture_slice(store, "e" * 32)
    with pytest.raises(LibraryError, match="not found"):
        store.attempt_events(other.id, first.id)
    with pytest.raises(LibraryError, match="immutable"):
        store.record_attempt(attempt(other))
    store.delete(capture.id)
    with pytest.raises(LibraryError, match="not found"):
        store.record_attempt(first)
    assert store.verify()["ok"]


def test_native_restart_interruption_and_deletion_never_dispatch(tmp_path: Path):
    store = LibraryStore(tmp_path)
    row = receipt()
    # The observer only receives a readonly file interface, no print commands.
    inbox = Mock(spec=["open_verified"])
    inbox.open_verified.side_effect = lambda _: io.BytesIO(b"exact submitted slice")
    assert archive_native(store, inbox, row)
    cid = identity("native-capture", row["id"])
    aid = identity("native-attempt", row["id"])
    assert store.get(cid)["plate"] == 2
    for state, extra in [
        ("sent", {}),
        ("running", {"running_at": 120, "acknowledged": 1}),
        ("interrupted", {"running_at": 120, "terminal_at": 140, "acknowledged": 1}),
    ]:
        store = LibraryStore(tmp_path)
        assert archive_native(store, inbox, {**row, "start_state": state, **extra})
    events = store.attempt_events(cid, aid)
    assert [e["attempt"]["state"] for e in events] == [
        "queued",
        "submitted",
        "active",
        "interrupted",
    ]
    assert events[-1]["attempt"]["finished_at"] == 140
    assert events[-1]["attempt"]["ams_mapping"] == [3, 1]
    inbox.open_verified.assert_called_once()
    store.delete(cid)
    assert not archive_native(store, inbox, row)
    assert store.list() == []


def test_same_name_and_identical_bytes_do_not_merge_submissions(tmp_path: Path):
    store = LibraryStore(tmp_path)
    inbox = Mock(spec=["open_verified"])
    inbox.open_verified.side_effect = lambda _: io.BytesIO(b"exact submitted slice")
    for char in "ab":
        archive_native(store, inbox, receipt(char * 32))
    rows = store.list()
    assert len(rows) == 2 and rows[0]["title"] == rows[1]["title"]
    assert rows[0]["attempts"][0]["id"] != rows[1]["attempts"][0]["id"]
    assert store.usage()["stored_bytes"] == len(b"exact submitted slice")


def test_slower_observer_cannot_overwrite_a_newer_terminal_receipt(tmp_path: Path):
    store = LibraryStore(tmp_path)
    capture = capture_slice(store)
    latest = attempt(
        capture, source="native", source_revision=8, state="interrupted", source_state="interrupted"
    )
    store.record_attempt(latest)
    store.record_attempt(
        attempt(capture, source="native", source_revision=7, state="active", source_state="running")
    )
    store.record_attempt(latest)
    assert store.get(capture.id)["attempts"][0]["state"] == "interrupted"
    assert len(store.attempt_events(capture.id, latest.id)) == 1


def test_expired_missing_or_corrupt_native_payloads_cannot_claim_saved(tmp_path: Path):
    store = LibraryStore(tmp_path)
    inbox = Mock(spec=["open_verified"])
    inbox.open_verified.side_effect = OSError("private path")
    row = receipt()
    assert not archive_native(store, inbox, {**row, "retained": 0})
    inbox.open_verified.assert_not_called()
    with pytest.raises(OSError):
        archive_native(store, inbox, row)
    assert store.list() == []
    inbox.open_verified.side_effect = lambda _: io.BytesIO(b"corrupted payload!!!")
    with pytest.raises(LibraryError):
        archive_native(store, inbox, row)
    assert store.get(identity("native-capture", row["id"]))["state"] == "pending"


async def test_scan_pages_isolates_failures_and_retries_without_command_access(tmp_path: Path):
    store = LibraryStore(tmp_path)
    inbox = Mock(spec=["archive_page", "open_verified"])
    first, second = receipt(), {**receipt("b" * 32), "archive_cursor": 2}
    inbox.archive_page.side_effect = lambda after: {0: [first], 1: [second], 2: []}[after]
    inbox.open_verified.side_effect = [OSError("secret/path"), io.BytesIO(b"exact submitted slice")]
    history = LibraryHistory(store, lambda: inbox)
    await history.scan()
    assert len(store.list()) == 1 and history.last_scan is not None
    assert history.last_error == {"source_id": first["id"], "code": "OSError"}
    inbox.open_verified.side_effect = lambda _: io.BytesIO(b"exact submitted slice")
    await history.scan()
    assert len(store.list()) == 2 and history.last_error is None
    history.start()
    await asyncio.wait_for(history.close(), 2)
    assert history.task is None
