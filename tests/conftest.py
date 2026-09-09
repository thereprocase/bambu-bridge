"""Shared test fixtures: a self-contained mock Bambu printer.

The mock printer is two real servers wired to the same TLS cert the printer's
self-signed cert stands in for:

* an in-process **amqtt** MQTT broker on :8883-equivalent (ephemeral port,
  implicit TLS) with a co-located :class:`MockPrinter` that answers ``pushall``
  with a canned ``push_status`` — exercising the real aiomqtt TLS path.
* an in-process **aioftp** server with *implicit* TLS, the same quirk the P1S
  FTPS server has.

Nothing here imports FastAPI; the protocol layer is tested in isolation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import ssl
import struct
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aioftp
import aiomqtt
import pytest
import pytest_asyncio
import trustme
from amqtt.broker import Broker
from fastapi import FastAPI

from bambu_bridge.config import Settings
from bambu_bridge.db.jobs import Database, PrinterRepo
from bambu_bridge.main import create_app
from bambu_bridge.slicedoc import (
    AmsFeed,
    Filament,
    PlateInfo,
    StaticMembers,
    read_member,
    synthesize,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

API_KEY = "test-bridge-key"


def build_app(
    db_path: object,
    *,
    api_key: str = API_KEY,
    viz_token: str | None = None,
    mqtt_port: int = 1,
    ftps_port: int = 990,
    camera_port: int = 6000,
    camera_linger_s: float = 10.0,
) -> FastAPI:
    """A fully wired app for API tests.

    ``mqtt_port=1`` => the registry never reaches a broker (CRUD tests don't
    need a live link); pass real broker/ftps ports for integration.

    ``viz_token`` sets ``BRIDGE_VIZ_TOKEN`` — the read-only viewer token.
    When ``None`` (default), the feature is off: only the master key works on
    the viz/snapshot routes.
    """
    settings = Settings(
        bridge_api_key=api_key,
        bridge_viz_token=viz_token,
        bridge_db_path=str(db_path),
        bridge_log_level="warning",
        bridge_log_format="console",
        bridge_allow_loopback_host=True,  # in-process broker binds 127.0.0.1
        bridge_camera_linger_s=camera_linger_s,
    )
    return create_app(
        settings,
        mqtt_port=mqtt_port,
        ftps_port=ftps_port,
        camera_port=camera_port,
    )


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def patch_discovery_ok(
    monkeypatch: pytest.MonkeyPatch,
    *,
    serial: str,
    fingerprint: str = "deadbeef" * 8,
) -> None:
    """Make ``POST /api/v1/printers`` succeed against the in-process MQTT
    test broker — TLS-probe + auth-probe are skipped with a fake CertProbe
    (CN=serial) and a successful AuthProbe. Tests that just need a
    registered-and-running printer call this once before posting.

    Also stubs :func:`bambu_bridge.protocol.tls.leaf_cert_fingerprint` to
    return the same ``fingerprint`` — used by PrinterService's
    TOFU-compare-on-connect (PR A.2). Without this stub the in-process
    broker's real fingerprint would not match the registered one and every
    GET / control call would return 403 ``printer_cert_changed``.
    """
    from bambu_bridge.protocol import discovery, tls

    async def _fake_cert(host: str, *, port: int = 8883, timeout: float = 0) -> Any:  # noqa: ARG001
        return discovery.CertProbe(
            serial=serial,
            raw_subject=f"CN={serial}",
            fingerprint_sha256=fingerprint,
        )

    async def _fake_probe(
        host: str, serial: str, access_code: str, **_kw: Any  # noqa: ARG001
    ) -> Any:
        return discovery.AuthProbe(ok=True)

    async def _fake_leaf(host: str, port: int, **_kw: Any) -> Any:  # noqa: ARG001
        return tls.LeafCert(
            der=b"\x00" * 16,  # opaque; only fingerprint matters to the caller
            fingerprint_sha256=fingerprint,
            common_name=serial,
            subject_rfc4514=f"CN={serial}",
        )

    monkeypatch.setattr(discovery, "extract_serial_from_cert", _fake_cert)
    monkeypatch.setattr(discovery, "probe_mqtt_auth", _fake_probe)
    monkeypatch.setattr(tls, "leaf_cert_fingerprint", _fake_leaf)
    # Also patch the binding imported by PrinterService — module re-import
    # at runtime defeats setattr otherwise.
    import bambu_bridge.service.printer as service_printer

    monkeypatch.setattr(
        service_printer.tls_probe, "leaf_cert_fingerprint", _fake_leaf
    )


def _insecure_client_ctx() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


SERIAL = "00M00A000000000"
ACCESS_CODE = "12345678"

# A representative push_status. Mixes fields we model with fields we don't,
# so tests can prove extra="allow" preserves the unmodelled ones.
SAMPLE_PUSH_STATUS: dict[str, Any] = {
    "print": {
        "command": "push_status",
        "sequence_id": "0",
        "msg": 0,
        "gcode_state": "RUNNING",
        "gcode_file": "Metadata/plate_1.gcode",
        "subtask_name": "benchy",
        "mc_percent": 42,
        "mc_remaining_time": 73,
        "layer_num": 120,
        "total_layer_num": 285,
        "nozzle_temper": 219.8,
        "nozzle_target_temper": 220.0,
        "bed_temper": 59.9,
        "bed_target_temper": 60.0,
        "chamber_temper": 31.0,
        "cooling_fan_speed": "15",
        "big_fan1_speed": "0",
        "big_fan2_speed": "10",
        "heatbreak_fan_speed": "12",
        "print_error": 0,
        "mc_print_error_code": "0",
        "ams": {
            "ams": [
                {
                    "id": "0",
                    "humidity": "4",
                    "temp": "28.0",
                    "tray": [
                        {"id": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF", "remain": 80},
                        {"id": "1", "tray_type": "PETG", "tray_color": "000000FF", "remain": 50},
                    ],
                }
            ],
            "tray_now": "0",
        },
        # Unmodelled firmware fields — must survive round-trip via model_extra.
        "wifi_signal": "-54dBm",
        "spd_lvl": 2,
        "lifecycle": "product",
        "vt_tray": {"id": "254", "tray_type": "ABS"},
    }
}


class MockPrinter:
    """Stands in for the P1S MQTT side: answers pushall, can push reports."""

    def __init__(
        self,
        host: str,
        port: int,
        report: dict[str, Any],
        *,
        simulate_print: bool = False,
        stall: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._report = report
        # When set, react to print.project_file with RUNNING then (briefly
        # later) FINISH, and to print.stop with IDLE — enough to drive the
        # job state machine end to end. ``stall`` = go RUNNING but never make
        # progress and never FINISH (the §6.3 "printed air" shape) so the
        # FED_NO_PROGRESS watchdog has to fire.
        self._simulate = simulate_print
        self._stall = stall
        self.report_topic = f"device/{SERIAL}/report"
        self.request_topic = f"device/{SERIAL}/request"
        self.requests: list[dict[str, Any]] = []
        self._ready = asyncio.Event()
        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task[None] | None = None
        self._side_tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        await asyncio.wait_for(self._ready.wait(), timeout=10)

    async def stop(self) -> None:
        for t in self._side_tasks:
            t.cancel()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        async with aiomqtt.Client(
            hostname=self._host,
            port=self._port,
            username="bblp",
            password=ACCESS_CODE,
            identifier="mock-printer",
            tls_context=_insecure_client_ctx(),
            keepalive=30,
        ) as client:
            self._client = client
            await client.subscribe(self.request_topic, qos=1)
            self._ready.set()
            async for message in client.messages:
                payload = json.loads(message.payload)
                self.requests.append(payload)
                await self._react(payload)

    async def _emit(self, print_fields: dict[str, Any]) -> None:
        assert self._client is not None
        await self._client.publish(
            self.report_topic, json.dumps({"print": print_fields}), qos=0
        )

    async def _react(self, payload: dict[str, Any]) -> None:
        if payload.get("pushing", {}).get("command") == "pushall":
            assert self._client is not None
            await self._client.publish(
                self.report_topic, json.dumps(self._report), qos=0
            )
            return
        if not self._simulate:
            return
        cmd = payload.get("print", {}).get("command")
        if cmd == "project_file":
            await self._emit(
                {"gcode_state": "RUNNING", "mc_percent": 0, "subtask_name": "job"}
            )
            if not self._stall:
                self._side_tasks.append(
                    asyncio.create_task(self._finish_soon())
                )
        elif cmd == "stop":
            await self._emit({"gcode_state": "IDLE", "mc_percent": 0})

    async def _finish_soon(self) -> None:
        await asyncio.sleep(0.3)
        await self._emit({"gcode_state": "FINISH", "mc_percent": 100})

    async def push_report(self, payload: dict[str, Any]) -> None:
        await self._ready.wait()
        assert self._client is not None
        await self._client.publish(self.report_topic, json.dumps(payload), qos=0)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def tls_cert(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    """A throwaway self-signed cert/key (the printer's cert is self-signed)."""
    d = tmp_path_factory.mktemp("tls")
    ca = trustme.CA()
    cert = ca.issue_cert("127.0.0.1", "localhost")
    cert_path = d / "server.pem"
    key_path = d / "server.key"
    cert.cert_chain_pems[0].write_to_path(str(cert_path))
    cert.private_key_pem.write_to_path(str(key_path))
    return str(cert_path), str(key_path)


@pytest_asyncio.fixture
async def mqtt_broker(tls_cert: tuple[str, str]) -> AsyncIterator[int]:
    """In-process TLS MQTT broker. Yields its port."""
    cert_path, key_path = tls_cert
    port = _free_port()
    broker = Broker(
        {
            "listeners": {
                "default": {
                    "type": "tcp",
                    "bind": f"127.0.0.1:{port}",
                    "ssl": True,
                    "certfile": cert_path,
                    "keyfile": key_path,
                }
            },
            # Only the anonymous-auth plugin: no sys plugin (deprecated
            # top-level sys_interval), no logging plugins — keeps tests quiet.
            "plugins": {
                "amqtt.plugins.authentication.AnonymousAuthPlugin": {
                    "allow_anonymous": True
                }
            },
        }
    )
    await broker.start()
    try:
        yield port
    finally:
        # amqtt cancels its broadcast loop on shutdown; if a QoS1 delivery is
        # still awaiting a PUBACK it leaks a CancelledError out of shutdown().
        # That's an amqtt teardown artifact, not a bridge concern — absorb it.
        await asyncio.sleep(0.05)  # let in-flight QoS1 flows settle
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(broker.shutdown(), timeout=10)


IDLE_PUSH_STATUS: dict[str, Any] = {
    "print": {
        "command": "push_status",
        "sequence_id": "0",
        "gcode_state": "IDLE",
        "mc_percent": 0,
        "nozzle_temper": 25.0,
        "bed_temper": 24.0,
    }
}


@pytest_asyncio.fixture
async def mock_printer(mqtt_broker: int) -> AsyncIterator[MockPrinter]:
    """A running MockPrinter on the broker (RUNNING seed, no simulation)."""
    printer = MockPrinter("127.0.0.1", mqtt_broker, SAMPLE_PUSH_STATUS)
    await printer.start()
    try:
        yield printer
    finally:
        await printer.stop()


@pytest_asyncio.fixture
async def printing_mock(mqtt_broker: int) -> AsyncIterator[MockPrinter]:
    """Idle-seeded MockPrinter that simulates a print on project_file."""
    printer = MockPrinter(
        "127.0.0.1", mqtt_broker, IDLE_PUSH_STATUS, simulate_print=True
    )
    await printer.start()
    try:
        yield printer
    finally:
        await printer.stop()


@pytest_asyncio.fixture
async def stalling_mock(mqtt_broker: int) -> AsyncIterator[MockPrinter]:
    """RUNNING but never progresses — exercises the FED_NO_PROGRESS watchdog."""
    printer = MockPrinter(
        "127.0.0.1",
        mqtt_broker,
        IDLE_PUSH_STATUS,
        simulate_print=True,
        stall=True,
    )
    await printer.start()
    try:
        yield printer
    finally:
        await printer.stop()


@pytest.fixture(scope="session")
def valid_gcode_3mf() -> bytes:
    """A *consistent* single-tray ``.gcode.3mf`` synthesized from the real
    probe's static members + gcode (gcode loads AMS tray 1 → bind tray 1).

    This is the antidote to the old "any bytes pass" test: the job-flow tests
    now upload something `slicedoc.validate` actually accepts.
    """
    probe = (
        Path(__file__).resolve().parents[1]
        / "probes"
        / "3DBenchy_PETG_slot2.gcode.3mf"
    )
    if not probe.exists():
        pytest.skip("probe .gcode.3mf fixture not present")
    raw = probe.read_bytes()
    feed = AmsFeed.single(
        Filament("GFG96", "PETG", "#161616", 3.71, 11.33), tray=1
    )
    plate = PlateInfo(
        printer_model_id="C12",
        total_layers=200,
        prediction_s=3382,
        weight_g=11.33,
        first_layer_time_s=470.344147,
        object_id=83,
        object_name="3DBenchy.drc",
    )
    # The donor gcode is internally inconsistent (M620 S1A vs M621 S0A) —
    # normalize_ams rewrites every selector to the bound tray so the
    # container is genuinely coherent (the 2026-05-19 fix).
    return synthesize(
        gcode=read_member(raw, "Metadata/plate_1.gcode"),
        feed=feed,
        plate=plate,
        static=StaticMembers.from_zip(raw),
        normalize_ams=True,
    )


class FakeCamera:
    """A P1S-camera stand-in: TLS TCP, 80-byte auth, then framed JPEGs.

    Tracks concurrent connections so tests can prove the bridge multiplexes
    one upstream to many viewers (spec 5.3).
    """

    def __init__(self, ssl_ctx: ssl.SSLContext) -> None:
        self._ssl = ssl_ctx
        self.active = 0
        self.max_active = 0
        self.total = 0
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._conns: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, ssl=self._ssl
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for task in self._conns:
            task.cancel()
        for task in list(self._conns):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._conns.add(asyncio.current_task())  # type: ignore[arg-type]
        self.active += 1
        self.total += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await reader.readexactly(80)  # consume auth; content unchecked
            i = 0
            while True:
                jpeg = b"\xff\xd8" + f"frame-{i}".encode() + b"\xff\xd9"
                writer.write(struct.pack("<I", len(jpeg)) + b"\x00" * 12 + jpeg)
                await writer.drain()
                i += 1
                await asyncio.sleep(0.03)
        except (
            asyncio.IncompleteReadError,
            ConnectionResetError,
            asyncio.CancelledError,
            BrokenPipeError,
        ):
            pass
        finally:
            self.active -= 1
            self._conns.discard(asyncio.current_task())  # type: ignore[arg-type]
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)


