"""Owner enrollment: trust boundary, QR contract, cancellation and revocation."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from bambu_bridge.config import Settings
from bambu_bridge.main import create_app
from bambu_bridge.pairing import PairingStore, identity

OWNER = {"Authorization": "Bearer dashboard-test-owner"}


def test_dashboard_pairing_lifecycle_and_trust_boundary(tmp_path: Path) -> None:
    settings = Settings(
        bridge_db_path=str(tmp_path / "jobs.db"),
        bridge_pairing_dir=str(tmp_path / "pairing"),
        bridge_api_key="dashboard-test-owner",
        bridge_viz_token="dashboard-test-viewer",
        bridge_pairing_url="https://lan.invalid:8443/api/v1",
        bridge_pairing_remote_url="https://tailnet.invalid:8443/api/v1",
        bridge_log_level="error",
    )
    with TestClient(create_app(settings), base_url="https://proxy.invalid") as client:
        for path in ["options", "devices", "invitations/" + "a" * 64]:
            assert client.get("/api/v1/pairing/" + path).status_code == 401
            assert (
                client.get("http://proxy.invalid/api/v1/pairing/" + path, headers=OWNER).status_code
                == 403
            )
        assert client.post("/api/v1/pairing/invitations", json={}).status_code == 401
        assert (
            client.post(
                "http://proxy.invalid/api/v1/pairing/invitations", json={}, headers=OWNER
            ).status_code
            == 403
        )
        options = client.get("/api/v1/pairing/options", headers=OWNER)
        assert options.headers["cache-control"] == "no-store"
        assert [t["id"] for t in options.json()["targets"]] == ["local", "remote"]
        for target, expected in [
            ("local", settings.bridge_pairing_url),
            ("remote", settings.bridge_pairing_remote_url),
        ]:
            response = client.post(
                "/api/v1/pairing/invitations",
                json={"target": target},
                headers={
                    **OWNER,
                    "Host": "attacker.invalid",
                    "X-Forwarded-Host": "attacker.invalid",
                },
            )
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            invitation = response.json()
            payload = json.loads(invitation["payload"])
            assert payload["base_url"] == expected
            assert payload["spki"] == identity(tmp_path / "pairing")[2]
            assert "dashboard-test-owner" not in response.text
            assert "<svg" in invitation["qr_svg"] and "<script" not in invitation["qr_svg"]
            path = "/api/v1/pairing/invitations/" + invitation["id"]
            assert client.get(path, headers=OWNER).json() == {"active": True}
            if target == "local":
                assert client.delete(path, headers=OWNER).status_code == 204
                assert client.get(path, headers=OWNER).json() == {"active": False}
                assert (
                    client.post(
                        "/api/v1/pairing/claim",
                        json={"secret": payload["secret"], "name": "Cancelled phone"},
                    ).status_code
                    == 401
                )
            else:
                claimed = client.post(
                    "/api/v1/pairing/claim",
                    json={"secret": payload["secret"], "name": "Dashboard phone"},
                )
                assert claimed.status_code == 200
                device = claimed.json()
                assert client.get(path, headers=OWNER).json() == {"active": False}
                auth = {"Authorization": "Bearer " + device["token"]}
                for token in [device["token"], "dashboard-test-viewer"]:
                    assert (
                        client.post(
                            "/api/v1/pairing/invitations",
                            json={},
                            headers={"Authorization": "Bearer " + token},
                        ).status_code
                        == 401
                    )
                assert client.get("/api/v1/printers", headers=auth).status_code == 200
                rows = client.get("/api/v1/pairing/devices", headers=OWNER).json()
                assert rows[0]["name"] == "Dashboard phone"
                assert (
                    client.delete(
                        "/api/v1/pairing/devices/" + device["device_id"], headers=OWNER
                    ).status_code
                    == 204
                )
                assert client.get("/api/v1/printers", headers=auth).status_code == 401


def test_unconfigured_remote_and_invalid_targets_do_not_mint_codes(tmp_path: Path) -> None:
    settings = Settings(
        bridge_db_path=str(tmp_path / "jobs.db"),
        bridge_pairing_dir=str(tmp_path / "pairing"),
        bridge_api_key="dashboard-test-owner",
        bridge_log_level="error",
        bridge_pairing_url="https://lan.invalid:8443/api/v1",
    )
    with TestClient(create_app(settings), base_url="https://proxy.invalid") as client:
        for target, status in [("remote", 409), ("https://attacker.invalid/api/v1", 422)]:
            assert (
                client.post(
                    "/api/v1/pairing/invitations", json={"target": target}, headers=OWNER
                ).status_code
                == status
            )
        store = PairingStore(tmp_path / "pairing")
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM invitations").fetchone()[0] == 0
