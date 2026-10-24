"""Copying printed files into the library and counting prints the bridge did not send."""

from __future__ import annotations

import hashlib
import io
import time
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bambu_bridge.library import LibraryStore
from bambu_bridge.native_inbox import NativeInbox
from bambu_bridge.protocol.ftps import FileEntry
from bambu_bridge.protocol.models import ReportMessage
from bambu_bridge.service import print_capture
from bambu_bridge.service.library_history import archive_native, identity
from bambu_bridge.service.print_capture import capture_once, catch_up, running_file
from bambu_bridge.spool_assignments import AssignmentSnapshot, SpoolAssignment

SERIAL = "01P00A000000001"
GCODE = b"; filament used [g] = 3.0\nM83\nM73 L1\nT0\nG1 X1 E5\nM73 L2\nG1 X2 E5\n"
MD5 = hashlib.md5(GCODE).hexdigest().upper()


class Storage:
    """The printer's storage: files by (directory, name), with listing times."""

    def __init__(self, files: dict[tuple[str, str], tuple[bytes, float | None]]):
        self.files = files
        self.downloads: list[str] = []

    async def download_bytes(self, name, *, remote_dir=""):
        self.downloads.append(name)
        return self.files[(remote_dir, name)][0]

    async def list_dir_with_timestamps(self, remote_dir=""):
        return [
            FileEntry(name, datetime.fromtimestamp(when, UTC) if when else None)
            for (directory, name), (_, when) in self.files.items()
            if directory == remote_dir
        ]


IDENTITY = {"md5", "file", "param", "ams_mapping", "use_ams", "subtask_name"}


def printer(fresh=True, started_at=None, reported=IDENTITY, **fields):
    raw = {
        "gcode_state": "RUNNING",
        "file": "part.gcode",
        "gcode_file": "part.gcode",
        "md5": MD5,
        "subtask_name": "part",
        "ams_mapping": [-1],
        "use_ams": False,
        "param": "",
        **fields,
    }
    return SimpleNamespace(
        serial=SERIAL,
        ip="127.0.0.1",
        access_code="fixture",
        connected=True,
        print_md5_fresh=fresh,
        fresh_print_fields=lambda: set(reported) if fresh else set(),
        print_started_at=started_at,
        native_snapshot=lambda: {"print": raw},
    )


@pytest.fixture
def storage(monkeypatch):
    monkeypatch.setattr(print_capture, "_FETCHES", {})
    monkeypatch.setattr(print_capture, "_REFUSED", set())
    files = Storage({("", "part.gcode"): (GCODE, None)})
    monkeypatch.setattr(print_capture, "_ftps", lambda _printer, _port: files)
    return files


def inventory():
    external = SpoolAssignment(SERIAL, "external", 5)
    snapshot = AssignmentSnapshot("provider", 3, (external,))
    return SimpleNamespace(assignments=SimpleNamespace(snapshot=AsyncMock(return_value=snapshot)))


def idle(**fields):
    return printer(gcode_state="IDLE", **fields)


def test_running_file_needs_an_active_print_a_name_and_an_md5():
    raw = printer().native_snapshot()["print"]
    assert running_file(raw) == ("part.gcode", MD5)
    assert running_file({**raw, "gcode_state": "FINISH"}) is None
    assert running_file({**raw, "md5": ""}) is None
    assert running_file({**raw, "file": "", "gcode_file": "/sdcard/x.gcode"}) == ("x.gcode", MD5)


async def test_a_foreign_print_is_copied_and_adopted_once(tmp_path, storage):
    store, inbox = LibraryStore(tmp_path / "library"), NativeInbox(tmp_path / "inbox")
    started = time.time() - 120
    cid = await capture_once(printer(started_at=started), store, inbox, inventory(), 990)
    assert cid == identity("printer-file", MD5)
    assert store.capture_state(cid) == "stored"
    [listed] = [r for r in inbox.status() if r["kind"] == "observed"]
    row = inbox.get(listed["id"])
    assert row["start_state"] == "running" and row["remote"] == "/part.gcode"
    assert row["sha256"] == hashlib.sha256(GCODE).hexdigest()
    # Refills are counted from when the print began, not from when it was adopted.
    assert row["dispatched_at"] == int(started)
    with inbox.connect() as db:
        manifest = db.execute("SELECT manifest FROM accounting_manifests").fetchone()[0]
    assert '"layer_grams":[{"0":1.5},{"0":1.5}]' in manifest
    assert '"tool_wires":{"0":254}' in manifest and '"wire_spools":{"254":5}' in manifest
    # The next tick finds it copied and counted: no download, no second row.
    await capture_once(printer(), store, inbox, inventory(), 990)
    assert storage.downloads == ["part.gcode"]
    assert len([r for r in inbox.status() if r["kind"] == "observed"]) == 1


