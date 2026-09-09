"""Control endpoints — the write half of the passthrough.

A connected printer is driven through the HTTP API and the envelope is
asserted to have actually reached the (mock) printer. Plus the failure
mapping: unknown -> 404, offline -> 409, bad params -> 422.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    ACCESS_CODE,
    API_KEY,
    SERIAL,
    MockPrinter,
    build_app,
    patch_discovery_ok,
)

_AUTH = {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture(autouse=True)
def _patched_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)


def _register(c: TestClient) -> None:
    assert (
        c.post(
            "/api/v1/printers",
            headers=_AUTH,
            json={
                "host": "127.0.0.1",
                "access_code": ACCESS_CODE,
                "friendly_name": "Ctl P1S",
            },
        ).status_code
        == 201
    )


def _wait_connected(c: TestClient, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get(f"/api/v1/printers/{SERIAL}", headers=_AUTH)
        # PR B: `connected` moved under the translated `session` block.
        if r.status_code == 200 and r.json().get("session", {}).get("connected"):
            return
        time.sleep(0.05)
    raise AssertionError("printer never connected")


def _wait_request(
    mock: MockPrinter, match: Any, timeout: float = 5.0
) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for req in list(mock.requests):
            if match(req):
                return req
        time.sleep(0.02)
    raise AssertionError(f"command never reached printer; saw {mock.requests}")


@pytest.mark.asyncio
async def test_typed_and_raw_commands_reach_the_printer(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    app = build_app(tmp_path / "ctl.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)

            # raw passthrough
            assert (
                c.post(
                    f"/api/v1/printers/{SERIAL}/command",
                    headers=_AUTH,
                    json={"category": "print", "command": "pause"},
                ).status_code
                == 200
            )
            _wait_request(
                mock_printer,
                lambda r: r.get("print", {}).get("command") == "pause",
            )

            # typed: stop
            c.post(f"/api/v1/printers/{SERIAL}/print/stop", headers=_AUTH)
            _wait_request(
                mock_printer,
                lambda r: r.get("print", {}).get("command") == "stop",
            )

            # typed: chamber light (the canonical safe command)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/light",
                headers=_AUTH,
                json={"on": True},
            )
            assert r.status_code == 200, r.text
            led = _wait_request(
                mock_printer,
                lambda r: r.get("system", {}).get("command") == "ledctrl",
            )
            assert led["system"]["led_mode"] == "on"
            assert led["system"]["led_node"] == "chamber_light"

            # typed: dual temperature -> two gcode_line envelopes
            r = c.post(
                f"/api/v1/printers/{SERIAL}/temperature",
                headers=_AUTH,
                json={"nozzle": 250, "bed": 70},
            )
            assert r.status_code == 200, r.text
            assert len(r.json()["sent"]) == 2
            _wait_request(
                mock_printer,
                lambda r: r.get("print", {}).get("param") == "M104 S250\n",
            )
            _wait_request(
                mock_printer,
                lambda r: r.get("print", {}).get("param") == "M140 S70\n",
            )

            # typed: speed + gcode
            c.post(
                f"/api/v1/printers/{SERIAL}/speed",
                headers=_AUTH,
                json={"level": 3},
            )
            _wait_request(
                mock_printer,
                lambda r: r.get("print", {}).get("command") == "print_speed"
                and r["print"].get("param") == "3",
            )

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_control_failure_mapping(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    app = build_app(tmp_path / "ctlfail.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            # unknown printer -> 404
            assert (
                c.post(
                    "/api/v1/printers/nope/print/stop", headers=_AUTH
                ).status_code
                == 404
            )
            _register(c)
            _wait_connected(c)
            # invalid print action -> 422
            assert (
                c.post(
                    f"/api/v1/printers/{SERIAL}/print/fly", headers=_AUTH
                ).status_code
                == 422
            )
            # bad builder arg (fan part) -> 422
            assert (
                c.post(
                    f"/api/v1/printers/{SERIAL}/fan",
                    headers=_AUTH,
                    json={"part": "rocket", "percent": 50},
                ).status_code
                == 422
            )
            # pydantic bound (speed 1..4) -> 422
            assert (
                c.post(
                    f"/api/v1/printers/{SERIAL}/speed",
                    headers=_AUTH,
                    json={"level": 99},
                ).status_code
                == 422
            )

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_offline_printer_is_409(tmp_path: Path) -> None:
    # mqtt_port=1 => the registry never reaches a broker; registered but offline.
    app = build_app(tmp_path / "ctloff.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/light",
                headers=_AUTH,
                json={"on": False},
            )
            assert r.status_code == 409, r.text

    await asyncio.to_thread(run)


# ---------------------------------------------------------------------------
# Wave-1: new endpoints — work_light, ipcam/record, ipcam/timelapse,
# get_version; and the nozzle-clamp gate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_work_light_reaches_printer(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    app = build_app(tmp_path / "wl.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)

            # on — timing fields must be 0 per control matrix §4 work_light payload
            r = c.post(
                f"/api/v1/printers/{SERIAL}/work_light",
                headers=_AUTH,
                json={"mode": "on"},
            )
            assert r.status_code == 200, r.text
            req = _wait_request(
                mock_printer,
                lambda r: r.get("system", {}).get("command") == "ledctrl"
                and r["system"].get("led_node") == "work_light"
                and r["system"].get("led_mode") == "on",
            )
            assert req["system"]["led_on_time"] == 0
            assert req["system"]["led_off_time"] == 0

            # off
            r = c.post(
                f"/api/v1/printers/{SERIAL}/work_light",
                headers=_AUTH,
                json={"mode": "off"},
            )
            assert r.status_code == 200, r.text
            _wait_request(
                mock_printer,
                lambda r: r.get("system", {}).get("led_node") == "work_light"
                and r["system"].get("led_mode") == "off",
            )

            # flashing mode
            r = c.post(
                f"/api/v1/printers/{SERIAL}/work_light",
                headers=_AUTH,
                json={"mode": "flashing", "loop_times": 2, "interval_time": 300},
            )
            assert r.status_code == 200, r.text
            req = _wait_request(
                mock_printer,
                lambda r: r.get("system", {}).get("led_node") == "work_light"
                and r["system"].get("led_mode") == "flashing",
            )
            assert req["system"]["loop_times"] == 2
            assert req["system"]["interval_time"] == 300

            # invalid mode → 422
            r = c.post(
                f"/api/v1/printers/{SERIAL}/work_light",
                headers=_AUTH,
                json={"mode": "strobe"},
            )
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_ipcam_record_reaches_printer(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    app = build_app(tmp_path / "ipcam.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/ipcam/record",
                headers=_AUTH,
                json={"enabled": True},
            )
            assert r.status_code == 200, r.text
            req = _wait_request(
                mock_printer,
                lambda r: r.get("camera", {}).get("command") == "ipcam_record_set",
            )
            assert req["camera"]["control"] == "enable"

            r = c.post(
                f"/api/v1/printers/{SERIAL}/ipcam/record",
                headers=_AUTH,
                json={"enabled": False},
            )
            assert r.status_code == 200, r.text
            _wait_request(
                mock_printer,
                lambda r: r.get("camera", {}).get("command") == "ipcam_record_set"
                and r["camera"]["control"] == "disable",
            )

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_ipcam_timelapse_reaches_printer(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    app = build_app(tmp_path / "tlapse.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/ipcam/timelapse",
                headers=_AUTH,
                json={"enabled": False},
            )
            assert r.status_code == 200, r.text
            req = _wait_request(
                mock_printer,
                lambda r: r.get("camera", {}).get("command") == "ipcam_timelapse",
            )
            assert req["camera"]["control"] == "disable"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_get_version_reaches_printer(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    app = build_app(tmp_path / "ver.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/get_version",
                headers=_AUTH,
            )
            assert r.status_code == 200, r.text
            _wait_request(
                mock_printer,
                lambda r: r.get("info", {}).get("command") == "get_version",
            )

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_nozzle_clamp_default_stainless(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Stainless default: 280 accepted, 281 rejected with 422."""
    app = build_app(tmp_path / "nozzle_std.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)

            # 280 accepted
            r = c.post(
                f"/api/v1/printers/{SERIAL}/temperature",
                headers=_AUTH,
                json={"nozzle": 280},
            )
            assert r.status_code == 200, r.text
            _wait_request(
                mock_printer,
                lambda r: r.get("print", {}).get("param") == "M104 S280\n",
            )

            # 281 rejected
            r = c.post(
                f"/api/v1/printers/{SERIAL}/temperature",
                headers=_AUTH,
                json={"nozzle": 281},
            )
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_nozzle_clamp_hardened_steel(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Hardened-steel nozzle: 300 accepted, 301 always rejected."""
    app = build_app(tmp_path / "nozzle_hd.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)

            # Manually set nozzle_type on the service to hardened_steel.
            # Wave-1 provides the DB column; in tests we set it directly on
            # the in-memory service to avoid needing a separate API endpoint.
            registry = app.state.registry  # type: ignore[attr-defined]
            svc = registry.get(SERIAL)
            svc.nozzle_type = "hardened_steel"

            # 300 accepted
            r = c.post(
                f"/api/v1/printers/{SERIAL}/temperature",
                headers=_AUTH,
                json={"nozzle": 300},
            )
            assert r.status_code == 200, r.text
            _wait_request(
                mock_printer,
                lambda r: r.get("print", {}).get("param") == "M104 S300\n",
            )

            # 301 rejected even for hardened
            r = c.post(
                f"/api/v1/printers/{SERIAL}/temperature",
                headers=_AUTH,
                json={"nozzle": 301},
            )
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_gcode_line_4kb_cap(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Lines over 4 KB are rejected at the API layer with 422."""
    app = build_app(tmp_path / "gcap.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)

            # Just over the limit
            big = "G0 " + "X" * 4096
            r = c.post(
                f"/api/v1/printers/{SERIAL}/gcode",
                headers=_AUTH,
                json={"line": big},
            )
            assert r.status_code == 422, r.text
            assert "4096" in r.text

            # Well within limit
            r = c.post(
                f"/api/v1/printers/{SERIAL}/gcode",
                headers=_AUTH,
                json={"line": "G28"},
            )
            assert r.status_code == 200, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_raw_command_params_guard(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """_check_raw_params rejects oversized or too-deeply-nested params with 422."""
    app = build_app(tmp_path / "rawguard.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)

            # Oversized params: value whose serialised form exceeds 4096 bytes.
            big_value = "A" * 4097
            r = c.post(
                f"/api/v1/printers/{SERIAL}/command",
                headers=_AUTH,
                json={"category": "print", "command": "pause",
                      "params": {"data": big_value}},
            )
            assert r.status_code == 422, r.text
            assert "4096" in r.text

            # Over-deep params: a 3-level dict (depth > 2).
            r = c.post(
                f"/api/v1/printers/{SERIAL}/command",
                headers=_AUTH,
                json={"category": "print", "command": "pause",
                      "params": {"a": {"b": {"c": "deep"}}}},
            )
            assert r.status_code == 422, r.text
            assert "depth" in r.text.lower()

            # Valid params (depth 1, small): accepted.
            r = c.post(
                f"/api/v1/printers/{SERIAL}/command",
                headers=_AUTH,
                json={"category": "print", "command": "pause", "params": {}},
            )
            assert r.status_code == 200, r.text

    await asyncio.to_thread(run)
