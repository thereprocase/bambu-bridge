"""Archiving an empty spool can record the bare spool's weight and offer it to the vendor."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from bambu_bridge.service.inventory import InventoryService
from bambu_bridge.spool_assignments import SpoolAssignment
from tests.api.test_inventory import AUTH
from tests.conftest import build_app

VENDOR = {"id": 5, "name": "Elegoo", "empty_spool_weight": 154.0}
PRODUCT = {
    "id": 2,
    "name": "Rapid PETG White",
    "material": "PETG",
    "weight": 1000,
    "vendor": VENDOR,
}
URL = "/api/v1/inventory"


class Fake:
    """Just enough Spoolman: one spool, one vendor, PATCHes applied like the real one."""

    def __init__(self, product: dict) -> None:
        self.spool = {"id": 8, "filament": product, "remaining_weight": 0.0, "archived": False}
        self.vendor = dict(VENDOR)
        self.writes: list[httpx.Request] = []
        self.lie_about: str | None = None  # a field the reply gets wrong

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rstrip("/")
        if request.method != "GET":
            self.writes.append(request)
            body = json.loads(request.content)
            if "/vendor/" in path:
                self.vendor.update(body)
                reply = dict(self.vendor)
            else:
                self.spool.update(body)
                reply = dict(self.spool)
            if self.lie_about:
                reply[self.lie_about] = 1.0
            return httpx.Response(200, json=reply)
        if path.endswith("/spool"):
            offset = int(request.url.params.get("offset", "0"))
            return httpx.Response(200, json=[self.spool] if offset == 0 else [])
        if path.endswith("/vendor/5"):
            return httpx.Response(200, json=self.vendor)
        if "/vendor/" in path:
            return httpx.Response(404, json={})
        if path.endswith("/health"):
            return httpx.Response(200, json={"status": "healthy"})
        return httpx.Response(200, json=self.spool)


@contextmanager
def app_with(tmp_path: Path, product: dict = PRODUCT):
    fake = Fake({**product, "vendor": fake_vendor(product)})

    async def open_inventory() -> InventoryService:
        return await InventoryService.open(
            tmp_path / "inventory",
            "https://inventory.invalid/",
            transport=httpx.MockTransport(fake),
        )

    app = build_app(tmp_path / "jobs.sqlite")
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.inventory = client.portal.call(open_inventory)
        yield client, fake


def fake_vendor(product: dict) -> dict | None:
    return product.get("vendor")


def archive(client, body=None):
    return client.post(f"{URL}/spools/8/archive", headers=AUTH, json=body)


def sent(fake: Fake) -> list[dict]:
    return [json.loads(w.content) for w in fake.writes]


def test_archiving_with_a_weight_stores_it_on_the_spool_and_offers_it_to_the_vendor(tmp_path):
    with app_with(tmp_path) as (client, fake):
        response = archive(client, {"empty_spool_weight": 161.5})
        assert response.status_code == 200, response.text
        assert sent(fake) == [{"archived": True, "spool_weight": 161.5}]  # one write, spool only
        assert response.json()["archived"] is True
        assert response.json()["vendor_offer"] == {
            "vendor_id": 5,
            "vendor": "Elegoo",
            "current_empty_spool_weight": 154.0,
            "proposed_empty_spool_weight": 161.5,
        }
        assert fake.vendor["empty_spool_weight"] == 154.0  # offered, never applied unasked


@pytest.mark.parametrize("body", [None, {}, {"empty_spool_weight": None}])
def test_archiving_without_a_weight_is_unchanged(tmp_path, body):
    with app_with(tmp_path) as (client, fake):
        response = archive(client, body)
        assert response.status_code == 200, response.text
        assert sent(fake) == [{"archived": True}]
        assert "vendor_offer" not in response.json()


def test_a_weight_equal_to_the_vendors_makes_no_offer(tmp_path):
    with app_with(tmp_path) as (client, fake):
        response = archive(client, {"empty_spool_weight": 154})
        assert response.status_code == 200
        assert sent(fake) == [{"archived": True, "spool_weight": 154}]
        assert "vendor_offer" not in response.json()


def test_a_vendor_without_a_default_is_offered_the_weight(tmp_path):
    bare = {**PRODUCT, "vendor": {"id": 5, "name": "Elegoo"}}
    with app_with(tmp_path, bare) as (client, fake):
        offer = archive(client, {"empty_spool_weight": 160}).json()["vendor_offer"]
        assert offer["current_empty_spool_weight"] is None
        assert offer["proposed_empty_spool_weight"] == 160


def test_a_product_without_a_vendor_has_nobody_to_offer_to(tmp_path):
    loose = {k: v for k, v in PRODUCT.items() if k != "vendor"}
    with app_with(tmp_path, loose) as (client, fake):
        response = archive(client, {"empty_spool_weight": 160})
        assert response.status_code == 200
        assert sent(fake) == [{"archived": True, "spool_weight": 160}]
        assert "vendor_offer" not in response.json()


@pytest.mark.parametrize(
    "body",
    [
        {"empty_spool_weight": 0},
        {"empty_spool_weight": -3},
        {"empty_spool_weight": True},
        {"empty_spool_weight": "160"},
        {"empty_spool_weight": 160, "archived": False},  # the body cannot reach other fields
    ],
)
def test_a_bad_weight_is_refused_before_anything_is_written(tmp_path, body):
    with app_with(tmp_path) as (client, fake):
        assert archive(client, body).status_code == 422
        assert not fake.writes


def test_nan_and_infinity_are_refused(tmp_path):
    with app_with(tmp_path) as (client, fake):
        for raw in (b'{"empty_spool_weight": NaN}', b'{"empty_spool_weight": Infinity}'):
            response = client.post(
                f"{URL}/spools/8/archive",
                headers={**AUTH, "Content-Type": "application/json"},
                content=raw,
            )
            assert response.status_code == 422, raw
        assert not fake.writes


def test_a_spool_still_in_a_slot_is_not_archived_with_a_weight_either(tmp_path):
    with app_with(tmp_path) as (client, fake):

        async def assign() -> None:
            store = client.app.state.inventory.assignments
            snap = await store.snapshot()
            await store.replace(
                (SpoolAssignment("p1", "external", 8),),
                provider_id=snap.provider_id,
                expected_revision=snap.revision,
            )

        client.portal.call(assign)
        assert archive(client, {"empty_spool_weight": 160}).status_code == 409
        assert not fake.writes


def test_an_already_archived_spool_takes_no_weight(tmp_path):
    with app_with(tmp_path) as (client, fake):
        fake.spool["archived"] = True
        assert archive(client, {"empty_spool_weight": 160}).status_code == 422
        assert not fake.writes


def test_a_reply_that_disagrees_about_the_weight_is_reported_as_uncertain(tmp_path):
    with app_with(tmp_path) as (client, fake):
        fake.lie_about = "spool_weight"
        response = archive(client, {"empty_spool_weight": 161.5})
        assert response.status_code != 200, response.text
        assert len(fake.writes) == 1  # never retried


def test_accepting_the_offer_sets_the_vendors_default(tmp_path):
    with app_with(tmp_path) as (client, fake):
        response = client.post(
            f"{URL}/vendors/5/empty_spool_weight", headers=AUTH, json={"empty_spool_weight": 161.5}
        )
        assert response.status_code == 200, response.text
        assert sent(fake) == [{"empty_spool_weight": 161.5}]  # the id travels in the path only
        assert fake.writes[0].method == "PATCH" and fake.writes[0].url.path.endswith("/vendor/5")
        assert response.json()["empty_spool_weight"] == 161.5


@pytest.mark.parametrize("body", [{}, {"empty_spool_weight": None}, {"empty_spool_weight": 0}])
def test_the_vendor_route_needs_a_positive_weight(tmp_path, body):
    with app_with(tmp_path) as (client, fake):
        response = client.post(f"{URL}/vendors/5/empty_spool_weight", headers=AUTH, json=body)
        assert response.status_code == 422
        assert not fake.writes


def test_an_unknown_vendor_is_a_404_and_writes_nothing(tmp_path):
    with app_with(tmp_path) as (client, fake):
        response = client.post(
            f"{URL}/vendors/99/empty_spool_weight", headers=AUTH, json={"empty_spool_weight": 160}
        )
        assert response.status_code == 404
        assert not fake.writes


def test_a_vendor_reply_that_disagrees_is_reported_as_uncertain(tmp_path):
    with app_with(tmp_path) as (client, fake):
        fake.lie_about = "empty_spool_weight"
        response = client.post(
            f"{URL}/vendors/5/empty_spool_weight", headers=AUTH, json={"empty_spool_weight": 161.5}
        )
        assert response.status_code != 200


def test_the_vendor_route_requires_auth(tmp_path):
    with app_with(tmp_path) as (client, fake):
        response = client.post(
            f"{URL}/vendors/5/empty_spool_weight", json={"empty_spool_weight": 1}
        )
        assert response.status_code == 401
        assert not fake.writes


def test_creating_a_product_still_sends_its_vendor_id(tmp_path):
    # set_vendor_empty_weight pops vendor_id from the body; create_product must not.
    with app_with(tmp_path) as (client, fake):
        client.post(
            f"{URL}/products",
            headers=AUTH,
            json={
                "name": "X",
                "vendor_id": 5,
                "material": "PLA",
                "color_hex": "FFFFFF",
                "density": 1.24,
                "weight": 1000,
            },
        )
        assert sent(fake)[0]["vendor_id"] == 5
