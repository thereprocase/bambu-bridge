"""Safety contract: rejected controls publish zero commands."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bambu_bridge.api import advanced, control, skip_objects
from bambu_bridge.api.auth import require_auth
from bambu_bridge.service.events import EventBus
from bambu_bridge.service.remaining_estimates import RemainingEstimates


@pytest.fixture
def rig():
    raw = {
        "gcode_state": "RUNNING",
        "info": {"module": [{"name": "ota", "product_name": "P1S"}]},
        "home_flag": 0,
        "lights_report": [{"node": "chamber_light"}],
    }
    snapshot = {
        "_raw": raw,
        "session": {"connected": True, "last_telemetry_at": datetime.now(UTC).isoformat()},
    }
    service = SimpleNamespace(
        model=None,
        connected=True,
        cert_status="ok",
        snapshot=lambda: snapshot,
        send_raw=AsyncMock(),
        raw_bus=EventBus(),
        remaining_estimates=RemainingEstimates(),
    )
    app = FastAPI()
    app.state.registry = SimpleNamespace(get=lambda _: service)
    app.dependency_overrides[require_auth] = lambda: None
    app.include_router(control.router)
    app.include_router(advanced.router)
    app.include_router(skip_objects.router)
    with TestClient(app) as client:
        yield client, service, snapshot


@pytest.mark.parametrize(
    "path,body",
    [
        ("xcam", {"module_name": "spaghetti_detector", "enabled": True}),
        ("ams/drying", {}),
        ("set_accessories/nozzle", {"nozzle_type": "hardened_steel", "nozzle_diameter": 0.4}),
        ("steppers/off", {}),
        ("skip_objects", {"obj_list": [0]}),
        ("work_light", {"mode": "on"}),
        ("print_option", {"sound_enable": True}),
        ("print_option", {"air_print_detect": True}),
    ],
)
def test_unqualified_controls_are_withheld(rig, path, body):
    client, service, _ = rig
    assert client.post(f"/printers/p/{path}", json=body).status_code == 409
    service.send_raw.assert_not_awaited()


IDLE_ONLY = [
    ("home", {}),
    ("move", {"axis": "Z", "distance_mm": 10}),
    ("extrude", {"distance_mm": 10}),
    ("calibration", {"option": 4}),
    ("ams/change", {"target_tray": 0}),
    ("filament/unload", {}),
]


@pytest.mark.parametrize("path,body", IDLE_ONLY)
def test_motion_and_filament_controls_wait_for_an_idle_printer(rig, path, body):
    """Orca greys these out while a print runs; the rig's printer is RUNNING."""
    client, service, _ = rig
    service._state = {"gcode_state": "RUNNING"}
    assert client.post(f"/printers/p/{path}", json=body).status_code == 409
    service.send_raw.assert_not_awaited()


@pytest.mark.parametrize(
    "path,body",
    [
        ("home", {}),
        ("calibration", {"option": 4}),
        ("ams/change", {"target_tray": 0}),
        ("filament/unload", {}),
    ],
)
def test_verified_controls_publish_when_idle(rig, path, body):
    """Home, bed/vibration calibration, AMS change and unload match Orca's open builders."""
    client, service, snapshot = rig
    snapshot["_raw"]["gcode_state"] = "IDLE"
    snapshot["_raw"]["ams"] = {"ams": [{"id": "0", "tray": [{"id": "0"}]}]}
    service._state = {"gcode_state": "IDLE", "ams": {"tray_now": "255"}}
    service.reset_motion_state = lambda *_args, **_kwargs: None
    response = client.post(f"/printers/p/{path}", json=body)
    assert response.status_code == 200, response.text
    service.send_raw.assert_awaited_once()


@pytest.mark.parametrize("path", ["xcam", "set_accessories/nozzle"])
def test_features_the_p1s_lacks_say_so(rig, path):
    """Orca's C12.json: support_ai_monitoring and enable_set_nozzle_info are false."""
    client, _, snapshot = rig
    snapshot["_raw"]["gcode_state"] = "IDLE"
    body = (
        {"module_name": "spaghetti_detector", "enabled": True}
        if path == "xcam"
        else {"nozzle_type": "hardened_steel", "nozzle_diameter": 0.4}
    )
    response = client.post(f"/printers/p/{path}", json=body)
    assert response.status_code == 409
    assert response.json()["detail"]["message"] == "Not available on P1S"


@pytest.mark.parametrize("model", [None, "X1 Carbon", "P2S", "A1 mini", "H2D", "P1S lookalike"])
def test_other_model_flags_do_not_qualify_adapter(rig, model):
    client, service, snapshot = rig
    snapshot["_raw"]["info"]["module"][0]["product_name"] = model
    snapshot["_raw"]["home_flag"] = 1 << 18
    assert client.post("/printers/p/print_option", json={"sound_enable": True}).status_code == 409
    service.send_raw.assert_not_awaited()


def test_valid_command_reports_pending_confirmation(rig):
    client, service, _ = rig
    response = client.post("/printers/p/light", json={"on": True})
    assert response.status_code == 200, response.text
    assert response.json()["confirmation"] == "pending"
    service.send_raw.assert_awaited_once()


