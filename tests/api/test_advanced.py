"""Advanced control endpoints — wave 2 (YELLOW) + wave 3 (RED/BLACK).

Guard tests matter more than happy paths: every server-side guard must have a
rejection test that verifies the correct HTTP status code and error enum.

Mocked broker only — never touches the live printer.
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
            json={"host": "127.0.0.1", "access_code": ACCESS_CODE, "friendly_name": "Adv P1S"},
        ).status_code
        == 201
    )


def _wait_connected(c: TestClient, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get(f"/api/v1/printers/{SERIAL}", headers=_AUTH)
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


def _service(app: Any) -> Any:
    from bambu_bridge.service.registry import Registry
    registry: Registry = app.state.registry
    return registry.get(SERIAL)


# --------------------------------------------------------------------------- #
# Offline / not-found gate (common to all advanced endpoints)                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_advanced_offline_is_409(tmp_path: Path) -> None:
    """All advanced endpoints return 409 when the printer is not connected."""
    app = build_app(tmp_path / "adv_off.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            # xcam
            assert (
                c.post(
                    f"/api/v1/printers/{SERIAL}/xcam",
                    headers=_AUTH,
                    json={"module_name": "spaghetti_detector", "enabled": True},
                ).status_code
                == 409
            )
            # calibration
            assert (
                c.post(
                    f"/api/v1/printers/{SERIAL}/calibration",
                    headers=_AUTH,
                    json={"option": 1},
                ).status_code
                == 409
            )

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_advanced_unknown_printer_is_404(tmp_path: Path, mqtt_broker: int) -> None:
    """All advanced endpoints return 404 for an unknown printer_id."""
    app = build_app(tmp_path / "adv_404.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            assert (
                c.post(
                    "/api/v1/printers/nope/xcam",
                    headers=_AUTH,
                    json={"module_name": "spaghetti_detector", "enabled": True},
                ).status_code
                == 404
            )

    await asyncio.to_thread(run)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,body,env", [
    # YELLOW: xcam
    ("/xcam", {"module_name": "spaghetti_detector", "enabled": True}, {}),
    # YELLOW: ams drying op
    ("/ams/drying", {"ams_id": 0, "temp": 55, "cooling_temp": 35, "duration": 60,
                     "humidity": 20}, {}),
    # RED: extrude
    ("/extrude", {"distance_mm": 5.0}, {}),
    # RED: steppers/off
    ("/steppers/off", None, {}),
    # BLACK: gcode/raw when gate is open
    ("/gcode/raw", {"line": "G28"}, {"BRIDGE_ENABLE_RAW_GCODE": "1"}),
])
async def test_offline_is_409_across_risk_tiers(
    tmp_path: Path,
    endpoint: str,
    body: dict[str, Any] | None,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every risk tier returns 409 when the printer is not connected."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    db = tmp_path / f"off_{endpoint.replace('/', '_')}.db"
    app = build_app(db, mqtt_port=1)  # unreachable broker → printer stays offline

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            kwargs: dict[str, Any] = {"headers": _AUTH}
            if body is not None:
                kwargs["json"] = body
            r = c.post(f"/api/v1/printers/{SERIAL}{endpoint}", **kwargs)
            assert r.status_code == 409, f"{endpoint}: expected 409, got {r.status_code}: {r.text}"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_raw_gcode_enabled_but_offline_is_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate open + printer offline → 409, not 403 (gate check runs before online check)."""
    monkeypatch.setenv("BRIDGE_ENABLE_RAW_GCODE", "1")
    app = build_app(tmp_path / "rgc_off2.db", mqtt_port=1)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/gcode/raw",
                headers=_AUTH,
                json={"line": "G28"},
            )
            # Gate is open so we get past the 403; offline means 409.
            assert r.status_code == 409, r.text

    await asyncio.to_thread(run)


