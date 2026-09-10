"""The bridge is a second printer, with separate identity and credentials."""

import asyncio
import copy
import json
import re

import aiomqtt
from cryptography import x509
from cryptography.x509.oid import NameOID

from bambu_bridge.native_gateway import NativeGateway
from bambu_bridge.service.events import Event
from tests.api.test_native import gateway, port, tls  # noqa: F401


async def test_virtual_identity_is_distinct_persistent_and_used_by_certificate(gateway):  # noqa: F811
    serial = gateway.serial
    assert re.fullmatch(r"01P[A-Z0-9]{12}", serial)
    assert serial != gateway.service().serial
    assert gateway.status()["printer_id"] == gateway.service().serial
    assert gateway.status()["serial"] == serial
    directory = gateway.store.directory
    cert = x509.load_pem_x509_certificate((directory / "native-rsa/identity.crt").read_bytes())
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == serial
    key = (directory / "native-rsa/identity.key").read_bytes()
    code = gateway.saved_code()
    await gateway.close()
    resumed = NativeGateway(
        gateway.app, gateway.store, gateway.host, ports=(0, 0, 0), detect_port=0
    )
    try:
        await resumed.start()
        assert resumed.serial == serial and resumed.saved_code() == code
        assert (directory / "native-rsa/identity.key").read_bytes() == key
        await resumed.disable()
        await resumed.enable(gateway.service().serial)
        assert resumed.serial == serial
    finally:
        await resumed.close()


async def test_native_identity_and_password_are_translated_without_leaking_upstream(gateway):  # noqa: F811
    service = gateway.service()
    physical = service.serial
    original = {
        "info": {"module": [{"name": "ota", "sn": physical}, {"name": "ams", "sn": "COMPONENT"}]},
        "system": {"command": "get_access_code", "access_code": service.access_code},
    }
    before = copy.deepcopy(original)
    service.native_snapshot = lambda: original
    async with aiomqtt.Client(
        gateway.host,
        port=port(gateway, 0),
        username="bblp",
        password=gateway.test_code,
        tls_context=tls(),
    ) as client:
        rejected = await client.subscribe(f"device/{physical}/report")
        assert rejected[0].is_failure
        await client.subscribe(f"device/{gateway.serial}/report")

        async def receive():
            return json.loads(
                (await asyncio.wait_for(anext(client.messages.__aiter__()), 3)).payload
            )

        first = await receive()
        assert first["info"]["module"][0]["sn"] == gateway.serial
        assert first["info"]["module"][1]["sn"] == "COMPONENT"
        assert first["system"]["access_code"] == gateway.test_code
        assert service.access_code not in json.dumps(first)
        assert original == before
        await client.publish(
            f"device/{gateway.serial}/request",
            json.dumps(
                {
                    "system": {"command": "get_access_code", "sequence_id": "17"},
                }
            ),
            qos=1,
        )
        assert await receive() == {
            "system": {
                "command": "get_access_code",
                "sequence_id": "17",
                "access_code": gateway.test_code,
            }
        }
        service.send_raw.assert_not_awaited()
        service.raw_bus.publish(Event("snapshot", original))
        assert await receive() == first
        command = {
            "print": {
                "command": "project_file",
                "device_id": gateway.serial,
                "use_ams": True,
                "ams_mapping": [3, 1],
                "param": "Metadata/plate_2.gcode",
                "url": "file:///sdcard/model.3mf",
            }
        }
        await client.publish(f"device/{gateway.serial}/request", json.dumps(command), qos=1)
        expected = copy.deepcopy(command)
        expected["print"]["device_id"] = physical
        service.send_raw.assert_awaited_once_with(expected)
        assert command["print"]["device_id"] == gateway.serial
