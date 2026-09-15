"""Replay through real durable receipts and the native worker, with a fake printer."""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bambu_bridge.library import LibraryError, LibraryStore
from bambu_bridge.library_backup import backup, restore
from bambu_bridge.library_replay import ReplayStart
from bambu_bridge.native_gateway import NativeGateway
from bambu_bridge.native_inbox import NativeInbox
from bambu_bridge.service.events import Event, EventBus
from bambu_bridge.service.library_history import LibraryHistory
from bambu_bridge.service.library_replay import LibraryReplay
from bambu_bridge.service.material_inventory import MaterialInventory, inventory_view
from tests.test_library_replay import PRINTER, archive_slice, materials, sliced


def setup(tmp_path, *, data=None, external=False):
    store = LibraryStore(tmp_path / "library")
    cid = archive_slice(store, data or sliced())
    inbox = NativeInbox(tmp_path / "pairing")
    reports = materials()
    if external:
        reports["vt_tray"] = {"id": "254", "tray_type": "PLA", "tray_color": "00FF00FF"}
    service = SimpleNamespace(
        connected=True,
        cert_status="trusted",
        material_inventory=MaterialInventory(),
        raw_bus=EventBus(),
        summary=lambda: {**PRINTER, "gcode_state": "IDLE"},
        native_snapshot=lambda: {"print": {"nozzle_diameter": "0.4"}},
        send_raw=AsyncMock(),
    )

    async def refresh(category, command, **fields):
        assert (category, command) == ("pushing", "pushall")
        service.material_inventory.observe(reports)
        service.raw_bus.publish(Event("snapshot", {"print": reports}))

    service.send_command = AsyncMock(side_effect=refresh)
    service.material_inventory.observe(reports)
    gateway = SimpleNamespace(
        inbox=inbox,
        config={"printer_id": "FIXTURE"},
        service=lambda: service,
        inbox_dispatch_lock=asyncio.Lock(),
        inbox_wake=asyncio.Event(),
        ensure_idle=AsyncMock(),
        inbox_failure=Mock(),
        arm_inbox_expiry=Mock(),
        inbox_owner=contextvars.ContextVar("fixture_inbox_owner", default=None),
        app=SimpleNamespace(
            state=SimpleNamespace(
                ftps_port=0,
                settings=SimpleNamespace(bridge_max_transfer_bytes=64 * 1024 * 1024),
            )
        ),
    )
    manager = LibraryReplay(store, lambda: gateway, enabled=True)
    gateway.app.state.library_replay = manager
    return SimpleNamespace(
        store=store,
        cid=cid,
        inbox=inbox,
        reports=reports,
        service=service,
        gateway=gateway,
        manager=manager,
    )


def approval(fixture, *, identifier="a" * 32, choices=None):
    return ReplayStart(
        id=identifier,
        printer_id="FIXTURE",
        choices=choices or {0: 3, 1: 1},
        inventory_fingerprint=inventory_view(fixture.service.material_inventory.snapshot())[
            "fingerprint"
        ],
        nozzle_diameter=0.4,
        bed_type="Textured PEI Plate",
        ready_confirmed=True,
    )


