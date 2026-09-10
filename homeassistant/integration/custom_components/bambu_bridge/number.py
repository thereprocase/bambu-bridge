"""Number platform — nozzle / bed target-temperature setpoints."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BambuConfigEntry
from .api import BridgeError
from .coordinator import BambuBridgeCoordinator
from .entity import BambuBridgeEntity, dig


@dataclass(frozen=True, kw_only=True)
class BambuNumberDescription(NumberEntityDescription):
    """Number description — snapshot reader + the /temperature body field."""

    value_fn: Callable[[dict[str, Any]], float | None]
    field: str


# Ranges are the G4 safety envelope the bridge also enforces (contract §9).
NUMBERS: tuple[BambuNumberDescription, ...] = (
    BambuNumberDescription(
        key="nozzle_target",
        translation_key="nozzle_target",
        field="nozzle",
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=0,
        native_max_value=280,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda p: dig(p, "temps", "nozzle", "target_c"),
    ),
    BambuNumberDescription(
        key="bed_target",
        translation_key="bed_target",
        field="bed",
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=0,
        native_max_value=120,
        native_step=1,
        mode=NumberMode.BOX,
        value_fn=lambda p: dig(p, "temps", "bed", "target_c"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BambuConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the temperature setpoints for every printer."""
    coordinator = entry.runtime_data
    async_add_entities(
        BambuNumber(coordinator, printer_id, desc)
        for printer_id in coordinator.data
        for desc in NUMBERS
    )


class BambuNumber(BambuBridgeEntity, NumberEntity):
    """A target-temperature setpoint."""

    entity_description: BambuNumberDescription

    def __init__(
        self,
        coordinator: BambuBridgeCoordinator,
        printer_id: str,
        description: BambuNumberDescription,
    ) -> None:
        super().__init__(coordinator, printer_id)
        self.entity_description = description
        self._attr_unique_id = f"{printer_id}_{description.key}"

    @property
    def native_value(self) -> float | None:
        return self.entity_description.value_fn(self.printer)

    async def async_set_native_value(self, value: float) -> None:
        body = {self.entity_description.field: int(value)}
        try:
            await self.coordinator.client.post_command(
                self._printer_id, "/temperature", body
            )
        except BridgeError as err:
            raise HomeAssistantError(
                f"Bridge rejected temperature: {err}"
            ) from err
        await self.coordinator.async_request_refresh()
