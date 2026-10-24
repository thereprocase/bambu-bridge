from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from bambu_bridge.service.inventory import InventoryService
from tests.conftest import API_KEY, build_app

AUTH = {"Authorization": f"Bearer {API_KEY}"}
PRODUCT = {
    "id": 2,
    "name": "ASA Black",
    "material": "ASA",
    "weight": 1000,
    "diameter": 1.75,
    "color_hex": "000000",
}
SPOOL = {"id": 43, "filament": PRODUCT, "remaining_weight": 436}


class Writes(list):
    failure: str | None = None


@contextmanager
def inventory_app(tmp_path: Path):
    writes = Writes()
    spool = dict(SPOOL)

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal spool
        if request.method != "GET":
            writes.append(request)
            if writes.failure == "timeout":
                raise httpx.ReadTimeout("reply lost", request=request)
            body = json.loads(request.content)
            spool = {
                **spool,
                "remaining_weight": body.get("remaining_weight", spool["remaining_weight"]),
                "archived": body.get("archived", spool.get("archived", False)),
                "comment": body.get("comment", spool.get("comment")),
            }
            return httpx.Response(200, json=spool)
        if request.url.path.endswith("/spool"):
            return httpx.Response(
                200, json=[spool] if int(request.url.params.get("offset", "0")) == 0 else []
            )
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "healthy"})
        if request.url.path.endswith("/info"):
            return httpx.Response(200, json={"version": "0.22.1", "data_dir": "/private/data"})
        return httpx.Response(200, json=PRODUCT if "/filament/" in request.url.path else spool)

    async def open_inventory():
        service = await InventoryService.open(
            tmp_path / "inventory",
            "https://inventory.invalid/",
            transport=httpx.MockTransport(handle),
        )
        return service

    app = build_app(tmp_path / "jobs.sqlite")
    with TestClient(app) as client:
        service = client.portal.call(open_inventory)
        app.state.inventory = service
        yield client, writes


def test_disabled_inventory_and_auth(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "jobs.sqlite")) as client:
        assert client.get("/api/v1/inventory").status_code == 401
        response = client.get("/api/v1/inventory", headers=AUTH)
        assert response.json()["configured"] is False
        assert client.get("/api/v1/inventory/spools", headers=AUTH).status_code == 503


def test_create_spool_forwards_to_spoolman_and_answers_with_its_record(tmp_path: Path) -> None:
    with inventory_app(tmp_path) as (client, writes):
        body = {"filament_id": 2, "remaining_weight": 400}
        assert client.post("/api/v1/inventory/spools", json=body).status_code == 401
        created = client.post("/api/v1/inventory/spools", headers=AUTH, json=body)
        assert created.status_code == 201, created.text
        assert created.json()["id"] == 43
        assert len(writes) == 1
        assert json.loads(writes[0].content) == body


def test_mass_and_pagination_validation(tmp_path: Path) -> None:
    with inventory_app(tmp_path) as (client, writes):
        path = "/api/v1/inventory/spools/43/weigh"
        assert client.post(path, headers=AUTH, json={"remaining_weight": -1}).status_code == 422
        assert client.post(path, headers=AUTH, json={}).status_code == 422
        assert (
            client.post(
                path, headers=AUTH, json={"remaining_weight": 1, "gross_weight": 2}
            ).status_code
            == 422
        )
        assert client.get("/api/v1/inventory/spools?limit=1000", headers=AUTH).status_code == 422
        assert not writes


def test_provider_health_omits_private_details(tmp_path: Path) -> None:
    with inventory_app(tmp_path) as (client, _):
        response = client.get("/api/v1/inventory/health", headers=AUTH)
        assert response.status_code == 200
        assert response.json() == {"healthy": True, "version": "0.22.1", "tested_version": "0.22.1"}


