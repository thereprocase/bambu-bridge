"""M4 + slicedoc acceptance: the job pipeline is no longer payload-blind.

The old version of this test uploaded ``b"PK\\x03\\x04 3mf"`` and asserted
"completed" — green because ``MockPrinter`` accepts anything. That manufactured
false confidence in the one thing that matters (REPORT §6.3 / SYNTHESIS §3).

Now:

* a *consistent* synthesized ``.gcode.3mf`` runs queued→…→completed;
* garbage and the **real §6.3 container** are refused *before upload* — the
  printer is never driven;
* a printer that goes RUNNING but never progresses trips the
  ``FED_NO_PROGRESS`` watchdog instead of waiting out a 17-minute dry run.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import bambu_bridge.service.jobs as jobs_mod
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
    """Every test in this file registers a printer via the new POST shape;
    bypass the TLS+MQTT probe with a successful fake."""
    patch_discovery_ok(monkeypatch, serial=SERIAL)
_PROBE = (
    Path(__file__).resolve().parents[2]
    / "probes"
    / "3DBenchy_PETG_slot2.gcode.3mf"
)


def _register(c: TestClient, name: str = "Job P1S") -> None:
    # discovery patched per-test via patch_discovery_ok(); just POST the new shape.
    assert (
        c.post(
            "/api/v1/printers",
            headers=_AUTH,
            json={
                "host": "127.0.0.1",
                "access_code": ACCESS_CODE,
                "friendly_name": name,
            },
        ).status_code
        == 201
    )
    deadline = time.time() + 5
    while time.time() < deadline:
        snapshot = c.get(f"/api/v1/printers/{SERIAL}", headers=_AUTH).json()
        if snapshot.get("_raw", {}).get("info", {}).get("module"):
            return
        time.sleep(0.02)
    raise AssertionError("Printer identity discovery timed out")


def _poll_terminal(
    c: TestClient, job_id: str, *, timeout: float = 20.0
) -> dict[str, Any]:
    deadline = time.time() + timeout
    detail: dict[str, Any] = {}
    while time.time() < deadline:
        detail = c.get(f"/api/v1/jobs/{job_id}", headers=_AUTH).json()
        if detail["job"]["state"] in ("completed", "failed", "canceled"):
            break
        time.sleep(0.1)
    return detail


def _transitions(events: list[dict[str, Any]]) -> list[str]:
    return [
        e["payload"].get("to")
        for e in events
        if e["event_type"] == "state_change"
    ]


@pytest.mark.asyncio
async def test_full_job_lifecycle_to_completed(
    tmp_path: Path,
    mqtt_broker: int,
    printing_mock: MockPrinter,
    ftps_server: tuple[int, Path],
    valid_gcode_3mf: bytes,
) -> None:
    ftps_port, _ = ftps_server
    app = build_app(
        tmp_path / "jobs.db", mqtt_port=mqtt_broker, ftps_port=ftps_port
    )
    captured: dict[str, object] = {}

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            submit = c.post(
                f"/api/v1/printers/{SERIAL}/jobs",
                headers=_AUTH,
                data={"ams_mapping": "1"},  # matches the gcode's M620 S1A
                files={
                    "file": (
                        "cube.3mf",
                        valid_gcode_3mf,
                        "application/octet-stream",
                    )
                },
            )
            assert submit.status_code == 201, submit.text
            assert submit.json()["state"] == "queued"
            captured["detail"] = _poll_terminal(c, submit.json()["id"])

    await asyncio.to_thread(run)

    detail = captured["detail"]
    job = detail["job"]  # type: ignore[index]
    events = detail["events"]  # type: ignore[index]

    assert job["state"] == "completed", detail
    # SD card root, slicedoc-normalised name (not the old /model/<orig>).
    assert job["file_path"] == "/cube.gcode.3mf"
    assert job["finished_at"] is not None
    # PR B JobState remap (contract §7.4): started → submitted, plus
    # new `preparing` between submitted and printing.
    # MockPrinter goes RUNNING → FINISH without intermediate layer reports,
    # so PREPARING → COMPLETED is the legal short-circuit (see _ALLOWED).
    # A real printer with `layer_num > 0` reports between RUNNING and
    # FINISH would hit PRINTING in the middle; either path is acceptable
    # per contract §7.4 — the test exercises the short-circuit branch
    # because that's the mock's wire behavior.
    assert _transitions(events) == [
        "uploading",
        "submitted",
        "preparing",
        "completed",
    ]
    assert events[0]["event_type"] == "job_created"
    # Orca's ams_mapping goes to the printer unchanged: tray 1 = physical slot 2.
    sent = [
        r["print"] for r in printing_mock.requests
        if r.get("print", {}).get("command") == "project_file"
    ]
    assert [(p["ams_mapping"], p["use_ams"]) for p in sent] == [([1], True)]


@pytest.mark.parametrize(
    "raw,expected",
    [("1", [1]), ("0,-1,3", [0, -1, 3]), ("-1", [-1]), ("", None), (None, None)],
)
def test_ams_mapping_form_is_orca_trays(raw: str | None, expected: list[int] | None) -> None:
    from bambu_bridge.api.jobs import _parse_ams

    assert _parse_ams(raw) == expected


@pytest.mark.parametrize("raw", ["1000", "-2", "16", "a", ",".join(["0"] * 65)])
def test_ams_mapping_form_out_of_range_is_422(raw: str) -> None:
    from fastapi import HTTPException

    from bambu_bridge.api.jobs import _parse_ams

    with pytest.raises(HTTPException) as err:
        _parse_ams(raw)
    assert err.value.status_code == 422


@pytest.mark.asyncio
async def test_garbage_upload_refused_before_printer(
    tmp_path: Path,
    mqtt_broker: int,
    printing_mock: MockPrinter,
    ftps_server: tuple[int, Path],
) -> None:
    """Not-a-zip → validation fails from queued; printer never driven."""
    ftps_port, _ = ftps_server
    app = build_app(
        tmp_path / "bad.db", mqtt_port=mqtt_broker, ftps_port=ftps_port
    )
    captured: dict[str, object] = {}

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            sub = c.post(
                f"/api/v1/printers/{SERIAL}/jobs",
                headers=_AUTH,
                files={
                    "file": (
                        "junk.3mf",
                        b"definitely not a zip",
                        "application/octet-stream",
                    )
                },
            )
            captured["sub"] = sub.json()
            captured["status"] = sub.status_code

    await asyncio.to_thread(run)
    # Contract §7.2: sync 422 with structured issues; no job row created.
    assert captured["status"] == 422
    body = captured["sub"]
    assert body["error"] == "invalid_3mf"
    issues = body["issues"]
    assert issues and all({"code", "category", "message"} <= i.keys() for i in issues)
    # The printer never saw a project_file (or any command).
    assert not any(
        r.get("print", {}).get("command") == "project_file"
        for r in printing_mock.requests
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not _PROBE.exists(), reason="probe fixture absent")
async def test_real_6_3_container_is_refused(
    tmp_path: Path,
    mqtt_broker: int,
    printing_mock: MockPrinter,
    ftps_server: tuple[int, Path],
) -> None:
    """The exact file that wasted 17 min of real hardware never uploads."""
    ftps_port, _ = ftps_server
    app = build_app(
        tmp_path / "six3.db", mqtt_port=mqtt_broker, ftps_port=ftps_port
    )
    captured: dict[str, object] = {}

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            sub = c.post(
                f"/api/v1/printers/{SERIAL}/jobs",
                headers=_AUTH,
                data={"ams_mapping": "1"},
                files={
                    "file": (
                        "3DBenchy.gcode.3mf",
                        _PROBE.read_bytes(),
                        "application/octet-stream",
                    )
                },
            )
            captured["sub"] = sub.json()
            captured["status"] = sub.status_code

    await asyncio.to_thread(run)
    # Contract §7.2: the §6.3 donor file is refused synchronously w/ G5 issues.
    assert captured["status"] == 422
    body = captured["sub"]
    assert body["error"] == "invalid_3mf"
    assert any(i["code"] == "G5" for i in body["issues"])
    assert not any(
        r.get("print", {}).get("command") == "project_file"
        for r in printing_mock.requests
    )


@pytest.mark.asyncio
async def test_fed_no_progress_watchdog(
    tmp_path: Path,
    mqtt_broker: int,
    stalling_mock: MockPrinter,
    ftps_server: tuple[int, Path],
    valid_gcode_3mf: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RUNNING but no progress → auto-stop + FAILED(FED_NO_PROGRESS)."""
    monkeypatch.setattr(jobs_mod, "_FEED_DEADLINE_S", 0.5)
    ftps_port, _ = ftps_server
    app = build_app(
        tmp_path / "fed.db", mqtt_port=mqtt_broker, ftps_port=ftps_port
    )
    captured: dict[str, object] = {}

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            sub = c.post(
                f"/api/v1/printers/{SERIAL}/jobs",
                headers=_AUTH,
                data={"ams_mapping": "1"},
                files={
                    "file": (
                        "cube.3mf",
                        valid_gcode_3mf,
                        "application/octet-stream",
                    )
                },
            )
            captured["detail"] = _poll_terminal(c, sub.json()["id"])

    await asyncio.to_thread(run)
    detail = captured["detail"]
    job = detail["job"]  # type: ignore[index]
    assert job["state"] == "failed"
    assert job["error_code"] == "FED_NO_PROGRESS"
    # The watchdog actually told the printer to stop.
    assert any(
        r.get("print", {}).get("command") == "stop"
        for r in stalling_mock.requests
    )
    trans = _transitions(detail["events"])  # type: ignore[index]
    # PR B: FED_NO_PROGRESS now aborts during the PREPARING phase — the
    # printer never crossed layer_num > 0 because the AMS never engaged.
    # The watchdog never lets us reach PRINTING here, which is correct.
    assert trans[:3] == ["uploading", "submitted", "preparing"]
    assert trans[-1] == "failed"


