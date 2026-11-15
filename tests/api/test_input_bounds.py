"""F25/F26/F12 at the HTTP layer: bad numbers are 422 envelopes, unknown printer is 404."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from bambu_bridge.api import advanced, control, errors
from bambu_bridge.api.auth import require_auth
from bambu_bridge.api.capabilities import control_capability_gate
from bambu_bridge.service.registry import PrinterNotFoundError


@pytest.fixture
def client(monkeypatch):
    """Routers with auth and the capability gate lifted, so the handlers' own checks run."""
    monkeypatch.setenv("BRIDGE_ENABLE_RAW_GCODE", "1")
    service = SimpleNamespace(
        send_raw=AsyncMock(),
        snapshot=lambda: {"_raw": {}},
        _state={},
        cert_status="ok",
    )
    app = FastAPI()
    errors.install(app)
    app.state.registry = SimpleNamespace(get=lambda _: service)
    app.dependency_overrides[require_auth] = lambda: None
    app.dependency_overrides[control_capability_gate] = lambda: None
    app.include_router(control.router)
    app.include_router(advanced.router)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, service


def _raw(c, path: str, body: str):
    return c.post(path, content=body, headers={"content-type": "application/json"})


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_move_non_finite_distance_is_422(client, token: str) -> None:
    c, service = client
    r = _raw(c, "/printers/p/move", '{"axis":"X","distance_mm":' + token + "}")
    assert r.status_code == 422, r.text
    service.send_raw.assert_not_called()


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_extrude_non_finite_distance_is_422(client, token: str) -> None:
    c, service = client
    r = _raw(c, "/printers/p/extrude", '{"distance_mm":' + token + ',"feedrate":300}')
    assert r.status_code == 422, r.text
    service.send_raw.assert_not_called()


@pytest.mark.parametrize("token", ["NaN", "Infinity"])
def test_nozzle_diameter_non_finite_is_422(client, token: str) -> None:
    c, _ = client
    r = _raw(
        c,
        "/printers/p/set_accessories/nozzle",
        '{"nozzle_type":"hardened_steel","nozzle_diameter":' + token + "}",
    )
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("key", ["category", "command"])
def test_raw_command_reserved_param_key_is_422(client, key: str) -> None:
    c, service = client
    r = c.post(
        "/printers/p/command",
        json={"category": "print", "command": "pause", "params": {key: "x"}},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "invalid_input"
    service.send_raw.assert_not_called()


@pytest.mark.parametrize(
    ("axis", "feed", "status"),
    [("Z", 601, 422), ("X", 3001, 422), ("y", 99999, 422), ("Z", 600, 200), ("X", 3000, 200)],
)
def test_relative_move_feed_ceiling_in_model(axis: str, feed: int, status: int) -> None:
    if status == 200:
        control.MoveBody(axis=axis, distance_mm=1, feed_mm_min=feed)
    else:
        with pytest.raises(ValueError, match="feed_mm_min"):
            control.MoveBody(axis=axis, distance_mm=1, feed_mm_min=feed)


@pytest.mark.parametrize("field", ["cur_temp", "tar_temp"])
@pytest.mark.parametrize("value", [301, -1, 10**9])
def test_ams_change_temperatures_bounded_422(client, field: str, value: int) -> None:
    c, service = client
    r = c.post("/printers/p/ams/change", json={"target_tray": 0, field: value})
    assert r.status_code == 422, r.text
    service.send_raw.assert_not_called()


def test_stop_hold_unknown_printer_is_404() -> None:
    def missing(serial: str):
        raise PrinterNotFoundError(serial)

    registry = SimpleNamespace(get=missing)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(control.stop_hold("nope", control.HoldLeaseBody(lease="a" * 32), registry))
    assert exc.value.status_code == 404
