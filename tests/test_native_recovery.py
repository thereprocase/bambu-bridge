"""Native inbox snapshots preserve receipts without replaying print starts."""

import asyncio
import sqlite3
import zipfile

import pytest

from bambu_bridge.native_inbox import NativeInbox
from bambu_bridge.native_recovery import create_backup, list_backups, stage_backup


async def test_backup_restore_requires_review_before_any_start(tmp_path):
    inbox = NativeInbox(tmp_path)
    row = inbox.reserve("fixture-printer", "/saved.3mf", 1024)
    reader = asyncio.StreamReader()
    reader.feed_data(b"saved print")
    reader.feed_eof()
    await inbox.receive(reader, row, 1024)
    inbox.hold_start(
        "fixture-printer",
        {"print": {"command": "project_file", "url": "file:///sdcard/saved.3mf"}},
    )
    snapshot = create_backup(inbox, tmp_path, "fixture-printer")
    assert [item["id"] for item in list_backups(tmp_path)] == [snapshot["id"]]
    staged = stage_backup(tmp_path, snapshot["id"], "fixture-printer")
    restored = NativeInbox(staged.parent)
    assert restored.payload(row["id"]).read_bytes() == b"saved print"
    assert restored.get(row["id"])["start_state"] == "blocked"
    assert restored.get(row["id"])["code"] == "BBRESTORE_REVIEW"
    assert restored.claim_start(row["id"]) is None


def test_restore_rejects_wrong_printer_and_corrupt_backup(tmp_path):
    inbox = NativeInbox(tmp_path)
    snapshot = create_backup(inbox, tmp_path, "fixture-printer")
    with pytest.raises(ValueError, match="printer does not match"):
        stage_backup(tmp_path, snapshot["id"], "other-printer")
    archive = tmp_path / "native-backups" / f"{snapshot['id']}.zip"
    with archive.open("r+b") as handle:
        handle.seek(-25, 2)
        handle.write(b"broken snapshot contents")
    with pytest.raises((ValueError, OSError, sqlite3.DatabaseError, zipfile.BadZipFile)):
        stage_backup(tmp_path, snapshot["id"], "fixture-printer")
