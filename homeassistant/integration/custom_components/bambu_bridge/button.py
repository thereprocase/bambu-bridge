"""Button platform — pause / resume / stop / home."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BambuConfigEntry
from .api import BridgeError
from .coordinator import BambuBridgeCoordinator
from .entity import BambuBridgeEntity


@dataclass(frozen=True, kw_only=True)
class BambuButtonDescription(ButtonEntityDescription):
    """Button description — the control endpoint to POST (contract §9)."""

    api_path: str
    api_body: dict[str, Any] | None = None


BUTTONS: tuple[BambuButtonDescription, ...] = (
    BambuButtonDescription(
        key="pause", translation_key="pause", api_path="/print/pause"
    ),
    BambuButtonDescription(
        key="resume", translation_key="resume", api_path="/print/resume"
    ),
    BambuButtonDescription(
        key="stop", translation_key="stop", api_path="/print/stop"
    ),
    BambuButtonDescription(key="home", translation_key="home", api_path="/home"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BambuConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up control buttons for every printer."""
    coordinator = entry.runtime_data
    async_add_entities(
        BambuButton(coordinator, printer_id, desc)
        for printer_id in coordinator.data
        for desc in BUTTONS
    )


class BambuButton(BambuBridgeEntity, ButtonEntity):
    """A one-shot control action forwarded to the bridge."""

    entity_description: BambuButtonDescription

    def __init__(
        self,
        coordinator: BambuBridgeCoordinator,
        printer_id: str,
        description: BambuButtonDescription,
    ) -> None:
        super().__init__(coordinator, printer_id)
        self.entity_description = description
        self._attr_unique_id = f"{printer_id}_{description.key}"

    async def async_press(self) -> None:
        desc = self.entity_description
        try:
            await self.coordinator.client.post_command(
                self._printer_id, desc.api_path, desc.api_body
            )
        except BridgeError as err:
            raise HomeAssistantError(f"Bridge rejected {desc.key}: {err}") from err
        # The WS pushes the real transition; refresh gives snappy feedback.
        await self.coordinator.async_request_refresh()
