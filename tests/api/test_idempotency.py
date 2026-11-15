"""Idempotency-Key: a repeated command runs once (api/idempotency.py)."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI, Response

from bambu_bridge.api import idempotency
from bambu_bridge.api.idempotency import IdempotencyMiddleware
from bambu_bridge.main import create_app

KEY = "k" * 22


def _app(clock=None):
    app = FastAPI()
    calls = {"n": 0}
    gate = asyncio.Event()

    @app.post("/cmd")
    async def cmd() -> dict[str, int]:
        calls["n"] += 1
        return {"run": calls["n"]}

    @app.post("/slow")
    async def slow() -> dict[str, int]:
        calls["n"] += 1
        await gate.wait()
        return {"run": calls["n"]}

    @app.post("/big")
    async def big() -> Response:
        calls["n"] += 1
        return Response(b"x" * (idempotency.MAX_BODY + 1))

    @app.post("/refused")
    async def refused() -> Response:
        calls["n"] += 1
        return Response(b'{"error":"conflict","message":"busy"}', status_code=409)

    @app.post("/crash")
    async def crash() -> None:
        calls["n"] += 1
        raise RuntimeError("failed part-way")

    @app.get("/read")
    async def read() -> dict[str, int]:
        calls["n"] += 1
        return {"run": calls["n"]}

    kwargs = {"clock": clock} if clock else {}
    wrapped = IdempotencyMiddleware(app, **kwargs)
    transport = httpx.ASGITransport(app=wrapped, raise_app_exceptions=False)
    client = httpx.AsyncClient(transport=transport, base_url="http://bridge")
    return client, calls, gate


@pytest.mark.asyncio
async def test_repeat_with_same_key_runs_once_and_returns_the_first_response():
    client, calls, _ = _app()
    first = await client.post("/cmd", headers={"Idempotency-Key": KEY})
    second = await client.post("/cmd", headers={"Idempotency-Key": KEY})
    assert calls["n"] == 1
    assert first.json() == second.json() == {"run": 1}
    assert "idempotency-replayed" not in first.headers
    assert second.headers["idempotency-replayed"] == "true"


@pytest.mark.asyncio
async def test_requests_without_a_key_and_with_a_new_key_each_run():
    client, calls, _ = _app()
    await client.post("/cmd")
    await client.post("/cmd")
    await client.post("/cmd", headers={"Idempotency-Key": KEY})
    await client.post("/cmd", headers={"Idempotency-Key": "j" * 22})
    assert calls["n"] == 4


@pytest.mark.asyncio
async def test_key_is_bound_to_credential_and_target():
    client, calls, _ = _app()
    await client.post("/cmd", headers={"Idempotency-Key": KEY, "Authorization": "Bearer a"})
    await client.post("/cmd", headers={"Idempotency-Key": KEY, "Authorization": "Bearer b"})
    await client.post("/cmd?x=1", headers={"Idempotency-Key": KEY, "Authorization": "Bearer a"})
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_repeat_during_a_running_command_waits_for_its_response():
    client, calls, gate = _app()
    first = asyncio.create_task(client.post("/slow", headers={"Idempotency-Key": KEY}))
    while calls["n"] == 0:
        await asyncio.sleep(0)
    second = asyncio.create_task(client.post("/slow", headers={"Idempotency-Key": KEY}))
    await asyncio.sleep(0.01)
    assert not second.done()
    gate.set()
    a, b = await asyncio.gather(first, second)
    assert calls["n"] == 1
    assert a.json() == b.json() == {"run": 1}


@pytest.mark.asyncio
async def test_refusals_are_repeated_too():
    client, calls, _ = _app()
    first = await client.post("/refused", headers={"Idempotency-Key": KEY})
    second = await client.post("/refused", headers={"Idempotency-Key": KEY})
    assert (first.status_code, second.status_code) == (409, 409)
    assert second.json()["message"] == "busy"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_oversized_response_is_not_rerun():
    client, calls, _ = _app()
    await client.post("/big", headers={"Idempotency-Key": KEY})
    second = await client.post("/big", headers={"Idempotency-Key": KEY})
    assert calls["n"] == 1
    assert second.status_code == 409
    assert second.json()["error"] == "conflict"


@pytest.mark.asyncio
async def test_command_that_raised_is_not_rerun():
    client, calls, _ = _app()
    first = await client.post("/crash", headers={"Idempotency-Key": KEY})
    second = await client.post("/crash", headers={"Idempotency-Key": KEY})
    assert first.status_code == 500
    assert second.status_code == 409
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_stored_response_expires():
    now = [0.0]
    client, calls, _ = _app(clock=lambda: now[0])
    await client.post("/cmd", headers={"Idempotency-Key": KEY})
    now[0] = idempotency.TTL_S + 1
    again = await client.post("/cmd", headers={"Idempotency-Key": KEY})
    assert calls["n"] == 2
    assert again.json() == {"run": 2}


@pytest.mark.asyncio
async def test_store_is_bounded():
    client, calls, _ = _app()
    for i in range(idempotency.MAX_ENTRIES + 20):
        await client.post("/cmd", headers={"Idempotency-Key": f"key-{i:018d}"})
    transport = client._transport  # noqa: SLF001
    assert len(transport.app._entries) <= idempotency.MAX_ENTRIES  # noqa: SLF001


@pytest.mark.asyncio
async def test_malformed_key_is_rejected_without_running():
    client, calls, _ = _app()
    response = await client.post("/cmd", headers={"Idempotency-Key": "short"})
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_input"
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_reads_ignore_the_key():
    client, calls, _ = _app()
    await client.get("/read", headers={"Idempotency-Key": KEY})
    await client.get("/read", headers={"Idempotency-Key": KEY})
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_bridge_advertises_the_feature_on_every_response():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as client:
        ok = await client.get("/api/v1/health")
        missing = await client.get("/api/v1/nope")
    assert ok.headers["x-bridge-features"] == "idempotency-key"
    assert missing.headers["x-bridge-features"] == "idempotency-key"
