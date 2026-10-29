"""The gateway prunes its own job files from the printer: hourly, idle only, keeping what
a start or the library still needs."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bambu_bridge import native_gateway
from bambu_bridge.native_gateway import STORAGE_PRUNE_INTERVAL, NativeGateway
from bambu_bridge.protocol import ftps as ftps_module

OLD, NEW, CURRENT = ("1" * 32, "2" * 32, "3" * 32)
DAY = 86400


def gateway(*, idle=True, pruned_at=0.0):
    now = time.time()
    rows = [
        {"id": OLD, "state": "delivered", "start_state": "completed", "created": now - 5 * DAY},
        {"id": NEW, "state": "delivered", "start_state": "completed", "created": now - 60},
        {"id": CURRENT, "state": "delivered", "start_state": "running", "created": now - 600},
    ]
    inbox = SimpleNamespace(remote_rows=Mock(return_value=rows), remote_pruned=Mock())
    service = SimpleNamespace(
        ip="192.0.2.9",
        access_code="private",
        snapshot=lambda: {"_raw": {"gcode_file": f"beluga-{CURRENT}.gcode.3mf"}},
    )

    def require_idle():
        if not idle:
            raise ValueError("BBSTART_NOT_IDLE")

    return SimpleNamespace(
        inbox=inbox,
        config={"printer_id": "fixture"},
        storage_pruned_at=pruned_at,
        require_idle=require_idle,
        service=lambda: service,
        app=SimpleNamespace(state=SimpleNamespace(ftps_port=0)),
    )


@pytest.fixture
def ftps(monkeypatch):
    listing = [
        f"beluga-{OLD}.gcode.3mf",
        f"beluga-{NEW}.gcode.3mf",
        f"beluga-{CURRENT}.gcode.3mf",
        f"beluga-{'9' * 32}.bin",
        "owner-model.gcode.3mf",
    ]
    fake = SimpleNamespace(list_dir=AsyncMock(return_value=listing), delete=AsyncMock())
    monkeypatch.setattr(ftps_module, "FtpsTransfer", lambda *a, **k: fake)
    return fake


async def test_deletes_settled_old_uploads_and_orphans_only(ftps, monkeypatch):
    # Two settled uploads in the fixture; keep the newest one so the old one is eligible.
    monkeypatch.setattr(native_gateway, "STORAGE_PRUNE_KEEP", 1)
    g = gateway()
    await NativeGateway.prune_printer_storage(g)
    deleted = sorted(call.args[0] for call in ftps.delete.await_args_list)
    assert deleted == sorted([f"/beluga-{OLD}.gcode.3mf", f"/beluga-{'9' * 32}.bin"])
    g.inbox.remote_pruned.assert_called_once()
    assert sorted(g.inbox.remote_pruned.call_args.args[0]) == sorted([OLD, "9" * 32])
    assert g.storage_pruned_at > 0


async def test_skips_when_printer_busy_or_recently_pruned(ftps):
    busy = gateway(idle=False)
    await NativeGateway.prune_printer_storage(busy)
    recent = gateway(pruned_at=time.time() - STORAGE_PRUNE_INTERVAL / 2)
    await NativeGateway.prune_printer_storage(recent)
    ftps.list_dir.assert_not_awaited()
    ftps.delete.assert_not_awaited()


async def test_refused_listing_waits_for_the_next_hour(ftps):
    ftps.list_dir.side_effect = ftps_module.ftplib.error_perm("550 ")
    g = gateway()
    await NativeGateway.prune_printer_storage(g)
    ftps.delete.assert_not_awaited()
    g.inbox.remote_pruned.assert_not_called()
    assert g.storage_pruned_at > 0
    assert native_gateway.STORAGE_PRUNE_KEEP >= 1
