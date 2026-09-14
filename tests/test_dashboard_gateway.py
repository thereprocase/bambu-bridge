from __future__ import annotations

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bambu_bridge.dashboard_gateway import DashboardGateway

ORIGIN = "https://bridge.example.ts.net"
HEADERS = {"Tailscale-User-Login": "owner@example.com", "X-Forwarded-Proto": "https"}


def gateway() -> DashboardGateway:
    app = FastAPI()

    @app.get("/api/test")
    @app.post("/api/test")
    def probe(request: Request) -> dict[str, object]:
        return {
            "authorized": request.headers.get("authorization") == "Bearer owner-secret",
            "scheme": request.url.scheme,
            "query": request.url.query,
        }

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        await socket.send_json(
            {
                "authorized": socket.headers.get("authorization") == "Bearer owner-secret",
                "query": socket.url.query,
                "scheme": socket.url.scheme,
            }
        )
        await socket.close()

    return DashboardGateway(app, ORIGIN, "owner@example.com", "owner-secret")


def test_session_and_authorized_http_do_not_return_key() -> None:
    with TestClient(gateway(), base_url=ORIGIN, client=("127.0.0.1", 5000)) as client:
        response = client.get("/app/session", headers=HEADERS)
        assert response.json() == {"connected": True, "authentication": "tailscale"}
        assert "owner-secret" not in response.text
        assert response.headers["cache-control"] == "no-store"
        # Clicking a link from another site must open the dashboard normally.
        assert (
            client.get(
                "/app/session",
                headers={
                    **HEADERS,
                    "Sec-Fetch-Site": "cross-site",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Dest": "document",
                },
            ).status_code
            == 200
        )
        response = client.get("/api/test?token=tailscale-session&overlay=false", headers=HEADERS)
        assert response.json() == {"authorized": True, "scheme": "https", "query": "overlay=false"}
        assert client.post("/api/test", headers=HEADERS).status_code == 403
        assert client.post("/api/test", headers={**HEADERS, "Origin": ORIGIN}).status_code == 200


@pytest.mark.parametrize(
    "change",
    [
        {"Tailscale-User-Login": "other@example.com"},
        {"Tailscale-User-Login": ""},
        {"X-Forwarded-Proto": "http"},
        {"Host": "attacker.example"},
        {"Origin": "https://attacker.example"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_reject_untrusted_headers(change: dict[str, str]) -> None:
    with TestClient(gateway(), base_url=ORIGIN, client=("127.0.0.1", 5000)) as client:
        assert client.get("/app/session", headers={**HEADERS, **change}).status_code == 403


def test_remote_socket_cannot_spoof_identity() -> None:
    with TestClient(gateway(), base_url=ORIGIN, client=("100.64.0.2", 5000)) as client:
        assert client.get("/app/session", headers=HEADERS).status_code == 403


def test_websocket_identity_and_origin() -> None:
    with TestClient(gateway(), base_url=ORIGIN, client=("127.0.0.1", 5000)) as client:
        with client.websocket_connect(
            ORIGIN.replace("https:", "wss:") + "/ws?token=tailscale-session",
            headers={**HEADERS, "Origin": ORIGIN},
        ) as ws:
            assert ws.receive_json() == {"authorized": True, "scheme": "wss", "query": ""}
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(ORIGIN.replace("https:", "wss:") + "/ws", headers=HEADERS),
        ):
            pass


@pytest.mark.parametrize(
    "origin",
    ["http://bridge.example.ts.net", "https://example.com", "https://bridge.example.ts.net/evil"],
)
def test_invalid_origin_fails_closed(origin: str) -> None:
    with pytest.raises(ValueError):
        DashboardGateway(FastAPI(), origin, "owner@example.com", "key")


def test_configured_dashboard_shell_is_only_offered_through_gateway() -> None:
    from bambu_bridge.api.viz import app_shell_router
    from bambu_bridge.config import Settings

    app = FastAPI()
    app.state.settings = Settings(bridge_dashboard_port=8081)
    app.include_router(app_shell_router)
    with TestClient(app) as client:
        for path in ("/", "/app/", "/downloads/android"):
            assert client.get(path).status_code == 403
    trusted = DashboardGateway(app, ORIGIN, "owner@example.com", "owner-secret")
    with TestClient(trusted, base_url=ORIGIN, client=("127.0.0.1", 5000)) as client:
        assert client.get("/app/", headers=HEADERS).status_code == 200