async def run_worker_once(fixture, identifier, *, delivered=True):
    # Model a completed, verified file delivery. No FTP/printer network runs.
    if delivered:
        assert fixture.inbox.transition(identifier, "stored", "delivered", "BBDELIVERY_OK")
    finished = asyncio.Event()
    fixture.service.send_raw.side_effect = lambda _: finished.set()
    fixture.gateway.inbox_failure.side_effect = lambda *_: finished.set()
    task = asyncio.create_task(NativeGateway.deliver_inbox(fixture.gateway))
    try:
        await asyncio.wait_for(finished.wait(), 3)
        # Wait for the worker's durable post-publish receipt, rather than assert
        # in the middle of the fake transport's send callback.
        async with asyncio.timeout(3):
            while fixture.inbox.get(identifier)["start_state"] == "dispatching":
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_duplicate_concurrent_requests_start_once_and_link_the_original_capture(tmp_path):
    fixture = setup(tmp_path)
    body = approval(fixture)
    first, retry = await asyncio.gather(
        fixture.manager.submit(fixture.cid, body),
        fixture.manager.submit(fixture.cid, body),
    )
    assert first["id"] == retry["id"] == body.id
    assert len(fixture.inbox.status()) == 1
    fixture.service.send_raw.assert_not_awaited()
    await run_worker_once(fixture, body.id)
    fixture.service.send_raw.assert_awaited_once()
    sent = fixture.service.send_raw.call_args.args[0]["print"]
    assert sent["ams_mapping"] == [3, 1, -1, -1]  # full preset count, unused entries retained
    assert sent["use_ams"] is True
    assert sent["param"] == "Metadata/plate_1.gcode"
    assert sent["url"].endswith(f"beluga-{body.id}.gcode.3mf")
    row = fixture.inbox.get(body.id)
    active = {"print": {"gcode_state": "RUNNING", "gcode_file": row["remote"]}}
    fixture.inbox.observe("FIXTURE", active, active)
    terminal = {"print": {**active["print"], "gcode_state": "FINISH"}}
    fixture.inbox.observe("FIXTURE", terminal, terminal)
    await LibraryHistory(fixture.store, lambda: fixture.inbox).scan()
    captures = fixture.store.list()
    assert len(captures) == 1 and captures[0]["id"] == fixture.cid
    attempt = captures[0]["attempts"][0]
    assert attempt["source"] == "replay" and attempt["state"] == "completed"
    assert attempt["start_options"]["bed_type"] == "textured_plate"
    assert (await fixture.manager.submit(fixture.cid, body))["state"] == "completed"
    fixture.service.send_raw.assert_awaited_once()
    # Even after the native receipt ages out, an old request cannot masquerade
    # as an unsent request or produce a second Start.
    with fixture.inbox.connect() as db:
        db.execute("DELETE FROM uploads WHERE id=?", (body.id,))
    assert fixture.manager.status(body.id)["state"] == "completed"
    assert (await fixture.manager.submit(fixture.cid, body))["state"] == "completed"
    fixture.store.delete(fixture.cid)
    assert fixture.manager.status(body.id)["state"] == "receipt_unavailable"


async def test_external_selection_254_becomes_disabled_ams_and_minus_one_array(tmp_path):
    fixture = setup(tmp_path, data=sliced(indices=(0,)), external=True)
    body = approval(fixture, choices={0: 254})
    await fixture.manager.submit(fixture.cid, body)
    await run_worker_once(fixture, body.id)
    sent = fixture.service.send_raw.call_args.args[0]["print"]
    assert sent["use_ams"] is False and sent["ams_mapping"] == [-1] * 4
    capture = fixture.store.get(fixture.cid)
    original, _ = fixture.store.download(fixture.cid, "slice.3mf")
    assert fixture.inbox.payload(body.id).read_bytes() == original.read_bytes()
    assert fixture.inbox.get(body.id)["sha256"] == capture["artifacts"][0]["sha256"]


@pytest.mark.parametrize("reason", ["inventory", "disabled", "deleted", "options"])
async def test_final_dispatch_gate_blocks_changed_or_unavailable_replay_context(tmp_path, reason):
    fixture = setup(tmp_path)
    body = approval(fixture)
    await fixture.manager.submit(fixture.cid, body)
    if reason == "inventory":
        fixture.reports["ams"]["ams"][0]["tray"][3]["tray_uuid"] = "replacement-spool"
    elif reason == "disabled":
        fixture.inbox.recover()  # queued survives; disabled library must still gate it
        fixture.gateway.app.state.library_replay = None
    elif reason == "deleted":
        fixture.store.delete(fixture.cid)
    else:
        row = fixture.inbox.get(body.id)
        command = json.loads(row["command"])
        command["print"]["ams_mapping"] = [0, 2, -1, -1]
        with fixture.inbox.connect() as db:
            db.execute("UPDATE uploads SET command=? WHERE id=?", (json.dumps(command), body.id))
    await run_worker_once(fixture, body.id)
    fixture.service.send_raw.assert_not_awaited()
    assert fixture.inbox.get(body.id)["start_state"] == "blocked"
    assert fixture.inbox.claim_start(body.id) is None
    assert (await fixture.manager.submit(fixture.cid, body))["state"] == "blocked"


async def test_crash_after_claim_and_failed_staging_do_not_retry_dispatch(tmp_path, monkeypatch):
    fixture = setup(tmp_path)
    body = approval(fixture)
    reserve = Mock(side_effect=OSError("synthetic storage unavailable"))
    monkeypatch.setattr(fixture.inbox, "reserve", reserve)
    with pytest.raises(OSError):
        await fixture.manager.submit(fixture.cid, body)
    assert fixture.store.replay_request(body.id)
    recovered = LibraryReplay(fixture.store, lambda: fixture.gateway, enabled=True)
    assert (await recovered.submit(fixture.cid, body))["state"] == "not_started"
    reserve.assert_called_once()
    fixture.service.send_raw.assert_not_awaited()
    with pytest.raises(LibraryError, match="different print choices"):
        await recovered.submit(fixture.cid, body.model_copy(update={"choices": {0: 0, 1: 2}}))


