"""Native wire acceptance against isolated MQTT, camera and TLS/FTP fixtures."""

from __future__ import annotations

import asyncio
import ftplib
import io
import json
import ssl
import struct
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiomqtt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from bambu_bridge.config import Settings
from bambu_bridge.native_gateway import NativeGateway
from bambu_bridge.pairing import PairingStore, identity
from bambu_bridge.protocol.camera import build_auth_packet
from bambu_bridge.protocol.ftps import FtpsTransfer, _ImplicitFTP_TLS
from bambu_bridge.service.events import Event, EventBus
from tests.conftest import ACCESS_CODE

SERIAL = "NATIVE_TEST_P1S"
JPEG = b"\xff\xd8fixture-frame\xff\xd9"


def tls():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # isolated loopback fixture only
    return context


@pytest.fixture
async def gateway(tmp_path):
    @asynccontextmanager
    async def frames():
        queue = asyncio.Queue()
        queue.put_nowait(JPEG)
        yield queue

    snapshot = {
        "print": {
            "command": "push_status",
            "gcode_state": "IDLE",
            "ams": {"ams": [{"id": "0", "tray": [{"id": "3", "tray_type": "PETG"}]}]},
            "vt_tray": {"id": "254", "tray_type": "PLA"},
        },
        "info": {"command": "get_version", "module": [{"name": "ota", "sw_ver": "01.02"}]},
    }
    service = SimpleNamespace(
        serial=SERIAL,
        ip="127.0.0.1",
        model="P1S",
        connected=True,
        cert_status="trusted",
        access_code=ACCESS_CODE,
        raw_bus=EventBus(),
        send_raw=AsyncMock(),
        native_snapshot=lambda: snapshot,
        camera=SimpleNamespace(subscribe=frames),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            registry=SimpleNamespace(get=lambda _: service), settings=Settings(), ftps_port=1
        )
    )
    gateway = NativeGateway(
        app, PairingStore(tmp_path / "pairing"), "127.0.0.1", ports=(0, 0, 0), detect_port=0
    )
    setup = await gateway.enable(SERIAL)
    gateway.test_code = setup["access_code"]
    try:
        yield gateway
    finally:
        await gateway.close()


def port(gateway, index):
    return gateway.servers[index].sockets[0].getsockname()[1]


async def test_orca_ip_detect_returns_identity_without_credentials(gateway):
    # Native bind_detect framing, independent of the server encoder. Splitting
    # the request across TCP writes covers a real stream rather than one recv.
    body = b'{"login":{"command":"detect","sequence_id":"20000"}}'
    frame = b"\xa5\xa5" + struct.pack("<H", len(body) + 6) + body + b"\xa7\xa7"
    reader, writer = await asyncio.open_connection("127.0.0.1", port(gateway, 3))
    for part in (frame[:1], frame[1:5], frame[5:]):
        writer.write(part)
        await writer.drain()
        await asyncio.sleep(0)
    response = await asyncio.wait_for(reader.read(), 3)
    writer.close()
    await writer.wait_closed()
    assert response[:2] == b"\xa5\xa5" and response[-2:] == b"\xa7\xa7"
    assert struct.unpack_from("<H", response, 2)[0] == len(response)
    assert json.loads(response[4:-2]) == {
        "login": {
            "command": "detect",
            "sequence_id": "20000",
            "id": gateway.serial,
            "model": "C12",
            "name": "Bridge P1S",
            "version": "01.02",
            "bind": "free",
            "connect": "lan",
        }
    }
    assert gateway.test_code.encode() not in response
    assert ACCESS_CODE.encode() not in response
    gateway.service().send_raw.assert_not_awaited()
    assert gateway.status()["connections"]["detect"]["phase"] == "identity_sent"
    assert gateway.status()["connections"]["detect"]["tls_connections"] == 0


