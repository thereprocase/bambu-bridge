import asyncio
from unittest.mock import AsyncMock

import pytest

from bambu_bridge.inventory_journal import InventoryConflict, InventoryJournal
from bambu_bridge.material_mappings import MaterialMappings, catalogue
from bambu_bridge.spoolman import Product

KEY = "Polymaker/PolyLite ASA @base.json"
PRODUCT = Product(
    id=2, name="ASA Black", material="ASA", diameter=1.75, weight=1000, color_hex="000000"
)


@pytest.fixture
async def mappings(tmp_path):
    journal = await InventoryJournal.open(tmp_path / "inventory.sqlite")
    yield MaterialMappings(journal)
    await journal.close()


def test_pinned_catalogue_keeps_known_stock_ids_and_real_material_inheritance():
    profiles = {p.key: p for p in catalogue()}
    assert len(profiles) == 180
    assert profiles[KEY].material_id == "GFB61"
    assert profiles["Generic ASA @base.json"].material_id == "GFB98"
    assert profiles["Generic PETG @base.json"].material_id == "GFG99"
    assert profiles["Generic PC @base.json"].material_id == "GFC99"
    assert profiles["Generic PCTG @base.json"].material == "PCTG"
    assert all(
        len(p.resource_sha256) == 64 and len(p.inheritance_sha256) == 64 for p in profiles.values()
    )


async def test_selection_survives_restart_and_is_provider_namespaced(tmp_path):
    journal = await InventoryJournal.open(tmp_path / "inventory.sqlite")
    store = MaterialMappings(journal)
    await store.select("provider-a", PRODUCT, KEY, revision=0)
    await journal.close()
    journal = await InventoryJournal.open(tmp_path / "inventory.sqlite")
    try:
        store = MaterialMappings(journal)
        result = await store.lookup("provider-a", PRODUCT)
        assert result["selection"]["material_id"] == "GFB61"
        assert result["selection"]["runtime_qualified"] is False
        assert (await store.lookup("provider-b", PRODUCT))["selection"] is None
        with pytest.raises(InventoryConflict):
            await store.select("provider-a", PRODUCT, None, revision=0)
        assert (await store.snapshot("provider-a"))["revision"] == 1
    finally:
        await journal.close()


async def test_changed_product_or_catalogue_needs_review_but_color_correction_keeps_material(
    mappings, monkeypatch
):
    await mappings.select("provider", PRODUCT, KEY, revision=0)
    assert (await mappings.lookup("provider", PRODUCT.model_copy(update={"color_hex": "FFFFFF"})))[
        "selection"
    ]
    changed = PRODUCT.model_copy(update={"name": "Another manufacturer product"})
    assert (await mappings.lookup("provider", changed))["issue"] == "inventory_product_changed"
    monkeypatch.setattr("bambu_bridge.material_mappings.catalogue_digest", lambda: "new-catalogue")
    assert (await mappings.lookup("provider", PRODUCT))["issue"] == "catalogue_changed"


@pytest.mark.parametrize(
    "product,key",
    [
        (PRODUCT, "not-a-stock-profile"),
        (PRODUCT, "Generic PC @base.json"),
        (PRODUCT.model_copy(update={"diameter": 2.85}), KEY),
        (PRODUCT.model_copy(update={"diameter": None}), KEY),
    ],
)
async def test_unknown_profile_wrong_chemistry_or_diameter_cannot_be_selected(
    mappings, product, key
):
    with pytest.raises(ValueError):
        await mappings.select("provider", product, key, revision=0)
    assert (await mappings.snapshot("provider"))["revision"] == 0


async def test_commit_failure_does_not_publish_a_partial_mapping_or_revision(mappings, monkeypatch):
    monkeypatch.setattr(mappings.conn, "commit", AsyncMock(side_effect=OSError("commit failed")))
    with pytest.raises(OSError):
        await mappings.select("provider", PRODUCT, KEY, revision=0)
    assert await mappings.snapshot("provider") == {
        "provider_id": "provider",
        "revision": 0,
        "mappings": [],
    }


async def test_two_phones_cannot_replace_each_others_reviewed_revision(mappings):
    results = await asyncio.gather(
        mappings.select("provider", PRODUCT, KEY, revision=0),
        mappings.select("provider", PRODUCT, "Generic ASA @base.json", revision=0),
        return_exceptions=True,
    )
    assert sum(isinstance(result, InventoryConflict) for result in results) == 1
    assert (await mappings.snapshot("provider"))["revision"] == 1
