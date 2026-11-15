"""Status from before a link drop is never fresh on the new session."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from bambu_bridge.api import capabilities, control
from bambu_bridge.api.auth import require_auth
from bambu_bridge.protocol.models import ReportMessage
from tests.service.test_printer import _service


def _report(payload: dict) -> ReportMessage:
    return ReportMessage.model_validate(payload)


async def _idle_then_reconnected():
    """An idle homed printer whose link dropped and came back seconds later."""
    service = _service(1883)
    service._mqtt.publish = AsyncMock()  # type: ignore[method-assign]
    service._tofu_compare = AsyncMock()  # type: ignore[method-assign]
    await service._handle_connected()
    await service._handle_report(_report({"print": {"gcode_state": "IDLE", "home_flag": 7}}))
    assert capabilities.telemetry_fresh(service)
    assert service._maintenance_ready("XYZ")
    await service._handle_lost()
    await service._handle_connected()
    return service


async def test_reconnect_is_not_fresh_until_the_new_session_reports_gcode_state():
    service = await _idle_then_reconnected()
    # The last pre-drop report is under a second old; it still must not count.
    assert service.snapshot()["session"]["last_telemetry_at"] is not None
    assert not capabilities.telemetry_fresh(service)
    assert not service._maintenance_ready("XYZ")
    with pytest.raises(HTTPException) as refused:
        capabilities.fresh_state(service)
    assert refused.value.status_code == 409

    # The get_version reply says nothing about the print state.
    await service._handle_report(_report({"info": {"command": "get_version", "module": []}}))
    assert not capabilities.telemetry_fresh(service)

    await service._handle_report(_report({"print": {"gcode_state": "IDLE", "home_flag": 7}}))
    assert capabilities.telemetry_fresh(service)
    assert service._maintenance_ready("XYZ")
    assert capabilities.fresh_state(service) == "IDLE"


async def test_disconnect_makes_status_pending_until_the_next_session_reports():
    service = _service(1883)
    service._mqtt.publish = AsyncMock()  # type: ignore[method-assign]
    service._tofu_compare = AsyncMock()  # type: ignore[method-assign]
    await service._handle_connected()
    await service._handle_report(_report({"print": {"gcode_state": "IDLE", "home_flag": 7}}))
    assert not service.status_pending
    await service._handle_lost()
    assert service.status_pending
    await service._handle_connected()
    assert service.status_pending
    await service._handle_report(_report({"print": {"gcode_state": "RUNNING"}}))
    assert not service.status_pending


def test_absolute_xy_move_is_refused_on_pre_drop_status(monkeypatch):
    # Entry point: POST /printers/{id}/move/absolute-xy right after a reconnect.
    service = asyncio.run(_idle_then_reconnected())
    service._mqtt.publish.reset_mock()
    monkeypatch.setattr(capabilities, "FRESH_WAIT_S", 0.2)
    app = FastAPI()
    app.state.registry = SimpleNamespace(get=lambda _: service)
    app.dependency_overrides[require_auth] = lambda: None
    app.include_router(control.router)
    with TestClient(app) as client:
        response = client.post(
            f"/printers/{service.serial}/move/absolute-xy", json={"x_mm": 128, "y_mm": 128}
        )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["message"] == "Fresh printer status required"
    sent = [call.args[0] for call in service._mqtt.publish.await_args_list]
    assert all((e.get("print") or {}).get("command") != "gcode_line" for e in sent)