@pytest_asyncio.fixture
async def fake_camera(tls_cert: tuple[str, str]) -> AsyncIterator[FakeCamera]:
    cert_path, key_path = tls_cert
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    cam = FakeCamera(ctx)
    await cam.start()
    try:
        yield cam
    finally:
        await cam.stop()


@pytest_asyncio.fixture
async def ftps_server(
    tls_cert: tuple[str, str], tmp_path: Path
) -> AsyncIterator[tuple[int, Path]]:
    """Implicit-TLS aioftp server (P1S quirk). Yields (port, storage_root)."""
    cert_path, key_path = tls_cert
    storage = tmp_path / "ftproot"
    (storage / "model").mkdir(parents=True)
    (storage / "cache").mkdir(parents=True)

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_path, key_path)

    server = aioftp.Server(
        [
            aioftp.User(
                "bblp",
                ACCESS_CODE,
                base_path=storage,
                permissions=[aioftp.Permission("/", readable=True, writable=True)],
            )
        ],
        ssl=server_ctx,
    )
    await server.start("127.0.0.1", 0)
    try:
        yield server.server_port, storage
    finally:
        await server.close()


@pytest_asyncio.fixture
async def database() -> AsyncIterator[Database]:
    """A connected in-memory SQLite database with the schema applied."""
    db = Database(":memory:")
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def printer_repo(database: Database) -> PrinterRepo:
    return PrinterRepo(database)
