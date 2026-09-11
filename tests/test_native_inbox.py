"""Custody, restart and start-ordering contracts; synthetic payloads only."""

import asyncio
import hashlib

import pytest

from bambu_bridge.native_ftps import UploadReader
from bambu_bridge.native_inbox import InboxError, NativeInbox, logical_path


async def stored(inbox, name="/cache/test.3mf", payload=b"fixture"):
    row = inbox.reserve("fixture-printer", name, 1024)
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return await inbox.receive(reader, row, 1024)


def start(url="file:///sdcard/cache/test.3mf", sequence="17"):
    return {"print": {"command": "project_file", "url": url, "sequence_id": sequence}}


async def test_receipt_is_durable_and_independent_of_printer(tmp_path):
    inbox = NativeInbox(tmp_path)
    row = await stored(inbox)
    resumed = NativeInbox(tmp_path)
    resumed.recover()
    assert resumed.get(row["id"])["state"] == "stored"
    assert resumed.payload(row["id"]).read_bytes() == b"fixture"
    assert row["sha256"] == hashlib.sha256(b"fixture").hexdigest()


async def test_reset_never_becomes_stored(tmp_path):
    inbox = NativeInbox(tmp_path)
    row = inbox.reserve("fixture-printer", "/partial.3mf", 1024)
    reader = asyncio.StreamReader()
    reader.feed_data(b"partial")
    reader.set_exception(ConnectionResetError(104, "private payload"))
    with pytest.raises(ConnectionResetError):
        await inbox.receive(reader, row, 1024)
    assert inbox.get(row["id"])["state"] == "failed"
    assert inbox.pending("fixture-printer") == []


async def test_limit_failure_never_becomes_stored(tmp_path):
    inbox = NativeInbox(tmp_path)
    with pytest.raises(InboxError, match="BBFTP_SIZE_LIMIT"):
        await stored(inbox, payload=b"x" * 1025)
    assert inbox.status()[0]["state"] == "failed"


