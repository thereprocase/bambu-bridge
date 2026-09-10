"""Orca wire contract, least-privilege keys, and upload/print separation."""

from __future__ import annotations

import asyncio
import io
import zipfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from bambu_bridge.config import Settings
from bambu_bridge.log_redaction import redact
from bambu_bridge.main import create_app
from bambu_bridge.protocol.ftps import FtpsTransfer
from bambu_bridge.service.registry import PrinterNotFoundError
from tests.conftest import ACCESS_CODE
from tests.slicedoc.test_validate_source_agnostic import _SINGLE_PETG, _container

OWNER = {"Authorization": "Bearer orca-fixture-owner"}
SERIAL = "ORCA_TEST_PRINTER"
HOST = f"/orca/{SERIAL}"
MANAGE = "/api/v1/orca/clients"
SLICE = _container(_SINGLE_PETG, b"M620 S0A\nT0\nM621 S0A\nM104 S220\n")
REAL_UPLOAD = FtpsTransfer.upload_bytes


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    app = create_app(
        Settings(
            bridge_api_key="orca-fixture-owner",
            bridge_log_level="error",
            bridge_db_path=str(tmp_path / "jobs.db"),
            bridge_pairing_dir=str(tmp_path / "pairing"),
        )
    )
    with TestClient(app, base_url="https://bridge.invalid") as c:
        service = SimpleNamespace(
            connected=True,
            ip="127.0.0.1",
            access_code="mock-only",
            summary=lambda: {"gcode_state": "IDLE"},
        )

        def get(printer_id: str) -> SimpleNamespace:
            if printer_id != SERIAL:
                raise PrinterNotFoundError(printer_id)
            return service

        monkeypatch.setattr(app.state.registry, "get", get)
        monkeypatch.setattr(
            app.state.jobs, "submit", AsyncMock(return_value=SimpleNamespace(id="job"))
        )
        monkeypatch.setattr(app.state.jobs, "history", AsyncMock(return_value=[]))
        monkeypatch.setattr(FtpsTransfer, "upload_bytes", AsyncMock(return_value="/uploaded"))
        yield c


def key(c: TestClient, mapping: list[int] | None = None) -> tuple[str, dict[str, str]]:
    res = c.post(
        MANAGE,
        headers=OWNER,
        json={"name": "Desktop", "printer_id": SERIAL, "ams_mapping": mapping},
    )
    assert res.status_code == 201, res.text
    assert res.headers["cache-control"] == "no-store"
    assert res.json()["host_path"] == HOST
    return res.json()["id"], {"X-Api-Key": res.json()["token"]}


def send(
    c: TestClient,
    auth: dict[str, str],
    *,
    data: bytes = SLICE,
    fields: dict[str, str] | None = None,
    name: str = "cube.gcode.3mf",
):
    return c.post(
        HOST + "/api/files/local",
        headers=auth,
        data={"print": "false", "path": "", "plateindex": "1", **(fields or {})},
        files={"file": (name, data, "application/octet-stream")},
    )


def test_key_scope_https_revocation_and_hash_at_rest(client: TestClient) -> None:
    cid, auth = key(client)
    token = auth["X-Api-Key"]
    for path in [MANAGE, HOST + "/api/version"]:
        assert client.get(path).status_code == 401
        assert (
            client.get("http://bridge.invalid" + path, headers={**OWNER, **auth}).status_code == 403
        )
    assert client.get(HOST + "/api/version", headers=OWNER).status_code == 401
    assert (
        client.get(HOST + "/api/version", headers={"X-Api-Key": "orca-fixture-owner"}).status_code
        == 401
    )
    assert client.get(HOST + "/api/version?apikey=" + token).status_code == 401
    assert client.get("/orca/WRONG/api/version", headers=auth).status_code == 401
    assert (
        client.get("/api/v1/printers", headers={"Authorization": "Bearer " + token}).status_code
        == 401
    )
    assert client.post(MANAGE, headers=auth, json={}).status_code == 401
    response = client.get(HOST + "/api/version", headers=auth)
    assert response.status_code == 200
    assert response.json()["api"] == "0.1"
    assert response.json()["text"].startswith("OctoPrint")
    assert response.headers["cache-control"] == "no-store"
    listing = client.get(MANAGE, headers=OWNER)
    assert token not in listing.text and "hash" not in listing.text
    assert token.encode() not in client.app.state.pairing.path.read_bytes()
    assert token not in redact("X-Api-Key: " + token)
    client.delete(MANAGE + "/" + cid, headers=OWNER)
    assert client.get(HOST + "/api/version", headers=auth).status_code == 401


def test_upload_only_transfers_exact_bytes_never_starts(client: TestClient) -> None:
    _, auth = key(client)
    response = send(client, auth)
    assert response.status_code == 201, response.text
    name = response.json()["files"]["local"]["name"]
    assert name.startswith("cube-") and name.endswith(".gcode.3mf")
    FtpsTransfer.upload_bytes.assert_awaited_once_with(SLICE, name, remote_dir="")
    client.app.state.jobs.submit.assert_not_awaited()
    assert send(client, auth, fields={"print": "true"}).status_code == 403
    client.app.state.jobs.submit.assert_not_awaited()


