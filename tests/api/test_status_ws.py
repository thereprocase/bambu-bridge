"""M3 acceptance: register a printer via the API, connect the status
WebSocket, and verify the snapshot-then-deltas pattern over a live (mock)
link.

The FastAPI TestClient is synchronous and runs the app in its own portal
loop; the amqtt broker + MockPrinter run in this test's loop. They meet over
the broker's TCP port, so a delta pushed from here propagates through the
app's MQTT client into the WebSocket.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    ACCESS_CODE,
    API_KEY,
    SERIAL,
    MockPrinter,
    build_app,
    patch_discovery_ok,
)

_AUTH = {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture(autouse=True)
def _patched_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)


@pytest.mark.asyncio
async def test_ws_snapshot_then_delta(
    tmp_path: Path, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    app = build_app(tmp_path / "ws.db", mqtt_port=mqtt_broker)
    result: dict[str, object] = {}

    async def push_delta_later() -> None:
        await asyncio.sleep(1.0)  # after the app's link has seeded
        await mock_printer.push_report({"print": {"mc_percent": 77}})

    def run_client() -> None:
        with TestClient(app) as c:
            r = c.post(
                "/api/v1/printers",
                headers=_AUTH,
                json={
                    "host": "127.0.0.1",
                    "access_code": ACCESS_CODE,
                    "friendly_name": "WS P1S",
                },
            )
            assert r.status_code == 201, r.text

            url = f"/api/v1/printers/{SERIAL}/status?token={API_KEY}"
            with c.websocket_connect(url) as ws:
                hello = ws.receive_json()
                assert hello["type"] == "hello"
                assert hello["protocol_version"] == 1

                snapshot = ws.receive_json()
                assert snapshot["type"] == "snapshot"
                result["snapshot"] = snapshot

                # Drain forward until the pushed change shows up. The WS
                # stream is §6-translated end to end (contract §5.3): the
                # delta carries translated leaves, with the raw push_status
                # value preserved under `_raw` — never a bare root key.
                saw_delta = False
                leaked_raw_key = False
                for _ in range(30):
                    msg = ws.receive_json()
                    if msg["type"] != "delta":
                        continue
                    data = msg.get("data", {})
                    if "mc_percent" in data:  # raw P1S key leaked to the root
                        leaked_raw_key = True
                    if data.get("_raw", {}).get("mc_percent") == 77:
                        saw_delta = True
                        break
                result["saw_delta"] = saw_delta
                result["leaked_raw_key"] = leaked_raw_key

    pusher = asyncio.create_task(push_delta_later())
    try:
        await asyncio.to_thread(run_client)
    finally:
        pusher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pusher

    assert result["snapshot"]["type"] == "snapshot"  # type: ignore[index]
    # First frame is the §6-translated shape, not raw push_status.
    assert "phase" in result["snapshot"]["data"]  # type: ignore[index,operator]
    assert result["saw_delta"] is True
    assert result["leaked_raw_key"] is False


@pytest.mark.asyncio
async def test_ws_rejects_bad_token(tmp_path: Path) -> None:
    app = build_app(tmp_path / "ws-auth.db")

    def run_client() -> None:
        from starlette.websockets import WebSocketDisconnect

        with TestClient(app) as c:
            c.post(
                "/api/v1/printers",
                headers=_AUTH,
                json={
                    "serial": SERIAL,
                    "ip": "127.0.0.1",
                    "access_code": ACCESS_CODE,
                    "friendly_name": "x",
                },
            )
            with (
                pytest.raises(WebSocketDisconnect),
                c.websocket_connect(
                    f"/api/v1/printers/{SERIAL}/status?token=wrong"
                ) as ws,
            ):
                ws.receive_json()

    await asyncio.to_thread(run_client)