@pytest.mark.asyncio
async def test_cancel_before_printer_ack(
    tmp_path: Path,
    ftps_server: tuple[int, Path],
    valid_gcode_3mf: bytes,
) -> None:
    """Valid file, no broker -> never gets a printer ack; cancel => terminal."""
    ftps_port, _ = ftps_server
    app = build_app(tmp_path / "cancel.db", mqtt_port=1, ftps_port=ftps_port)
    captured: dict[str, object] = {}

    def run() -> None:
        with TestClient(app) as c:
            _register(c, "C")
            sub = c.post(
                f"/api/v1/printers/{SERIAL}/jobs",
                headers=_AUTH,
                data={"ams_mapping": "1"},
                files={
                    "file": (
                        "x.3mf",
                        valid_gcode_3mf,
                        "application/octet-stream",
                    )
                },
            )
            job_id = sub.json()["id"]
            c.post(f"/api/v1/jobs/{job_id}/cancel", headers=_AUTH)
            captured["state"] = _poll_terminal(c, job_id, timeout=15.0)[
                "job"
            ]["state"]

    await asyncio.to_thread(run)
    assert captured["state"] in ("canceled", "failed")


def _two_filament_container(layer_list: str) -> bytes:
    """Minimal P1S slice (the three members the gate reads) with two filaments and one
    layer range listing both, OrcaSlicer's shape for a two-colour layer."""
    import hashlib
    import io
    import zipfile

    gcode = b"M620 S0A\nT0\nM621 S0A\nG1 X1\nM620 S1A\nT1\nM621 S1A\n"
    info = (
        '<?xml version="1.0"?><config><plate>'
        '<metadata key="printer_model_id" value="C12"/>'
        '<metadata key="nozzle_diameters" value="0.4"/>'
        '<filament id="1" type="ASA" color="#161616"/>'
        '<filament id="2" type="ASA" color="#FFF144"/>'
        f'<layer_filament_lists><layer_filament_list filament_list="{layer_list}" '
        'layer_ranges="0 9" /></layer_filament_lists>'
        "</plate></config>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/plate_1.gcode", gcode)
        md5 = hashlib.md5(gcode).hexdigest().upper()  # noqa: S324
        z.writestr("Metadata/plate_1.gcode.md5", md5)
        z.writestr("Metadata/slice_info.config", info)
    return buf.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("layer_list", ["0 1", "0 x"])