def test_changed_printer_certificate_blocks_upload(client: TestClient) -> None:
    _, auth = key(client)
    service = client.app.state.registry.get(SERIAL)
    service.cert_status = "changed"
    service.serial = SERIAL
    service.expected_fingerprint = "old"
    service.current_fingerprint = "new"
    assert send(client, auth).status_code == 403
    FtpsTransfer.upload_bytes.assert_not_awaited()
    client.app.state.jobs.submit.assert_not_awaited()


def test_print_uses_existing_jobs_with_explicit_mapping(client: TestClient) -> None:
    _, auth = key(client, [2])
    response = send(client, auth, fields={"print": "true"})
    assert response.status_code == 201, response.text
    assert response.json()["bridge_state"] == "queued"
    client.app.state.jobs.submit.assert_awaited_once_with(
        SERIAL, SLICE, response.json()["files"]["local"]["name"], ams_mapping=[2]
    )
    FtpsTransfer.upload_bytes.assert_not_awaited()


@pytest.mark.parametrize(
    "fields,data,name,status",
    [
        ({"plateindex": "2"}, SLICE, "cube.gcode.3mf", 422),
        ({"plateindex": "0"}, SLICE, "cube.gcode.3mf", 422),
        ({"path": "../cache"}, SLICE, "cube.gcode.3mf", 422),
        ({"print": "perhaps"}, SLICE, "cube.gcode.3mf", 422),
        ({}, b"not a slice", "cube.gcode.3mf", 422),
        ({}, SLICE, "../cube.gcode.3mf", 422),
        ({}, SLICE, "unsafe:name.gcode.3mf", 422),
        ({}, SLICE, "cube.3mf", 422),
        ({}, _container(_SINGLE_PETG, b"M104 S999\n"), "cube.gcode.3mf", 422),
    ],
)
def test_bad_uploads_never_touch_printer(client, fields, data, name, status):
    _, auth = key(client, [0])
    response = send(client, auth, data=data, fields=fields, name=name)
    assert response.status_code == status, response.text
    FtpsTransfer.upload_bytes.assert_not_awaited()
    client.app.state.jobs.submit.assert_not_awaited()


def test_multiple_plates_and_wrong_mapping_rejected(client: TestClient) -> None:
    _, auth = key(client, [0, 1])
    assert send(client, auth, fields={"print": "true"}).status_code == 422
    buf = io.BytesIO(SLICE)
    with zipfile.ZipFile(buf, "a") as archive:
        archive.writestr("Metadata/plate_2.gcode", "M104 S220\n")
    assert send(client, auth, data=buf.getvalue()).status_code == 422
    client.app.state.jobs.submit.assert_not_awaited()


def test_busy_offline_and_transfer_failure(client: TestClient) -> None:
    _, auth = key(client, [0])
    service = client.app.state.registry.get(SERIAL)
    service.connected = False
    assert send(client, auth).status_code == 409
    service.connected = True
    service.summary = lambda: {"gcode_state": "RUNNING"}
    assert send(client, auth, fields={"print": "true"}).status_code == 409
    service.summary = lambda: {"gcode_state": "IDLE"}
    client.app.state.jobs.history.return_value = [
        SimpleNamespace(state=SimpleNamespace(terminal=False))
    ]
    assert send(client, auth, fields={"print": "true"}).status_code == 409
    FtpsTransfer.upload_bytes.side_effect = RuntimeError("private-printer-detail")
    response = send(client, auth)
    assert response.status_code == 502 and "private-printer-detail" not in response.text
    client.app.state.jobs.submit.assert_not_awaited()


def test_external_spool_and_invalid_client_config(client: TestClient) -> None:
    _, auth = key(client, [])
    assert send(client, auth, fields={"print": "true"}).status_code == 201
    assert client.app.state.jobs.submit.call_args.kwargs["ams_mapping"] is None
    for mapping in [[-1], [16], list(range(17))]:
        assert (
            client.post(
                MANAGE,
                headers=OWNER,
                json={"name": "bad", "printer_id": SERIAL, "ams_mapping": mapping},
            ).status_code
            == 422
        )


async def test_actual_ftps_upload_without_print(client, ftps_server, monkeypatch) -> None:
    port, root = ftps_server
    client.app.state.ftps_port = port
    client.app.state.registry.get(SERIAL).access_code = ACCESS_CODE
    monkeypatch.setattr(FtpsTransfer, "upload_bytes", REAL_UPLOAD)
    _, auth = key(client)
    response = await asyncio.to_thread(send, client, auth)
    assert response.status_code == 201, response.text
    name = response.json()["files"]["local"]["name"]
    assert (root / name).read_bytes() == SLICE
    client.app.state.jobs.submit.assert_not_awaited()
