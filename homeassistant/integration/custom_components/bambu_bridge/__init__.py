"""The Bambu Bridge integration.

A Home Assistant client of the bambu-bridge server. The bridge owns the printer
protocol; this integration consumes its `/api/v1` REST + WebSocket surface and
maps the translated snapshot to HA entities.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BambuBridgeClient
from .const import CONF_BASE_URL, PLATFORMS
from .coordinator import BambuBridgeCoordinator

type BambuConfigEntry = ConfigEntry[BambuBridgeCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: BambuConfigEntry) -> bool:
    """Set up Bambu Bridge from a config entry."""
    client = BambuBridgeClient(
        async_get_clientsession(hass),
        entry.data[CONF_BASE_URL],
        entry.data[CONF_API_KEY],
    )
    coordinator = BambuBridgeCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_start()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BambuConfigEntry) -> bool:
    """Unload a config entry — stop the WebSocket loops, drop the platforms."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded
