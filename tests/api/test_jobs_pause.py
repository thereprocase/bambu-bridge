"""A printer PAUSE is not a failed job (2026-09-24).

The P1S pauses with a non-zero ``print_error`` for things a person settles at
the printer — the nozzle-setting check before a print starts (0x0500803C,
seen on every masonry-key job: the printer's stored nozzle differs from the
file's), filament runout, a door — and waits. The bridge used to turn the
``error`` event of that pause into ``failed / printer_error`` 20 s after
``submitted``; the person resumed, the printer printed for twelve hours, and
the job row stayed failed. These replay that sequence over the mock printer
and pin the rules: a pause holds the job with no deadline (the
FED_NO_PROGRESS watchdog included) and logs ``printer_paused`` /
``printer_resumed``; the job completes normally after the resume; and real
failures (FAILED after a pause, a stop at the printer's screen, a refused job)
still fail.
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
    IDLE_PUSH_STATUS,
    SERIAL,
    MockPrinter,
    build_app,
    patch_discovery_ok,
)

_AUTH = {"Authorization": f"Bearer {API_KEY}"}
_NOZZLE_CHECK = 83918908  # 0x0500803C: the printer's nozzle setting differs from the file's


@pytest.fixture(autouse=True)
def _patched_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)


@pytest.fixture(autouse=True)
def _short_deadlines(monkeypatch: pytest.MonkeyPatch) -> None:
    """The start deadline far shorter than the scripted pauses (the feed
    deadline is set the same way through build_app): a pause that counted
    against either would fail the job."""
    monkeypatch.setattr(jobs_mod, "_RUNNING_TIMEOUT_S", 0.6)


async def _run_job(
    tmp_path: Path,
    mqtt_broker: int,
    ftps_server: tuple[int, Path],
    gcode_3mf: bytes,
    script: list[tuple[float, dict[str, Any]]],
) -> tuple[dict[str, Any], MockPrinter]:
    printer = MockPrinter("127.0.0.1", mqtt_broker, IDLE_PUSH_STATUS, script=script)
    await printer.start()
    ftps_port, _ = ftps_server
    app = build_app(
        tmp_path / "pause.db", mqtt_port=mqtt_broker, ftps_port=ftps_port, feed_deadline_s=0.6
    )
    captured: dict[str, Any] = {}

    def run() -> None:
        with TestClient(app) as c:
            assert (
                c.post(
                    "/api/v1/printers",
                    headers=_AUTH,
                    json={"host": "127.0.0.1", "access_code": ACCESS_CODE, "friendly_name": "P"},
                ).status_code
                == 201
            )
            sub = c.post(
                f"/api/v1/printers/{SERIAL}/jobs",
                headers=_AUTH,
                data={"ams_mapping": "1"},
                files={"file": ("keys.3mf", gcode_3mf, "application/octet-stream")},
            )
            assert sub.status_code == 201, sub.text
            job_id = sub.json()["id"]
            deadline = time.time() + 20
            detail: dict[str, Any] = {}
            while time.time() < deadline:
                detail = c.get(f"/api/v1/jobs/{job_id}", headers=_AUTH).json()
                if detail["job"]["state"] in ("completed", "failed", "canceled", "interrupted"):
                    break
                time.sleep(0.1)
            captured["detail"] = detail

    try:
        await asyncio.to_thread(run)
    finally:
        await printer.stop()
    return captured["detail"], printer


def _transitions(events: list[dict[str, Any]]) -> list[str]:
    return [e["payload"]["to"] for e in events if e["event_type"] == "state_change"]


def _of_type(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [e for e in events if e["event_type"] == kind]


def _stops(printer: MockPrinter) -> int:
    return sum(r.get("print", {}).get("command") == "stop" for r in printer.requests)


@pytest.mark.asyncio
async def test_nozzle_check_pause_then_resume_completes(
    tmp_path: Path,
    mqtt_broker: int,
    ftps_server: tuple[int, Path],
    valid_gcode_3mf: bytes,
) -> None:
    """Tonight's job, replayed: IDLE -> PAUSE (print_error 0x0500803C, layer 0,
    held longer than both deadlines) -> resume -> layers -> FINISH."""
    detail, printer = await _run_job(
        tmp_path,
        mqtt_broker,
        ftps_server,
        valid_gcode_3mf,
        [
            (0.05, {"gcode_state": "IDLE", "subtask_name": "keys"}),
            (
                0.1,
                {
                    "gcode_state": "PAUSE",
                    "print_error": _NOZZLE_CHECK,
                    "mc_print_stage": "3",
                    "layer_num": 0,
                    "hms": [],
                },
            ),
            (1.5, {"gcode_state": "RUNNING", "print_error": 0}),
            (0.2, {"layer_num": 1, "mc_percent": 10}),
            (0.2, {"layer_num": 2, "mc_percent": 60}),
            (0.2, {"gcode_state": "FINISH", "mc_percent": 100}),
        ],
    )
    job, events = detail["job"], detail["events"]
    assert job["state"] == "completed", detail
    assert job["error_code"] is None
    assert _transitions(events) == ["uploading", "submitted", "preparing", "printing", "completed"]
    [paused] = _of_type(events, "printer_paused")
    assert paused["payload"]["phase"] == "submitted"
    assert paused["payload"]["print_error"]["code"] == str(_NOZZLE_CHECK)
    assert paused["payload"]["print_error"]["hex"] == "0500803c"
    assert paused["payload"]["hms"] == []
    assert len(_of_type(events, "printer_resumed")) == 1
    [preparing] = [e for e in _of_type(events, "state_change") if e["payload"]["to"] == "preparing"]
    assert preparing["payload"]["trigger"] == "gcode_running_after_pause"
    assert _stops(printer) == 0


@pytest.mark.asyncio
async def test_pause_while_preparing_suspends_the_feed_watchdog(
    tmp_path: Path,
    mqtt_broker: int,
    ftps_server: tuple[int, Path],
    valid_gcode_3mf: bytes,
) -> None:
    """RUNNING, then a pause longer than BRIDGE_FEED_DEADLINE_S: no stop, no
    FED_NO_PROGRESS; the watchdog starts over on resume and the print completes."""
    detail, printer = await _run_job(
        tmp_path,
        mqtt_broker,
        ftps_server,
        valid_gcode_3mf,
        [
            (0.05, {"gcode_state": "RUNNING", "layer_num": 0, "subtask_name": "keys"}),
            (0.1, {"gcode_state": "PAUSE", "print_error": _NOZZLE_CHECK}),
            (1.5, {"gcode_state": "RUNNING", "print_error": 0}),
            (0.2, {"layer_num": 1}),
            (0.2, {"gcode_state": "FINISH", "mc_percent": 100}),
        ],
    )
    job, events = detail["job"], detail["events"]
    assert job["state"] == "completed", detail
    assert _transitions(events) == ["uploading", "submitted", "preparing", "printing", "completed"]
    assert [e["payload"]["phase"] for e in _of_type(events, "printer_paused")] == ["preparing"]
    assert _stops(printer) == 0


@pytest.mark.asyncio
async def test_pause_then_printer_failed_still_fails(
    tmp_path: Path,
    mqtt_broker: int,
    ftps_server: tuple[int, Path],
    valid_gcode_3mf: bytes,
) -> None:
    """A pause that ends in FAILED is a real failure: failed / printer_error,
    with the printer's error on the failing transition."""
    detail, _ = await _run_job(
        tmp_path,
        mqtt_broker,
        ftps_server,
        valid_gcode_3mf,
        [
            (0.05, {"gcode_state": "RUNNING", "layer_num": 0, "subtask_name": "keys"}),
            (0.1, {"gcode_state": "PAUSE", "print_error": _NOZZLE_CHECK}),
            (0.3, {"gcode_state": "FAILED"}),
        ],
    )
    job, events = detail["job"], detail["events"]
    assert job["state"] == "failed", detail
    assert job["error_code"] == "printer_error"
    assert len(_of_type(events, "printer_paused")) == 1
    assert not _of_type(events, "printer_resumed")
    failed = [e for e in _of_type(events, "state_change") if e["payload"]["to"] == "failed"]
    assert failed[-1]["payload"]["print_error"]["code"] == str(_NOZZLE_CHECK)