async def test_a_possibly_stale_md5_is_never_used(tmp_path, storage):
    store, inbox = LibraryStore(tmp_path / "library"), NativeInbox(tmp_path / "inbox")
    assert await capture_once(printer(fresh=False), store, inbox, inventory(), 990) is None
    assert storage.downloads == [] and inbox.status() == []


async def test_a_file_that_does_not_match_its_md5_is_tried_once(tmp_path, storage):
    store, inbox = LibraryStore(tmp_path / "library"), NativeInbox(tmp_path / "inbox")
    wrong = printer(md5="0" * 32)
    assert await capture_once(wrong, store, inbox, inventory(), 990) is None
    assert await capture_once(wrong, store, inbox, inventory(), 990) is None
    assert storage.downloads == ["part.gcode"] * 2  # root and cache, one round only
    assert inbox.status() == []


async def test_without_inventory_the_print_is_copied_but_not_counted(tmp_path, storage):
    store, inbox = LibraryStore(tmp_path / "library"), NativeInbox(tmp_path / "inbox")
    cid = await capture_once(printer(), store, inbox, None, 990)
    assert store.capture_state(cid) == "stored" and inbox.status() == []


async def adopted(tmp_path, storage):
    store, inbox = LibraryStore(tmp_path / "library"), NativeInbox(tmp_path / "inbox")
    await capture_once(printer(), store, inbox, inventory(), 990)
    [row] = inbox.status()
    return store, inbox, row["id"]


async def test_an_adopted_print_is_followed_by_its_file_name_to_the_end(tmp_path, storage):
    store, inbox, attempt = await adopted(tmp_path, storage)
    # gcode_file names the plate member and the task name differs: `file` still matches.
    current = {"file": "part.gcode", "gcode_file": "Metadata/plate_1.gcode", "subtask_name": "x"}
    running = {"print": {"gcode_state": "RUNNING", "layer_num": 2}}
    inbox.observe(SERIAL, running, {"print": {**current, **running["print"]}})
    assert inbox.get(attempt)["layer_reached"] == 2
    finish = {"print": {"gcode_state": "FINISH"}}
    inbox.observe(SERIAL, finish, {"print": {**current, **finish["print"]}})
    assert inbox.get(attempt)["start_state"] == "completed"
    assert inbox.accounting_event(attempt)["outcome"] == "completed"
    # The library records the print against the copy, so its eviction clock moves.
    [row] = inbox.archive_page(0)
    assert archive_native(store, inbox, row)
    with store.connect() as db:
        assert db.execute("SELECT capture, source_id FROM attempts").fetchone()[:] == (
            identity("printer-file", MD5),
            attempt,
        )


async def test_an_adopted_print_ends_when_another_file_starts(tmp_path, storage):
    _, inbox, attempt = await adopted(tmp_path, storage)
    other = {"print": {"gcode_state": "RUNNING", "file": "next.gcode", "gcode_file": "next.gcode"}}
    inbox.observe(SERIAL, other, other)
    assert inbox.get(attempt)["code"] == "BBSTART_ENDED"
    assert inbox.accounting_event(attempt)["outcome"] == "interrupted"


def plate(number: int) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as bundle:
        bundle.writestr(f"Metadata/plate_{number}.gcode", GCODE)
    return data.getvalue()


