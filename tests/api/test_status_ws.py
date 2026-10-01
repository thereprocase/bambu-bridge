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
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bambu_bridge.api import status as status_api
from tests.conftest import (
    ACCESS_CODE,
    API_KEY,
    SERIAL,
    MockPrinter,
    build_app,
    patch_discovery_ok,
)

_AUTH = {"Authorization": f"Bearer {API_KEY}"}


@pytest.mark.asyncio
async def test_quiet_websocket_refreshes_telemetry_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(status_api, "_HEARTBEAT_S", 0.01)
    sent: list[dict[str, Any]] = []

    class QuietSubscription:
        async def get(self) -> None:
            await asyncio.Event().wait()

    class FakeSocket:
        async def send_json(self, data: dict[str, Any]) -> None:
            sent.append(data)

    initial = {"session": {"last_telemetry_at": "2026-09-27T12:00:00Z"}}
    current = {"session": {"last_telemetry_at": "2026-09-27T12:00:05Z"}}
    service = SimpleNamespace(snapshot=lambda: current)
    task = asyncio.create_task(
        status_api._pump(FakeSocket(), QuietSubscription(), service, initial)
    )
    try:
        async with asyncio.timeout(1):
            while not any(message.get("type") == "delta" for message in sent):
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert {
        "type": "delta",
        "data": {"session": {"last_telemetry_at": "2026-09-27T12:00:05Z"}},
    } in sent


def test_clock_probe_reply_echoes_t0_with_bridge_time() -> None:
    assert status_api.clock_reply('{"type": "clock", "t0": 1700000000123.5}', now_ms=42.0) == {
        "type": "clock", "t0": 1700000000123.5, "t1": 42.0}
    before = time.time() * 1000
    reply = status_api.clock_reply('{"type": "clock", "t0": 1}')
    assert reply is not None and before <= reply["t1"] <= time.time() * 1000
    for frame in ['{"type": "pong"}', "not json", "[]", '{"type": "clock"}',
                  '{"type": "clock", "t0": "x"}', '{"type": "clock", "t0": true}',
                  '{"type": "clock", "t0": NaN}']:
        assert status_api.clock_reply(frame) is None, frame


@pytest.mark.asyncio
async def test_clock_replies_interleave_safely_with_drain() -> None:
    frames = ['{"type": "pong"}', '{"type": "clock", "t0": 5}', '{"type": "clock", "t0": 6}']
    sent: list[dict[str, Any]] = []

    class Socket:
        async def receive_text(self) -> str:
            if not frames:
                raise status_api.WebSocketDisconnect()
            return frames.pop(0)

        async def send_json(self, data: dict[str, Any]) -> None:
            sent.append(data)

    await status_api._drain_client(status_api._LockedSocket(Socket()))
    assert [m["t0"] for m in sent] == [5, 6] and all(m["type"] == "clock" for m in sent)
    # RFC 5905 on-wire stamps: receive t1 never after transmit t2.
    assert all(m["t1"] <= m["t2"] for m in sent)


@pytest.mark.asyncio
async def test_clock_transmit_stamp_excludes_queueing_behind_status() -> None:
    sent: list[dict[str, Any]] = []

    class Socket:
        async def send_json(self, data: dict[str, Any]) -> None:
            sent.append(data)
            if data.get("type") == "delta":
                await asyncio.sleep(0.05)       # a slow status frame holds the lock

    locked = status_api._LockedSocket(Socket())
    reply = status_api.clock_reply('{"type": "clock", "t0": 1}')
    assert reply is not None
    await asyncio.gather(locked.send_json({"type": "delta", "data": {}}), locked.send_json(reply))
    clock = next(m for m in sent if m["type"] == "clock")
    assert clock["t2"] - clock["t1"] >= 40      # the wait shows up as server hold time, not offset


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
                c.websocket_connect(f"/api/v1/printers/{SERIAL}/status?token=wrong") as ws,
            ):
                ws.receive_json()

    await asyncio.to_thread(run_client)
