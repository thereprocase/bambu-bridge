import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bambu_bridge.service.slot_display import InventoryDisplay, InventoryProjection
from tests.test_native_spools import spool


def display(*, qualified=True, timeout=1):
    projection = InventoryProjection("provider-a", 1, (spool(),), ())
    inventory = SimpleNamespace(native_projection=AsyncMock(return_value=projection))
    notify = Mock()
    result = InventoryDisplay(
        "bridge", lambda: inventory, lambda: object(), notify, qualified=qualified, timeout=timeout
    )
    return result, inventory, notify, projection


async def test_default_qualification_and_both_opt_ins_are_required():
    controller, inventory, notify, _ = display(qualified=False)
    controller.policy(global_enabled=True, printer_enabled=True)
    await controller.refresh()
    inventory.native_projection.assert_not_awaited()
    assert not controller.overlay.enabled
    notify.assert_not_called()
    controller.qualified = True
    controller.policy(global_enabled=True, printer_enabled=False)
    await controller.refresh()
    inventory.native_projection.assert_not_awaited()
    controller.policy(global_enabled=True, printer_enabled=True)
    await controller.refresh()
    assert controller.overlay.enabled
    notify.assert_called_once()
    await controller.refresh()  # unchanged state does not flood idle clients
    notify.assert_called_once()
    controller.policy(global_enabled=False, printer_enabled=True)
    assert not controller.overlay.enabled and not controller.overlay.bindings
    assert notify.call_count == 2


@pytest.mark.parametrize("cause", ["provider", "timeout", "missing"])
async def test_failed_refresh_withdraws_previous_bindings(cause):
    controller, inventory, notify, _ = display(timeout=0.01)
    controller.policy(global_enabled=True, printer_enabled=True)
    await controller.refresh()
    if cause == "provider":
        inventory.native_projection.side_effect = RuntimeError("sensitive provider details")
    elif cause == "timeout":

        async def pending(_):
            await asyncio.Event().wait()

        inventory.native_projection.side_effect = pending
    else:
        controller.inventory = lambda: None
    await controller.refresh()
    assert not controller.overlay.enabled and not controller.overlay.bindings
    assert "sensitive" not in controller.reason
    assert notify.call_count == 2


async def test_disable_while_reading_never_publishes_late_result():
    controller, inventory, notify, projection = display()
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(_):
        started.set()
        await release.wait()
        return projection

    inventory.native_projection.side_effect = delayed
    controller.policy(global_enabled=True, printer_enabled=True)
    task = asyncio.create_task(controller.refresh())
    await started.wait()
    controller.policy(global_enabled=False, printer_enabled=True)
    release.set()
    await task
    assert not controller.overlay.enabled and not controller.overlay.bindings
    notify.assert_not_called()


async def test_worker_cancellation_restores_then_stops():
    controller, inventory, notify, projection = display()
    controller.policy(global_enabled=True, printer_enabled=True)
    await controller.refresh()
    inventory.native_projection.return_value = replace(
        projection, bindings=(replace(spool(), remaining_g=0),)
    )
    await controller.refresh()
    assert controller.overlay.bindings[0].remaining_percent == 0
    task = asyncio.create_task(controller.run())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not controller.overlay.enabled and not controller.overlay.bindings
    assert notify.call_count == 3


async def test_persisted_policy_is_loaded_and_read_failure_restores_physical_state():
    controller, inventory, notify, _ = display()
    controller.load_policy = AsyncMock(return_value=(True, True))
    await controller.refresh()
    assert controller.overlay.enabled
    controller.load_policy.return_value = (False, False)
    await controller.refresh()
    assert not controller.overlay.enabled and notify.call_count == 2
    controller.load_policy.return_value = (True, True)
    await controller.refresh()
    controller.load_policy.side_effect = RuntimeError("private database detail")
    await controller.refresh()
    assert not controller.overlay.enabled and not controller.overlay.bindings
    assert controller.reason == "policy_unavailable"


async def test_disable_during_policy_read_cannot_be_reenabled_by_old_preferences():
    controller, inventory, _, _ = display()
    started, release = asyncio.Event(), asyncio.Event()

    async def stale_policy():
        started.set()
        await release.wait()
        return True, True

    controller.load_policy = stale_policy
    task = asyncio.create_task(controller.refresh())
    await started.wait()
    controller.policy(global_enabled=False, printer_enabled=False)
    release.set()
    await task
    assert not controller.active and not controller.overlay.enabled
    inventory.native_projection.assert_not_awaited()