async def test_receiving_failure_releases_reserved_start_and_claim_survives_backup(
    tmp_path, monkeypatch
):
    fixture = setup(tmp_path)
    body = approval(fixture)
    monkeypatch.setattr(
        fixture.inbox, "receive", AsyncMock(side_effect=OSError("disk unavailable"))
    )
    with pytest.raises(OSError):
        await fixture.manager.submit(fixture.cid, body)
    assert not fixture.inbox.unresolved("FIXTURE")
    assert fixture.inbox.get(body.id)["start_state"] == "cancelled"
    fixture.store.delete(fixture.cid)
    backup(fixture.store, tmp_path / "backup")
    restored = restore(tmp_path / "backup", tmp_path / "restored")
    assert restored.replay_request(body.id) == fixture.store.replay_request(body.id)
    assert restored.capture_state(fixture.cid) == "deleted"


async def test_claim_is_visible_during_slow_preflight_and_failed_check_is_not_retried(tmp_path):
    fixture = setup(tmp_path)
    body = approval(fixture)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_idle(**kwargs):
        entered.set()
        await release.wait()
        raise ValueError("printer started another job")

    fixture.gateway.ensure_idle.side_effect = slow_idle
    task = asyncio.create_task(fixture.manager.submit(fixture.cid, body))
    await asyncio.wait_for(entered.wait(), 3)
    assert fixture.manager.status(body.id)["state"] == "preparing_transfer"
    assert (await fixture.manager.submit(fixture.cid, body))["state"] == "preparing_transfer"
    assert fixture.inbox.status() == []
    release.set()
    with pytest.raises(ValueError):
        await task
    assert (await fixture.manager.submit(fixture.cid, body))["state"] == "not_started"
    fixture.gateway.ensure_idle.assert_awaited_once()
    fixture.service.send_raw.assert_not_awaited()


async def test_cancelled_reservation_joins_writer_and_releases_its_fence(tmp_path, monkeypatch):
    fixture = setup(tmp_path)
    body = approval(fixture)
    entered, release = threading.Event(), threading.Event()
    reserve = fixture.inbox.reserve

    def slow_reserve(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return reserve(*args, **kwargs)

    monkeypatch.setattr(fixture.inbox, "reserve", slow_reserve)
    task = asyncio.create_task(fixture.manager.submit(fixture.cid, body))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not fixture.inbox.unresolved("FIXTURE")
    assert fixture.inbox.get(body.id)["start_state"] == "cancelled"
    assert (await fixture.manager.submit(fixture.cid, body))["state"] == "cancelled"
    fixture.service.send_raw.assert_not_awaited()


async def test_transfer_limit_is_checked_before_printer_io(tmp_path):
    fixture = setup(tmp_path)
    body = approval(fixture)
    fixture.gateway.app.state.settings.bridge_max_transfer_bytes = 1
    with pytest.raises(LibraryError, match="transfer limit"):
        await fixture.manager.submit(fixture.cid, body)
    fixture.gateway.ensure_idle.assert_not_awaited()
    fixture.service.send_command.assert_not_awaited()
    assert fixture.manager.status(body.id)["state"] == "not_started"


async def test_final_inventory_refresh_revealing_busy_printer_cannot_start(tmp_path):
    fixture = setup(tmp_path)
    body = approval(fixture)
    await fixture.manager.submit(fixture.cid, body)
    fixture.gateway.ensure_idle.side_effect = [None, ValueError("BBSTART_PRINTER_BUSY")]
    await run_worker_once(fixture, body.id)
    fixture.service.send_raw.assert_not_awaited()
    assert fixture.inbox.get(body.id)["start_state"] == "blocked"


@pytest.mark.parametrize("restart", [False, True])
async def test_delivery_failure_and_restart_release_unsent_replay_without_retry(
    tmp_path, monkeypatch, restart
):
    from bambu_bridge.protocol.ftps import FtpsTransfer

    fixture = setup(tmp_path)
    body = approval(fixture)
    await fixture.manager.submit(fixture.cid, body)
    if restart:
        fixture.inbox.transition(body.id, "stored", "delivering", "BBDELIVERY_PENDING")
        fixture.inbox.recover()
    else:
        fixture.service.ip = "127.0.0.1"
        fixture.service.access_code = "synthetic-no-network"
        connect = Mock(side_effect=OSError("synthetic FTP failure"))
        monkeypatch.setattr(FtpsTransfer, "_connect", connect)
        await run_worker_once(fixture, body.id, delivered=False)
        connect.assert_called_once()
    assert not fixture.inbox.unresolved("FIXTURE")
    assert (await fixture.manager.submit(fixture.cid, body))["state"] == "blocked"
    fixture.service.send_raw.assert_not_awaited()
