"""Avoid unrelated database/status work without weakening start readiness."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bambu_bridge.native_gateway import NativeGateway


async def test_unchanged_reports_do_not_reload_receipt_history():
    inbox = SimpleNamespace(
        observe=Mock(return_value=False), status=Mock(return_value=[{"id": "new"}])
    )
    gateway = SimpleNamespace(
        inbox=inbox,
        config={"printer_id": "fixture"},
        service=lambda: SimpleNamespace(native_snapshot=lambda: {}),
        inbox_status=[{"id": "old"}],
    )
    await NativeGateway.observe_inbox(gateway, {"print": {"gcode_state": "RUNNING"}})
    inbox.status.assert_not_called()
    assert gateway.inbox_status == [{"id": "old"}]
    inbox.observe.return_value = True
    await NativeGateway.observe_inbox(gateway, {"print": {"gcode_state": "FINISH"}})
    assert gateway.inbox_status == [{"id": "new"}]


async def test_delivered_upload_without_start_skips_readiness():
    row = {"id": "fixture", "state": "delivered", "start_state": None}
    completed = asyncio.Event()

    def status():
        return [row]

    inbox = SimpleNamespace(
        expire_dispatch=lambda: [],
        prune=lambda: None,
        pending=lambda _: [row],
        get=lambda _: row,
        status=status,
    )
    gateway = SimpleNamespace(
        inbox=inbox,
        config={"printer_id": "fixture"},
        inbox_wake=asyncio.Event(),
        inbox_dispatch_lock=asyncio.Lock(),
        service=lambda: None,
        ensure_idle=AsyncMock(),
        recover_lost_start=AsyncMock(),
    )
    loop = asyncio.get_running_loop()
    inbox.status = lambda: (loop.call_soon_threadsafe(completed.set), [row])[1]
    gateway.inbox_wake.set()
    task = asyncio.create_task(NativeGateway.deliver_inbox(gateway))
    try:
        await asyncio.wait_for(completed.wait(), 2)
        gateway.ensure_idle.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def recovery_gateway(*, recoverable, ensure_idle):
    return SimpleNamespace(
        inbox=SimpleNamespace(recoverable=Mock(return_value=recoverable)),
        config={"printer_id": "fixture"},
        ensure_idle=ensure_idle,
    )


async def test_lost_start_recovery_requests_status_only_when_a_start_is_recoverable():
    gateway = recovery_gateway(recoverable=False, ensure_idle=AsyncMock())
    await NativeGateway.recover_lost_start(gateway)
    gateway.ensure_idle.assert_not_awaited()
    gateway = recovery_gateway(recoverable=True, ensure_idle=AsyncMock())
    await NativeGateway.recover_lost_start(gateway)
    gateway.inbox.recoverable.assert_called_once_with("fixture")
    gateway.ensure_idle.assert_awaited_once_with(force_refresh=True)


async def test_lost_start_recovery_defers_when_printer_is_not_ready():
    gateway = recovery_gateway(
        recoverable=True, ensure_idle=AsyncMock(side_effect=ValueError("BBSTART_NOT_IDLE"))
    )
    await NativeGateway.recover_lost_start(gateway)
    gateway.ensure_idle.assert_awaited_once()


async def test_lost_start_recovery_failure_never_reaches_the_caller():
    gateway = recovery_gateway(
        recoverable=True, ensure_idle=AsyncMock(side_effect=RuntimeError("unexpected"))
    )
    await NativeGateway.recover_lost_start(gateway)
    gateway.ensure_idle.assert_awaited_once()