async def test_disk_sync_failure_never_becomes_stored(tmp_path, monkeypatch):
    inbox = NativeInbox(tmp_path)

    def fail(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr("bambu_bridge.native_inbox.os.fsync", fail)
    with pytest.raises(InboxError, match="BBFTP_STORAGE_WRITE_FAILED"):
        await stored(inbox)
    assert inbox.status()[0]["state"] == "failed"


async def test_start_waits_for_delivery_and_is_claimed_once(tmp_path):
    inbox = NativeInbox(tmp_path)
    row = await stored(inbox)
    assert inbox.hold_start("different-printer", start()) is None
    inbox.hold_start("fixture-printer", start())
    assert inbox.claim_start(row["id"]) is None
    inbox.transition(row["id"], "stored", "delivered", "BBDELIVERY_OK")
    payload = inbox.claim_start(row["id"])
    assert payload["print"]["url"] == "file:///sdcard" + row["remote"]
    assert inbox.claim_start(row["id"]) is None
    inbox.hold_start("fixture-printer", start())
    assert inbox.claim_start(row["id"]) is None
    with pytest.raises(ValueError, match="BBSTART_CONFLICT"):
        inbox.hold_start("fixture-printer", start(sequence="18"))


async def test_restart_never_replays_uncertain_dispatch_or_delivery(tmp_path):
    inbox = NativeInbox(tmp_path)
    row = await stored(inbox)
    inbox.hold_start("fixture-printer", start())
    inbox.transition(row["id"], "stored", "delivered", "BBDELIVERY_OK")
    assert inbox.claim_start(row["id"])
    other = await stored(inbox, "/other.3mf")
    inbox.transition(other["id"], "stored", "delivering", "BBDELIVERY_PENDING")
    resumed = NativeInbox(tmp_path)
    resumed.recover()
    assert resumed.get(row["id"])["start_state"] == "unknown"
    assert resumed.claim_start(row["id"]) is None
    assert resumed.get(other["id"])["state"] == "failed"


async def test_same_name_uses_immutable_printer_objects(tmp_path):
    inbox = NativeInbox(tmp_path)
    first = await stored(inbox)
    inbox.hold_start("fixture-printer", start())
    second = await stored(inbox)
    assert first["remote"] != second["remote"]
    with pytest.raises(ValueError, match="BBSTART_GENERATION_AMBIGUOUS"):
        inbox.hold_start("fixture-printer", start())
    with pytest.raises(ValueError, match="BBSTART_UNRESOLVED"):
        inbox.hold_start("fixture-printer", start(sequence="18"))
    inbox.cancel_queued("fixture-printer")
    assert inbox.hold_start("fixture-printer", start(sequence="18"))["id"] == second["id"]
    assert inbox.get(first["id"])["command"] != inbox.get(second["id"])["command"]


async def test_stop_cancels_held_start_without_replaying_it(tmp_path):
    inbox = NativeInbox(tmp_path)
    row = await stored(inbox)
    inbox.hold_start("fixture-printer", start())
    inbox.cancel_queued("fixture-printer")
    inbox.transition(row["id"], "stored", "delivered", "BBDELIVERY_OK")
    assert inbox.claim_start(row["id"]) is None
    with pytest.raises(ValueError):
        inbox.hold_start("fixture-printer", start())


def test_capacity_is_reserved_before_receiving(tmp_path):
    inbox = NativeInbox(tmp_path, budget=1024)
    inbox.reserve("fixture-printer", "/one.3mf", 1024)
    with pytest.raises(OSError):
        inbox.reserve("fixture-printer", "/two.3mf", 1)
    assert logical_path("../test.3mf", "/sdcard/cache") == "/test.3mf"


@pytest.mark.parametrize("error", [ConnectionResetError, BrokenPipeError])
async def test_late_teardown_does_not_erase_protocol_eof(error):
    reader = UploadReader()
    reader.feed_data(b"complete fixture")
    reader.feed_eof()
    reader.set_exception(error())
    assert await reader.read() == b"complete fixture"
    assert await reader.read() == b""


@pytest.mark.parametrize("error", [ConnectionResetError, BrokenPipeError])
async def test_reset_before_eof_remains_fatal(error):
    reader = UploadReader()
    reader.feed_data(b"partial fixture")
    reader.set_exception(error())
    with pytest.raises(error):
        await reader.read()


async def dispatched(inbox):
    row = await stored(inbox)
    inbox.hold_start("fixture-printer", start())
    inbox.transition(row["id"], "stored", "delivered", "BBDELIVERY_OK")
    payload = inbox.claim_start(row["id"])
    inbox.dispatched(row["id"], "sent")
    return inbox.get(row["id"]), payload


async def test_fresh_matching_run_then_finish_admits_deliberate_second_copy(tmp_path):
    inbox = NativeInbox(tmp_path)
    row, payload = await dispatched(inbox)
    terminal = {"print": {"gcode_state": "FINISH", "gcode_file": row["remote"]}}
    inbox.observe("fixture-printer", terminal, terminal)
    assert inbox.get(row["id"])["start_state"] == "sent"
    foreign = {"print": {"gcode_state": "RUNNING", "gcode_file": "/foreign.3mf"}}
    inbox.observe("fixture-printer", foreign, foreign)
    assert inbox.get(row["id"])["start_state"] == "sent"
    active = {"print": {"gcode_state": "RUNNING", "gcode_file": row["remote"]}}
    inbox.observe("fixture-printer", active, active)
    assert inbox.get(row["id"])["start_state"] == "running"
    inbox.observe("fixture-printer", terminal, terminal)
    assert inbox.get(row["id"])["start_state"] == "completed"
    second = await stored(inbox)
    assert inbox.hold_start("fixture-printer", start())["id"] == second["id"]
    assert second["id"] != row["id"]


async def test_ack_identity_roundtrip_and_early_ack_not_overwritten(tmp_path):
    inbox = NativeInbox(tmp_path)
    row = await stored(inbox)
    inbox.hold_start("fixture-printer", start())
    inbox.transition(row["id"], "stored", "delivered", "BBDELIVERY_OK")
    payload = inbox.claim_start(row["id"])
    sequence = payload["print"]["sequence_id"]
    assert sequence != "17"
    ack = {"print": {"command": "project_file", "sequence_id": sequence, "result": "SUCCESS"}}
    inbox.observe("fixture-printer", ack, {})
    inbox.dispatched(row["id"], "sent")
    assert inbox.get(row["id"])["start_state"] == "accepted"
    assert inbox.translated_report("fixture-printer", ack)["print"]["sequence_id"] == "17"


async def test_unknown_requires_review_but_unique_active_file_can_reconcile(tmp_path):
    inbox = NativeInbox(tmp_path)
    row, _ = await dispatched(inbox)
    with inbox.connect() as db:
        db.execute("UPDATE uploads SET dispatched_at=0 WHERE id=?", (row["id"],))
    inbox.expire_dispatch()
    assert inbox.get(row["id"])["start_state"] == "unknown"
    assert inbox.claim_start(row["id"]) is None
    active = {"print": {"gcode_state": "RUNNING", "gcode_file": row["remote"]}}
    inbox.observe("fixture-printer", active, active)
    assert inbox.get(row["id"])["start_state"] == "running"
    assert inbox.claim_start(row["id"]) is None


async def test_common_external_start_reservation_blocks_native_until_resolved(tmp_path):
    inbox = NativeInbox(tmp_path)
    row = await stored(inbox)
    command = start(url="file:///sdcard/existing.3mf")
    external = inbox.claim_external("fixture-printer", command)
    assert command["print"]["sequence_id"] != "17"
    with pytest.raises(ValueError, match="BBSTART_UNRESOLVED"):
        inbox.hold_start("fixture-printer", start())
    inbox.dispatched(external, "unknown")
    inbox.resolve(external)
    assert inbox.hold_start("fixture-printer", start())["id"] == row["id"]
    with pytest.raises(ValueError, match="BBSTART_UNRESOLVED"):
        inbox.claim_external("fixture-printer", start(url="file:///sdcard/another.3mf"))


def test_worker_lock_prevents_second_process_recovery(tmp_path):
    first, second = NativeInbox(tmp_path), NativeInbox(tmp_path)
    first.acquire()
    try:
        with pytest.raises(BlockingIOError):
            second.acquire()
    finally:
        first.close()
    second.acquire()
    second.close()


async def test_integrity_retention_and_explicit_discard(tmp_path):
    inbox = NativeInbox(tmp_path)
    row = await stored(inbox)
    with inbox.open_verified(row["id"]) as source:
        assert source.read() == b"fixture"
    with pytest.raises(ValueError):
        inbox.discard(row["id"])
    inbox.transition(row["id"], "stored", "delivered", "BBDELIVERY_OK")
    other = await stored(inbox, "/keep.3mf")
    with inbox.connect() as db:
        db.execute("UPDATE uploads SET created=unixepoch()-700000")
    inbox.prune()
    assert not inbox.payload(row["id"]).exists()
    assert inbox.payload(other["id"]).exists()


async def test_plain_gcode_staging_preserves_command_path_form(tmp_path):
    inbox = NativeInbox(tmp_path)
    row = await stored(inbox, "/cache/fixture.gcode")
    assert row["remote"].endswith(".gcode")
    command = {
        "print": {
            "command": "gcode_file",
            "sequence_id": "22",
            "param": "/sdcard/cache/fixture.gcode",
        }
    }
    inbox.hold_start("fixture-printer", command)
    inbox.transition(row["id"], "stored", "delivered", "BBDELIVERY_OK")
    claimed = inbox.claim_start(row["id"])
    assert claimed["print"]["param"] == "/sdcard" + row["remote"]
    ack = {
        "print": {
            "command": "gcode_file",
            "sequence_id": claimed["print"]["sequence_id"],
            "result": "SUCCESS",
        }
    }
    assert inbox.translated_report("fixture-printer", ack)["print"]["sequence_id"] == "22"
