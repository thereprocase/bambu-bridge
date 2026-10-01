"""Safety contract: rejected controls publish zero commands."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bambu_bridge.api import advanced, control
from bambu_bridge.api.auth import require_auth


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
    )
    app = FastAPI()
    app.state.registry = SimpleNamespace(get=lambda _: service)
    app.dependency_overrides[require_auth] = lambda: None
    app.include_router(control.router)
    app.include_router(advanced.router)
    with TestClient(app) as client:
        yield client, service, snapshot


@pytest.mark.parametrize(
    "path,body",
    [
        ("xcam", {"module_name": "spaghetti_detector", "enabled": True}),
        ("calibration", {"option": 4}),
        ("ams/drying", {}),
        ("set_accessories/nozzle", {"nozzle_type": "hardened_steel", "nozzle_diameter": 0.4}),
        ("home", {}),
        ("move", {"axis": "Z", "distance_mm": -10}),
        ("extrude", {"distance_mm": 10}),
        ("steppers/off", {}),
        ("ams/change", {"target_tray": 0}),
        ("filament/unload", {}),
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
    snapshot["_raw"]["ams"] = {"ams": [{"id": "1", "tray": [{"id": "3"}]}]}
    assert client.post("/printers/p/ams/rfid", json={"ams_id": 0, "slot_id": 0}).status_code == 409
    service.send_raw.assert_not_awaited()
    response = client.post("/printers/p/ams/rfid", json={"ams_id": 1, "slot_id": 3})
    assert response.status_code == 200, response.text
    service.send_raw.assert_awaited_once()
