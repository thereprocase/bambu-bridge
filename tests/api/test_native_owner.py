"""Native gateway setup remains owner-only and HTTPS-only."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
            authenticate=AsyncMock(return_value=True),
            enable=AsyncMock(return_value={"access_code": "FIXTURE1", "enabled": True}),
            disable=AsyncMock(),
            announce=AsyncMock(),
            setup_status=lambda _: {"printer_connected": False, "camera_streaming": False},
            close=AsyncMock(),
        )
        app.state.native_gateway = gateway
        app.state.registry.get = lambda _: SimpleNamespace(model="P1S")
        for method, path in [
            ("GET", "/native"),
            ("GET", "/native/access-code"),
            ("GET", "/native/setup"),
            ("GET", "/native/setup-status"),
            ("POST", "/native/access-code"),
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
        with patch("bambu_bridge.api.native.setup_material", return_value={"script": "fixture"}):
            setup = client.get("/api/v1/native/setup", headers=owner)
            assert setup.status_code == 200 and setup.json() == {"script": "fixture"}
            assert setup.headers["cache-control"] == "no-store"
            assert setup.headers["referrer-policy"] == "no-referrer"
        check = client.get("/api/v1/native/setup-status", headers=owner)
        assert check.json() == {"printer_connected": False, "camera_streaming": False}
        assert check.headers["cache-control"] == "no-store"
        saved = client.post(
            "/api/v1/native/access-code", headers=owner, json={"access_code": "FIXTURE1"}
        )
        assert saved.json() == {"access_code": "FIXTURE1"}
        assert saved.headers["cache-control"] == "no-store"
        gateway.authenticate.return_value = False
        rejected = client.post(
            "/api/v1/native/access-code", headers=owner, json={"access_code": "WRONG123"}
        )
        assert rejected.status_code == 422 and "WRONG123" not in rejected.text
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
        for path in ["/native/setup", "/native/setup-status"]:
            assert (
                client.get(
                    "/api/v1" + path, headers={"Authorization": "Bearer " + phone["token"]}
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
