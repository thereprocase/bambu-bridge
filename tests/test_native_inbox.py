"""Custody, restart and start-ordering contracts; synthetic payloads only."""

import asyncio
import hashlib

import pytest

from bambu_bridge.native_ftps import UploadReader
from bambu_bridge.native_inbox import InboxError, NativeInbox, command_path, logical_path


@pytest.mark.parametrize(
    "url",
    [
        "ftp://tray.gcode.3mf",
        "ftps://tray.gcode.3mf",
        "file:///sdcard/tray.gcode.3mf",
        "ftp:///tray.gcode.3mf",
    ],
)
def test_orca_archive_url_path(url):
    assert command_path({"command": "project_file", "url": url}) == "/tray.gcode.3mf"


@pytest.mark.parametrize("url", ["", "ftp://", "file:///", "https://example.test/file"])
def test_missing_archive_path_cannot_reserve_printer(tmp_path, url):
    inbox = NativeInbox(tmp_path)
    with pytest.raises(ValueError, match="BBSTART_INVALID_PATH"):
        inbox.claim_external("fixture-printer", start(url))
    assert not inbox.unresolved("fixture-printer")


async def test_real_orca_url_uses_staged_object_and_releases_after_finish(tmp_path):
    inbox = NativeInbox(tmp_path)
    row = await stored(inbox, "/tray.gcode.3mf")
    command = start("ftp://tray.gcode.3mf")
    command["print"].update(
        file="tray.gcode.3mf", param="Metadata/plate_1.gcode", subtask_name="tray"
    )
    assert inbox.hold_start("fixture-printer", command)["id"] == row["id"]
    inbox.transition(row["id"], "stored", "delivered", "BBDELIVERY_OK")
    payload = inbox.claim_start(row["id"])
    assert payload["print"]["url"] == "ftp://" + row["remote"].lstrip("/")
    assert payload["print"]["file"] == row["remote"].lstrip("/")
    assert payload["print"]["param"] == "Metadata/plate_1.gcode"
    active = {
        "print": {
            "gcode_state": "RUNNING",
            "gcode_file": "Metadata/plate_1.gcode",
            "subtask_name": "tray",
        }
    }
    inbox.observe("fixture-printer", active, active)
    assert inbox.get(row["id"])["start_state"] == "dispatching"
    ack = {
        "print": {
            "command": "project_file",
            "sequence_id": payload["print"]["sequence_id"],
            "result": "SUCCESS",
        }
    }
    inbox.observe("fixture-printer", ack, active)
    inbox.observe("fixture-printer", active, active)
    assert inbox.get(row["id"])["start_state"] == "running"
    terminal = {"print": {**active["print"], "gcode_state": "FINISH"}}
    inbox.observe("fixture-printer", terminal, terminal)
    assert not inbox.unresolved("fixture-printer")
    assert inbox.claim_start(row["id"]) is None


def test_external_orca_start_tracks_archive_and_requires_matching_task(tmp_path):
    inbox = NativeInbox(tmp_path)
    command = start("ftp://tray.gcode.3mf")
    command["print"]["subtask_name"] = "tray"
    identifier = inbox.claim_external("fixture-printer", command)
    assert inbox.get(identifier)["remote"] == "/tray.gcode.3mf"
    ack = {
        "print": {
            "command": "project_file",
            "sequence_id": command["print"]["sequence_id"],
            "result": "SUCCESS",
        }
    }
    inbox.observe("fixture-printer", ack, {})
    foreign = {
        "print": {
            "gcode_state": "RUNNING",
            "gcode_file": "Metadata/plate_1.gcode",
            "subtask_name": "other",
        }
    }
    inbox.observe("fixture-printer", foreign, foreign)
    assert inbox.get(identifier)["start_state"] == "accepted"
    active = {"print": {**foreign["print"], "subtask_name": "tray"}}
    inbox.observe("fixture-printer", active, active)
    terminal = {"print": {**active["print"], "gcode_state": "FINISH"}}
    inbox.observe("fixture-printer", terminal, terminal)
    assert inbox.get(identifier)["start_state"] == "completed"


async def stored(inbox, name="/cache/test.3mf", payload=b"fixture"):
    row = inbox.reserve("fixture-printer", name, 1024)
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return await inbox.receive(reader, row, 1024)


def start(url="file:///sdcard/cache/test.3mf", sequence="17"):
    return {"print": {"command": "project_file", "url": url, "sequence_id": sequence}}


