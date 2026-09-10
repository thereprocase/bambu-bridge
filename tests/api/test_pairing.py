from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bambu_bridge.api.auth import check_ws_token
from bambu_bridge.config import Settings
from bambu_bridge.main import create_app
from bambu_bridge.pairing import PairingStore, identity, invitation_payload, validate_base
from bambu_bridge.service.events import EventBus


def test_invitation_is_single_use_under_concurrency(tmp_path: Path) -> None:
    store = PairingStore(tmp_path)
    secret, _ = store.invite()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.claim(secret, "Phone"), range(8)))
    assert sum(r is not None for r in results) == 1
    result = next(r for r in results if r)
    assert store.authenticate(result["token"]) == result["device_id"]
    assert result["token"].encode() not in store.path.read_bytes()
    assert secret.encode() not in store.path.read_bytes()
    assert store.revoke(result["device_id"])
    assert store.authenticate(result["token"]) is None


def test_expired_invitation_cannot_enroll(tmp_path: Path) -> None:
    store = PairingStore(tmp_path)
    secret, _ = store.invite()
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE invitations SET expires=0")
    assert store.claim(secret, "Phone") is None
    assert store.devices() == []


def test_identity_persists_and_missing_key_fails_closed(tmp_path: Path) -> None:
    key, cert, pin = identity(tmp_path)
    before = key.read_bytes()
    assert identity(tmp_path)[2] == pin
    cert.unlink()  # renewing leaf certificate retains the paired public key
    assert identity(tmp_path)[2] == pin
    assert key.read_bytes() == before
    key.unlink()
    with pytest.raises(ValueError, match="key missing"):
        identity(tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "http://bridge/api/v1",
        "https://u:p@bridge/api/v1",
        "https://bridge/api/v1?token=x",
        "https://bridge/api/v1#x",
        "https://bridge/app",
    ],
)
def test_pairing_url_rejects_unsafe_forms(url: str) -> None:
    with pytest.raises(ValueError):
        validate_base(url)


def test_qr_contains_only_invitation_not_owner_or_device_key(tmp_path: Path) -> None:
    store = PairingStore(tmp_path)
    data = json.loads(invitation_payload(store, "https://bridge.invalid:8443/api/v1"))
    assert set(data) == {"type", "version", "base_url", "spki", "secret", "expires"}
    assert len(data["spki"]) == 44


def test_http_rejected_and_device_permissions_revocable(tmp_path: Path) -> None:
    settings = Settings(
        bridge_db_path=str(tmp_path / "jobs.db"),
        bridge_api_key="owner-key",
        bridge_pairing_dir=str(tmp_path / "security"),
        bridge_log_level="error",
    )
    store = PairingStore(settings.bridge_pairing_dir)
    secret, _ = store.invite()
    app = create_app(settings)
    with TestClient(app, base_url="https://bridge.invalid") as client:
        body = {"secret": secret, "name": "Phone"}
        assert (
            client.post("http://bridge.invalid/api/v1/pairing/claim", json=body).status_code == 403
        )
        response = client.post("/api/v1/pairing/claim", json=body)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        device = response.json()
        auth = {"Authorization": "Bearer " + device["token"]}
        assert client.get("/api/v1/printers", headers=auth).status_code == 200
        assert client.get("http://bridge.invalid/api/v1/printers", headers=auth).status_code == 401
        assert (
            client.get(
                "http://bridge.invalid/api/v1/printers",
                headers={**auth, "X-Forwarded-Proto": "https"},
            ).status_code
            == 401
        )
        assert client.get("/api/v1/pairing/devices", headers=auth).status_code == 401
        assert client.post("/api/v1/pairing/claim", json=body).status_code == 401
        assert check_ws_token(device["token"], settings, store, secure=True)
        assert not check_ws_token(device["token"], settings, store, secure=False)
        assert client.delete("/api/v1/pairing/self", headers=auth).status_code == 204
        assert client.get("/api/v1/printers", headers=auth).status_code == 401
        assert not check_ws_token(device["token"], settings, store, secure=True)
        owner = {"Authorization": "Bearer owner-key"}
        assert client.get("http://bridge.invalid/api/v1/printers", headers=owner).status_code == 200
        assert client.get("/api/v1/pairing/devices", headers=owner).json()[0]["revoked"]


def test_pairing_works_without_legacy_owner_key(tmp_path: Path) -> None:
    settings = Settings(
        bridge_db_path=str(tmp_path / "jobs.db"),
        bridge_api_key="",
        bridge_pairing_dir=str(tmp_path / "security"),
        bridge_log_level="error",
    )
    store = PairingStore(settings.bridge_pairing_dir)
    secret, _ = store.invite()
    with TestClient(create_app(settings), base_url="https://bridge.invalid") as client:
        token = client.post(
            "/api/v1/pairing/claim", json={"secret": secret, "name": "Phone"}
        ).json()["token"]
        assert (
            client.get("/api/v1/printers", headers={"Authorization": "Bearer " + token}).status_code
            == 200
        )


def test_revocation_closes_an_already_open_secure_websocket(tmp_path: Path) -> None:
    from starlette.websockets import WebSocketDisconnect

    settings = Settings(
        bridge_db_path=str(tmp_path / "jobs.db"),
        bridge_pairing_dir=str(tmp_path / "security"),
        bridge_log_level="error",
    )
    app = create_app(settings)
    store = PairingStore(settings.bridge_pairing_dir)
    secret, _ = store.invite()
    device = store.claim(secret, "Phone")
    assert device
    with TestClient(app, base_url="https://bridge.invalid") as client:
        service = SimpleNamespace(bus=EventBus(), snapshot=lambda: {"connected": True})
        app.state.registry = SimpleNamespace(get=lambda _: service)
        with client.websocket_connect(
            "wss://bridge.invalid/api/v1/printers/test/status",
            headers={"Authorization": "Bearer " + device["token"]},
        ) as ws:
            assert ws.receive_json()["type"] == "hello"
            assert ws.receive_json()["type"] == "snapshot"
            store.revoke(device["device_id"])
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 1008


def test_validation_does_not_echo_pairing_secrets(tmp_path: Path) -> None:
    settings = Settings(
        bridge_db_path=str(tmp_path / "jobs.db"),
        bridge_pairing_dir=str(tmp_path / "security"),
        bridge_log_level="error",
    )
    with TestClient(create_app(settings), base_url="https://bridge.invalid") as client:
        secret = "private-invitation-that-must-not-be-echoed"
        response = client.post("/api/v1/pairing/claim", json={"secret": secret, "name": ""})
        assert response.status_code == 422
        assert secret not in response.text


def test_console_pairing_uses_service_environment_and_keeps_code_private(tmp_path: Path) -> None:
    import html
    import re

    security = tmp_path / "service-security"
    envfile = tmp_path / "bridge.env"
    envfile.write_text(f"BRIDGE_PAIRING_DIR={security}\n")
    output = tmp_path / "pair.html"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "bambu_bridge.main",
            "--env-file",
            str(envfile),
            "pair",
            "--url",
            "https://bridge.invalid:8443/api/v1",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    page = output.read_text()
    code = json.loads(html.unescape(re.search(r"<pre[^>]*>(.*?)</pre>", page).group(1)))
    assert code["secret"] not in result.stdout + result.stderr
    assert PairingStore(security).claim(code["secret"], "Phone")
    assert "<svg" in page and "<script" not in page
    assert output.stat().st_mode & 0o077 == 0
