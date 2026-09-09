"""Camera platform — polled snapshot of the printer's chamber camera."""

from __future__ import annotations

import logging

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BambuConfigEntry
from .api import BridgeError
from .coordinator import BambuBridgeCoordinator
from .entity import BambuBridgeEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BambuConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a camera per printer."""
    coordinator = entry.runtime_data
    async_add_entities(
        BambuCamera(coordinator, printer_id) for printer_id in coordinator.data
    )


class BambuCamera(BambuBridgeEntity, Camera):
    """Chamber camera — 1 fps JPEG snapshot poll (contract §11)."""

    _attr_translation_key = "camera"
    # The bridge serves ~1 fps; no MJPEG stream in v0 (contract §11.3).
    _attr_frame_interval = 1.0

    def __init__(
        self, coordinator: BambuBridgeCoordinator, printer_id: str
    ) -> None:
        super().__init__(coordinator, printer_id)
        Camera.__init__(self)
        self._attr_unique_id = f"{printer_id}_camera"

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        try:
            return await self.coordinator.client.snapshot(self._printer_id)
        except BridgeError as err:
            _LOGGER.debug("camera snapshot failed for %s: %s", self._printer_id, err)
            return None