def test_heater_request_validated_atomically(rig):
    client, service, _ = rig
    assert (
        client.post("/printers/p/temperature", json={"nozzle": 250, "bed": 101}).status_code == 422
    )
    service.send_raw.assert_not_awaited()


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "garbage"])
@pytest.mark.parametrize("path", ["command", "gcode", "gcode/raw"])
def test_raw_aliases_require_explicit_enable(rig, monkeypatch, value, path):
    client, service, _ = rig
    monkeypatch.setenv("BRIDGE_ENABLE_RAW_GCODE", value)
    assert client.post(f"/printers/p/{path}", json={"line": "G28"}).status_code == 403
    service.send_raw.assert_not_awaited()


def test_stale_state_and_invalid_lifecycle_are_withheld(rig):
    client, service, snapshot = rig
    snapshot["_raw"]["gcode_state"] = "FINISH"
    assert client.post("/printers/p/print/resume").status_code == 409
    snapshot["session"]["last_telemetry_at"] = "2000-01-01T00:00:00Z"
    assert client.post("/printers/p/light", json={"on": True}).status_code == 409
    service.send_raw.assert_not_awaited()


def test_version_discovery_available_before_identity(rig):
    client, service, snapshot = rig
    snapshot["_raw"].pop("info")
    assert client.post("/printers/p/get_version").status_code == 200
    service.send_raw.assert_awaited_once()


def test_alphanumeric_credentials_preserve_case():
    from bambu_bridge.api.printers import RegisterPrinter

    body = RegisterPrinter(host="192.168.1.20", access_code="aB12Cd34")
    assert body.access_code == "aB12Cd34"


def test_sparse_ams_address_rejected_before_publication(rig):
    client, service, snapshot = rig
    snapshot["_raw"]["gcode_state"] = "IDLE"  # tags are read only while idle
    snapshot["_raw"]["ams"] = {"ams": [{"id": "1", "tray": [{"id": "3"}]}]}
    assert client.post("/printers/p/ams/rfid", json={"ams_id": 0, "slot_id": 0}).status_code == 409
    service.send_raw.assert_not_awaited()
    response = client.post("/printers/p/ams/rfid", json={"ams_id": 1, "slot_id": 3})
    assert response.status_code == 200, response.text
    service.send_raw.assert_awaited_once()
    assert response.json()["refresh_status"] == "requested"
    assert response.json()["remaining_estimate"]["refresh_confirmed"] is False
    assert 0 not in service.remaining_estimates.slots
    assert 7 in service.remaining_estimates.slots


def _with_ams(snapshot):
    snapshot["_raw"]["ams"] = {"ams": [{"id": "0", "tray": [{"id": "0"}, {"id": "1"}]}]}


def test_ams_resume_reaches_printer_without_unit_id(rig):
    client, service, snapshot = rig
    _with_ams(snapshot)
    response = client.post("/printers/p/ams/control", json={"action": "resume"})
    assert response.status_code == 200, response.text
    assert service.send_raw.await_args.args[0]["print"]["param"] == "resume"


@pytest.mark.parametrize("action", ["pause", "reset"])
def test_ams_actions_orca_never_sends_are_refused(rig, action):
    client, service, snapshot = rig
    _with_ams(snapshot)
    assert client.post("/printers/p/ams/control", json={"action": action}).status_code == 422
    service.send_raw.assert_not_awaited()


def test_ams_control_needs_a_reported_ams(rig):
    client, service, _ = rig
    assert client.post("/printers/p/ams/control", json={"action": "resume"}).status_code == 409
    service.send_raw.assert_not_awaited()


@pytest.mark.parametrize("path", ["ams/rfid", "ams/control", "ams/user_setting", "print_option"])
@pytest.mark.parametrize("body", [b"[1]", b"7", b"null", b"not json"])
def test_non_object_bodies_are_422_not_500(rig, path, body):
    client, service, snapshot = rig
    _with_ams(snapshot)
    response = client.post(
        f"/printers/p/{path}", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422, response.text
    service.send_raw.assert_not_awaited()


def _quiet(snapshot, seconds: float = 26) -> None:
    """An idle P1S between bursts: its last report is older than the 15 s window."""
    stamp = datetime.now(UTC).timestamp() - seconds
    snapshot["session"]["last_telemetry_at"] = datetime.fromtimestamp(stamp, UTC).isoformat()
    snapshot["_raw"]["gcode_state"] = "IDLE"
    snapshot["_raw"]["ams"] = {"ams": [{"id": "0", "tray": [{"id": "1"}]}]}


def test_a_quiet_idle_printer_is_asked_for_status_before_refusing(rig):
    from bambu_bridge.service.events import Event

    client, service, snapshot = rig
    _quiet(snapshot)

    async def reply():
        snapshot["session"]["last_telemetry_at"] = datetime.now(UTC).isoformat()
        service.raw_bus.publish(Event("snapshot", {}))
        return True

    service.request_full_report = AsyncMock(side_effect=reply)
    response = client.post("/printers/p/ams/rfid", json={"ams_id": 0, "slot_id": 1})
    assert response.status_code == 200, response.text
    service.request_full_report.assert_awaited_once()


def test_a_printer_that_never_answers_is_still_refused(rig, monkeypatch):
    from bambu_bridge.api import capabilities

    client, service, snapshot = rig
    _quiet(snapshot)
    service.request_full_report = AsyncMock(return_value=True)
    monkeypatch.setattr(capabilities, "FRESH_WAIT_S", 0.2)
    response = client.post("/printers/p/ams/rfid", json={"ams_id": 0, "slot_id": 1})
    assert response.status_code == 409
    assert response.json()["detail"]["message"] == "Fresh printer status required"
    service.send_raw.assert_not_awaited()