# --------------------------------------------------------------------------- #
# Wave-2: xcam (YELLOW)                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_xcam_spaghetti_detector_reaches_printer(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Happy path: xcam spaghetti_detector with print_halt=True."""
    app = build_app(tmp_path / "xcam_sp.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/xcam",
                headers=_AUTH,
                json={"module_name": "spaghetti_detector", "enabled": True, "print_halt": True},
            )
            assert r.status_code == 200, r.text
            req = _wait_request(
                mock_printer,
                lambda r: r.get("xcam", {}).get("command") == "xcam_control_set",
            )
            assert req["xcam"]["module_name"] == "spaghetti_detector"
            assert req["xcam"]["control"] is True
            assert req["xcam"]["print_halt"] is True

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_xcam_first_layer_inspector_reaches_printer(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """first_layer_inspector with print_halt=False."""
    app = build_app(tmp_path / "xcam_fl.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/xcam",
                headers=_AUTH,
                json={"module_name": "first_layer_inspector", "enabled": False},
            )
            assert r.status_code == 200, r.text
            req = _wait_request(
                mock_printer,
                lambda r: r.get("xcam", {}).get("module_name") == "first_layer_inspector",
            )
            assert req["xcam"]["control"] is False
            assert req["xcam"]["print_halt"] is False  # default

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_xcam_unknown_module_is_422(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard: unknown module_name → 422 (not forwarded to printer)."""
    app = build_app(tmp_path / "xcam_bad.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/xcam",
                headers=_AUTH,
                json={"module_name": "buildplate_marker_detector", "enabled": True},
            )
            assert r.status_code == 422, r.text
            # Nothing must have reached the printer.
            assert not any(
                req.get("xcam") for req in mock_printer.requests
            ), mock_printer.requests

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_xcam_requires_enabled_field(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Missing ``enabled`` field → 422 pydantic validation."""
    app = build_app(tmp_path / "xcam_mis.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/xcam",
                headers=_AUTH,
                json={"module_name": "spaghetti_detector"},  # missing enabled
            )
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


# --------------------------------------------------------------------------- #
# Wave-2: print_option (YELLOW)                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_print_option_air_print_detect_reaches_printer(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Happy path: air_print_detect=True."""
    app = build_app(tmp_path / "po_apd.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/print_option",
                headers=_AUTH,
                json={"air_print_detect": True},
            )
            assert r.status_code == 200, r.text
            req = _wait_request(
                mock_printer,
                lambda r: r.get("print", {}).get("command") == "print_option",
            )
            assert req["print"]["air_print_detect"] is True

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_print_option_unknown_flag_is_422(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard: unknown flag → 422."""
    app = build_app(tmp_path / "po_unk.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/print_option",
                headers=_AUTH,
                json={"magic_detect": True},
            )
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_print_option_empty_body_is_422(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard: empty body (no flags) → 422."""
    app = build_app(tmp_path / "po_empty.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/print_option",
                headers=_AUTH,
                json={},
            )
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_print_option_non_boolean_value_is_422(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard: flag value is a string instead of bool → 422."""
    app = build_app(tmp_path / "po_str.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/print_option",
                headers=_AUTH,
                json={"air_print_detect": "yes"},  # string, not bool
            )
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_print_option_multiple_flags_combined(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Multiple valid flags sent in one call."""
    app = build_app(tmp_path / "po_multi.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/print_option",
                headers=_AUTH,
                json={"air_print_detect": True, "auto_recovery": False},
            )
            assert r.status_code == 200, r.text
            req = _wait_request(
                mock_printer,
                lambda r: r.get("print", {}).get("command") == "print_option"
                and "air_print_detect" in r["print"],
            )
            assert req["print"]["air_print_detect"] is True
            assert req["print"]["auto_recovery"] is False

    await asyncio.to_thread(run)


# --------------------------------------------------------------------------- #
# Wave-2: skip_objects (YELLOW)                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_skip_objects_reaches_printer(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Happy path: valid obj_list forwarded."""
    app = build_app(tmp_path / "so.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/skip_objects",
                headers=_AUTH,
                json={"obj_list": [1, 2, 3]},
            )
            assert r.status_code == 200, r.text
            req = _wait_request(
                mock_printer,
                lambda r: r.get("print", {}).get("command") == "skip_objects",
            )
            assert req["print"]["obj_list"] == [1, 2, 3]
            assert isinstance(req["print"]["timestamp"], int)

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_skip_objects_empty_list_is_422(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard: empty obj_list → 422."""
    app = build_app(tmp_path / "so_empty.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/skip_objects",
                headers=_AUTH,
                json={"obj_list": []},
            )
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


# --------------------------------------------------------------------------- #
# Wave-2: AMS filament_setting (YELLOW)                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ams_filament_setting_reaches_printer(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Happy path: valid filament profile forwarded."""
    app = build_app(tmp_path / "ams_fs.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/ams/filament_setting",
                headers=_AUTH,
                json={
                    "ams_id": 0,
                    "tray_id": 1,
                    "tray_info_idx": "GFG96",
                    "tray_color": "161616FF",
                    "nozzle_temp_min": 230,
                    "nozzle_temp_max": 270,
                    "tray_type": "PETG",
                },
            )
            assert r.status_code == 200, r.text
            req = _wait_request(
                mock_printer,
                lambda r: r.get("print", {}).get("command") == "ams_filament_setting",
            )
            assert req["print"]["tray_type"] == "PETG"
            assert req["print"]["ams_id"] == 0
            assert req["print"]["tray_id"] == 1

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_ams_filament_setting_bad_color_is_422(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard: invalid tray_color → 422."""
    app = build_app(tmp_path / "ams_fs_bc.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/ams/filament_setting",
                headers=_AUTH,
                json={
                    "ams_id": 0, "tray_id": 0, "tray_info_idx": "",
                    "tray_color": "ZZZ",  # invalid
                    "nozzle_temp_min": 190, "nozzle_temp_max": 230,
                    "tray_type": "PLA",
                },
            )
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_ams_filament_setting_unknown_type_is_422(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard: unknown tray_type → 422."""
    app = build_app(tmp_path / "ams_fs_ut.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/ams/filament_setting",
                headers=_AUTH,
                json={
                    "ams_id": 0, "tray_id": 0, "tray_info_idx": "",
                    "tray_color": "FFFFFFFF",
                    "nozzle_temp_min": 190, "nozzle_temp_max": 230,
                    "tray_type": "UNOBTANIUM",
                },
            )
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


# --------------------------------------------------------------------------- #
# Wave-2: AMS rfid (GREEN)                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ams_rfid_reaches_printer(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Happy path: ams_get_rfid forwarded."""
    app = build_app(tmp_path / "ams_rfid.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/ams/rfid",
                headers=_AUTH,
                json={"ams_id": 0, "slot_id": 2},
            )
            assert r.status_code == 200, r.text
            req = _wait_request(
                mock_printer,
                lambda r: r.get("print", {}).get("command") == "ams_get_rfid",
            )
            assert req["print"]["ams_id"] == 0
            assert req["print"]["slot_id"] == 2

    await asyncio.to_thread(run)


# --------------------------------------------------------------------------- #
# Wave-2: AMS drying (YELLOW)                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ams_drying_reaches_printer(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Happy path: ams_filament_drying forwarded."""
    app = build_app(tmp_path / "ams_dry.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/ams/drying",
                headers=_AUTH,
                json={"ams_id": 0, "temp": 55, "cooling_temp": 35, "duration": 240, "humidity": 15},
            )
            assert r.status_code == 200, r.text
            req = _wait_request(
                mock_printer,
                lambda r: r.get("print", {}).get("command") == "ams_filament_drying",
            )
            assert req["print"]["temp"] == 55
            assert req["print"]["duration"] == 240

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_ams_drying_zero_duration_is_422(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard: duration=0 → 422."""
    app = build_app(tmp_path / "ams_dry_z.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/ams/drying",
                headers=_AUTH,
                json={"ams_id": 0, "temp": 55, "cooling_temp": 35, "duration": 0, "humidity": 15},
            )
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_ams_drying_temp_at_ceiling_accepted(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """AMS drying temp exactly at AMS_DRYING_MAX_TEMP_C (75 °C) is accepted."""
    from bambu_bridge.protocol.commands import AMS_DRYING_MAX_TEMP_C
    app = build_app(tmp_path / "ams_dry_ceil.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/ams/drying",
                headers=_AUTH,
                json={"ams_id": 0, "temp": AMS_DRYING_MAX_TEMP_C,
                      "cooling_temp": 35, "duration": 60, "humidity": 15},
            )
            assert r.status_code == 200, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_ams_drying_temp_above_ceiling_is_422(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard: AMS drying temp > 75 °C → 422 (hardware safety ceiling)."""
    from bambu_bridge.protocol.commands import AMS_DRYING_MAX_TEMP_C
    app = build_app(tmp_path / "ams_dry_hot.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/ams/drying",
                headers=_AUTH,
                json={"ams_id": 0, "temp": AMS_DRYING_MAX_TEMP_C + 1,
                      "cooling_temp": 35, "duration": 60, "humidity": 15},
            )
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_ams_drying_ams_id_above_max_is_422(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard: ams_id > 3 → 422 (P1S supports at most 4 AMS units, 0-indexed)."""
    from bambu_bridge.protocol.commands import AMS_ID_MAX
    app = build_app(tmp_path / "ams_dry_id.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/ams/drying",
                headers=_AUTH,
                json={"ams_id": AMS_ID_MAX + 1, "temp": 55,
                      "cooling_temp": 35, "duration": 60, "humidity": 15},
            )
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


# --------------------------------------------------------------------------- #
# Wave-2: AMS user_setting (YELLOW)                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ams_user_setting_reaches_printer(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Happy path: ams_user_setting forwarded."""
    app = build_app(tmp_path / "ams_us.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/ams/user_setting",
                headers=_AUTH,
                json={"ams_id": 0, "startup_read_option": True, "tray_read_option": False},
            )
            assert r.status_code == 200, r.text
            req = _wait_request(
                mock_printer,
                lambda r: r.get("print", {}).get("command") == "ams_user_setting",
            )
            assert req["print"]["startup_read_option"] is True
            assert req["print"]["tray_read_option"] is False

    await asyncio.to_thread(run)


# --------------------------------------------------------------------------- #
# Wave-2: calibration (RED)                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_calibration_vibration_reaches_printer(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Happy path: calibration option=1 (vibration) forwarded."""
    app = build_app(tmp_path / "cal_vib.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/calibration",
                headers=_AUTH,
                json={"option": 1},
            )
            assert r.status_code == 200, r.text
            req = _wait_request(
                mock_printer,
                lambda r: r.get("print", {}).get("command") == "calibration",
            )
            assert req["print"]["option"] == 1

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_calibration_all_p1s_confirmed_options_accepted(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """All valid P1S bit combinations (1–7) are accepted.

    Includes combinations 3 (vibration+bed), 5 (vibration+flow), and
    6 (bed+flow) which were previously incorrectly rejected.
    """
    app = build_app(tmp_path / "cal_all.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            for opt in [1, 2, 3, 4, 5, 6, 7]:
                r = c.post(
                    f"/api/v1/printers/{SERIAL}/calibration",
                    headers=_AUTH,
                    json={"option": opt},
                )
                assert r.status_code == 200, f"option={opt} rejected: {r.text}"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_calibration_non_p1s_bits_rejected(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard: 0 and any value with bits 3+ set → 422; nothing reaches printer."""
    app = build_app(tmp_path / "cal_bad.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            for bad_opt in [0, 8, 16, 255]:
                r = c.post(
                    f"/api/v1/printers/{SERIAL}/calibration",
                    headers=_AUTH,
                    json={"option": bad_opt},
                )
                assert r.status_code == 422, (
                    f"option={bad_opt} should be 422, got {r.status_code}: {r.text}"
                )
            # None of the bad options should have reached the printer.
            assert not any(
                req.get("print", {}).get("command") == "calibration"
                for req in mock_printer.requests
            )

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_calibration_error_message_names_matrix(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """The 422 error message must name the control matrix as authority."""
    app = build_app(tmp_path / "cal_msg.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/calibration",
                headers=_AUTH,
                json={"option": 8},  # X1-only LIDAR bit
            )
            assert r.status_code == 422, r.text
            assert "CONTROL-MATRIX" in r.text or "matrix" in r.text.lower()

    await asyncio.to_thread(run)


# --------------------------------------------------------------------------- #
# Wave-2: set_accessories/nozzle (YELLOW)                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_set_nozzle_reaches_printer(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Happy path: stainless_steel 0.4mm nozzle forwarded."""
    app = build_app(tmp_path / "nz.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/set_accessories/nozzle",
                headers=_AUTH,
                json={"nozzle_type": "stainless_steel", "nozzle_diameter": 0.4},
            )
            assert r.status_code == 200, r.text
            req = _wait_request(
                mock_printer,
                lambda r: r.get("system", {}).get("command") == "set_accessories",
            )
            assert req["system"]["nozzle_type"] == "stainless_steel"
            assert req["system"]["nozzle_diameter"] == 0.4
            assert req["system"]["accessory_type"] == "nozzle"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_set_nozzle_updates_in_memory_service_type(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """set_accessories/nozzle updates service.nozzle_type in-memory."""
    app = build_app(tmp_path / "nz_mem.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)

            svc = _service(app)
            assert svc.nozzle_type != "hardened_steel"

            r = c.post(
                f"/api/v1/printers/{SERIAL}/set_accessories/nozzle",
                headers=_AUTH,
                json={"nozzle_type": "hardened_steel", "nozzle_diameter": 0.6},
            )
            assert r.status_code == 200, r.text
            # Wait for the command to be sent before checking state.
            _wait_request(
                mock_printer,
                lambda r: r.get("system", {}).get("command") == "set_accessories",
            )
            assert svc.nozzle_type == "hardened_steel"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_set_nozzle_invalid_type_is_422(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard: unknown nozzle_type → 422."""
    app = build_app(tmp_path / "nz_bad.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/set_accessories/nozzle",
                headers=_AUTH,
                json={"nozzle_type": "titanium", "nozzle_diameter": 0.4},
            )
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_set_nozzle_invalid_diameter_is_422(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard: diameter not in {0.2, 0.4, 0.6, 0.8} → 422."""
    app = build_app(tmp_path / "nz_diam.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/set_accessories/nozzle",
                headers=_AUTH,
                json={"nozzle_type": "stainless_steel", "nozzle_diameter": 0.3},
            )
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


# --------------------------------------------------------------------------- #
# Wave-3: extrude / retract (RED) — GUARD TESTS FIRST                         #
# --------------------------------------------------------------------------- #


def _inject_state(app: Any, patch: dict[str, Any], *, wait_seed: float = 2.0) -> None:
    """Inject real P1S state fields for guard testing."""
    svc = _service(app)
    deadline = time.time() + wait_seed
    while time.time() < deadline:
        if svc._state:  # noqa: SLF001
            break
        time.sleep(0.02)
    svc._state.update(patch)  # noqa: SLF001
    time.sleep(0.08)


@pytest.mark.asyncio
async def test_extrude_while_finish_is_409(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard 1: FINISH gcode_state → 409 extrude_state_not_allowed.

    FINISH means the print completed and the bed is still warm / cooling down.
    Extrusion is not expected nor safe in this state.
    """
    app = build_app(tmp_path / "ext_finish.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {
                "gcode_state": "FINISH",
                "home_flag": 7,
                "nozzle_temper": 220.0,
            })
            r = c.post(
                f"/api/v1/printers/{SERIAL}/extrude",
                headers=_AUTH,
                json={"distance_mm": 10.0},
            )
            assert r.status_code == 409, r.text
            body = r.json()
            assert body["error"] == "extrude_state_not_allowed"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_extrude_while_failed_is_409(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard 1: FAILED gcode_state → 409 extrude_state_not_allowed.

    A failed print means the printer may be in an unknown mechanical state;
    extrusion should not be allowed until the operator has acknowledged and
    cleared the failure (typically by homing and returning to IDLE).
    """
    app = build_app(tmp_path / "ext_failed.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {
                "gcode_state": "FAILED",
                "home_flag": 7,
                "nozzle_temper": 220.0,
            })
            r = c.post(
                f"/api/v1/printers/{SERIAL}/extrude",
                headers=_AUTH,
                json={"distance_mm": 10.0},
            )
            assert r.status_code == 409, r.text
            body = r.json()
            assert body["error"] == "extrude_state_not_allowed"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_extrude_while_printing_is_409(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard 1: RUNNING gcode_state → 409 extrude_state_not_allowed."""
    app = build_app(tmp_path / "ext_print.db", mqtt_port=mqtt_broker)
    # mock_printer uses SAMPLE_PUSH_STATUS which has gcode_state=RUNNING

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            # The default mock has gcode_state=RUNNING — should block extrude.
            _inject_state(app, {"home_flag": 7, "nozzle_temper": 220.0})
            r = c.post(
                f"/api/v1/printers/{SERIAL}/extrude",
                headers=_AUTH,
                json={"distance_mm": 10.0},
            )
            assert r.status_code == 409, r.text
            body = r.json()
            assert body["error"] == "extrude_state_not_allowed"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_extrude_not_homed_absent_flag_is_409(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard 2: absent home_flag → 409 jog_not_homed."""
    app = build_app(tmp_path / "ext_nh.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            # Set state to IDLE but no home_flag key at all.
            _inject_state(app, {"gcode_state": "IDLE", "nozzle_temper": 220.0})
            r = c.post(
                f"/api/v1/printers/{SERIAL}/extrude",
                headers=_AUTH,
                json={"distance_mm": 10.0},
            )
            assert r.status_code == 409, r.text
            body = r.json()
            assert body["error"] == "jog_not_homed"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_extrude_home_flag_zero_is_409(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard 2: home_flag=0 (powered-on never-homed) → 409 even though key is present."""
    app = build_app(tmp_path / "ext_nhz.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            # home_flag present but zero — all axis bits clear.
            _inject_state(app, {"gcode_state": "IDLE", "home_flag": 0, "nozzle_temper": 220.0})
            r = c.post(
                f"/api/v1/printers/{SERIAL}/extrude",
                headers=_AUTH,
                json={"distance_mm": 10.0},
            )
            assert r.status_code == 409, r.text
            body = r.json()
            assert body["error"] == "jog_not_homed"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_extrude_partial_home_xy_only_is_409(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard 2: home_flag with X|Y set but Z clear → 409, response names Z."""
    app = build_app(tmp_path / "ext_nhz2.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            # X=0x01 | Y=0x02 = 0x03 — Z axis bit (0x04) is absent.
            _inject_state(app, {"gcode_state": "IDLE", "home_flag": 0x03, "nozzle_temper": 220.0})
            r = c.post(
                f"/api/v1/printers/{SERIAL}/extrude",
                headers=_AUTH,
                json={"distance_mm": 10.0},
            )
            assert r.status_code == 409, r.text
            body = r.json()
            assert body["error"] == "jog_not_homed"
            # The response must name the unhomed axis.
            assert "Z" in body.get("message", "") or "Z" in str(
                body.get("context", {}).get("unhomed_axes", [])
            ), f"Z not named in: {body}"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_extrude_all_axes_homed_passes_guard2(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard 2 passes when all three axis bits (0x07) are set."""
    from bambu_bridge.protocol.commands import EXTRUDE_MIN_TEMP_C
    app = build_app(tmp_path / "ext_homed.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            # 0x07 = X|Y|Z all homed.
            _inject_state(app, {
                "gcode_state": "IDLE",
                "home_flag": 0x07,
                "nozzle_temper": float(EXTRUDE_MIN_TEMP_C + 30),
            })
            r = c.post(
                f"/api/v1/printers/{SERIAL}/extrude",
                headers=_AUTH,
                json={"distance_mm": 5.0},
            )
            assert r.status_code == 200, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_extrude_cold_nozzle_is_422(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard 3: nozzle below EXTRUDE_MIN_TEMP_C → 422 nozzle_too_cold."""
    from bambu_bridge.protocol.commands import EXTRUDE_MIN_TEMP_C
    app = build_app(tmp_path / "ext_cold.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {
                "gcode_state": "IDLE",
                "home_flag": 7,
                "nozzle_temper": float(EXTRUDE_MIN_TEMP_C - 1),
            })
            r = c.post(
                f"/api/v1/printers/{SERIAL}/extrude",
                headers=_AUTH,
                json={"distance_mm": 5.0},
            )
            assert r.status_code == 422, r.text
            body = r.json()
            assert body["error"] == "nozzle_too_cold"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_extrude_no_temp_data_is_422(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard 3 (unavailable): no nozzle_temper in state → 422 (conservative)."""
    app = build_app(tmp_path / "ext_no_temp.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            svc = _service(app)
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if svc._state:  # noqa: SLF001
                    break
                time.sleep(0.02)
            # Force-remove nozzle_temper from state.
            svc._state.update({"gcode_state": "IDLE", "home_flag": 7})  # noqa: SLF001
            svc._state.pop("nozzle_temper", None)  # noqa: SLF001
            time.sleep(0.08)

            r = c.post(
                f"/api/v1/printers/{SERIAL}/extrude",
                headers=_AUTH,
                json={"distance_mm": 5.0},
            )
            assert r.status_code == 422, r.text
            body = r.json()
            assert body["error"] == "nozzle_too_cold"

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_extrude_over_100mm_is_422(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard 4 (builder): |distance_mm| > 100 → 422."""
    app = build_app(tmp_path / "ext_big.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {"gcode_state": "IDLE", "home_flag": 7, "nozzle_temper": 220.0})
            r = c.post(
                f"/api/v1/printers/{SERIAL}/extrude",
                headers=_AUTH,
                json={"distance_mm": 150.0},
            )
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_extrude_bad_feedrate_is_422(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Guard 5 (builder): feedrate not in {120,300,600} → 422."""
    app = build_app(tmp_path / "ext_fr.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {"gcode_state": "IDLE", "home_flag": 7, "nozzle_temper": 220.0})
            r = c.post(
                f"/api/v1/printers/{SERIAL}/extrude",
                headers=_AUTH,
                json={"distance_mm": 10.0, "feedrate": 400},
            )
            assert r.status_code == 422, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_extrude_at_exactly_100mm_is_allowed(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Boundary: distance_mm=100.0 (exactly at max) is accepted."""
    from bambu_bridge.protocol.commands import EXTRUDE_MIN_TEMP_C
    app = build_app(tmp_path / "ext_100.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {
                "gcode_state": "IDLE",
                "home_flag": 0x07,
                "nozzle_temper": float(EXTRUDE_MIN_TEMP_C + 30),
            })
            r = c.post(
                f"/api/v1/printers/{SERIAL}/extrude",
                headers=_AUTH,
                json={"distance_mm": 100.0},
            )
            assert r.status_code == 200, r.text
            _wait_request(
                mock_printer,
                lambda r: "G1 E100.0" in r.get("print", {}).get("param", ""),
            )

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_extrude_at_exactly_minus_100mm_is_allowed(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Boundary: distance_mm=-100.0 (exactly at negative max) is accepted."""
    from bambu_bridge.protocol.commands import EXTRUDE_MIN_TEMP_C
    app = build_app(tmp_path / "ext_m100.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {
                "gcode_state": "IDLE",
                "home_flag": 0x07,
                "nozzle_temper": float(EXTRUDE_MIN_TEMP_C + 30),
            })
            r = c.post(
                f"/api/v1/printers/{SERIAL}/extrude",
                headers=_AUTH,
                json={"distance_mm": -100.0},
            )
            assert r.status_code == 200, r.text
            _wait_request(
                mock_printer,
                lambda r: "G1 E-100.0" in r.get("print", {}).get("param", ""),
            )

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_extrude_happy_path_idle_warm(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Happy path: IDLE + homed + warm nozzle → extrude command reaches printer."""
    from bambu_bridge.protocol.commands import EXTRUDE_MIN_TEMP_C
    app = build_app(tmp_path / "ext_ok.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {
                "gcode_state": "IDLE",
                "home_flag": 7,
                "nozzle_temper": float(EXTRUDE_MIN_TEMP_C + 30),
            })
            r = c.post(
                f"/api/v1/printers/{SERIAL}/extrude",
                headers=_AUTH,
                json={"distance_mm": 20.0, "feedrate": 300},
            )
            assert r.status_code == 200, r.text
            req = _wait_request(
                mock_printer,
                lambda r: "G1 E20.0" in r.get("print", {}).get("param", ""),
            )
            assert "M83" in req["print"]["param"]
            assert "M82" in req["print"]["param"]

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_extrude_paused_state_allowed(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Paused print allows extrude (for filament load during pause)."""
    from bambu_bridge.protocol.commands import EXTRUDE_MIN_TEMP_C
    app = build_app(tmp_path / "ext_pause.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            _inject_state(app, {
                "gcode_state": "PAUSE",
                "home_flag": 7,
                "nozzle_temper": float(EXTRUDE_MIN_TEMP_C + 50),
            })
            r = c.post(
                f"/api/v1/printers/{SERIAL}/extrude",
                headers=_AUTH,
                json={"distance_mm": -5.0},  # retract
            )
            assert r.status_code == 200, r.text

    await asyncio.to_thread(run)


# --------------------------------------------------------------------------- #
# Wave-3: steppers/off (RED)                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_steppers_off_reaches_printer(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Happy path: M84 reaches the printer."""
    app = build_app(tmp_path / "m84.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/steppers/off",
                headers=_AUTH,
            )
            assert r.status_code == 200, r.text
            _wait_request(
                mock_printer,
                lambda r: "M84" in r.get("print", {}).get("param", ""),
            )

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_steppers_off_resets_dead_reckon_position(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """After steppers/off the bridge's dead-reckon position is UNKNOWN."""
    app = build_app(tmp_path / "m84_pos.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            svc = _service(app)
            # Seed a known position (as if we had homed + jogged).
            svc.mark_homed()
            svc.apply_jog("Z", 50.0)
            assert svc.tracked_position("Z") == 50.0

            r = c.post(
                f"/api/v1/printers/{SERIAL}/steppers/off",
                headers=_AUTH,
            )
            assert r.status_code == 200, r.text
            _wait_request(
                mock_printer,
                lambda r: "M84" in r.get("print", {}).get("param", ""),
            )
            # Position must now be unknown.
            assert svc.tracked_position("Z") is None
            assert svc.tracked_position("X") is None
            assert svc.tracked_position("Y") is None

    await asyncio.to_thread(run)


# --------------------------------------------------------------------------- #
# BLACK: raw G-code console (BRIDGE_ENABLE_RAW_GCODE gate)                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_raw_gcode_disabled_by_default_is_403(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: BRIDGE_ENABLE_RAW_GCODE not set → 403."""
    monkeypatch.delenv("BRIDGE_ENABLE_RAW_GCODE", raising=False)
    app = build_app(tmp_path / "rgc_off.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/gcode/raw",
                headers=_AUTH,
                json={"line": "G28"},
            )
            assert r.status_code == 403, r.text
            body = r.json()
            assert body["error"] == "raw_gcode_disabled"
            # Nothing must have reached the printer.
            assert not any(
                "G28" in req.get("print", {}).get("param", "")
                for req in mock_printer.requests
            )

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_raw_gcode_empty_env_is_403(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: BRIDGE_ENABLE_RAW_GCODE='' → 403."""
    monkeypatch.setenv("BRIDGE_ENABLE_RAW_GCODE", "")
    app = build_app(tmp_path / "rgc_empty.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/gcode/raw",
                headers=_AUTH,
                json={"line": "G28"},
            )
            assert r.status_code == 403, r.text

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_raw_gcode_enabled_reaches_printer(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When gate is open, command reaches the printer."""
    monkeypatch.setenv("BRIDGE_ENABLE_RAW_GCODE", "1")
    app = build_app(tmp_path / "rgc_on.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            r = c.post(
                f"/api/v1/printers/{SERIAL}/gcode/raw",
                headers=_AUTH,
                json={"line": "M503"},  # EEPROM dump — allowed when gate is open
            )
            assert r.status_code == 200, r.text
            _wait_request(
                mock_printer,
                lambda r: "M503" in r.get("print", {}).get("param", ""),
            )

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_raw_gcode_4kb_cap_enforced(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4096-byte cap is enforced even when the gate is open."""
    monkeypatch.setenv("BRIDGE_ENABLE_RAW_GCODE", "1")
    app = build_app(tmp_path / "rgc_big.db", mqtt_port=mqtt_broker)

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            _wait_connected(c)
            big = "G0 " + "X" * 4096
            r = c.post(
                f"/api/v1/printers/{SERIAL}/gcode/raw",
                headers=_AUTH,
                json={"line": big},
            )
            assert r.status_code == 422, r.text
            assert "4096" in r.text

    await asyncio.to_thread(run)
