from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from bambu_bridge.config import Settings
from bambu_bridge.main import create_app
from tests.test_library import manifest

OWNER = {"Authorization": "Bearer library-owner-fixture"}
BASE = "/orca/FIXTURE/library/captures"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    app = create_app(
        Settings(
            bridge_api_key="library-owner-fixture",
            bridge_log_level="error",
            bridge_db_path=str(tmp_path / "jobs.db"),
            bridge_pairing_dir=str(tmp_path / "pairing"),
            bridge_library_dir=str(tmp_path / "library"),
        )
    )
    with TestClient(app, base_url="https://bridge.invalid") as c:
        monkeypatch.setattr(app.state.registry, "get", lambda _: SimpleNamespace())
        monkeypatch.setattr(app.state.jobs, "submit", AsyncMock())
        yield c
        app.state.jobs.submit.assert_not_called()


def key(c: TestClient) -> tuple[str, dict[str, str]]:
    response = c.post(
        "/api/v1/orca/clients",
        headers=OWNER,
        json={
            "name": "Library test",
            "printer_id": "FIXTURE",
            "ams_mapping": None,
        },
    )
    assert response.status_code == 201
    return response.json()["id"], {"X-Api-Key": response.json()["token"]}


def test_scoped_ingest_and_authenticated_download(client: TestClient) -> None:
    _, auth = key(client)
    data = b"exact original"
    capture = manifest(data)
    assert client.post(BASE, json=capture.model_dump(mode="json")).status_code == 401
    response = client.post(BASE, headers=auth, json=capture.model_dump(mode="json"))
    assert response.status_code == 201, response.text
    cid = capture.id
    assert (
        client.put(f"{BASE}/{cid}/files/part.step?offset=0", headers=auth, content=data).status_code
        == 200
    )
    assert client.post(f"{BASE}/{cid}/finalize", headers=auth).status_code == 200
    path = f"/api/v1/library/captures/{cid}/files/part.step"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=auth).status_code == 401
    response = client.get(path, headers=OWNER)
    assert response.status_code == 200 and response.content == data
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment")
    assert client.get("/api/v1/library/captures", headers=OWNER).json()[0]["id"] == cid


def test_other_client_cannot_access_capture_and_revocation_takes_effect(client: TestClient) -> None:
    identity, auth = key(client)
    _, other = key(client)
    capture = manifest(b"private")
    client.post(BASE, headers=auth, json=capture.model_dump(mode="json"))
    assert client.get(BASE, headers=auth).json()[0]["id"] == capture.id
    assert client.get(BASE, headers=other).json() == []
    assert client.get(f"{BASE}/{capture.id}", headers=other).status_code == 404
    assert client.post(f"{BASE}/{capture.id}/finalize", headers=other).status_code == 404
    assert client.delete(f"/api/v1/orca/clients/{identity}", headers=OWNER).status_code == 204
    assert client.get(f"{BASE}/{capture.id}", headers=auth).status_code == 401


def test_plain_http_ingest_is_rejected(client: TestClient) -> None:
    _, auth = key(client)
    assert (
        client.post(
            "http://bridge.invalid" + BASE,
            headers=auth,
            json=manifest(b"x").model_dump(mode="json"),
        ).status_code
        == 403
    )


def test_owner_maintenance_and_pending_download_gate(client: TestClient) -> None:
    _, auth = key(client)
    capture = manifest(b"waiting")
    client.post(BASE, headers=auth, json=capture.model_dump(mode="json"))
    assert (
        client.get(
            f"/api/v1/library/captures/{capture.id}/files/part.step", headers=OWNER
        ).status_code
        == 404
    )
    assert client.post("/api/v1/library/verify", headers=OWNER).json()["ok"]
    assert client.delete(f"/api/v1/library/captures/{capture.id}", headers=auth).status_code == 401
    assert client.delete(f"/api/v1/library/captures/{capture.id}", headers=OWNER).status_code == 204
    assert client.get(f"/api/v1/library/captures/{capture.id}", headers=OWNER).status_code == 404