@pytest.mark.parametrize(
    "body",
    [
        b'{"login":{"command":"login","sequence_id":"1"}}',
        b'{"print":{"command":"project_file"}}',
        b'{"login":{"command":"detect","sequence_id":{}}}',
        b"[]",
        b"{",
    ],
)
async def test_identity_port_rejects_non_detection_requests(gateway, body):
    reader, writer = await asyncio.open_connection("127.0.0.1", port(gateway, 3))
    writer.write(b"\xa5\xa5" + struct.pack("<H", len(body) + 6) + body + b"\xa7\xa7")
    await writer.drain()
    assert await asyncio.wait_for(reader.read(), 3) == b""
    writer.close()
    await writer.wait_closed()
    gateway.service().send_raw.assert_not_awaited()


@pytest.mark.parametrize(
    "frame",
    [
        b"\xa5\xa5\xff\xff",
        b"\xa5\xa5\x00\x00",
        b"XX\x08\x00{}\xa7\xa7",
        b"\xa5\xa5\x08\x00{}XX",
    ],
)
async def test_identity_port_rejects_bad_framing_without_waiting(gateway, frame):
    reader, writer = await asyncio.open_connection("127.0.0.1", port(gateway, 3))
    writer.write(frame)
    await writer.drain()
    assert await asyncio.wait_for(reader.read(), 3) == b""
    writer.close()
    await writer.wait_closed()


async def test_disable_closes_identity_listener_and_partial_request(gateway):
    address = port(gateway, 3)
    reader, writer = await asyncio.open_connection("127.0.0.1", address)
    writer.write(b"\xa5")
    await writer.drain()
    await asyncio.sleep(0.02)
    await gateway.disable()
    assert await asyncio.wait_for(reader.read(), 3) == b""
    writer.close()
    await writer.wait_closed()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", address)


async def test_native_mqtt_live_ams_external_camera_and_command(gateway):
    async with aiomqtt.Client(
        "127.0.0.1",
        port=port(gateway, 0),
        username="bblp",
        password=gateway.test_code,
        tls_context=tls(),
    ) as client:
        await client.subscribe(f"device/{gateway.serial}/report")
        message = await asyncio.wait_for(anext(client.messages.__aiter__()), 3)
        state = json.loads(message.payload)
        assert state["print"]["ams"]["ams"][0]["tray"][0]["id"] == "3"
        assert state["print"]["vt_tray"]["id"] == "254"
        command = {
            "print": {
                "command": "project_file",
                "sequence_id": "17",
                "use_ams": True,
                "ams_mapping": [3, 0],
                "param": "Metadata/plate_3.gcode",
                "url": "file:///sdcard/test.gcode.3mf",
            }
        }
        await client.publish(f"device/{gateway.serial}/request", json.dumps(command), qos=1)
        gateway.service().send_raw.assert_awaited_once_with(command)
        # Identical acknowledgements must not be lost to normalized-state diffing.
        ack = {"print": {"command": "project_file", "sequence_id": "17", "result": "success"}}
        for _ in range(2):
            gateway.service().raw_bus.publish(Event("snapshot", ack))
            message = await asyncio.wait_for(anext(client.messages.__aiter__()), 3)
            assert json.loads(message.payload) == ack
        reader, writer = await asyncio.open_connection("127.0.0.1", port(gateway, 2), ssl=tls())
        writer.write(build_auth_packet("bblp", gateway.test_code))
        await writer.drain()
        header = await asyncio.wait_for(reader.readexactly(16), 3)
        # Real P1S JPEG headers mark an independent/keyframe at byte offset 8.
        # Checking payload length alone misses Orca's stream-start requirement.
        assert struct.unpack("<IIII", header) == (len(JPEG), 0, 1, 0)
        assert await reader.readexactly(struct.unpack_from("<I", header)[0]) == JPEG
        assert gateway.setup_status("127.0.0.1") == {
            "printer_connected": True,
            "camera_streaming": True,
        }
        assert gateway.setup_status("192.0.2.99") == {
            "printer_connected": False,
            "camera_streaming": False,
        }
        writer.close()
        await writer.wait_closed()