async def test_catch_up_copies_new_printer_files_newest_first_dated_by_the_printer(
    tmp_path, monkeypatch
):
    now = time.time()
    files = Storage(
        {
            ("", "older.gcode"): (GCODE + b";2\n", now - 7200),
            ("", "newest.gcode.3mf"): (plate(2), now - 3600),
            ("cache", "cached.gcode"): (GCODE + b";3\n", now - 10800),
            ("", "notes.txt"): (b"x", now - 60),
            ("", "ancient.gcode"): (GCODE + b";4\n", now - 30 * 86400),
            ("", "sent.gcode"): (GCODE + b";5\n", now - 60),
            ("", "undated.gcode"): (GCODE + b";6\n", None),
        }
    )
    monkeypatch.setattr(print_capture, "_ftps", lambda _printer, _port: files)
    store = LibraryStore(tmp_path / "library")
    inbox = SimpleNamespace(sent_file=lambda name, _printer, _written: name == "sent.gcode")
    assert await catch_up(idle(), store, inbox, 990) == 3
    assert files.downloads == ["newest.gcode.3mf", "older.gcode", "cached.gcode"]
    with store.connect() as db:
        created = [r[0] for r in db.execute("SELECT created FROM captures ORDER BY created DESC")]
        manifests = [r[0] for r in db.execute("SELECT manifest FROM captures")]
    assert created == pytest.approx([now - 3600, now - 7200, now - 10800], abs=1)
    assert any('"plate":2' in m for m in manifests)
    # Each file is looked at once; the bridge's own upload is marked but never fetched.
    assert await catch_up(idle(), store, inbox, 990) == 0
    assert files.downloads == ["newest.gcode.3mf", "older.gcode", "cached.gcode"]


async def test_catch_up_never_evicts_a_newer_print_for_an_older_file(tmp_path, monkeypatch):
    now = time.time()
    files = Storage({("", "older.gcode"): (GCODE + b";2\n", now - 7200)})
    monkeypatch.setattr(print_capture, "_ftps", lambda _printer, _port: files)
    store = LibraryStore(tmp_path / "library", quota=len(GCODE) + 4)
    newer = identity("printer-file", MD5)
    print_capture.store_copy(store, newer, "newer", 1, "part.gcode", GCODE)
    assert await catch_up(idle(), store, None, 990) == 0
    assert store.capture_state(newer) == "stored"
    assert store.printer_files(SERIAL) == {"/older.gcode": pytest.approx(now - 7200)}


async def test_the_reported_md5_is_fresh_only_once_seen_during_this_print():
    from bambu_bridge.service.printer import PrinterService

    service = PrinterService(
        SERIAL, "127.0.0.1", "fixture", friendly_name="P1S", model="P1S", mqtt_port=1883
    )

    async def report(**fields):
        await service._handle_report(ReportMessage.model_validate({"print": fields}))
        return service.print_md5_fresh

    assert await report(gcode_state="RUNNING", md5=MD5, gcode_file="a.gcode")
    assert not await report(gcode_state="FINISH")
    # A new print whose reports have not carried an MD5 yet still holds the last one.
    assert not await report(gcode_state="RUNNING", gcode_file="b.gcode")
    assert service.native_snapshot()["print"]["md5"] == MD5
    assert await report(md5="1" * 32)
    assert await report(gcode_state="PAUSE") and await report(gcode_state="RUNNING")


