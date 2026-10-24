import pytest

from bambu_bridge.inventory_journal import InventoryConflict
from tests.test_inventory_service import Provider, open_service


async def test_policy_is_revisioned_persistent_and_namespaced(tmp_path):
    provider = Provider()
    inventory = await open_service(tmp_path, provider)
    namespace = (await inventory.assignments.snapshot()).provider_id
    try:
        before = await inventory.display_policy.snapshot(namespace)
        assert before == {
            "provider_id": namespace,
            "revision": 0,
            "global_enabled": False,
            "printers": [],
        }
        saved = await inventory.set_display_policy(
            provider_id=namespace, revision=0, enabled=True, printers=["p1"], qualified=True
        )
        assert saved["revision"] == 1
        with pytest.raises(InventoryConflict, match="preferences changed"):
            await inventory.set_display_policy(
                provider_id=namespace, revision=0, enabled=False, printers=[], qualified=True
            )
        assert not (await inventory.display_policy.snapshot("different-provider"))["global_enabled"]
        assert not provider.writes
    finally:
        await inventory.close()
    inventory = await open_service(tmp_path, provider)
    try:
        assert await inventory.display_policy.snapshot(namespace) == saved
        with pytest.raises(InventoryConflict, match="not runtime qualified"):
            await inventory.set_display_policy(
                provider_id=namespace, revision=1, enabled=True, printers=["p1"], qualified=False
            )
        cleared = await inventory.set_display_policy(
            provider_id=namespace, revision=1, enabled=False, printers=[], qualified=False
        )
        assert cleared["revision"] == 2 and not cleared["global_enabled"]
    finally:
        await inventory.close()
