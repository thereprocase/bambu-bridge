"""M1 acceptance (part 2): MQTT round-trip publish/subscribe vs mock printer."""

from __future__ import annotations

import asyncio
import ssl

import pytest

from bambu_bridge.protocol import mqtt as _mqtt_mod
from bambu_bridge.protocol.models import GcodeState, ReportMessage, build_command
from bambu_bridge.protocol.mqtt import MqttClient
from bambu_bridge.protocol.tls import insecure_tls_context
from tests.conftest import ACCESS_CODE, SERIAL, MockPrinter


def test_tls_context_is_pinned_to_tls_1_2() -> None:
    """Gotcha #8 regression: the P1S broker is TLS-1.2-only and answers a
    TLS-1.3 ClientHello with handshake_failure (the ~11 s connect hang). The
    context must cap *and* floor at TLS 1.2 so the first ClientHello is one
    the printer accepts — the in-process amqtt broker accepts 1.3, so only a
    direct context assertion catches a regression here."""
    ctx = insecure_tls_context()
    assert ctx.minimum_version is ssl.TLSVersion.TLSv1_2
    assert ctx.maximum_version is ssl.TLSVersion.TLSv1_2
    assert ctx.verify_mode is ssl.CERT_NONE
    assert ctx.check_hostname is False


async def _wait(predicate, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_roundtrip_pushall_seeds_state(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    reports: list[ReportMessage] = []
    connected = asyncio.Event()

    client = MqttClient(
        "127.0.0.1",
        SERIAL,
        ACCESS_CODE,
        on_report=lambda r: _collect(reports, r),
        on_connected=lambda: _set(connected),
        port=mqtt_broker,
    )
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(connected.wait(), timeout=10)
        # subscribe-before-publish + pushall => mock answers with push_status
        await _wait(lambda: len(reports) >= 1)
        report = reports[0]
        assert report.print is not None
        assert report.print.gcode_state is GcodeState.RUNNING
        assert report.print.mc_percent == 42
        # The client published pushall; the mock recorded it.
        await _wait(
            lambda: any(
                r.get("pushing", {}).get("command") == "pushall"
                for r in mock_printer.requests
            )
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_publish_command_reaches_printer(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    client = MqttClient("127.0.0.1", SERIAL, ACCESS_CODE, port=mqtt_broker)
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(_until_connected(client), timeout=10)
        await client.publish(build_command("print", "pause"))
        await _wait(
            lambda: any(
                "print" in r and r["print"].get("command") == "pause"
                for r in mock_printer.requests
            )
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_publish_without_connection_raises() -> None:
    client = MqttClient("127.0.0.1", SERIAL, ACCESS_CODE, port=1)  # nothing there
    with pytest.raises(ConnectionError):
        await client.publish({"print": {"command": "pause"}}, timeout=0.2)


async def _collect(sink: list[ReportMessage], report: ReportMessage) -> None:
    sink.append(report)


async def _set(event: asyncio.Event) -> None:
    event.set()


async def _until_connected(client: MqttClient) -> None:
    while not client.is_connected:
        await asyncio.sleep(0.02)


class _RecordingClient:
    """Stand-in for aiomqtt.Client that records the QoS of every publish.

    The in-process amqtt broker PUBACKs, so a QoS-1 publish round-trips fine
    against it — the real P1S broker never PUBACKs and a QoS-1 publish hangs
    the whole session (gotcha #9, verified on hardware 2026-05-19). The
    round-trip tests therefore cannot catch a QoS regression; this can.
    """

    publishes: list[int] = []

    def __init__(self, **_: object) -> None:
        type(self).publishes = []

    async def __aenter__(self) -> _RecordingClient:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def subscribe(self, _topic: str, qos: int = 0) -> None:
        return None

    async def publish(self, _topic: str, _payload: object, qos: int = 0) -> None:
        type(self).publishes.append(qos)

    @property
    async def messages(self):  # type: ignore[no-untyped-def]
        await asyncio.Event().wait()  # block until the task is cancelled
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_all_publishes_use_qos0(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gotcha #9 regression: pushall *and* command publishes must be QoS 0.
    QoS 1 hangs against the real broker (no PUBACK) and kills the session
    before it ever connects."""
    monkeypatch.setattr(_mqtt_mod.aiomqtt, "Client", _RecordingClient)
    connected = asyncio.Event()
    client = MqttClient(
        "127.0.0.1", SERIAL, ACCESS_CODE, on_connected=lambda: _set(connected)
    )
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(connected.wait(), timeout=5)
        await client.publish(build_command("print", "pause"))
        await _wait(lambda: len(_RecordingClient.publishes) >= 2)
        assert _RecordingClient.publishes  # pushall seed + the command
        assert all(q == 0 for q in _RecordingClient.publishes), (
            f"QoS-1 publish would hang the real P1S: {_RecordingClient.publishes}"
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