async def test_two_filament_layer_list_is_a_clean_422_not_a_500(
    tmp_path: Path,
    mqtt_broker: int,
    printing_mock: MockPrinter,
    ftps_server: tuple[int, Path],
    layer_list: str,
) -> None:
    """filament_list="0 1" once raised ValueError out of validate(): a 500 from this endpoint.
    One ams_mapping slot for a two-filament slice must be refused cleanly as invalid_3mf."""
    ftps_port, _ = ftps_server
    app = build_app(tmp_path / "lfl.db", mqtt_port=mqtt_broker, ftps_port=ftps_port)
    captured: dict[str, object] = {}

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            sub = c.post(
                f"/api/v1/printers/{SERIAL}/jobs",
                headers=_AUTH,
                data={"ams_mapping": "1"},
                files={
                    "file": (
                        "two.gcode.3mf",
                        _two_filament_container(layer_list),
                        "application/octet-stream",
                    )
                },
            )
            captured["status"] = sub.status_code
            captured["body"] = sub.json()

    await asyncio.to_thread(run)
    assert captured["status"] == 422, captured
    messages = " ".join(i["message"] for i in captured["body"]["issues"])  # type: ignore[index]
    assert "ams_mapping arity 1" in messages, captured


async def _submit_and_wait(tmp_path: Path, mqtt_broker: int, ftps_port: int, payload: bytes,
                           name: str) -> dict[str, Any]:
    app = build_app(tmp_path / f"{name}.db", mqtt_port=mqtt_broker, ftps_port=ftps_port)
    captured: dict[str, Any] = {}

    def run() -> None:
        with TestClient(app) as c:
            _register(c)
            sub = c.post(f"/api/v1/printers/{SERIAL}/jobs", headers=_AUTH,
                         data={"ams_mapping": "1"},
                         files={"file": ("cube.3mf", payload, "application/octet-stream")})
            captured["detail"] = _poll_terminal(c, sub.json()["id"])

    await asyncio.to_thread(run)
    return captured["detail"]


