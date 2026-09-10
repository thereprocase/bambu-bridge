"""Select platform — print speed level (silent / standard / sport / ludicrous)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BambuConfigEntry
from .api import BridgeError
from .const import SPEED_LEVELS
from .coordinator import BambuBridgeCoordinator
from .entity import BambuBridgeEntity, dig


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BambuConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the print-speed select for every printer."""
    coordinator = entry.runtime_data
    async_add_entities(
        BambuSpeedSelect(coordinator, printer_id)
        for printer_id in coordinator.data
    )


class BambuSpeedSelect(BambuBridgeEntity, SelectEntity):
    """Print speed level — POST /speed {level} (contract §9)."""

    _attr_translation_key = "print_speed_level"
    _attr_options = list(SPEED_LEVELS)

    def __init__(
        self, coordinator: BambuBridgeCoordinator, printer_id: str
    ) -> None:
        super().__init__(coordinator, printer_id)
        self._attr_unique_id = f"{printer_id}_speed_level"

    @property
    def current_option(self) -> str | None:
        # v0 has no clean speed-level readback; derive from preserved _raw.
        raw = dig(self.printer, "_raw", "spd_lvl")
        if raw is None:
            return None
        for name, level in SPEED_LEVELS.items():
            if str(raw) == str(level):
                return name
        return None

    async def async_select_option(self, option: str) -> None:
        try:
            await self.coordinator.client.post_command(
                self._printer_id, "/speed", {"level": SPEED_LEVELS[option]}
            )
        except BridgeError as err:
            raise HomeAssistantError(
                f"Bridge rejected speed change: {err}"
            ) from err
        await self.coordinator.async_request_refresh()