@pytest.mark.parametrize("state,caught_up", [("RUNNING", False), ("FINISH", True), ("IDLE", True)])
async def test_catch_up_waits_until_the_printer_is_idle(
    tmp_path, storage, monkeypatch, state, caught_up
):
    import asyncio

    calls = []

    async def record(*_args):
        calls.append(state)

    async def stop(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(print_capture, "catch_up", record)
    monkeypatch.setattr(print_capture.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await print_capture.catch_up_loop(
            lambda: [printer(gcode_state=state)], LibraryStore(tmp_path), lambda: None, 990
        )
    assert bool(calls) is caught_up


async def test_a_deleted_copy_is_not_fetched_again_every_tick(tmp_path, storage):
    store, inbox = LibraryStore(tmp_path / "library"), NativeInbox(tmp_path / "inbox")
    await capture_once(printer(), store, inbox, None, 990)
    store.delete(identity("printer-file", MD5))
    print_capture._FETCHES.clear()  # a later run of the bridge
    for _ in range(4):
        await capture_once(printer(), store, inbox, None, 990)
    assert storage.downloads == ["part.gcode"] * 2


async def test_a_failed_fetch_is_retried_a_few_times_spaced_out(tmp_path, storage, monkeypatch):
    store = LibraryStore(tmp_path / "library")
    clock = [1000.0]
    monkeypatch.setattr(print_capture.time, "monotonic", lambda: clock[0])
    wrong = printer(md5="0" * 32)
    for _ in range(10):
        await capture_once(wrong, store, None, None, 990)
        clock[0] += 30
    # Root and cache each try; three tries, a minute apart.
    assert len(storage.downloads) == 2 * print_capture.FETCH_TRIES


async def test_a_mapping_not_reported_during_this_print_is_not_used(tmp_path, storage):
    store, inbox = LibraryStore(tmp_path / "library"), NativeInbox(tmp_path / "inbox")
    stale = printer(reported={"md5", "file"}, ams_mapping=[3], use_ams=True)
    await capture_once(stale, store, inbox, inventory(), 990)
    with inbox.connect() as db:
        manifest = db.execute("SELECT manifest FROM accounting_manifests").fetchone()[0]
        command = db.execute("SELECT command FROM uploads").fetchone()[0]
    assert '"tool_wires":{}' in manifest
    assert '"ams_mapping": null' in command and '"md5": "' + MD5 + '"' in command


async def test_an_adoption_after_the_print_ended_is_not_written(tmp_path, storage):
    store, inbox = LibraryStore(tmp_path / "library"), NativeInbox(tmp_path / "inbox")
    states = iter(["RUNNING", "FAILED"])
    raw = printer().native_snapshot()["print"]
    ending = printer()
    ending.native_snapshot = lambda: {"print": {**raw, "gcode_state": next(states)}}
    await capture_once(ending, store, inbox, inventory(), 990)
    assert inbox.status() == []


async def test_an_adopted_print_that_ended_unseen_is_closed_by_the_merged_state(tmp_path, storage):
    _, inbox, attempt = await adopted(tmp_path, storage)
    delta = {"print": {"nozzle_temper": 30}}
    inbox.observe(SERIAL, delta, {"print": {"file": "part.gcode", "gcode_state": "FAILED"}})
    assert inbox.get(attempt)["code"] == "BBSTART_ENDED"


async def test_a_reprint_of_the_same_file_ends_the_first_at_its_layer(tmp_path, storage):
    _, inbox, attempt = await adopted(tmp_path, storage)
    current = {"file": "part.gcode", "gcode_file": "part.gcode"}
    layer = {"print": {"gcode_state": "RUNNING", "layer_num": 50}}
    inbox.observe(SERIAL, layer, {"print": {**current, **layer["print"]}})
    # A lone layer delta going back does not end it.
    dip = {"print": {"layer_num": 0}}
    inbox.observe(SERIAL, dip, {"print": {**current, "gcode_state": "RUNNING", "layer_num": 0}})
    assert inbox.get(attempt)["start_state"] == "running"
    again = {"print": {"gcode_state": "RUNNING", "layer_num": 3}}
    inbox.observe(SERIAL, again, {"print": {**current, **again["print"]}})
    row = inbox.get(attempt)
    assert row["code"] == "BBSTART_ENDED" and row["layer_reached"] == 50


def count_reads(monkeypatch):
    """Record every whole-file read and G-code parse a capture tick makes."""
    reads = []
    stored_copy, read_plan = print_capture.stored_copy, print_capture.read_plan
    monkeypatch.setattr(
        print_capture, "stored_copy", lambda *a: reads.append("copy") or stored_copy(*a)
    )
    monkeypatch.setattr(
        print_capture, "read_plan", lambda *a: reads.append("plan") or read_plan(*a)
    )
    return reads


async def test_without_inventory_a_stored_copy_is_never_read_again(tmp_path, storage, monkeypatch):
    store, inbox = LibraryStore(tmp_path / "library"), NativeInbox(tmp_path / "inbox")
    await capture_once(printer(), store, inbox, None, 990)
    reads = count_reads(monkeypatch)
    for _ in range(3):
        await capture_once(printer(), store, inbox, None, 990)
        await capture_once(printer(), store, None, None, 990)
    assert reads == [] and storage.downloads == ["part.gcode"]


async def test_a_print_adoption_would_refuse_is_not_read_every_tick(tmp_path, storage, monkeypatch):
    store, inbox, attempt = await adopted(tmp_path, storage)
    # A bridge start waits for this printer: adoption refuses, so nothing is read for it.
    with inbox.connect() as db:
        db.execute("UPDATE uploads SET start_state='queued' WHERE id=?", (attempt,))
    reads = count_reads(monkeypatch)
    for _ in range(3):
        await capture_once(printer(), store, inbox, inventory(), 990)
    assert reads == [] and len(inbox.status()) == 1


async def test_catch_up_does_not_download_a_print_copied_while_it_ran(tmp_path, storage):
    store, inbox = LibraryStore(tmp_path / "library"), NativeInbox(tmp_path / "inbox")
    storage.files[("", "part.gcode")] = (GCODE, time.time() - 600)
    await capture_once(printer(), store, inbox, None, 990)
    assert await catch_up(idle(), store, inbox, 990) == 0
    assert storage.downloads == ["part.gcode"]


async def test_catch_up_stops_when_a_print_starts(tmp_path, monkeypatch):
    now = time.time()
    files = Storage({("", f"{n}.gcode"): (GCODE + b";%d\n" % n, now - n) for n in (60, 120)})
    monkeypatch.setattr(print_capture, "_ftps", lambda _printer, _port: files)
    starting = idle()
    download = files.download_bytes

    async def start_printing(name, *, remote_dir=""):
        starting.native_snapshot()["print"]["gcode_state"] = "PREPARE"
        return await download(name, remote_dir=remote_dir)

    files.download_bytes = start_printing
    assert await catch_up(starting, LibraryStore(tmp_path / "library"), None, 990) == 1
    assert files.downloads == ["60.gcode"]


async def test_catch_up_gives_up_on_a_file_it_cannot_read(tmp_path, monkeypatch):
    files = Storage({("", "bad.gcode"): (GCODE, time.time() - 60)})
    download = files.download_bytes

    async def fail(name, *, remote_dir=""):
        await download(name, remote_dir=remote_dir)
        raise OSError("550")

    files.download_bytes = fail
    monkeypatch.setattr(print_capture, "_ftps", lambda _printer, _port: files)
    monkeypatch.setattr(print_capture, "_UNREADABLE", print_capture.Counter())
    store = LibraryStore(tmp_path / "library")
    for _ in range(print_capture.FETCH_TRIES + 2):
        await catch_up(idle(), store, None, 990)
    assert files.downloads == ["bad.gcode"] * print_capture.FETCH_TRIES


async def test_a_copied_and_adopted_bundle_lists_its_plates_once(tmp_path, storage, monkeypatch):
    store, inbox = LibraryStore(tmp_path / "library"), NativeInbox(tmp_path / "inbox")
    bundle = plate(1)
    storage.files[("", "part.gcode")] = (bundle, None)
    listed = []
    plates = print_capture._plates
    monkeypatch.setattr(print_capture, "_plates", lambda data: listed.append(1) or plates(data))
    md5 = hashlib.md5(bundle).hexdigest().upper()
    await capture_once(printer(md5=md5), store, inbox, inventory(), 990)
    assert listed == [1] and len(inbox.status()) == 1


async def test_a_layer_advance_wakes_its_listener_after_it_is_written(tmp_path, storage):
    _, inbox, attempt = await adopted(tmp_path, storage)
    seen = []
    inbox.on_layer = lambda: seen.append(
        NativeInbox(tmp_path / "inbox").get(attempt)["layer_reached"]
    )
    current = {"file": "part.gcode", "gcode_file": "part.gcode"}
    for layer in (2, 2, 3):
        report = {"print": {"gcode_state": "RUNNING", "layer_num": layer}}
        inbox.observe(SERIAL, report, {"print": {**current, **report["print"]}})
    assert seen == [2, 3]
    # A listener that fails never stops the report from being observed.
    inbox.on_layer = lambda: 1 / 0
    report = {"print": {"gcode_state": "RUNNING", "layer_num": 4}}
    inbox.observe(SERIAL, report, {"print": {**current, **report["print"]}})
    assert inbox.get(attempt)["layer_reached"] == 4


async def test_the_accounting_page_shows_a_frozen_manifests_analysis_notes(tmp_path, storage):
    _, inbox, attempt = await adopted(tmp_path, storage)
    [row] = inbox.accounting_page()
    assert row["analysis_issues"] == []
    with inbox.connect() as db:
        db.execute(
            "UPDATE accounting_manifests SET manifest=json_set(manifest,'$.analysis_issues',"
            "json('[\"unweighed_filament_1\"]'))"
        )
    [row] = inbox.accounting_page()
    assert row["attempt_id"] == attempt and row["analysis_issues"] == ["unweighed_filament_1"]


def foreign_inbox(**overrides):
    fields = {
        "has_active_print": lambda _printer: False,
        "sent_file": lambda *_: False,
        "adopt_running_print": Mock(return_value="adopted"),
    }
    return SimpleNamespace(**{**fields, **overrides})


async def test_a_plate_the_library_cannot_hold_skips_only_that_file(tmp_path, monkeypatch):
    now = time.time()
    files = Storage(
        {
            ("", "older.gcode"): (GCODE + b";2\n", now - 7200),
            ("", "newest.gcode.3mf"): (plate(1001), now - 3600),
        }
    )
    monkeypatch.setattr(print_capture, "_ftps", lambda _printer, _port: files)
    store = LibraryStore(tmp_path / "library")
    assert await catch_up(idle(), store, None, 990) == 1
    assert files.downloads == ["newest.gcode.3mf", "older.gcode"]
    # Both were looked at, so neither is downloaded again.
    assert set(store.printer_files(SERIAL)) == {"/older.gcode", "/newest.gcode.3mf"}


async def test_a_plate_the_library_cannot_hold_is_still_adopted(tmp_path, storage):
    body = plate(1001)
    md5 = hashlib.md5(body).hexdigest().upper()
    storage.files[("", "part.gcode.3mf")] = (body, None)
    inbox = foreign_inbox()
    current = printer(
        md5=md5,
        file="part.gcode.3mf",
        gcode_file="part.gcode.3mf",
        param="Metadata/plate_1001.gcode",
    )
    store = LibraryStore(tmp_path / "library")
    await capture_once(current, store, inbox, inventory(), 990)
    assert inbox.adopt_running_print.called
    assert inbox.adopt_running_print.call_args.args[3]["plate"] == 1001


async def test_a_reprint_of_a_bridge_sent_file_from_the_printer_is_adopted(tmp_path, storage):
    # No bridge start owns the printer, so the print is foreign whatever its name.
    inbox = foreign_inbox(sent_file=lambda *_: True)
    await capture_once(printer(), LibraryStore(tmp_path / "library"), inbox, inventory(), 990)
    assert inbox.adopt_running_print.called and storage.downloads == ["part.gcode"]


def test_a_sent_file_is_one_this_printer_got_and_nothing_rewrote_since(tmp_path):
    inbox = NativeInbox(tmp_path / "inbox")
    with inbox.connect() as db:
        db.execute(
            "INSERT INTO uploads (id,printer,logical,remote,state,created,kind) "
            "VALUES (?,?,'/a.3mf','/beluga-a.gcode.3mf','delivered',1000,'upload')",
            ("a" * 32, SERIAL),
        )
    assert inbox.sent_file("beluga-a.gcode.3mf", SERIAL)
    assert inbox.sent_file("beluga-a.gcode.3mf", SERIAL, 1000 + 30)  # written as it arrived
    assert not inbox.sent_file("beluga-a.gcode.3mf", "OTHER")
    assert not inbox.sent_file("beluga-a.gcode.3mf", SERIAL, 1000 + 3600)  # rewritten later


async def test_catch_up_copies_a_file_rewritten_under_a_bridge_sent_name(tmp_path, monkeypatch):
    now = time.time()
    files = Storage({("", "part.gcode"): (GCODE + b";rewritten\n", now - 60)})
    monkeypatch.setattr(print_capture, "_ftps", lambda _printer, _port: files)
    # The bridge's upload of that name is two hours older than the file.
    inbox = SimpleNamespace(sent_file=lambda _n, _p, written: written < now - 7200)
    assert await catch_up(idle(), LibraryStore(tmp_path / "library"), inbox, 990) == 1


async def test_a_copy_that_failed_to_store_is_fetched_again_next_tick(
    tmp_path, storage, monkeypatch
):
    store = LibraryStore(tmp_path / "library")
    append, calls = store.append, []

    def flaky(*args):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("transient write failure")
        return append(*args)

    monkeypatch.setattr(store, "append", flaky)
    with pytest.raises(OSError):
        await capture_once(printer(), store, None, None, 990)
    await capture_once(printer(), store, None, None, 990)
    assert store.capture_state(identity("printer-file", MD5)) == "stored"


async def test_each_new_print_of_a_file_the_library_refused_is_adopted(tmp_path, storage):
    body = plate(1001)
    md5 = hashlib.md5(body).hexdigest().upper()
    storage.files[("", "part.gcode.3mf")] = (body, None)
    inbox = foreign_inbox()
    current = printer(md5=md5, file="part.gcode.3mf", gcode_file="part.gcode.3mf")
    store = LibraryStore(tmp_path / "library")
    for _ in range(2):  # the first print, then a reprint after it ended
        await capture_once(current, store, inbox, inventory(), 990)
    assert inbox.adopt_running_print.call_count == 2
    # Without counting on, the refused file is not fetched again every tick.
    for _ in range(3):
        await capture_once(current, store, inbox, None, 990)
    assert storage.downloads == ["part.gcode.3mf"] * 2