async def test_library_receipt_paging_requires_exact_stored_bytes_and_native_start(tmp_path):
    inbox = NativeInbox(tmp_path)
    first = await stored(inbox)
    command = start()
    command["print"].update(param="Metadata/plate_1.gcode", ams_mapping=[3], use_ams=True)
    assert inbox.archive_page() == []  # file upload is not a print attempt
    inbox.hold_start("fixture-printer", command)
    inbox.cancel_queued("fixture-printer")
    second = await stored(inbox, "/second.3mf")
    inbox.hold_start("fixture-printer", start("file:///sdcard/second.3mf"))
    inbox.cancel_queued("fixture-printer")
    inbox.claim_external("other-printer", start())
    page = inbox.archive_page(limit=1)
    assert [row["id"] for row in page] == [first["id"]]
    assert page[0]["start_requested_at"] is not None
    assert page[0]["revision"] > 0
    tail = inbox.archive_page(after=page[0]["archive_cursor"])
    assert [row["id"] for row in tail] == [second["id"]]


async def test_library_keeps_uncertain_attempt_after_native_recovery(tmp_path):
    from bambu_bridge.library import LibraryStore
    from bambu_bridge.service.library_history import LibraryHistory

    inbox = NativeInbox(tmp_path / "pairing")
    row = await stored(inbox)
    command = start()
    command["print"].update(param="Metadata/plate_1.gcode", ams_mapping=[3], use_ams=True)
    inbox.hold_start("fixture-printer", command)
    store = LibraryStore(tmp_path / "library")
    worker = LibraryHistory(store, lambda: inbox)
    await worker.scan()
    capture = store.list()[0]
    inbox.transition(row["id"], "stored", "delivered", "BBDELIVERY_OK")
    assert inbox.claim_start(row["id"])
    inbox.recover()  # outcome unknown; neither history nor recovery can resend
    await worker.scan()
    latest = store.get(capture["id"])
    assert latest["attempts"][0]["state"] == "unknown"
    assert latest["attempts"][0]["created_at"] == capture["attempts"][0]["created_at"]
    assert inbox.claim_start(row["id"]) is None
    assert store.download(capture["id"], "plate-1.gcode.3mf")[0].read_bytes() == b"fixture"


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


async def test_blocked_start_retains_actual_readiness_failure(tmp_path):
    inbox = NativeInbox(tmp_path)
    row = await stored(inbox)
    inbox.hold_start("fixture-printer", start())
    inbox.block_start(row["id"], "BBSTART_STATUS_REFRESH_TIMEOUT")
    assert inbox.get(row["id"])["code"] == "BBSTART_STATUS_REFRESH_TIMEOUT"
    assert inbox.claim_start(row["id"]) is None


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


async def test_same_filename_from_another_client_cannot_replace_start_input(tmp_path):
    inbox = NativeInbox(tmp_path)
    rows = []
    for peer in ("fixture-client-a", "fixture-client-b"):
        row = inbox.reserve("fixture-printer", "/cache/test.3mf", 1024, peer=peer)
        reader = asyncio.StreamReader()
        reader.feed_data(peer.encode())
        reader.feed_eof()
        rows.append(await inbox.receive(reader, row, 1024))
    with pytest.raises(ValueError, match="BBSTART_UPLOAD_OWNER_MISMATCH"):
        inbox.hold_start("fixture-printer", start(), peer="fixture-client-c")
    held = inbox.hold_start("fixture-printer", start(), peer="fixture-client-a")
    assert held["id"] == rows[0]["id"]


async def test_stage_timings_and_unchanged_telemetry(tmp_path):
    inbox = NativeInbox(tmp_path)
    row = await stored(inbox)
    identifier = row["id"]
    inbox.hold_start("fixture-printer", start())
    inbox.transition(identifier, "stored", "delivering", "BBDELIVERY_BUSY")
    inbox.transition(identifier, "delivering", "delivered", "BBDELIVERY_OK")
    inbox.mark_readiness(identifier)
    inbox.mark_readiness(identifier, ready=True)
    payload = inbox.claim_start(identifier)
    inbox.dispatched(identifier, "sent")
    ack = {
        "print": {
            "command": "project_file",
            "sequence_id": payload["print"]["sequence_id"],
            "result": "SUCCESS",
        }
    }
    assert inbox.observe("fixture-printer", ack, {})
    active = {"print": {"gcode_state": "RUNNING", "gcode_file": row["remote"]}}
    assert inbox.observe("fixture-printer", active, active)
    before = inbox.get(identifier)
    assert not inbox.observe("fixture-printer", active, active)
    assert inbox.get(identifier) == before
    finished = {"print": {"gcode_state": "FINISH", "gcode_file": row["remote"]}}
    assert inbox.observe("fixture-printer", finished, finished)
    receipt = inbox.status()[0]
    for field in (
        "received_at",
        "delivery_started_at",
        "delivered_at",
        "start_requested_at",
        "readiness_started_at",
        "ready_at",
        "acknowledged_at",
        "running_at",
        "terminal_at",
    ):
        assert receipt[field] is not None, field
    assert receipt["received_at"] <= receipt["delivered_at"] <= receipt["terminal_at"]


