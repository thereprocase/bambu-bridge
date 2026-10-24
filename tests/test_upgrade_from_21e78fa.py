"""The current code opens the state release 21e78fa leaves behind, and serves it.

tests/fixtures/upgrade/21e78fa/state.tar.gz holds every store as that release
(live on CT 105 on 2026-10-23) created and seeded it, built by its own code with
tests/fixtures/upgrade/make_21e78fa.py: jobs.db with a printer and a cabinet
spool, the inventory and refill databases with an initialised provider and one
assignment, the pairing/Orca stores, the native inbox with one delivered print
and one held for review the way 63f16c9b is, and a library with one capture.

Every store must open without a migration step, and the main routes must answer
on the old rows. Nothing is dropped on first start: the cabinet row and the old
journal tables are still there afterwards.
"""

from __future__ import annotations

import json
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from bambu_bridge.config import Settings
from bambu_bridge.db.jobs import Database
from bambu_bridge.library import LibraryStore
from bambu_bridge.main import create_app
from bambu_bridge.native_inbox import NativeInbox
from bambu_bridge.orca import OrcaStore
from bambu_bridge.pairing import PairingStore
from bambu_bridge.refill import RefillStore
from bambu_bridge.service.inventory import InventoryService
from tests.conftest import API_KEY
from tests.test_inventory_service import Provider

FIXTURE = Path(__file__).parent / "fixtures" / "upgrade" / "21e78fa"
AUTH = {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture
def seeded() -> dict[str, str]:
    text = (FIXTURE / "MANIFEST.md").read_text()
    return json.loads(text[text.index("{") : text.index("}") + 1])


@pytest.fixture
def state(tmp_path: Path) -> Path:
    with tarfile.open(FIXTURE / "state.tar.gz") as tar:
        tar.extractall(tmp_path, filter="data")
    return tmp_path / "state"


def tables(db: Path) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


async def test_every_store_opens_the_old_files_as_they_are(state: Path, seeded) -> None:
    db = Database(str(state / "jobs.db"))
    await db.connect()
    await db.close()
    inbox = NativeInbox(state / "pairing")
    assert inbox.accounting_event(seeded["held_attempt"])["delivery_state"] == "review"
    assert inbox.accounting_event(seeded["delivered_attempt"])["delivery_state"] == "delivered"
    OrcaStore(PairingStore(state / "pairing"))
    library = LibraryStore(state / "library")
    assert library.get(seeded["capture"]) is not None
    refill = RefillStore(state / "inventory" / "refill")
    assert refill.get(seeded["serial"]) is None
    refill.close()
    service = await InventoryService.open(
        state / "inventory", "https://inventory.invalid/", transport=httpx.MockTransport(Provider())
    )
    try:
        snapshot = await service.assignments.snapshot()
        assert snapshot.provider_id == seeded["provider_id"]
        assert [a.spool_id for a in snapshot.assignments] == [5]
    finally:
        await service.close()
    # Nothing is dropped: the cabinet row and the retired journal tables stay.
    with sqlite3.connect(state / "jobs.db") as conn:
        rows = conn.execute("SELECT id FROM spool_inventory").fetchall()
    assert rows == [(seeded["cabinet_spool"],)]
    retired = {
        "inventory_operations",
        "inventory_migrations",
        "inventory_rollbacks",
        "inventory_recoveries",
        "inventory_provider_checkpoint",
    }
    assert retired <= tables(state / "inventory" / "inventory.sqlite")
    assert {"assignment_history", "inventory_material_mappings"} <= tables(
        state / "inventory" / "inventory.sqlite"
    )
    assert {"usage_ledger", "usage_resolutions", "accounting_manifests"} <= tables(
        state / "pairing" / "native-inbox" / "inbox.sqlite3"
    )


def test_the_main_routes_answer_on_the_old_state(state: Path, seeded) -> None:
    settings = Settings(
        bridge_api_key=API_KEY,
        bridge_db_path=str(state / "jobs.db"),
        bridge_pairing_dir=str(state / "pairing"),
        bridge_library_dir=str(state / "library"),
        bridge_log_level="warning",
        bridge_log_format="console",
        bridge_allow_loopback_host=True,
    )
    app = create_app(settings, mqtt_port=1)

    async def open_inventory() -> InventoryService:
        return await InventoryService.open(
            state / "inventory",
            "https://inventory.invalid/",
            transport=httpx.MockTransport(Provider()),
        )

    serial, held, capture = seeded["serial"], seeded["held_attempt"], seeded["capture"]
    with TestClient(app) as client:
        service = client.portal.call(open_inventory)
        app.state.inventory = service

        async def nothing() -> None:
            return None

        # The app closes the inventory and the gateway itself on shutdown.
        app.state.native_gateway = SimpleNamespace(
            inbox=NativeInbox(state / "pairing"), close=nothing
        )
        try:
            printers = client.get("/api/v1/printers", headers=AUTH)
            assert printers.status_code == 200, printers.text
            assert any(p["serial"] == serial for p in printers.json())

            status = client.get("/api/v1/inventory", headers=AUTH)
            assert status.status_code == 200, status.text
            assert status.json()["configured"] and status.json()["review_count"] == 1
            assert status.json()["assignments"][0]["spool_id"] == 5

            listing = client.get("/api/v1/inventory/accounting", headers=AUTH)
            assert listing.status_code == 200, listing.text
            assert {a["attempt_id"] for a in listing.json()["attempts"]} >= {
                held,
                seeded["delivered_attempt"],
            }
            review = client.get("/api/v1/inventory/accounting?delivery_state=review", headers=AUTH)
            assert [a["attempt_id"] for a in review.json()["attempts"]] == [held]

            detail = client.get(f"/api/v1/inventory/accounting/{held}", headers=AUTH)
            assert detail.status_code == 200, detail.text
            # No layer plan in an old manifest: the slicer's figure against the one spool in play.
            assert detail.json()["suggested"] == [{"spool_id": 5, "grams": 83.78}]

            resolved = client.post(
                f"/api/v1/inventory/accounting/{held}/resolve",
                headers=AUTH,
                json={"action": "charged_elsewhere"},
            )
            assert resolved.status_code == 200, resolved.text
            assert resolved.json()["delivery_state"] == "delivered"
            assert client.get("/api/v1/inventory", headers=AUTH).json()["review_count"] == 0

            spools = client.get("/api/v1/inventory/spools", headers=AUTH)
            assert spools.status_code == 200, spools.text

            removed = client.put(
                f"/api/v1/inventory/printers/{serial}/assignment",
                headers=AUTH,
                json={"source": "external", "spool_id": None},
            )
            assert removed.status_code == 200, removed.text
            assert removed.json()["assignments"] == []

            sources = client.get(f"/api/v1/inventory/printers/{serial}/sources", headers=AUTH)
            assert sources.status_code != 500, sources.text
            refill = client.get(f"/api/v1/inventory/printers/{serial}/refill", headers=AUTH)
            assert refill.status_code == 200, refill.text

            captures = client.get("/api/v1/library/captures", headers=AUTH)
            assert captures.status_code == 200, captures.text
            assert any(row["id"] == capture for row in captures.json())
            one = client.get(f"/api/v1/library/captures/{capture}", headers=AUTH)
            assert one.status_code == 200, one.text
            picture = client.get(f"/api/v1/library/captures/{capture}/thumbnail", headers=AUTH)
            assert picture.status_code in (200, 404), picture.text
        finally:
            del service  # closed by the app's shutdown
