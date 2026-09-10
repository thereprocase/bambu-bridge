"""Address isolation and owner-generated onboarding against real native TLS."""

import asyncio
import base64
import copy
import ipaddress
import json
import re

import aiomqtt
import pytest

from bambu_bridge.native_gateway import native_report
from bambu_bridge.native_setup import setup_material
from bambu_bridge.service.events import Event
from tests.api.test_native import SERIAL, gateway, port, tls  # noqa: F401


def packed(address):
    return int.from_bytes(ipaddress.IPv4Address(address).packed, "little")


def test_address_transposition_preserves_payload_and_unrelated_numbers():
    physical, second, bridge = "192.0.2.33", "198.51.100.8", "100.64.0.9"
    original = {
        "print": {
            "net": {
                "info": [
                    {"ip": packed(physical), "mask": 0x00FFFFFF, "gw": packed("192.0.2.1")},
                    {"ip": packed(second)},
                    {"ip": 0},
                ]
            },
            "ipcam": {"rtsp_url": f"rtsps://{second}/streaming/live/1"},
            "download_url": f"ftp://{physical}:990/file.3mf",
            "unrelated_number": packed(physical),
            "unrelated_address": "192.0.2.330",
            "ams_mapping": [3, 0, 2, 1],
        }
    }
    before = copy.deepcopy(original)
    result = native_report(original, physical, bridge)
    assert original == before
    assert [i["ip"] for i in result["print"]["net"]["info"]] == [packed(bridge), packed(bridge), 0]
    assert result["print"]["download_url"] == f"ftp://{bridge}:990/file.3mf"
    assert result["print"]["ipcam"]["rtsp_url"] == f"rtsps://{bridge}/streaming/live/1"
    assert result["print"]["unrelated_number"] == packed(physical)
    assert result["print"]["unrelated_address"] == "192.0.2.330"
    assert result["print"]["ams_mapping"] == [3, 0, 2, 1]
    assert result["print"]["net"]["info"][0]["mask"] == 0xFFFFFFFF
    assert result["print"]["net"]["info"][0]["gw"] == 0
    for payload in [{}, {"print": None}, {"print": {"net": {"info": None}}}]:
        assert native_report(payload, physical, bridge) == payload


async def test_bootstrap_and_incremental_reports_keep_orca_on_gateway(gateway):  # noqa: F811
    service = gateway.service()
    service.ip = "192.0.2.33"
    report = {"print": {"net": {"info": [{"ip": packed(service.ip)}]}}}
    service.native_snapshot = lambda: report
    async with aiomqtt.Client(
        gateway.host,
        port=port(gateway, 0),
        username="bblp",
        password=gateway.test_code,
        tls_context=tls(),
    ) as client:
        await client.subscribe(f"device/{SERIAL}/report")
        first = json.loads((await asyncio.wait_for(anext(client.messages.__aiter__()), 3)).payload)
        assert first["print"]["net"]["info"][0]["ip"] == packed(gateway.host)
        assert gateway.setup_status("127.0.0.1")["printer_connected"]
        assert not gateway.setup_status("192.0.2.99")["printer_connected"]
        service.raw_bus.publish(Event("snapshot", report))
        delta = json.loads((await asyncio.wait_for(anext(client.messages.__aiter__()), 3)).payload)
        assert delta == first
        assert report["print"]["net"]["info"][0]["ip"] == packed(service.ip)
    for _ in range(30):
        if not gateway.setup_status("127.0.0.1")["printer_connected"]:
            break
        await asyncio.sleep(0.01)
    assert not gateway.setup_status("127.0.0.1")["printer_connected"]


async def test_setup_uses_instance_identity_and_saved_code_without_rotation(gateway):  # noqa: F811
    gateway.service().serial = "SETUPFIXTURE001"
    await gateway.close()
    await gateway.start()
    before = dict(gateway.config)
    material = setup_material(gateway)
    match = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", material["script"])
    assert match
    payload = json.loads(base64.b64decode(match[1]))
    assert payload["host"] == gateway.host
    assert payload["serial"] == "SETUPFIXTURE001"
    assert payload["code"] == gateway.test_code
    assert payload["fingerprint"] == material["certificate_sha256"]
    assert "PRIVATE KEY" not in material["certificate_pem"]
    assert "ExecutionPolicy" not in material["script"]
    assert "BRIDGE_API_KEY" not in material["script"]
    assert gateway.config == before
    assert setup_material(gateway) == material
    with gateway.store.connect() as db:
        db.execute("DELETE FROM native_code")
    with pytest.raises(ValueError, match="Save your existing"):
        setup_material(gateway)
