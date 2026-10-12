"""Named print events derive from the printer's last known state (audit 2026-10-01).

Mirrors OrcaSlicer's MachineObject::parse_json: a report only updates the
fields it carries, so a partial frame never becomes the baseline, and the
state known before an MQTT drop is what the next report is compared with.
"""

from __future__ import annotations

from typing import Any

import pytest

from bambu_bridge.protocol.models import ReportMessage
from bambu_bridge.service.printer import PrinterService
from tests.conftest import ACCESS_CODE, SERIAL

RUNNING = {
    "print": {
        "command": "push_status",
        "msg": 0,
        "gcode_state": "RUNNING",
        "layer_num": 120,
        "subtask_name": "benchy",
        "gcode_file": "benchy.gcode.3mf",
        "ams": {"tray_now": "1"},
    }
}
IDLE_EMPTY = {
    "print": {
        "command": "push_status",
        "msg": 0,
        "gcode_state": "IDLE",
        "gcode_file": "",
        "subtask_name": "",
    }
}


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> PrinterService:
    svc = PrinterService(
        SERIAL, "127.0.0.1", ACCESS_CODE, friendly_name="P", model="P1S", mqtt_port=8883
    )

    async def _quiet(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(svc, "_tofu_compare", _quiet)
    monkeypatch.setattr(svc, "send_command", _quiet)
    return svc


async def _feed(svc: PrinterService, *frames: dict[str, Any]) -> list[str]:
    async with svc.bus.subscribe() as sub:
        for frame in frames:
            await svc._handle_report(ReportMessage.model_validate(frame))
        names = []
        while not sub._queue.empty():
            ev = sub._queue.get_nowait()
            if ev.type == "event":
                names.append(ev.name)
        return names


async def _reconnect(svc: PrinterService) -> None:
    await svc._handle_lost()
    await svc._handle_connected()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first",
    [
        {"print": {"nozzle_temper": 220}},
        {"info": {"command": "get_version", "module": []}},
    ],
)
async def test_partial_first_frame_is_not_the_baseline(
    service: PrinterService, first: dict[str, Any]
) -> None:
    names = await _feed(service, first, RUNNING)
    assert "print_started" not in names
    assert "print_progress" not in names
    assert service.snapshot()["job"]["started_at"] is None  # honest unknown, not now()
    assert service.print_view()["gcode_state"] == "RUNNING"


@pytest.mark.asyncio
async def test_print_ending_during_a_drop_is_reported(service: PrinterService) -> None:
    await _feed(service, RUNNING)
    await _reconnect(service)
    assert service.print_view()["gcode_state"] is None  # unknown until a report has it
    finish = {"print": {**RUNNING["print"], "gcode_state": "FINISH"}}
    assert "print_completed" in await _feed(service, {"print": {"nozzle_temper": 30}}, finish)


@pytest.mark.asyncio
async def test_print_starting_during_a_drop_is_reported(service: PrinterService) -> None:
    await _feed(service, IDLE_EMPTY)
    await _reconnect(service)
    assert "print_started" in await _feed(service, RUNNING)


@pytest.mark.asyncio
async def test_reconnect_into_same_state_is_silent(service: PrinterService) -> None:
    await _feed(service, RUNNING)
    await _reconnect(service)
    names = await _feed(service, RUNNING)
    assert not {"print_started", "print_progress", "print_completed"} & set(names)


@pytest.mark.asyncio
async def test_idle_pushalls_do_not_announce_interruption(service: PrinterService) -> None:
    assert "print_interrupted" not in await _feed(service, IDLE_EMPTY, IDLE_EMPTY, IDLE_EMPTY)
    await _reconnect(service)
    assert "print_interrupted" not in await _feed(service, IDLE_EMPTY)


@pytest.mark.asyncio
async def test_job_lost_during_a_drop_is_announced_once(service: PrinterService) -> None:
    await _feed(service, RUNNING)
    await _reconnect(service)
    names = await _feed(service, IDLE_EMPTY, IDLE_EMPTY)
    assert names.count("print_interrupted") == 1
    assert service.print_view()["lost"] is True