@pytest.mark.asyncio
async def test_pause_then_stopped_at_the_printer_fails(
    tmp_path: Path,
    mqtt_broker: int,
    ftps_server: tuple[int, Path],
    valid_gcode_3mf: bytes,
) -> None:
    """Paused, then stopped from the printer's screen (PAUSE -> IDLE): failed / printer_stopped."""
    detail, _ = await _run_job(
        tmp_path,
        mqtt_broker,
        ftps_server,
        valid_gcode_3mf,
        [
            (0.05, {"gcode_state": "PAUSE", "print_error": _NOZZLE_CHECK, "layer_num": 0}),
            (0.3, {"gcode_state": "IDLE", "print_error": 0}),
        ],
    )
    job = detail["job"]
    assert job["state"] == "failed", detail
    assert job["error_code"] == "printer_stopped"


@pytest.mark.asyncio
async def test_error_without_run_or_pause_is_a_refused_job(
    tmp_path: Path,
    mqtt_broker: int,
    ftps_server: tuple[int, Path],
    valid_gcode_3mf: bytes,
) -> None:
    """The printer reports an error and never runs or pauses: the job was refused.
    It fails at the start deadline as printer_error (not no_running_within_60s)."""
    detail, _ = await _run_job(
        tmp_path,
        mqtt_broker,
        ftps_server,
        valid_gcode_3mf,
        [(0.05, {"gcode_state": "IDLE", "print_error": 0x0500C011})],
    )
    job, events = detail["job"], detail["events"]
    assert job["state"] == "failed", detail
    assert job["error_code"] == "printer_error"
    assert _transitions(events) == ["uploading", "submitted", "failed"]
    assert not _of_type(events, "printer_paused")
