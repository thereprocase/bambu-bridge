import pytest

from bambu_bridge.inventory_journal import InventoryConflict
from bambu_bridge.inventory_sources import source_choices
from bambu_bridge.native_spools import NativeSpoolOverlay
from bambu_bridge.spoolman import Product
from tests.test_inventory_service import PRODUCT, Provider, open_service
from tests.test_inventory_sources import printer


async def configured(tmp_path):
    provider = Provider()
    product = {**PRODUCT, "diameter": 1.75, "color_hex": "112233"}
    provider.spool = {**provider.spool, "filament": product}
    inventory = await open_service(tmp_path, provider)
    service = printer()
    frame = service.material_inventory.snapshot()["ams"]
    frame["ams"][0]["tray"][0].update(tag_uid="0", tray_info_idx="GFB98")
    service.material_inventory.observe({"ams": frame})
    choice = source_choices(service)[0]
    snapshot = await inventory.assignments.snapshot()
    await inventory.assign(
        provider_id=snapshot.provider_id,
        expected_revision=snapshot.revision,
        spool_id=43,
        printer_id=service.serial,
        source=choice["source"],
        observed_identity=choice["observed_identity"],
        choices=lambda: source_choices(service),
    )
    await inventory.materials.select(
        snapshot.provider_id, Product.model_validate(product), "Generic ASA @base.json", revision=0
    )
    return inventory, provider, service


async def test_verified_candidates_project_only_on_client_copy(tmp_path):
    inventory, provider, service = await configured(tmp_path)
    try:
        before = service.material_inventory.snapshot()
        projection = await inventory.native_projection(service)
        (binding,) = projection.bindings
        assert binding.material_id == "GFB98"
        assert binding.color == "112233FF"
        assert binding.remaining_percent == 44
        overlay = NativeSpoolOverlay("fixture", projection.bindings)
        assert not overlay.enabled
        overlay.enabled = True  # isolated protocol test, not a configured gateway
        raw = {"print": {"ams": before["ams"]}}
        result = overlay.project(raw, raw)
        assert result["print"]["ams"]["ams"][0]["tray"][0]["remain"] == 44
        assert service.material_inventory.snapshot() == before
        assert raw["print"]["ams"]["ams"][0]["tray"][0]["tag_uid"] == "0"
        assert not provider.writes
    finally:
        await inventory.close()


@pytest.mark.parametrize("change", ["archived", "renamed", "rfid", "redeclared"])
async def test_invalid_candidates_are_withdrawn_without_retargeting(tmp_path, change):
    inventory, provider, service = await configured(tmp_path)
    try:
        if change == "archived":
            provider.spool["archived"] = True
        elif change == "renamed":
            provider.spool["filament"] = {**provider.spool["filament"], "name": "New product"}
        else:
            frame = service.material_inventory.snapshot()["ams"]
            if change == "rfid":
                frame["ams"][0]["tray"][0]["tag_uid"] = "1234567890ABCDEF"
            else:
                frame["ams"][0]["tray"][0]["tray_type"] = "PETG"
            service.material_inventory.observe({"ams": frame})
        result = await inventory.native_projection(service)
        assert not result.bindings
        assert result.issues
        assert not provider.writes
    finally:
        await inventory.close()


async def test_source_change_during_provider_read_discards_whole_projection(tmp_path):
    inventory, _, service = await configured(tmp_path)
    get_spool = inventory.provider.get_spool

    async def racing_read(spool_id):
        spool = await get_spool(spool_id)
        frame = service.material_inventory.snapshot()["ams"]
        frame["ams"][0]["tray"][0]["tray_type"] = "PETG"
        service.material_inventory.observe({"ams": frame})
        return spool

    inventory.provider.get_spool = racing_read
    try:
        with pytest.raises(InventoryConflict, match="changed during projection"):
            await inventory.native_projection(service)
    finally:
        await inventory.close()


async def test_identical_receipt_during_provider_read_preserves_confirmation(tmp_path):
    inventory, _, service = await configured(tmp_path)
    get_spool = inventory.provider.get_spool

    async def fresh_receipt(spool_id):
        spool = await get_spool(spool_id)
        service.material_inventory.observe({"ams": service.material_inventory.snapshot()["ams"]})
        return spool

    inventory.provider.get_spool = fresh_receipt
    try:
        assert len((await inventory.native_projection(service)).bindings) == 1
    finally:
        await inventory.close()
