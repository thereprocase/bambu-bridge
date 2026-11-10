"""Absolute targets use firmware coordinates without pretending G28 completed."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from bambu_bridge.api import control
from bambu_bridge.protocol import commands

pytest_plugins = ["tests.api.test_capability_gate"]


def ready(rig):
    client, service, snapshot = rig
    snapshot["_raw"].update(gcode_state="FINISH", home_flag=7)
    service._state = snapshot["_raw"]
    service.reset_motion_state = Mock()
    return client, service, snapshot


@pytest.mark.parametrize("target", [0, 83, 167, 250])
def test_maintenance_targets_publish_with_unknown_bridge_position(rig, target):
    client, service, _ = ready(rig)
    response = client.post("/printers/p/move/absolute-z", json={"position_mm": target})
    assert response.status_code == 200, response.text
    assert response.json()["confirmation"] == "pending"
    sent = service.send_raw.await_args.args[0]
    assert sent["print"]["param"] == f"G90\nG1 Z{target} F600\nM400\n"
    service.reset_motion_state.assert_called_once()


@pytest.mark.parametrize(
    "body",
    [
        {"position_mm": -1},
        {"position_mm": 250.1},
        {"position_mm": "NaN"},
        {"position_mm": "Infinity"},
        {"position_mm": 250, "feed_mm_min": 601},
        {"position_mm": 250, "feed_mm_min": 0},
        {"position_mm": 250, "axis": "X"},
        {"position_mm": "250\nG1 Z-200"},
    ],
)
def test_invalid_targets_never_publish(rig, body):
    client, service, _ = ready(rig)
    assert client.post("/printers/p/move/absolute-z", json=body).status_code == 422
    service.send_raw.assert_not_awaited()
    service.reset_motion_state.assert_not_called()


@pytest.mark.parametrize("flag", [None, 0, 3, "7", True])
def test_absolute_move_needs_reported_homed_z(rig, flag):
    client, service, snapshot = ready(rig)
    snapshot["_raw"]["home_flag"] = flag
    assert client.post("/printers/p/move/absolute-z", json={"position_mm": 250}).status_code == 409
    service.send_raw.assert_not_awaited()


@pytest.mark.parametrize("kind", ["busy", "paused", "stale", "offline", "wrong_model"])
def test_absolute_move_preserves_capability_guards(rig, kind):
    client, service, snapshot = ready(rig)
    if kind == "busy":
        snapshot["_raw"]["gcode_state"] = "RUNNING"
    elif kind == "paused":
        snapshot["_raw"]["gcode_state"] = "PAUSE"
    elif kind == "stale":
        snapshot["session"]["last_telemetry_at"] = (
            datetime.now(UTC) - timedelta(minutes=1)
        ).isoformat()
    elif kind == "offline":
        service.connected = False
        snapshot["session"]["connected"] = False
    else:
        snapshot["_raw"]["info"]["module"][0]["product_name"] = "A1"
    assert client.post("/printers/p/move/absolute-z", json={"position_mm": 250}).status_code == 409
    service.send_raw.assert_not_awaited()


@pytest.mark.asyncio
async def test_overlapping_motion_publications_are_rejected():
    from types import SimpleNamespace

    started, release = asyncio.Event(), asyncio.Event()

    async def publish(_):
        started.set()
        await release.wait()

    service = SimpleNamespace(
        connected=True,
        cert_status="ok",
        _state={"gcode_state": "FINISH"},
        snapshot=lambda: {
            "_raw": {"home_flag": 7, "gcode_state": "FINISH"},
            "session": {"connected": True, "last_telemetry_at": datetime.now(UTC).isoformat()},
        },
        reset_motion_state=Mock(),
        send_raw=publish,
    )
    registry = SimpleNamespace(get=lambda _: service)
    first = asyncio.create_task(
        control.move_bed_absolute("p", control.AbsoluteBedBody(position_mm=250), registry)
    )
    await started.wait()
    try:
        with pytest.raises(HTTPException) as error:
            await control.home("p", registry)
        assert error.value.status_code == 409
        assert error.value.detail["error"] == "motion_request_pending"
    finally:
        release.set()
        await first


@pytest.mark.parametrize("target", [float("nan"), float("inf"), -1, 251])
def test_wire_builder_rejects_unbounded_target(target):
    with pytest.raises(ValueError):
        commands.move_bed_absolute(target)
