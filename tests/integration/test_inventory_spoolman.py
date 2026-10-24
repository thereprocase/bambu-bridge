"""Opt-in against a disposable local Spoolman v0.22.1, never a live inventory."""

import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from fastapi import HTTPException

from bambu_bridge.service.inventory import (
    ArchiveSpool,
    InventoryService,
    NewProduct,
    NewSpool,
    NewVendor,
    SpoolWeight,
)

URL = os.environ.get("SPOOLMAN_TEST_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="requires disposable local Spoolman")


async def test_real_record_changes_go_straight_to_spoolman(tmp_path: Path) -> None:
    assert urlsplit(URL).hostname in {"127.0.0.1", "localhost", "::1"}
    service = await InventoryService.open(tmp_path, URL)
    try:
        assert (await service.provider.health())["version"] == "0.22.1"
        vendor = await service.record(
            "create_vendor",
            NewVendor(name=f"Bridge fixture {uuid4().hex[:8]}", empty_spool_weight=150),
        )
        product = await service.record(
            "create_product",
            NewProduct(
                name="PolyLite ASA Black fixture",
                vendor_id=vendor["id"],
                material="ASA",
                color_hex="000000FF",
                density=1.07,
                weight=1000,
                spool_weight=150,
            ),
        )
        spool = await service.record(
            "create_spool", NewSpool(filament_id=product["id"], remaining_weight=400)
        )
        assert (await service.provider.get_spool(spool["id"])).remaining_percent == 40
        await service.record("weigh", SpoolWeight(spool_id=spool["id"], gross_weight=500))
        current = await service.provider.get_spool(spool["id"])
        assert current.remaining_weight == 350
        assert current.remaining_percent == 35
        assert any(s.id == spool["id"] for s in await service.provider.search_spools("ASA"))
        await service.record("archive", ArchiveSpool(spool_id=spool["id"]))
        assert (await service.provider.get_spool(spool["id"])).archived
    finally:
        await service.close()


async def test_real_usage_is_written_as_the_print_goes_and_witnessed_by_used_weight(tmp_path):
    """Real v0.22.1 endpoint, synthetic attempt: get, put, get on used_weight.

    The first /use lands and its reply is lost; the verifying read settles it.
    A spool with no known weight is debited all the same, and a hand edit
    between ticks is kept, since `use` is relative.
    """
    import httpx
    from tests.test_accounting_attempts import manifest
    from tests.test_usage_counting import printer

    from bambu_bridge.native_inbox import NativeInbox
    from bambu_bridge.service import usage_ledger
    from bambu_bridge.service.consumption_delivery import deliver_once

    assert urlsplit(URL).hostname in {"127.0.0.1", "localhost", "::1"}

    class LoseFirstUseReply(httpx.AsyncBaseTransport):
        def __init__(self):
            self.real = httpx.AsyncHTTPTransport()
            self.uses = 0

        async def handle_async_request(self, request):
            response = await self.real.handle_async_request(request)
            if (
                request.method == "PUT"
                and request.url.path.endswith("/use")
                and response.is_success
            ):
                self.uses += 1
                if self.uses == 1:
                    await response.aread()
                    await response.aclose()
                    raise httpx.ReadTimeout("injected after real acceptance", request=request)
            return response

        async def aclose(self):
            await self.real.aclose()

    transport = LoseFirstUseReply()
    service = await InventoryService.open(tmp_path / "inventory", URL, transport=transport)
    api = httpx.AsyncClient(base_url=URL.rstrip("/") + "/api/v1")
    try:
        assert (await service.provider.health())["version"] == "0.22.1"
        product = await service.record(
            "create_product",
            NewProduct(
                name=f"Usage fixture {uuid4().hex[:8]}",
                material="PETG",
                color_hex="000000",
                density=1.28,
                weight=1000,
            ),
        )
        weighed = await service.record(
            "create_spool", NewSpool(filament_id=product["id"], remaining_weight=400)
        )
        sid = weighed["id"]
        # A spool on a filament with no package weight: Spoolman knows no remaining weight.
        bare = await api.post(
            "/filament",
            json={
                "name": f"Bare {uuid4().hex[:8]}",
                "material": "PETG",
                "density": 1.28,
                "diameter": 1.75,
            },
        )
        assert bare.status_code == 200, bare.text
        unweighed = await api.post("/spool", json={"filament_id": bare.json()["id"]})
        assert unweighed.status_code == 200, unweighed.text
        uid = unweighed.json()["id"]
        base = (await service.provider.get_spool(sid)).used_weight
        assert (await service.provider.get_spool(uid)).remaining_weight is None

        snapshot = await service.assignments.snapshot()
        inbox = NativeInbox(tmp_path / "inbox")
        attempt = inbox.claim_external(
            "fixture", {"print": {"command": "project_file"}}, reserved=True
        )
        context = manifest()
        context.update(provider_id=snapshot.provider_id, assignment_revision=snapshot.revision)
        # Filament 0 from the external holder, filament 1 from AMS tray 1; 10 layers.
        context.update(
            layer_grams=[{"0": 1.0, "1": 0.5}] * 10,
            tool_wires={"0": 254, "1": 0},
            wire_spools={"254": sid, "0": uid},
        )
        inbox.freeze_accounting(attempt, context)
        with inbox.connect() as db:
            db.execute(
                "UPDATE uploads SET start_state='running',layer_reached=3 WHERE id=?", (attempt,)
            )
        service.consumption_qualified = True  # Test instance only; synthetic lifecycle.

        async def tick():
            await deliver_once(service, inbox, lambda _id: printer(), 0.0)

        await tick()  # layer 3: 2.5 g and 1.25 g; the first reply is lost, then witnessed
        rows = usage_ledger.rows(inbox, attempt)
        assert {k: (r["written_g"], r["state"]) for k, r in rows.items()} == {
            sid: (2.5, "open"),
            uid: (1.25, "open"),
        }
        spool = await service.provider.get_spool(sid)
        assert (spool.used_weight, spool.remaining_weight) == (base + 2.5, 397.5)
        spool = await service.provider.get_spool(uid)
        assert (spool.used_weight, spool.remaining_weight) == (1.25, None)
        assert transport.uses == 2
        await tick()  # within the write interval: nothing more is sent
        assert transport.uses == 2

        # The owner corrects the weighed spool by hand between ticks.
        edited = await api.patch(f"/spool/{sid}", json={"remaining_weight": 350})
        assert edited.status_code == 200, edited.text
        after_edit = (await service.provider.get_spool(sid)).used_weight
        with inbox.connect() as db:
            db.execute(
                "UPDATE uploads SET start_state='completed',layer_reached=10 WHERE id=?",
                (attempt,),
            )
        await tick()  # the end settles the rest on top of the edit: 7.5 g and 3.75 g
        spool = await service.provider.get_spool(sid)
        assert (spool.used_weight, spool.remaining_weight) == (after_edit + 7.5, 342.5)
        assert (await service.provider.get_spool(uid)).used_weight == 5.0
        assert inbox.accounting_event(attempt)["delivery_state"] == "delivered"
        rows = usage_ledger.rows(inbox, attempt)
        assert all(r["state"] == "done" and r["written_g"] == r["counted_g"] for r in rows.values())
        assert transport.uses == 4
        await tick()
        assert transport.uses == 4
    finally:
        await api.aclose()
        await service.close()


async def test_real_stock_material_selection_is_local_and_product_edits_require_review(tmp_path):
    import httpx

    assert urlsplit(URL).hostname in {"127.0.0.1", "localhost", "::1"}

    class Watch(httpx.AsyncBaseTransport):
        def __init__(self):
            self.real = httpx.AsyncHTTPTransport()
            self.writes = []

        async def handle_async_request(self, request):
            if request.method != "GET":
                self.writes.append((request.method, request.url.path))
            return await self.real.handle_async_request(request)

        async def aclose(self):
            await self.real.aclose()

    transport = Watch()
    service = await InventoryService.open(tmp_path, URL, transport=transport)
    try:
        product = await service.record(
            "create_product",
            NewProduct(
                name=f"Stock mapping fixture {uuid4().hex[:8]}",
                material="PETG",
                color_hex="112233",
                density=1.28,
                weight=1000,
            ),
        )
        pid = product["id"]
        provider = (await service.assignments.snapshot()).provider_id
        before = len(transport.writes)
        result = await service.select_material(
            provider_id=provider, revision=0, product_id=pid, stock_key="Generic PETG @base.json"
        )
        assert result["revision"] == 1
        product = await service.provider.get_product(pid)
        selection = await service.materials.lookup(provider, product)
        assert selection["selection"]["material_id"] == "GFG99"
        assert selection["selection"]["runtime_qualified"] is False
        assert len(transport.writes) == before
        async with httpx.AsyncClient() as external_operator:
            response = await external_operator.patch(
                URL.rstrip("/") + f"/api/v1/filament/{pid}",
                json={"name": "Externally renamed test product"},
            )
            response.raise_for_status()
        product = await service.provider.get_product(pid)
        assert (await service.materials.lookup(provider, product))[
            "issue"
        ] == "inventory_product_changed"
        assert len(transport.writes) == before
    finally:
        await service.close()


async def test_real_grams_move_between_spools_and_a_credit_below_zero_is_refused(tmp_path):
    """Real v0.22.1: PUT /spool/{id}/use takes a negative use_weight (the credit side).

    Also records what Spoolman itself does when used_weight would go below
    zero, and that the bridge refuses such a credit before sending it.
    """
    import httpx
    from tests.test_accounting_attempts import manifest
    from tests.test_usage_counting import printer

    from bambu_bridge.api.inventory_usage import Reassign, reassign_print
    from bambu_bridge.native_inbox import NativeInbox
    from bambu_bridge.service import usage_ledger
    from bambu_bridge.service.consumption_delivery import deliver_once

    assert urlsplit(URL).hostname in {"127.0.0.1", "localhost", "::1"}
    service = await InventoryService.open(tmp_path / "inventory", URL)
    api = httpx.AsyncClient(base_url=URL.rstrip("/") + "/api/v1")
    try:
        product = await service.record(
            "create_product",
            NewProduct(
                name=f"Move fixture {uuid4().hex[:8]}",
                material="PETG",
                color_hex="000000",
                density=1.28,
                weight=1000,
            ),
        )
        first = await service.record(
            "create_spool", NewSpool(filament_id=product["id"], remaining_weight=400)
        )
        second = await service.record(
            "create_spool", NewSpool(filament_id=product["id"], remaining_weight=400)
        )
        a, b = first["id"], second["id"]
        base_a = (await service.provider.get_spool(a)).used_weight
        base_b = (await service.provider.get_spool(b)).used_weight

        snapshot = await service.assignments.snapshot()
        inbox = NativeInbox(tmp_path / "inbox")
        attempt = inbox.claim_external(
            "fixture", {"print": {"command": "project_file"}}, reserved=True
        )
        context = manifest()
        context.update(provider_id=snapshot.provider_id, assignment_revision=snapshot.revision)
        context.update(layer_grams=[{"0": 1.0}] * 10, tool_wires={"0": 254}, wire_spools={"254": a})
        inbox.freeze_accounting(attempt, context)
        with inbox.connect() as db:
            db.execute(
                "UPDATE uploads SET start_state='completed',layer_reached=10 WHERE id=?",
                (attempt,),
            )
        service.consumption_qualified = True  # Test instance only; synthetic lifecycle.
        await deliver_once(service, inbox, lambda _id: printer(), 0.0)
        assert (await service.provider.get_spool(a)).used_weight == base_a + 10
        assert inbox.accounting_event(attempt)["delivery_state"] == "delivered"

        state = SimpleNamespace(
            native_gateway=SimpleNamespace(inbox=inbox),
            registry=SimpleNamespace(get=lambda _id: printer()),
            library=None,
        )
        request = SimpleNamespace(app=SimpleNamespace(state=state))
        body = await reassign_print(
            attempt, Reassign(from_spool_id=a, to_spool_id=b), request, service
        )
        spool_a, spool_b = await service.provider.get_spool(a), await service.provider.get_spool(b)
        assert (spool_a.used_weight, spool_a.remaining_weight) == (base_a, 400.0)
        assert (spool_b.used_weight, spool_b.remaining_weight) == (base_b + 10, 390.0)
        assert body["charged"] == [{"spool_id": b, "grams": 10.0, "layers": [1, 10]}]
        rows = usage_ledger.rows(inbox, attempt)
        assert rows[a]["written_g"] == 0.0 and rows[b]["written_g"] == 10.0

        # A credit past zero: Spoolman clamps used_weight to 0 (use_weight_safe's
        # CASE … ELSE 0.0) and says nothing, so the grams past zero would vanish.
        # That is why the bridge refuses such a credit before sending it.
        probe = await api.put(f"/spool/{b}/use", json={"use_weight": -(base_b + 10 + 5)})
        assert probe.status_code == 200, probe.text
        assert probe.json()["used_weight"] == 0
        await api.put(f"/spool/{b}/use", json={"use_weight": 5})  # back to 5 g used
        # The bridge never sends such a credit: b has 5 g used, this print says 10.
        with pytest.raises(HTTPException) as refused:
            await reassign_print(
                attempt, Reassign(from_spool_id=b, to_spool_id=a), request, service
            )
        assert refused.value.status_code == 409
        assert refused.value.detail == f"#{b} has only 5 g used in Spoolman"
        assert (await service.provider.get_spool(b)).used_weight == 5
    finally:
        await api.aclose()
        await service.close()