async def test_old_receipt_does_not_invent_past_timings(tmp_path):
    inbox = NativeInbox(tmp_path)
    row, _ = await dispatched(inbox)
    active = {"print": {"gcode_state": "RUNNING", "gcode_file": row["remote"]}}
    inbox.observe("fixture-printer", active, active)
    with inbox.connect() as db:
        db.execute("UPDATE uploads SET received_at=NULL, delivered_at=NULL, running_at=NULL")
    inbox = NativeInbox(tmp_path)
    finished = {"print": {"gcode_state": "FINISH", "gcode_file": row["remote"]}}
    inbox.observe("fixture-printer", finished, finished)
    receipt = inbox.get(row["id"])
    assert receipt["terminal_at"] is not None
    assert all(receipt[key] is None for key in ("received_at", "delivered_at", "running_at"))


@pytest.mark.parametrize("state", ["RUNNING", "PAUSE", "PREPARE"])
async def test_power_cycle_empty_idle_interrupts_confirmed_job_and_allows_fresh_start(
    tmp_path, state
):
    inbox = NativeInbox(tmp_path)
    row = await stored(inbox, "/lost.gcode.3mf")
    inbox.hold_start("fixture-printer", start("ftp://lost.gcode.3mf"))
    inbox.transition(row["id"], "stored", "delivered", "BBDELIVERY_OK")
    inbox.claim_start(row["id"])
    active = {"print": {"gcode_state": state, "gcode_file": row["remote"]}}
    inbox.observe("fixture-printer", active, active)
    assert inbox.get(row["id"])["seen_active"] == 1
    # Persisted evidence must survive a bridge restart too.
    inbox = NativeInbox(tmp_path)
    idle = {"print": {"gcode_state": "IDLE", "gcode_file": "", "subtask_name": ""}}
    assert inbox.observe("fixture-printer", idle, idle)
    ended = inbox.get(row["id"])
    assert ended["start_state"] == "interrupted"
    assert ended["code"] == "BBSTART_INTERRUPTED" and ended["terminal_at"] is not None
    assert not inbox.unresolved("fixture-printer")
    assert inbox.claim_start(row["id"]) is None
    assert not inbox.observe("fixture-printer", idle, idle)
    fresh = await stored(inbox, "/lost.gcode.3mf")
    assert inbox.hold_start("fixture-printer", start("ftp://lost.gcode.3mf"))["id"] == fresh["id"]


@pytest.mark.parametrize(
    "incoming",
    [
        {"gcode_state": "IDLE"},
        {"gcode_state": "IDLE", "gcode_file": "", "subtask_name": "resumable"},
        {"gcode_state": "PAUSE", "gcode_file": "", "subtask_name": ""},
        {"nozzle_temper": 25},
    ],
)
async def test_idle_without_explicit_lost_identity_never_releases_owner(tmp_path, incoming):
    inbox = NativeInbox(tmp_path)
    row = await stored(inbox, "/lost.gcode.3mf")
    inbox.hold_start("fixture-printer", start("ftp://lost.gcode.3mf"))
    inbox.transition(row["id"], "stored", "delivered", "BBDELIVERY_OK")
    inbox.claim_start(row["id"])
    active = {"print": {"gcode_state": "RUNNING", "gcode_file": row["remote"]}}
    inbox.observe("fixture-printer", active, active)
    inbox.observe("fixture-printer", {"print": incoming}, {"print": incoming})
    assert inbox.get(row["id"])["start_state"] == "running"


async def test_empty_idle_does_not_release_an_unconfirmed_start(tmp_path):
    inbox = NativeInbox(tmp_path)
    row = await stored(inbox, "/lost.gcode.3mf")
    inbox.hold_start("fixture-printer", start("ftp://lost.gcode.3mf"))
    inbox.transition(row["id"], "stored", "delivered", "BBDELIVERY_OK")
    inbox.claim_start(row["id"])
    idle = {"print": {"gcode_state": "IDLE", "gcode_file": "", "subtask_name": ""}}
    inbox.observe("fixture-printer", idle, idle)
    assert inbox.get(row["id"])["start_state"] == "dispatching"
