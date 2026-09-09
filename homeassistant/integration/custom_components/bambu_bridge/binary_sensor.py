"""Binary sensor platform — connectivity, printing, paused, problem."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BambuConfigEntry
from .coordinator import BambuBridgeCoordinator
from .entity import BambuBridgeEntity, dig


@dataclass(frozen=True, kw_only=True)
class BambuBinaryDescription(BinarySensorEntityDescription):
    """Binary sensor description with a state extractor."""

    value_fn: Callable[[dict[str, Any]], bool | None]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # Connectivity must keep reporting when the printer link is down.
    available_offline: bool = False


def _error_attrs(error: Any) -> dict[str, Any]:
    if not isinstance(error, dict):
        return {}
    return {
        "code": error.get("code"),
        "hex": error.get("hex"),
        "text": error.get("text"),
        "category": error.get("category"),
        "severity": error.get("severity"),
        "remediation": error.get("remediation"),
    }


BINARY_SENSORS: tuple[BambuBinaryDescription, ...] = (
    BambuBinaryDescription(
        key="connected",
        translation_key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        available_offline=True,
        value_fn=lambda p: dig(p, "session", "connected"),
    ),
    BambuBinaryDescription(
        key="printing",
        translation_key="printing",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda p: p.get("phase") == "printing",
    ),
    BambuBinaryDescription(
        key="paused",
        translation_key="paused",
        value_fn=lambda p: p.get("phase") == "paused",
    ),
    BambuBinaryDescription(
        key="problem",
        translation_key="problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda p: p.get("print_error") is not None,
        attrs_fn=lambda p: _error_attrs(p.get("print_error")),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BambuConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors for every printer."""
    coordinator = entry.runtime_data
    async_add_entities(
        BambuBinarySensor(coordinator, printer_id, desc)
        for printer_id in coordinator.data
        for desc in BINARY_SENSORS
    )


class BambuBinarySensor(BambuBridgeEntity, BinarySensorEntity):
    """A boolean derived from the translated snapshot."""

    entity_description: BambuBinaryDescription

    def __init__(
        self,
        coordinator: BambuBridgeCoordinator,
        printer_id: str,
        description: BambuBinaryDescription,
    ) -> None:
        super().__init__(coordinator, printer_id)
        self.entity_description = description
        self._attr_unique_id = f"{printer_id}_{description.key}"
        self._requires_connection = not description.available_offline

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.printer)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.printer)