async def test_shared_native_code_supports_simultaneous_computers(gateway):
    options = {
        "hostname": "127.0.0.1",
        "port": port(gateway, 0),
        "username": "bblp",
        "password": gateway.test_code,
        "tls_context": tls(),
    }
    async with (
        aiomqtt.Client(**options, identifier="computer-one") as first,
        aiomqtt.Client(**options, identifier="computer-two") as second,
    ):
        for client in (first, second):
            await client.subscribe(f"device/{gateway.serial}/report")
            await asyncio.wait_for(anext(client.messages.__aiter__()), 3)
        update = {"print": {"command": "push_status", "gcode_state": "RUNNING"}}
        gateway.service().raw_bus.publish(Event("snapshot", update))
        for client in (first, second):
            message = await asyncio.wait_for(anext(client.messages.__aiter__()), 3)
            assert json.loads(message.payload) == update


async def test_wrong_key_and_topic_never_reach_printer(gateway):
    with pytest.raises(aiomqtt.MqttError):
        async with aiomqtt.Client(
            "127.0.0.1",
            port=port(gateway, 0),
            username="bblp",
            password="BAD_CODE",
            tls_context=tls(),
        ):
            pytest.fail("Wrong code accepted")
    async with aiomqtt.Client(
        "127.0.0.1",
        port=port(gateway, 0),
        username="bblp",
        password=gateway.test_code,
        tls_context=tls(),
    ) as client:
        grants = await client.subscribe("device/OTHER/report")
        assert grants[0].is_failure
    gateway.service().send_raw.assert_not_awaited()


async def test_code_hash_disable_and_restart_state(gateway):
    code = gateway.test_code
    assert code.encode() not in gateway.store.path.read_bytes()
    assert code not in json.dumps(gateway.status())
    assert await gateway.authenticate("bblp", code, "fixture")
    old_port = port(gateway, 0)
    await gateway.disable()
    assert not gateway.status()["enabled"]
    assert not await gateway.authenticate("bblp", code, "fixture")
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", old_port)
    resumed = NativeGateway(
        gateway.app, gateway.store, gateway.host, ports=(0, 0, 0), detect_port=0
    )
    assert resumed.config is None


async def test_native_code_can_be_retrieved_after_restart_and_rotation(gateway):
    original = gateway.test_code
    assert gateway.saved_code() == original
    assert original not in json.dumps(gateway.status())
    assert original.encode() not in gateway.store.path.read_bytes()
    await gateway.close()
    resumed = NativeGateway(
        gateway.app, gateway.store, gateway.host, ports=(0, 0, 0), detect_port=0
    )
    try:
        await resumed.start()
        assert resumed.saved_code() == original
        assert resumed.saved_code() == original
        replaced = (await resumed.enable(SERIAL))["access_code"]
        assert replaced != original and resumed.saved_code() == replaced
        assert not await resumed.authenticate("bblp", original, "fixture")
        assert await resumed.authenticate("bblp", replaced, "fixture")
        await resumed.disable()
        assert resumed.saved_code() is None
    finally:
        await resumed.close()


async def test_verified_reconnect_recovers_legacy_code_without_rotation(gateway):
    original = gateway.test_code
    with gateway.store.connect() as db:
        db.execute("DELETE FROM native_code")
    await gateway.close()
    resumed = NativeGateway(
        gateway.app, gateway.store, gateway.host, ports=(0, 0, 0), detect_port=0
    )
    try:
        await resumed.start()
        before = dict(resumed.config)
        assert resumed.saved_code() is None
        assert not await resumed.authenticate("bblp", "BAD_CODE", "fixture")
        assert resumed.saved_code() is None
        assert await resumed.authenticate("bblp", original, "fixture")
        assert resumed.saved_code() == original
        for key in ["hash", "salt", "printer_id"]:
            assert resumed.config[key] == before[key]
        assert original.encode() not in gateway.store.path.read_bytes()
    finally:
        await resumed.close()


