"""The Spoolman pass-through routes at their edges. Fakes only; no network."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi.testclient import TestClient

from bambu_bridge.inventory_sources import declared_identity
from bambu_bridge.service.inventory import InventoryService
from bambu_bridge.spool_assignments import SpoolAssignment
from tests.api.test_inventory import AUTH, PRODUCT
from tests.conftest import build_app

ASA_IDENTITY = "a" * 64


def _spool(i: int, archived: bool = False) -> dict:
    return {"id": i, "filament": PRODUCT, "remaining_weight": 400.0, "archived": archived}


@contextmanager
def fixture(tmp_path: Path, spools: list[dict]):
    """Same wiring as tests/api/test_inventory.py::inventory_app, with several spools
    and raise_server_exceptions=False so an unhandled exception shows as a 500."""
    writes: list[httpx.Request] = []
    store = {s["id"]: dict(s) for s in spools}

    def handle(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.rstrip("/").split("/")
        if request.method != "GET":
            writes.append(request)
            body = json.loads(request.content)
            sid = int(parts[-1])
            store[sid] = {
                **store[sid],
                **{k: v for k, v in body.items() if k in {"remaining_weight", "archived"}},
            }
            return httpx.Response(200, json=store[sid])
        if parts[-1] == "spool":
            offset = int(request.url.params.get("offset", "0"))
            limit = int(request.url.params.get("limit", "50"))
            allow = request.url.params.get("allow_archived") == "true"
            rows = sorted(
                (s for s in store.values() if allow or not s["archived"]), key=lambda s: s["id"]
            )
            return httpx.Response(200, json=rows[offset : offset + limit])
        if parts[-2] == "spool" and parts[-1].isdigit():
            return httpx.Response(200, json=store[int(parts[-1])])
        if parts[-1] == "health":
            return httpx.Response(200, json={"status": "healthy"})
        if parts[-1] == "info":
            return httpx.Response(200, json={"version": "0.22.1"})
        if "filament" in parts:
            return httpx.Response(200, json=PRODUCT)
        return httpx.Response(404, json={})

    async def open_inventory() -> InventoryService:
        return await InventoryService.open(
            tmp_path / "inventory",
            "https://inventory.invalid/",
            transport=httpx.MockTransport(handle),
        )

    app = build_app(tmp_path / "jobs.sqlite")
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.inventory = client.portal.call(open_inventory)
        yield client, writes


def _seed(client: TestClient, rows: list[tuple[str, str, int, float, str | None]]) -> None:
    """rows: (printer, source, spool_id, assigned_at, observed_identity), one replace."""

    async def go() -> None:
        store = client.app.state.inventory.assignments
        snap = await store.snapshot()
        await store.replace(
            tuple(
                SpoolAssignment(p, s, sid, assigned_at=ts, observed_identity=ident)
                for p, s, sid, ts, ident in rows
            ),
            provider_id=snap.provider_id,
            expected_revision=snap.revision,
        )

    client.portal.call(go)


def test_nonpositive_spool_id_in_path_is_422_not_500(tmp_path: Path) -> None:
    with fixture(tmp_path, [_spool(43)]) as (client, writes):
        for path, body in [
            ("/api/v1/inventory/spools/0/archive", None),
            ("/api/v1/inventory/spools/-1/archive", None),
            ("/api/v1/inventory/spools/0/weigh", {"remaining_weight": 100}),
            ("/api/v1/inventory/spools/-5/weigh", {"gross_weight": 100}),
        ]:
            response = client.post(path, headers=AUTH, json=body)
            assert response.status_code == 422, (path, response.status_code, response.text[:200])
        assert not writes


def test_boolean_weight_is_not_silently_a_number(tmp_path: Path) -> None:
    with fixture(tmp_path, [_spool(43)]) as (client, writes):
        response = client.post(
            "/api/v1/inventory/spools/43/weigh", headers=AUTH, json={"remaining_weight": True}
        )
        assert response.status_code == 422, (response.status_code, response.text[:200])
        assert not writes, [w.content for w in writes]


def test_nan_weight_is_rejected(tmp_path: Path) -> None:
    with fixture(tmp_path, [_spool(43)]) as (client, writes):
        response = client.post(
            "/api/v1/inventory/spools/43/weigh",
            headers={**AUTH, "Content-Type": "application/json"},
            content=b'{"remaining_weight": NaN}',
        )
        assert response.status_code == 422, (response.status_code, response.text[:200])
        assert not writes


def test_review_count_is_not_capped_at_one_hundred(tmp_path: Path) -> None:
    class Inbox:
        def accounting_page(self, limit: int, offset: int, state: str | None) -> list[dict]:
            assert state == "review"
            rows = [{"attempt_id": i} for i in range(150)]
            return rows[offset : offset + limit]

        def review_count(self) -> int:
            return 150  # the inbox counts its held prints in one query; no page limit

    with fixture(tmp_path, [_spool(43)]) as (client, _):
        client.app.state.native_gateway = SimpleNamespace(inbox=Inbox())
        try:
            status = client.get("/api/v1/inventory", headers=AUTH).json()
        finally:
            client.app.state.native_gateway = None
        assert status["review_count"] == 150, status["review_count"]


def test_spool_list_puts_recently_assigned_first_across_pages(tmp_path: Path) -> None:
    # Spool 3 was assigned most recently but sits on page 2 of the id-sorted provider list.
    with fixture(tmp_path, [_spool(1), _spool(2), _spool(3)]) as (client, _):
        _seed(
            client,
            [
                ("p1", "external", 1, 100.0, "b" * 64),
                ("p1", "ams:x:tray:0", 3, 300.0, "c" * 64),
            ],
        )
        page1 = [
            s["id"] for s in client.get("/api/v1/inventory/spools?limit=2", headers=AUTH).json()
        ]
        page2 = [
            s["id"]
            for s in client.get("/api/v1/inventory/spools?limit=2&offset=2", headers=AUTH).json()
        ]
        assert page1 + page2 == [3, 1, 2], (page1, page2)


def test_last_spool_keeps_the_most_recent_move_of_a_spool(tmp_path: Path) -> None:
    # Spool 43 sat behind X (t=100), then Y (t=200), then back behind X (t=300).
    # Spool 44 sat behind Z at t=250. Spool 43 is the most recent, so it sorts first.
    x, y, z = "1" * 64, "2" * 64, "3" * 64
    with fixture(tmp_path, [_spool(43), _spool(44)]) as (client, _):
        _seed(client, [("p1", "ams:a:tray:0", 43, 100.0, x)])
        _seed(client, [("p1", "ams:a:tray:1", 43, 200.0, y)])
        _seed(client, [("p1", "ams:a:tray:2", 44, 250.0, z), ("p1", "ams:a:tray:1", 43, 200.0, y)])
        _seed(client, [("p1", "ams:a:tray:0", 43, 300.0, x), ("p1", "ams:a:tray:2", 44, 250.0, z)])
        ids = [s["id"] for s in client.get("/api/v1/inventory/spools", headers=AUTH).json()]
        assert ids == [43, 44], ids


def test_untagged_identical_trays_keep_their_own_last_spool(tmp_path: Path, monkeypatch) -> None:
    # Two untagged PLA trays declare the same material, so they share one identity.
    tray = {"tray_type": "PLA", "tray_info_idx": "GFA00", "tag_uid": "0000000000000000"}
    identity = declared_identity(tray)
    rows = [
        {"source": "ams:h:tray:0", "observed_identity": identity},
        {"source": "ams:h:tray:1", "observed_identity": identity},
    ]
    printer = SimpleNamespace(serial="p1", connected=True)
    with fixture(tmp_path, [_spool(43), _spool(44)]) as (client, _):
        monkeypatch.setattr(client.app.state.registry, "get", lambda _: printer)
        monkeypatch.setattr(
            "bambu_bridge.api.inventory.refresh_source_frame", AsyncMock(return_value=None)
        )
        monkeypatch.setattr("bambu_bridge.api.inventory.source_choices", lambda *_, **__: rows)
        _seed(
            client,
            [
                ("p1", "ams:h:tray:0", 43, 100.0, identity),
                ("p1", "ams:h:tray:1", 44, 200.0, identity),
            ],
        )
        got = client.get("/api/v1/inventory/printers/p1/sources", headers=AUTH).json()
        last = {row["source"]: row["last_spool_id"] for row in got}
        assert last == {"ams:h:tray:0": 43, "ams:h:tray:1": 44}, last


def test_stale_slots_are_listed_but_not_assigned(tmp_path: Path, monkeypatch) -> None:
    from bambu_bridge import inventory_sources
    from tests.test_inventory_sources import printer as sourced_printer

    printer = sourced_printer()
    now = time.time()
    printer.material_inventory.ams_at = now - 60  # no AMS report for a minute
    # Ages come from this clock; frozen so a loaded host cannot add a second.
    monkeypatch.setattr(
        inventory_sources, "time", SimpleNamespace(time=lambda: now, monotonic=time.monotonic)
    )
    with fixture(tmp_path, [_spool(43)]) as (client, _):
        monkeypatch.setattr(client.app.state.registry, "get", lambda _: printer)
        monkeypatch.setattr(
            "bambu_bridge.api.inventory.refresh_source_frame", AsyncMock(return_value=None)
        )
        rows = client.get("/api/v1/inventory/printers/p1/sources", headers=AUTH).json()
        assert len(rows) == 4 and all(row["stale"] for row in rows)
        assert rows[0]["age_seconds"] == 60.0 and not rows[0]["assignable"]
        body = {"spool_id": 43, "source": rows[0]["source"]}
        refused = client.put("/api/v1/inventory/printers/p1/assignment", json=body, headers=AUTH)
        assert refused.status_code == 409
        assert refused.json()["message"] == (
            "The printer hasn't reported its AMS for 60 s; try again in a moment"
        )
