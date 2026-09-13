from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from bambu_bridge.native_gateway import NativeGateway
from bambu_bridge.service.events import Event, EventBus


def fixture(state="FINISH", *, stale=True, reply="FINISH"):
    status = {"gcode_state": state}
    stamp = datetime.now(UTC) - timedelta(seconds=30 if stale else 0)
    session = {"last_telemetry_at": stamp.isoformat()}
    sent = []
    bus = EventBus()

    async def send(payload):
        sent.append(payload)
        if reply is not None:
            status["gcode_state"] = reply
            session["last_telemetry_at"] = datetime.now(UTC).isoformat()
            bus.publish(Event("snapshot", {"print": dict(status)}))

    service = SimpleNamespace(
        connected=True,
        raw_bus=bus,
        send_raw=send,
        native_snapshot=lambda: {"print": status},
        snapshot=lambda: {"session": session},
    )
    gateway = SimpleNamespace(service=lambda: service)
    gateway.require_idle = lambda: NativeGateway.require_idle(gateway)
    return gateway, sent


@pytest.mark.parametrize("state", ["IDLE", "FINISH", "FAILED"])
async def test_quiet_ready_printer_is_refreshed_without_stop(state):
    gateway, sent = fixture(state, reply=state)
    await NativeGateway.ensure_idle(gateway)
    assert sent == [{"pushing": {"command": "pushall", "version": 1, "push_target": 1}}]


async def test_fresh_finish_needs_no_extra_command():
    gateway, sent = fixture(stale=False)
    await NativeGateway.ensure_idle(gateway)
    assert sent == []


async def test_fresh_running_during_refresh_blocks_start():
    gateway, sent = fixture(reply="RUNNING")
    with pytest.raises(ValueError, match="BBSTART_NOT_IDLE"):
        await NativeGateway.ensure_idle(gateway)
    assert len(sent) == 1


async def test_missing_status_reply_has_explicit_reason():
    gateway, sent = fixture(reply=None)
    with pytest.raises(ValueError, match="BBSTART_STATUS_REFRESH_TIMEOUT"):
        await NativeGateway.ensure_idle(gateway, timeout=0.01)
    assert len(sent) == 1


async def test_busy_printer_is_not_overridden():
    gateway, sent = fixture("RUNNING", stale=False)
    with pytest.raises(ValueError, match="BBSTART_NOT_IDLE"):
        await NativeGateway.ensure_idle(gateway)
    assert sent == []


async def test_refresh_transport_failure_is_not_a_busy_error():
    gateway, _ = fixture()

    async def fail(_):
        raise ConnectionError("fixture disconnect")

    gateway.service().send_raw = fail
    with pytest.raises(ValueError, match="BBSTART_STATUS_REFRESH_FAILED"):
        await NativeGateway.ensure_idle(gateway)