@pytest.mark.asyncio
async def test_long_prepare_is_not_a_failure(
    tmp_path: Path, mqtt_broker: int, slow_prepare_mock: MockPrinter,
    ftps_server: tuple[int, Path], valid_gcode_3mf: bytes, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PREPARE outlasting the start window keeps the job preparing (2026-10-01)."""
    monkeypatch.setattr(jobs_mod, "_RUNNING_TIMEOUT_S", 0.4)   # printer prepares 1.2 s
    detail = await _submit_and_wait(tmp_path, mqtt_broker, ftps_server[0], valid_gcode_3mf, "prep")
    assert detail["job"]["state"] == "completed", detail["job"]
    changes = [e["payload"] for e in detail["events"] if e["event_type"] == "state_change"]
    assert {"from": "submitted", "to": "preparing", "trigger": "printer_preparing"} in changes
    assert sum(1 for c in changes if c["to"] == "preparing") == 1


@pytest.mark.asyncio
async def test_printer_failure_during_prepare_fails_the_job(
    tmp_path: Path, mqtt_broker: int, prepare_fails_mock: MockPrinter,
    ftps_server: tuple[int, Path], valid_gcode_3mf: bytes, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jobs_mod, "_RUNNING_TIMEOUT_S", 0.4)
    detail = await _submit_and_wait(
        tmp_path, mqtt_broker, ftps_server[0], valid_gcode_3mf, "prepfail")
    assert detail["job"]["state"] == "failed"
    assert detail["job"]["error_code"] == "printer_error"
