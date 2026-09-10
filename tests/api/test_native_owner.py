"""Native gateway setup remains owner-only and HTTPS-only."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from bambu_bridge.config import Settings
from bambu_bridge.main import create_app


def test_native_owner_boundary_and_no_store(tmp_path):
    app = create_app(
        Settings(
            bridge_api_key="native-fixture-owner",
            bridge_db_path=str(tmp_path / "jobs.db"),
            bridge_pairing_dir=str(tmp_path / "pairing"),
        )
    )
    owner = {"Authorization": "Bearer native-fixture-owner"}
    with TestClient(app, base_url="https://bridge.invalid") as client:
        gateway = SimpleNamespace(
            status=lambda: {"configured": True, "enabled": False},
            saved_code=lambda: "FIXTURE1",
            enable=AsyncMock(return_value={"access_code": "FIXTURE1", "enabled": True}),
            disable=AsyncMock(),
            announce=AsyncMock(),
            close=AsyncMock(),
        )
        app.state.native_gateway = gateway
        app.state.registry.get = lambda _: SimpleNamespace(model="P1S")
        for method, path in [
            ("GET", "/native"),
            ("GET", "/native/access-code"),
            ("POST", "/native"),
            ("DELETE", "/native"),
            ("POST", "/native/announce"),
        ]:
            kwargs = {"json": {"printer_id": "FIXTURE"}} if method == "POST" else {}
            assert client.request(method, "/api/v1" + path, **kwargs).status_code == 401
            assert (
                client.request(
                    method, "http://bridge.invalid/api/v1" + path, headers=owner, **kwargs
                ).status_code
                == 403
            )
        response = client.post("/api/v1/native", headers=owner, json={"printer_id": "FIXTURE"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "access_code" not in client.get("/api/v1/native", headers=owner).json()
        for _ in range(2):
            saved = client.get("/api/v1/native/access-code", headers=owner)
            assert saved.json() == {"access_code": "FIXTURE1"}
            assert saved.headers["cache-control"] == "no-store"
        invitation, _ = app.state.pairing.invite()
        phone = app.state.pairing.claim(invitation, "Fixture phone")
        assert phone is not None
        assert (
            client.get(
                "/api/v1/native/access-code", headers={"Authorization": "Bearer " + phone["token"]}
            ).status_code
            == 401
        )
        assert client.delete("/api/v1/native", headers=owner).status_code == 204
        gateway.disable.assert_awaited_once()
        app.state.registry.get = lambda _: SimpleNamespace(model="A1")
        assert (
            client.post("/api/v1/native", headers=owner, json={"printer_id": "FIXTURE"}).status_code
            == 422
        )
