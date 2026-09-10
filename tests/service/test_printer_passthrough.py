"""Full-passthrough contract: nothing the P1S says is dropped.

Before this, ``_handle_report`` early-returned on ``report.print is None`` —
so ``info`` (firmware/module identity), ``system``, ``mc_print``, etc. were
silently discarded. A phone/web client needs the whole surface.
"""

from __future__ import annotations

from typing import Any

import pytest

from bambu_bridge.protocol.models import ReportMessage
from bambu_bridge.service.printer import PrinterService
from tests.conftest import ACCESS_CODE, SERIAL


def _service() -> PrinterService:
    return PrinterService(
        SERIAL, "127.0.0.1", ACCESS_CODE, friendly_name="P1S", mqtt_port=1
    )


class _RecordingMqtt:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(self, payload: dict[str, Any]) -> None:
        self.published.append(payload)


@pytest.mark.asyncio
async def test_print_stays_flat_and_other_categories_are_preserved() -> None:
    svc = _service()
    await svc._handle_report(
        ReportMessage.parse(
            {
                "print": {"gcode_state": "RUNNING", "mc_percent": 12},
                "info": {
                    "command": "get_version",
                    "module": [{"name": "ota", "sw_ver": "01.07.00.00"}],
                },
                "system": {"command": "ledctrl", "result": "success"},
            }
        )
    )
    # PR B: snapshot is now the translated §6 shape; raw state lives
    # under `_raw` (full passthrough — nothing dropped).
    raw = svc.snapshot()["_raw"]
    assert raw["gcode_state"] == "RUNNING"
    assert raw["mc_percent"] == 12
    assert raw["info"]["module"][0]["sw_ver"] == "01.07.00.00"
    assert raw["system"]["result"] == "success"


@pytest.mark.asyncio
async def test_non_print_only_report_is_not_dropped() -> None:
    svc = _service()
    await svc._handle_report(
        ReportMessage.parse({"info": {"command": "get_version", "module": []}})
    )
    assert "info" in svc.snapshot()["_raw"]


@pytest.mark.asyncio
async def test_get_version_requested_on_connect() -> None:
    svc = _service()
    rec = _RecordingMqtt()
    svc._mqtt = rec  # type: ignore[assignment]
    await svc._handle_connected()
    assert any(
        env.get("info", {}).get("command") == "get_version"
        for env in rec.published
    ), rec.published


@pytest.mark.asyncio
async def test_empty_report_is_ignored() -> None:
    svc = _service()
    await svc._handle_report(ReportMessage.parse({}))
    # PR B: an empty report leaves _raw empty; snapshot still produces
    # a coherent translated shape with nulls/defaults everywhere.
    snap = svc.snapshot()
    assert snap["_raw"] == {}
    assert snap["phase"] == "unknown"
