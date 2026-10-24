"""Virtual status uses the real native MQTT transport, not physical telemetry."""

import asyncio
import copy
import json

import aiomqtt
import pytest

from bambu_bridge.service.events import Event
from tests.api.test_native import gateway, port, tls  # noqa: F401
from tests.test_native_spools import overlay, snapshot


async def receive(client):
    message = await asyncio.wait_for(anext(client.messages.__aiter__()), 3)
    return json.loads(message.payload)


async def test_two_clients_restore_virtual_status_without_physical_side_effects(gateway):  # noqa: F811
    raw = snapshot()
    before = copy.deepcopy(raw)
    gateway.service().native_snapshot = lambda: raw
    gateway.spool_overlay = overlay(raw)
    options = {
        "hostname": gateway.host,
        "port": port(gateway, 0),
        "username": "bblp",
        "password": gateway.test_code,
        "tls_context": tls(),
    }
    async with aiomqtt.Client(**options) as first, aiomqtt.Client(**options) as second:
        for client in (first, second):
            await client.subscribe(f"device/{gateway.serial}/report")
            report = await receive(client)
            assert report["print"]["ams"]["ams"][0]["tray"][0]["remain"] == 44
        gateway.spool_overlay.enabled = False
        # Dedicated local presentation event, not a fabricated printer report.
        gateway.spool_reports.publish(Event("snapshot", {}))
        for client in (first, second):
            report = await receive(client)
            tray = report["print"]["ams"]["ams"][0]["tray"][0]
            assert tray["tag_uid"] == "0" and tray["tray_info_idx"] == "GFB98"
            assert tray["remain"] == -1
            assert report["print"]["ams"]["calibrate_remain_flag"] is False
    assert raw == before
    gateway.service().send_raw.assert_not_awaited()


async def test_virtual_material_command_is_rejected_but_refill_toggle_passes(gateway):  # noqa: F811
    raw = snapshot()
    gateway.service().native_snapshot = lambda: raw
    gateway.spool_overlay = overlay(raw)
    async with aiomqtt.Client(
        gateway.host,
        port=port(gateway, 0),
        username="bblp",
        password=gateway.test_code,
        tls_context=tls(),
    ) as client:
        await client.subscribe(f"device/{gateway.serial}/report")
        await receive(client)
        await client.publish(
            f"device/{gateway.serial}/request",
            json.dumps(
                {
                    "print": {
                        "command": "ams_filament_setting",
                        "ams_id": 0,
                        "slot_id": 0,
                        "tray_info_idx": "GFB61",
                        "sequence_id": "19",
                    }
                }
            ),
            qos=1,
        )
        reply = await receive(client)
        assert reply["print"]["reason"] == "BBVIRTUAL_SPOOL_READ_ONLY"
        gateway.service().send_raw.assert_not_awaited()
        command = {"print": {"command": "print_option", "auto_switch_filament": True}}
        await client.publish(f"device/{gateway.serial}/request", json.dumps(command), qos=1)
        for _ in range(100):
            if gateway.service().send_raw.await_count:
                break
            await asyncio.sleep(0.01)
        gateway.service().send_raw.assert_awaited_once_with(command)


@pytest.mark.parametrize("cause", ["disconnect", "malformed"])
async def test_invalid_physical_state_closes_native_client_not_only_forwarding_task(gateway, cause):  # noqa: F811
    raw = snapshot()
    service = gateway.service()
    service.native_snapshot = lambda: raw
    gateway.spool_overlay = overlay(raw)
    with pytest.raises(aiomqtt.MqttError):
        async with aiomqtt.Client(
            gateway.host,
            port=port(gateway, 0),
            username="bblp",
            password=gateway.test_code,
            tls_context=tls(),
        ) as client:
            await client.subscribe(f"device/{gateway.serial}/report")
            await receive(client)
            if cause == "disconnect":
                service.connected = False
                service.bus.publish(Event("event", {}, name="connection_lost"))
            else:
                raw["print"]["ams"]["tray_exist_bits"] = "malformed"
                service.raw_bus.publish(Event("snapshot", {"print": {"command": "push_status"}}))
            await receive(client)
    service.send_raw.assert_not_awaited()


async def test_disconnect_without_clients_invalidates_the_previous_loading(gateway):  # noqa: F811
    raw = snapshot()
    service = gateway.service()
    service.native_snapshot = lambda: raw
    gateway.spool_overlay = overlay(raw)
    # Give the centrally installed lifecycle subscription a turn to start.
    await asyncio.sleep(0)
    service.connected = False
    service.bus.publish(Event("event", {}, name="connection_lost"))
    await asyncio.sleep(0)
    service.connected = True
    await service.native_observer(raw)
    result = gateway.spool_overlay.project(raw, raw)
    assert result["print"]["ams"]["ams"][0]["tray"][0]["tag_uid"] == "0"


async def test_inventory_refresh_updates_idle_clients_and_restores_after_provider_failure(gateway):  # noqa: F811
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from bambu_bridge.service.slot_display import InventoryProjection
    from tests.test_native_spools import spool

    raw = snapshot()
    before = copy.deepcopy(raw)
    service = gateway.service()
    service.native_snapshot = lambda: raw
    projection = InventoryProjection("provider-a", 1, (spool(raw),), ())
    inventory = SimpleNamespace(native_projection=AsyncMock(return_value=projection))
    controller = gateway.inventory_display
    assert controller is not None and not controller.qualified
    controller.inventory = lambda: inventory
    controller.load_policy = None  # transport fixture supplies its policy directly
    controller.qualified = True  # isolated transport fixture, no production opt-in
    controller.policy(global_enabled=True, printer_enabled=True)
    await controller.refresh()
    options = {
        "hostname": gateway.host,
        "port": port(gateway, 0),
        "username": "bblp",
        "password": gateway.test_code,
        "tls_context": tls(),
    }
    async with aiomqtt.Client(**options) as first, aiomqtt.Client(**options) as second:
        for client in (first, second):
            await client.subscribe(f"device/{gateway.serial}/report")
            assert (await receive(client))["print"]["ams"]["ams"][0]["tray"][0]["remain"] == 44
        inventory.native_projection.return_value = replace(
            projection, bindings=(replace(spool(raw), remaining_g=0),)
        )
        async with service.raw_bus.subscribe() as physical:
            await controller.refresh()
            for client in (first, second):
                assert (await receive(client))["print"]["ams"]["ams"][0]["tray"][0]["remain"] == 0
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(physical.get(), 0.01)
            inventory.native_projection.side_effect = RuntimeError("provider unavailable")
            await controller.refresh()
            for client in (first, second):
                tray = (await receive(client))["print"]["ams"]["ams"][0]["tray"][0]
                assert tray["tag_uid"] == "0" and tray["remain"] == -1
        async with aiomqtt.Client(**options) as third:
            await third.subscribe(f"device/{gateway.serial}/report")
            tray = (await receive(third))["print"]["ams"]["ams"][0]["tray"][0]
            assert tray["tag_uid"] == "0"
    assert raw == before
    service.send_raw.assert_not_awaited()