async def test_native_ftps_roundtrip_reaches_printer_before_success(gateway, ftps_server):
    upstream_port, storage = ftps_server
    gateway.app.state.ftps_port = upstream_port

    def exchange():
        ftp = _ImplicitFTP_TLS(context=tls(), timeout=10)
        ftp.connect("127.0.0.1", port(gateway, 1))
        ftp.login("bblp", gateway.test_code)
        ftp.prot_p()
        data = b"test sliced payload for transfer only"
        result = ftp.storbinary("STOR /native-test.gcode.3mf", io.BytesIO(data))
        assert result.startswith("226")
        assert (storage / "native-test.gcode.3mf").read_bytes() == data
        received = []
        ftp.retrbinary("RETR /native-test.gcode.3mf", received.append)
        assert b"".join(received) == data
        ftp.delete("/native-test.gcode.3mf")
        ftp.quit()

    await asyncio.to_thread(exchange)


async def test_native_upload_failure_is_sanitized_and_fresh_session_can_retry(
    gateway,
    ftps_server,
    monkeypatch,
):
    upstream_port, storage = ftps_server
    gateway.app.state.ftps_port = upstream_port
    original = FtpsTransfer._connect
    connection_count = 0

    def connect(transfer):
        nonlocal connection_count
        backend = original(transfer)
        connection_count += 1
        if connection_count == 2:  # fresh upload connection, not login connection

            def fail(*_args, **_kwargs):
                raise ConnectionResetError(104, "synthetic-private-filename-and-code")

            backend.storbinary = fail
        return backend

    monkeypatch.setattr(FtpsTransfer, "_connect", connect)

    def upload(expect_failure):
        ftp = _ImplicitFTP_TLS(context=tls(), timeout=10)
        try:
            ftp.connect("127.0.0.1", port(gateway, 1))
            ftp.login("bblp", gateway.test_code)
            ftp.prot_p()
            if expect_failure:
                with pytest.raises(ftplib.error_temp, match="451.*uploading_to_printer") as error:
                    ftp.storbinary("STOR /retry-fixture.3mf", io.BytesIO(b"fixture"))
                assert "synthetic-private" not in str(error.value)
            else:
                assert ftp.storbinary("STOR /retry-fixture.3mf", io.BytesIO(b"fixture")).startswith(
                    "226"
                )
        finally:
            ftp.close()

    await asyncio.to_thread(upload, True)
    diagnostic = gateway.diagnostics["ftps"]["last_transfer_failure"]
    assert diagnostic["phase"] == "uploading_to_printer"
    assert diagnostic["bytes"] == 7
    assert diagnostic["exception_type"] == "ConnectionResetError"
    assert diagnostic["errno"] == 104
    assert "synthetic-private" not in json.dumps(diagnostic)
    await asyncio.to_thread(upload, False)
    assert (storage / "retry-fixture.3mf").read_bytes() == b"fixture"
    gateway.service().send_raw.assert_not_awaited()


@pytest.mark.parametrize("offset", [None, "3"])
async def test_upload_does_not_reuse_idle_login_session_and_preserves_directory(
    gateway,
    ftps_server,
    monkeypatch,
    offset,
):
    upstream_port, storage = ftps_server
    gateway.app.state.ftps_port = upstream_port
    (storage / "cache").mkdir(exist_ok=True)
    (storage / "cache" / "relative.3mf").write_bytes(b"ABC")
    original = FtpsTransfer._connect
    connections = []
    writes = []

    def connect(transfer):
        backend = original(transfer)
        connections.append(backend)
        stored = backend.storbinary
        number = len(connections)

        def store(*args, **kwargs):
            writes.append((number, kwargs.get("rest")))
            if number == 1:
                raise ConnectionResetError("idle login session is unusable")
            return stored(*args, **kwargs)

        backend.storbinary = store
        return backend

    monkeypatch.setattr(FtpsTransfer, "_connect", connect)

    def exchange():
        ftp = _ImplicitFTP_TLS(context=tls(), timeout=10)
        try:
            ftp.connect("127.0.0.1", port(gateway, 1))
            ftp.login("bblp", gateway.test_code)
            ftp.prot_p()
            ftp.cwd("cache")
            if offset is not None:
                ftp.sendcmd("REST " + offset)
            result = ftp.storbinary("STOR relative.3mf", io.BytesIO(b"complete fixture"))
            assert result.startswith("226")
        finally:
            ftp.close()

    await asyncio.to_thread(exchange)
    assert len(connections) == 2
    assert writes == [(2, offset)]  # exactly one write, on the fresh connection
    prefix = b"ABC" if offset else b""
    assert (storage / "cache" / "relative.3mf").read_bytes() == prefix + b"complete fixture"
    gateway.service().send_raw.assert_not_awaited()


