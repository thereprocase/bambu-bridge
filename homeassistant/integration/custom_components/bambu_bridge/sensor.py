"""Sensor platform — temps, progress, layers, phase, speed/flow, AMS slots."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import BambuConfigEntry
from .const import PHASES
from .coordinator import BambuBridgeCoordinator
from .entity import BambuBridgeEntity, dig


@dataclass(frozen=True, kw_only=True)
class BambuSensorDescription(SensorEntityDescription):
    """Sensor description carrying a value extractor over the snapshot."""

    value_fn: Callable[[dict[str, Any]], Any]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


SENSORS: tuple[BambuSensorDescription, ...] = (
    BambuSensorDescription(
        key="nozzle_temp",
        translation_key="nozzle_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda p: dig(p, "temps", "nozzle", "current_c"),
    ),
    BambuSensorDescription(
        key="nozzle_target",
        translation_key="nozzle_target",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: dig(p, "temps", "nozzle", "target_c"),
    ),
    BambuSensorDescription(
        key="bed_temp",
        translation_key="bed_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda p: dig(p, "temps", "bed", "current_c"),
    ),
    BambuSensorDescription(
        key="bed_target",
        translation_key="bed_target",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: dig(p, "temps", "bed", "target_c"),
    ),
    BambuSensorDescription(
        key="chamber_temp",
        translation_key="chamber_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda p: dig(p, "temps", "chamber", "current_c"),
    ),
    BambuSensorDescription(
        key="progress",
        translation_key="progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        # null until layer_num > 0 — the prep-vs-progress rule (contract §6.0.1).
        value_fn=lambda p: dig(p, "job", "percent"),
    ),
    BambuSensorDescription(
        key="current_layer",
        translation_key="current_layer",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda p: dig(p, "job", "layer_num"),
    ),
    BambuSensorDescription(
        key="total_layers",
        translation_key="total_layers",
        value_fn=lambda p: dig(p, "job", "total_layer_num"),
    ),
    BambuSensorDescription(
        key="time_remaining",
        translation_key="time_remaining",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda p: dig(p, "job", "remaining_min"),
    ),
    BambuSensorDescription(
        key="print_started",
        translation_key="print_started",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda p: dt_util.parse_datetime(dig(p, "job", "started_at") or ""),
    ),
    BambuSensorDescription(
        key="phase",
        translation_key="phase",
        device_class=SensorDeviceClass.ENUM,
        options=list(PHASES),
        value_fn=lambda p: p.get("phase"),
    ),
    BambuSensorDescription(
        key="job_name",
        translation_key="job_name",
        value_fn=lambda p: dig(p, "job", "subtask_name"),
    ),
    BambuSensorDescription(
        key="headline",
        translation_key="headline",
        value_fn=lambda p: dig(p, "headline", "title"),
        attrs_fn=lambda p: {
            "subtitle": dig(p, "headline", "subtitle"),
            "indicator": dig(p, "headline", "indicator"),
        },
    ),
    BambuSensorDescription(
        key="print_speed",
        translation_key="print_speed",
        native_unit_of_measurement="mm/s",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: dig(p, "print_params", "speed_mm_s"),
    ),
    BambuSensorDescription(
        key="flow",
        translation_key="flow",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda p: dig(p, "print_params", "flow_pct"),
    ),
    BambuSensorDescription(
        key="ams_active_slot",
        translation_key="ams_active_slot",
        # int (physical slot), the string "external", or null — leave as-is.
        value_fn=lambda p: dig(p, "ams", "engaged_slot"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BambuConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors for every printer the bridge currently reports."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for printer_id, snapshot in coordinator.data.items():
        entities.extend(
            BambuSensor(coordinator, printer_id, desc) for desc in SENSORS
        )
        for slot in dig(snapshot, "ams", "slots") or []:
            physical = slot.get("physical_slot")
            if physical is not None:
                entities.append(
                    BambuAmsSlotSensor(coordinator, printer_id, physical)
                )
    async_add_entities(entities)


class BambuSensor(BambuBridgeEntity, SensorEntity):
    """A single value pulled from the translated snapshot."""

    entity_description: BambuSensorDescription

    def __init__(
        self,
        coordinator: BambuBridgeCoordinator,
        printer_id: str,
        description: BambuSensorDescription,
    ) -> None:
        super().__init__(coordinator, printer_id)
        self.entity_description = description
        self._attr_unique_id = f"{printer_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.printer)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.printer)


class BambuAmsSlotSensor(BambuBridgeEntity, SensorEntity):
    """Remaining filament % for one AMS slot; material/colour as attributes."""

    _attr_translation_key = "ams_slot"
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(
        self,
        coordinator: BambuBridgeCoordinator,
        printer_id: str,
        physical_slot: int,
    ) -> None:
        super().__init__(coordinator, printer_id)
        self._slot = physical_slot
        self._attr_unique_id = f"{printer_id}_ams_slot_{physical_slot}"
        self._attr_translation_placeholders = {"slot": str(physical_slot)}

    def _slot_data(self) -> dict[str, Any]:
        for slot in dig(self.printer, "ams", "slots") or []:
            if slot.get("physical_slot") == self._slot:
                return slot
        return {}

    @property
    def native_value(self) -> Any:
        return self._slot_data().get("remaining_pct")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slot = self._slot_data()
        attrs: dict[str, Any] = {
            "material": slot.get("type"),
            "color": slot.get("color"),
            "state": slot.get("state"),
            "rfid_tray": slot.get("rfid_tray"),
            "remaining_g": slot.get("remaining_g"),
        }
        # G3 — merge filament memory fields when present.
        mem = slot.get("memory")
        if isinstance(mem, dict):
            attrs["filament_make"] = mem.get("make")
            attrs["filament_model"] = mem.get("model")
            attrs["filament_profile"] = mem.get("profile")
        else:
            attrs["filament_make"] = None
            attrs["filament_model"] = None
            attrs["filament_profile"] = None
        return attrs
