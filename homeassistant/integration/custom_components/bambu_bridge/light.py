"""Light platform — the chamber light (on/off only)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BambuConfigEntry
from .api import BridgeError
from .coordinator import BambuBridgeCoordinator
from .entity import BambuBridgeEntity, dig


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BambuConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the chamber light for every printer."""
    coordinator = entry.runtime_data
    async_add_entities(
        BambuChamberLight(coordinator, printer_id)
        for printer_id in coordinator.data
    )


class BambuChamberLight(BambuBridgeEntity, LightEntity):
    """Chamber light — POST /light {on} (contract §9)."""

    _attr_translation_key = "chamber_light"
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(
        self, coordinator: BambuBridgeCoordinator, printer_id: str
    ) -> None:
        super().__init__(coordinator, printer_id)
        self._attr_unique_id = f"{printer_id}_chamber_light"

    @property
    def is_on(self) -> bool:
        return bool(dig(self.printer, "lights", "chamber_on"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, on: bool) -> None:
        try:
            await self.coordinator.client.post_command(
                self._printer_id, "/light", {"on": on}
            )
        except BridgeError as err:
            raise HomeAssistantError(
                f"Bridge rejected light command: {err}"
            ) from err
        await self.coordinator.async_request_refresh()