def test_remove_assignment_after_printer_deleted(tmp_path: Path) -> None:
    from bambu_bridge.spool_assignments import SpoolAssignment

    with inventory_app(tmp_path) as (client, writes):

        async def seed():
            store = client.app.state.inventory.assignments
            initial = await store.snapshot()
            return await store.replace(
                (SpoolAssignment("deleted-printer", "external", 43),),
                provider_id=initial.provider_id,
                expected_revision=initial.revision,
            )

        snapshot = client.portal.call(seed)
        response = client.put(
            "/api/v1/inventory/printers/deleted-printer/assignment",
            headers=AUTH,
            json={
                "provider_id": snapshot.provider_id,
                "expected_revision": snapshot.revision,
                "source": "external",
                "spool_id": None,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["assignments"] == []
        assert not writes


def test_a_lost_reply_says_check_spoolman(tmp_path: Path) -> None:
    with inventory_app(tmp_path) as (client, writes):
        writes.failure = "timeout"
        response = client.post(
            "/api/v1/inventory/spools/43/weigh", headers=AUTH, json={"remaining_weight": 400}
        )
        assert response.status_code == 503, response.text
        assert response.json()["message"] == "Spoolman didn't answer"
        assert len(writes) == 1  # sent once; the bridge never retries a lost reply


def test_status_carries_the_sheet_fields_and_assignment_needs_no_revision(tmp_path: Path) -> None:
    with inventory_app(tmp_path) as (client, writes):
        status = client.get("/api/v1/inventory", headers=AUTH).json()
        assert status["provider_reachable"] is True
        assert status["provider_last_ok_at"] > 0
        assert status["review_count"] == 0
        assert status["spoolman_web_url"] is None
        response = client.put(
            "/api/v1/inventory/printers/p1/assignment",
            headers=AUTH,
            json={"source": "external", "spool_id": None},
        )
        assert response.status_code == 200, response.text
        assert response.json()["revision"] == 1
        assert not writes


def test_shadow_accounting_is_authenticated_bounded_and_never_debits(tmp_path):
    from types import SimpleNamespace

    from bambu_bridge.native_inbox import NativeInbox

    with inventory_app(tmp_path) as fixture:
        client, writes = fixture
        inbox = NativeInbox(tmp_path / "accounting")
        identifier = inbox.claim_external(
            "fixture", {"print": {"command": "project_file"}}, reserved=True
        )
        inbox.review_accounting(identifier, "submitted_artifact_unavailable")
        client.app.state.native_gateway = SimpleNamespace(inbox=inbox)
        before = len(writes)
        url = "/api/v1/inventory/accounting"
        assert client.get(url).status_code == 401
        assert client.get(url + "?limit=101", headers=AUTH).status_code == 422
        response = client.get(url, headers=AUTH)
        assert response.status_code == 200
        payload = response.json()
        assert payload["automatic_consumption_enabled"] is False
        assert payload["attempts"][0]["attempt_id"] == identifier
        assert payload["attempts"][0]["planned_grams"] is None
        assert payload["attempts"][0]["automatic_debit_eligible"] is False
        assert "command" not in payload["attempts"][0]
        assert len(writes) == before
        client.app.state.native_gateway = None


def test_refill_status_is_authenticated_physical_read_only_and_freshness_aware(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from bambu_bridge.service.refill_reports import RefillReports
    from tests.test_refill_reports import raw

    with inventory_app(tmp_path) as (client, writes):
        tracker = RefillReports()
        tracker.observe(raw())
        printer = SimpleNamespace(refill_reports=tracker, connected=True, send_raw=AsyncMock())
        monkeypatch.setattr(client.app.state.registry, "get", lambda _: printer)
        before = len(writes)
        url = "/api/v1/inventory/printers/fixture/refill"
        assert client.get(url).status_code == 401
        response = client.get(url, headers=AUTH)
        assert response.status_code == 200
        status = response.json()
        assert status["enabled"] is False and status["state"] == "disabled"
        assert status["writes_qualified"] is True
        assert status["qualification_issue"] is None
        # Both gates are on by default since the N9 firmware run (2026-10-22).
        assert status["execution_qualified"] is True
        assert status["printer_auto_refill"] is True
        assert status["reported_backup_masks"] == [3]
        assert status["slots"][0]["declaration"]["tray_color"] == "FF0000FF"
        assert "tag_uid" not in response.text and "fixture-ams" not in response.text
        tracker.auto_at -= 60
        status = client.get(url, headers=AUTH).json()
        assert status["printer_auto_refill"] is None
        assert status["last_reported_auto_refill"] is True
        assert status["eligibility_fresh"] is False
        assert len(writes) == before
        printer.send_raw.assert_not_awaited()


def test_stock_selection_is_explicit_authenticated_and_never_writes_provider(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from bambu_bridge.spoolman import SpoolmanError

    with inventory_app(tmp_path) as (client, writes):
        assert client.get("/api/v1/inventory/stock-materials").status_code == 401
        catalog = client.get("/api/v1/inventory/stock-materials?material=ASA", headers=AUTH).json()
        assert catalog["runtime_qualified"] is False
        assert all(p["material"] == "ASA" for p in catalog["profiles"])
        selected = next(p for p in catalog["profiles"] if p["material_id"] == "GFB61")
        provider = client.get("/api/v1/inventory", headers=AUTH).json()["provider_id"]
        before = len(writes)
        url = "/api/v1/inventory/material-mappings/2"
        body = {
            "provider_id": provider,
            "expected_revision": 0,
            "stock_key": selected["key"],
            "catalogue_digest": catalog["catalogue_digest"],
        }
        assert client.put(url, json=body).status_code == 401
        response = client.put(url, json=body, headers=AUTH)
        assert response.status_code == 200, response.text
        assert response.json()["revision"] == 1
        stale = client.put(
            url, json={**body, "expected_revision": 1, "catalogue_digest": "0" * 64}, headers=AUTH
        )
        assert stale.status_code == 409
        assert "catalogue changed" in stale.text
        assert (
            client.put(
                url, json={"provider_id": provider, "expected_revision": 1}, headers=AUTH
            ).status_code
            == 422
        )
        assert client.put(url, json=body, headers=AUTH).status_code == 409
        spool = client.get("/api/v1/inventory/spools/43", headers=AUTH).json()
        assert spool["filament"]["color_hex"] == "000000"
        assert spool["orca_material"]["selection"]["material_id"] == "GFB61"
        assert len(writes) == before
        service = client.app.state.inventory
        monkeypatch.setattr(
            service.provider, "get_product", AsyncMock(side_effect=SpoolmanError("unavailable"))
        )
        response = client.put(
            url, json={**body, "expected_revision": 1, "stock_key": None}, headers=AUTH
        )
        assert response.status_code == 200, response.text
        service.provider.get_product.assert_not_awaited()
        assert response.json()["mappings"] == []
        assert len(writes) == before


def test_display_policy_requires_auth_and_never_saves_unqualified_opt_in(tmp_path):
    with inventory_app(tmp_path) as (client, writes):
        url = "/api/v1/inventory/display"
        assert client.get(url).status_code == 401
        initial = client.get(url, headers=AUTH).json()
        assert initial["runtime_qualified"] is False
        assert initial["effective_enabled"] is False
        assert initial["global_enabled"] is False
        body = {
            "provider_id": initial["provider_id"],
            "expected_revision": 0,
            "global_enabled": True,
            "printers": ["p1"],
        }
        assert client.put(url, json=body).status_code == 401
        assert client.put(url, json=body, headers=AUTH).status_code == 409
        assert client.get(url, headers=AUTH).json()["revision"] == 0
        off = {**body, "global_enabled": False, "printers": []}
        assert client.put(url, json=off, headers=AUTH).json()["revision"] == 1
        assert client.put(url, json=off, headers=AUTH).status_code == 409
        assert (
            client.put(
                url, json={**off, "expected_revision": 1, "global_enabled": "false"}, headers=AUTH
            ).status_code
            == 422
        )
        assert not writes


def test_qualified_display_policy_updates_controller_and_clear_is_immediate(tmp_path):
    from types import SimpleNamespace

    from bambu_bridge.service.slot_display import InventoryDisplay

    with inventory_app(tmp_path) as (client, writes):
        controller = InventoryDisplay(
            "fixture", lambda: None, lambda: None, lambda: None, qualified=True
        )
        client.app.state.native_gateway = SimpleNamespace(
            inventory_display=controller, config={"printer_id": "p1"}
        )
        url = "/api/v1/inventory/display"
        initial = client.get(url, headers=AUTH).json()
        body = {
            "provider_id": initial["provider_id"],
            "expected_revision": 0,
            "global_enabled": True,
            "printers": ["p1"],
        }
        assert client.put(url, json=body, headers=AUTH).status_code == 200
        assert controller.active
        # Policy acceptance alone is not a successful runtime readback.
        status = client.get(url, headers=AUTH).json()
        assert status["global_enabled"] and not status["effective_enabled"]
        assert (
            client.put(
                url,
                json={**body, "expected_revision": 1, "provider_id": str(uuid4())},
                headers=AUTH,
            ).status_code
            == 409
        )
        assert (
            client.put(
                url,
                json={**body, "expected_revision": 1, "global_enabled": False, "printers": []},
                headers=AUTH,
            ).status_code
            == 200
        )
        assert not controller.active and not controller.overlay.bindings
        assert not writes
        client.app.state.native_gateway = None
