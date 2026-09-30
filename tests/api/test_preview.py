from types import SimpleNamespace

from fastapi.testclient import TestClient

from tests.conftest import API_KEY, build_app


def test_preview_status_and_retry_auth(tmp_path, monkeypatch):
    app = build_app(tmp_path / "preview.db")
    retried = []
    gateway = SimpleNamespace(
        config={"printer_id": "fixture"},
        preview_status=lambda: {"state": "retrying", "frames": 12, "total_frames": 360},
        video_overlay=SimpleNamespace(shapes=SimpleNamespace(retry=lambda: retried.append(True))),
    )
    with TestClient(app, base_url="https://testserver") as client:
        app.state.native_gateway = gateway
        monkeypatch.setattr(app.state.settings, "bridge_viz_token", "fixture-viewer")
        endpoint = "/api/v1/printers/fixture/viz/preview"
        assert client.get(endpoint).status_code == 401
        assert (
            client.get(endpoint, headers={"Authorization": "Bearer fixture-viewer"}).json()[
                "frames"
            ]
            == 12
        )
        assert (
            client.post(
                endpoint + "/retry", headers={"Authorization": "Bearer fixture-viewer"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                endpoint + "/retry", headers={"Authorization": f"Bearer {API_KEY}"}
            ).status_code
            == 200
        )
        # Granted Android phones are managers; retry accepts paired HTTPS auth.
        monkeypatch.setattr(
            app.state,
            "pairing",
            SimpleNamespace(authenticate=lambda token: token == "fixture-phone"),
        )
        assert (
            client.post(
                endpoint + "/retry", headers={"Authorization": "Bearer fixture-phone"}
            ).status_code
            == 200
        )
        assert len(retried) == 2
        assert (
            client.get(
                endpoint.replace("fixture", "other"), headers={"Authorization": f"Bearer {API_KEY}"}
            ).status_code
            == 404
        )
        app.state.native_gateway = None
