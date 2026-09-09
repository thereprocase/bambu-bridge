"""Fan platform — part / aux / chamber cooling fans (0-100%)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BambuConfigEntry
from .api import BridgeError
from .coordinator import BambuBridgeCoordinator
from .entity import BambuBridgeEntity, dig

# (POST /fan `part` value, cooling.* snapshot key, translation key).
FANS: tuple[tuple[str, str, str], ...] = (
    ("part", "part_fan", "part_fan"),
    ("aux", "aux_fan", "aux_fan"),
    ("chamber", "chamber_fan", "chamber_fan"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BambuConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the cooling fans for every printer."""
    coordinator = entry.runtime_data
    async_add_entities(
        BambuFan(coordinator, printer_id, kind, cooling_key, translation_key)
        for printer_id in coordinator.data
        for kind, cooling_key, translation_key in FANS
    )


class BambuFan(BambuBridgeEntity, FanEntity):
    """A cooling fan — percentage maps to the bridge's native 0-15 scale."""

    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )

    def __init__(
        self,
        coordinator: BambuBridgeCoordinator,
        printer_id: str,
        kind: str,
        cooling_key: str,
        translation_key: str,
    ) -> None:
        super().__init__(coordinator, printer_id)
        self._kind = kind
        self._cooling_key = cooling_key
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{printer_id}_{kind}_fan"

    @property
    def percentage(self) -> int | None:
        return dig(self.printer, "cooling", self._cooling_key, "percent")

    @property
    def is_on(self) -> bool:
        pct = self.percentage
        return pct is not None and pct > 0

    async def async_set_percentage(self, percentage: int) -> None:
        await self._post(percentage)

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        await self._post(100 if percentage is None else percentage)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._post(0)

    async def _post(self, percentage: int) -> None:
        try:
            await self.coordinator.client.post_command(
                self._printer_id,
                "/fan",
                {"part": self._kind, "percent": int(percentage)},
            )
        except BridgeError as err:
            raise HomeAssistantError(
                f"Bridge rejected fan command: {err}"
            ) from err
        await self.coordinator.async_request_refresh()