async def test_failed_auth_rate_limited(gateway):
    for _ in range(5):
        assert not await gateway.authenticate("bblp", "BAD_CODE", "attacker")
    assert not await gateway.authenticate("bblp", gateway.test_code, "attacker")
    assert await gateway.authenticate("bblp", gateway.test_code, "different-peer")


async def test_rsa_only_native_tls_preserves_phone_identity(gateway):
    directory = gateway.store.directory
    phone_key, _, phone_pin = identity(directory)
    before = phone_key.read_bytes()
    native_cert = directory / "native-rsa/identity.crt"
    key = serialization.load_pem_private_key(
        (directory / "native-rsa/identity.key").read_bytes(), password=None
    )
    assert isinstance(key, rsa.RSAPrivateKey) and key.key_size >= 2048
    context = ssl.create_default_context(cafile=str(native_cert))
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers("ECDHE-RSA-AES128-GCM-SHA256")
    async with aiomqtt.Client(
        "127.0.0.1",
        port=port(gateway, 0),
        username="bblp",
        password=gateway.test_code,
        tls_context=context,
    ) as client:
        await client.subscribe(f"device/{gateway.serial}/report")
        message = await asyncio.wait_for(anext(client.messages.__aiter__()), 3)
        assert "print" in json.loads(message.payload)
        assert gateway.status()["connections"]["mqtt"]["phase"] == "authenticated"
    assert identity(directory)[2] == phone_pin and phone_key.read_bytes() == before


async def test_connection_diagnostics_do_not_expose_secrets(gateway):
    with pytest.raises(aiomqtt.MqttError):
        async with aiomqtt.Client(
            "127.0.0.1",
            port=port(gateway, 0),
            username="bblp",
            password="BAD_CODE",
            tls_context=tls(),
        ):
            pytest.fail("Wrong code accepted")
    diagnostics = gateway.status()["connections"]
    assert diagnostics["mqtt"]["auth_failures"] == 1
    value = json.dumps(diagnostics)
    assert all(secret not in value for secret in ["BAD_CODE", gateway.test_code, "127.0.0.1"])


async def test_disable_disconnects_active_clients(gateway):
    reader, writer = await asyncio.open_connection("127.0.0.1", port(gateway, 2), ssl=tls())
    writer.write(build_auth_packet("bblp", gateway.test_code))
    await writer.drain()
    await reader.readexactly(16 + len(JPEG))
    await asyncio.wait_for(gateway.disable(), 5)
    assert await asyncio.wait_for(reader.read(), 2) == b""
    writer.close()
    await writer.wait_closed()


async def test_discovery_is_private_and_contains_no_access_code(gateway, monkeypatch):
    messages = []

    async def capture(send, data, address):
        messages.append((data, address))
        return len(data)

    monkeypatch.setattr(asyncio, "to_thread", capture)
    await gateway.announce("127.0.0.1")
    assert [address[1] for _, address in messages] == [1990, 2021]
    for data, _ in messages:
        assert b"DevModel.bambu.com: C12" in data
        assert f"USN: {gateway.serial}\r\n".encode() in data
        assert b"DevName.bambu.com: Bridge P1S" in data
        assert gateway.service().serial.encode() not in data
        assert gateway.test_code.encode() not in data
        assert b"Location: 127.0.0.1" in data
    with pytest.raises(ValueError):
        await gateway.announce("8.8.8.8")
