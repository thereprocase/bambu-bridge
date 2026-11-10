"""Absolute targets use firmware coordinates without pretending G28 completed."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from bambu_bridge.api import control
from bambu_bridge.protocol import commands
from bambu_bridge.service.maintenance_motion import MaintenanceMotion

pytest_plugins = ["tests.api.test_capability_gate"]


def ready(rig):
    client, service, snapshot = rig
    snapshot["_raw"].update(gcode_state="FINISH", home_flag=7)
    service._state = snapshot["_raw"]
    service.reset_motion_state = Mock()
    service.maintenance_motion = MaintenanceMotion()

    async def publish(envelope, targets):
        await service.send_raw(envelope)
        service.maintenance_motion.requested(targets)

    service.send_maintenance = AsyncMock(side_effect=publish)
    return client, service, snapshot


@pytest.mark.parametrize("target", [0, 83, 167, 250])
def test_maintenance_targets_publish_with_unknown_bridge_position(rig, target):
    client, service, _ = ready(rig)
    response = client.post("/printers/p/move/absolute-z", json={"position_mm": target})
    assert response.status_code == 200, response.text
    assert response.json()["confirmation"] == "pending"
    sent = service.send_raw.await_args.args[0]
    assert sent["print"]["param"] == commands.move_bed_absolute(target)["print"]["param"]
    assert response.json()["maintenance"]["last_requested"]["Z"] == target


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

    async def publish(*_):
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
        send_maintenance=publish,
        maintenance_motion=MaintenanceMotion(),
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


@pytest.mark.parametrize(
    "x,y",
    [
        (0, 256),
        (256, 256),
        (64, 192),
        (192, 192),
        (128, 128),
        (64, 64),
        (192, 64),
        (0, 0),
        (256, 0),
    ],
)
def test_nine_xy_presets_are_bounded_absolute_targets(rig, x, y):
    client, service, _ = ready(rig)
    response = client.post("/printers/p/move/absolute-xy", json={"x_mm": x, "y_mm": y})
    assert response.status_code == 200, response.text
    sent = service.send_raw.await_args.args[0]
    assert sent["print"]["param"] == commands.move_xy_absolute(x, y)["print"]["param"]
    assert "M211 X1 Y1 Z1" in sent["print"]["param"]
    assert response.json()["maintenance"]["last_requested"] == {"X": x, "Y": y, "Z": None}
    assert response.json()["maintenance"]["position_verified"] is False


@pytest.mark.parametrize(
    "dx,dy", [(-1, 1), (0, 1), (1, 1), (-1, 0), (1, 0), (-1, -1), (0, -1), (1, -1)]
)
def test_eight_direction_steps_become_absolute_targets(rig, dx, dy):
    client, service, _ = ready(rig)
    service.maintenance_motion.requested({"X": 128, "Y": 128})
    response = client.post(
        "/printers/p/move/maintenance-xy-step",
        json={"direction_x": dx, "direction_y": dy, "step_mm": 10},
    )
    assert response.status_code == 200, response.text
    assert (
        service.send_raw.await_args.args[0]["print"]["param"]
        == commands.move_xy_absolute(128 + dx * 10, 128 + dy * 10)["print"]["param"]
    )


@pytest.mark.parametrize("step", [1, 10, 50])
def test_z_steps_use_requested_reference_and_never_overrun(rig, step):
    client, service, _ = ready(rig)
    service.maintenance_motion.requested({"Z": 250})
    response = client.post(
        "/printers/p/move/maintenance-step", json={"axis": "Z", "direction": -1, "step_mm": step}
    )
    assert response.status_code == 200, response.text
    assert response.json()["maintenance"]["last_requested"]["Z"] == 250 - step
    service.send_raw.reset_mock()
    service.maintenance_motion.requested({"Z": 250})
    response = client.post(
        "/printers/p/move/maintenance-step", json={"axis": "Z", "direction": 1, "step_mm": step}
    )
    assert response.status_code == 409, response.text
    service.send_raw.assert_not_awaited()


@pytest.mark.parametrize(
    "path,body",
    [
        ("maintenance-step", {"axis": "Z", "direction": 1, "step_mm": 10}),
        ("maintenance-xy-step", {"direction_x": 1, "direction_y": 1, "step_mm": 10}),
    ],
)
def test_step_needs_explicit_absolute_reference(rig, path, body):
    client, service, _ = ready(rig)
    response = client.post("/printers/p/move/" + path, json=body)
    assert response.status_code == 409, response.text
    assert "Select an absolute" in response.text
    service.send_raw.assert_not_awaited()


@pytest.mark.parametrize("flag", [0, 1, 2, 4])
def test_xy_requires_both_homed_bits(rig, flag):
    client, service, snapshot = ready(rig)
    snapshot["_raw"]["home_flag"] = flag
    response = client.post("/printers/p/move/absolute-xy", json={"x_mm": 128, "y_mm": 128})
    assert response.status_code == 409, response.text
    service.send_raw.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {"x_mm": -1, "y_mm": 128},
        {"x_mm": 257, "y_mm": 128},
        {"x_mm": 128, "y_mm": 257},
        {"x_mm": "NaN", "y_mm": 128},
        {"x_mm": 128, "y_mm": "Infinity"},
        {"x_mm": 128, "y_mm": 128, "feed_mm_min": 3001},
    ],
)
def test_xy_rejects_unbounded_targets_and_feed(rig, body):
    client, service, _ = ready(rig)
    assert client.post("/printers/p/move/absolute-xy", json=body).status_code == 422
    service.send_raw.assert_not_awaited()


@pytest.mark.parametrize("state", ["RUNNING", "PREPARE", "PAUSE"])
@pytest.mark.parametrize(
    "path,body",
    [
        ("absolute-xy", {"x_mm": 128, "y_mm": 128}),
        ("maintenance-step", {"axis": "Z", "direction": -1, "step_mm": 10}),
        ("maintenance-xy-step", {"direction_x": 1, "direction_y": 1, "step_mm": 10}),
    ],
)
def test_all_maintenance_routes_need_idle(rig, state, path, body):
    client, service, snapshot = ready(rig)
    snapshot["_raw"]["gcode_state"] = state
    assert client.post("/printers/p/move/" + path, json=body).status_code == 409
    service.send_raw.assert_not_awaited()
