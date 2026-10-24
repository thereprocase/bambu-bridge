"""Import path kept until service/inventory.py switches to slot_display."""

from bambu_bridge.service.slot_display import InventoryProjection, build_projection

__all__ = ["InventoryProjection", "build_projection"]
