"""Config flow for Bambu Bridge.

One config entry represents one bridge (a hub); each printer the bridge knows
becomes a Home Assistant device under it. Setup needs just the bridge base URL
and the bearer API key (contract §1).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BambuBridgeClient, BridgeAuthError, BridgeConnectionError, BridgeError
from .const import CONF_BASE_URL, DEFAULT_BASE_URL, DOMAIN


def _unique_id(base_url: str) -> str:
    """Stable per-bridge id — the host:port, so re-adds dedupe."""
    parsed = urlparse(base_url)
    return (parsed.netloc or base_url).lower()


def _title(base_url: str) -> str:
    parsed = urlparse(base_url)
    return f"Bambu Bridge ({parsed.hostname or base_url})"


class BambuBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the user-driven setup of a bridge."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect base URL + API key, validate, and create the entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            base_url = user_input[CONF_BASE_URL].rstrip("/")
            api_key = user_input[CONF_API_KEY]
            error = await self._validate(base_url, api_key)
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(_unique_id(base_url))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=_title(base_url),
                    data={CONF_BASE_URL: base_url, CONF_API_KEY: api_key},
                )

        suggested = (user_input or {}).get(CONF_BASE_URL, DEFAULT_BASE_URL)
        schema = vol.Schema(
            {
                vol.Required(CONF_BASE_URL, default=suggested): str,
                vol.Required(CONF_API_KEY): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    async def _validate(self, base_url: str, api_key: str) -> str | None:
        """Probe the bridge. Returns an error key, or None when good."""
        client = BambuBridgeClient(
            async_get_clientsession(self.hass), base_url, api_key
        )
        try:
            await client.get_version()
        except BridgeError:
            return "cannot_connect"
        try:
            await client.list_printers()
        except BridgeAuthError:
            return "invalid_auth"
        except BridgeConnectionError:
            return "cannot_connect"
        except BridgeError:
            return "unknown"
        return None
