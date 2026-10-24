"""Every bridge print start passes the same printer gate (capabilities.require_start)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from bambu_bridge.api.capabilities import require_start
from bambu_bridge.config import Settings
from bambu_bridge.db.jobs import Printer, PrinterRepo
from bambu_bridge.main import create_app
from bambu_bridge.protocol.ftps import FtpsTransfer
from tests.api.test_orca import SLICE

OWNER = {"Authorization": "Bearer gate-fixture-owner"}
SERIAL = "GATE_TEST_P1S"


def printer(
    *,
    state: str = "IDLE",
    model: str = "P1S",
    cert: str = "trusted",
    age_s: float = 0,
    nozzle: Any = "0.4",
) -> SimpleNamespace:
    stamp = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()
    snapshot = {
        "model": model,
        "_raw": {"gcode_state": state, "nozzle_diameter": nozzle},
        "session": {"connected": True, "last_telemetry_at": stamp},
    }
    return SimpleNamespace(
        serial=SERIAL,
        connected=True,
        ip="127.0.0.1",
        access_code="mock-only",
        cert_status=cert,
        expected_fingerprint="a" * 64,
        current_fingerprint="b" * 64,
        snapshot=lambda: snapshot,
    )


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    app = create_app(
        Settings(
            bridge_api_key="gate-fixture-owner",
            bridge_log_level="error",
            bridge_db_path=str(tmp_path / "jobs.db"),
            bridge_pairing_dir=str(tmp_path / "pairing"),
        )
    )
    with TestClient(app, base_url="https://bridge.invalid") as client:
        client.app.state.printer = printer()
        monkeypatch.setattr(app.state.registry, "get", lambda _id: client.app.state.printer)
        job = SimpleNamespace(id="job", model_dump=lambda **_: {"id": "job"})
        monkeypatch.setattr(app.state.jobs, "submit", AsyncMock(return_value=job))
        monkeypatch.setattr(app.state.jobs, "history", AsyncMock(return_value=[]))
        monkeypatch.setattr(FtpsTransfer, "download_bytes", AsyncMock(return_value=SLICE))
        yield client


def start_jobs(client: TestClient) -> Any:
    return client.post(
        f"/api/v1/printers/{SERIAL}/jobs",
        headers=OWNER,
        files={"file": ("cube.gcode.3mf", SLICE, "application/octet-stream")},
    )


def start_queue(client: TestClient) -> Any:
    row = Printer(id=SERIAL, friendly_name="Gate", ip="127.0.0.1", access_code="x" * 8, added_at=0)
    client.portal.call(PrinterRepo(client.app.state.db).add, row)  # queue rows need a printer
    item = client.post(
        f"/api/v1/printers/{SERIAL}/queue",
        headers=OWNER,
        json={"file_path": "/cube.gcode.3mf", "file_name": "cube.gcode.3mf"},
    )
    assert item.status_code == 201, item.text
    return client.post(f"/api/v1/queue/{item.json()['id']}/start", headers=OWNER)


def start_orca(client: TestClient) -> Any:
    created = client.post(
        "/api/v1/orca/clients",
        headers=OWNER,
        json={"name": "Desktop", "printer_id": SERIAL, "ams_mapping": [0]},
    )
    assert created.status_code == 201, created.text
    return client.post(
        f"/orca/{SERIAL}/api/files/local",
        headers={"X-Api-Key": created.json()["token"]},
        data={"print": "true", "path": "", "plateindex": "1"},
        files={"file": ("cube.gcode.3mf", SLICE, "application/octet-stream")},
    )


ROUTES: dict[str, Callable[[TestClient], Any]] = {
    "jobs": start_jobs,
    "queue": start_queue,
    "orca": start_orca,
}

REFUSED = {
    "changed certificate": (printer(cert="changed"), 403),
    "not a P1S": (printer(model="X1C"), 409),
    "stale status": (printer(age_s=600), 409),
    "printing": (printer(state="RUNNING"), 409),
    "preparing": (printer(state="PREPARE"), 409),
}


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("case", REFUSED)
def test_every_start_route_refuses_the_same_printer_states(
    app_client: TestClient, route: str, case: str
) -> None:
    app_client.app.state.printer, status = REFUSED[case]
    response = ROUTES[route](app_client)
    assert response.status_code == status, response.text
    if route != "orca" and status == 403:
        assert response.json()["error"] == "printer_cert_changed"
    app_client.app.state.jobs.submit.assert_not_awaited()


@pytest.mark.parametrize("state", ["IDLE", "FINISH", "FAILED"])
def test_orca_print_passes_the_gate_from_restartable_states(
    app_client: TestClient, state: str
) -> None:
    app_client.app.state.printer = printer(state=state)
    assert start_orca(app_client).status_code == 201
    app_client.app.state.jobs.submit.assert_awaited_once()


@pytest.mark.parametrize("nozzle", [None, 0, "0", ""])
def test_unknown_printer_nozzle_is_assumed_to_match_like_orca(nozzle: Any) -> None:
    # OrcaSlicer _is_same_nozzle_diameters: "Assume matching if diameter is unknown".
    assert require_start(printer(nozzle=nozzle)) is None


def test_reported_nozzle_is_returned_for_the_slice_check() -> None:
    assert require_start(printer(nozzle="0.6")) == 0.6


def test_changed_certificate_is_checked_before_anything_else() -> None:
    with pytest.raises(HTTPException) as caught:
        require_start(printer(cert="changed", model="X1C", state="RUNNING", age_s=600))
    assert caught.value.status_code == 403
    assert caught.value.detail["error"] == "printer_cert_changed"
