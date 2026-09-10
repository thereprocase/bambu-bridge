"""Constants for the Bambu Bridge integration.

The integration is a thin Home Assistant client of the bambu-bridge server
(`docs/API-CONTRACT.md` in the same repo). It does not speak the printer
protocol itself — the bridge owns the single MQTT/TLS/FTPS connection.
"""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "bambu_bridge"
MANUFACTURER = "Bambu Lab"

CONF_BASE_URL = "base_url"
# The API key uses homeassistant.const.CONF_API_KEY.

DEFAULT_BASE_URL = "http://localhost:8080"

# WS reconnect backoff schedule in seconds (API contract §5.4).
WS_BACKOFF: tuple[int, ...] = (1, 2, 4, 8, 16, 30)

# Slow REST reconcile interval — a safety net behind the push WebSocket.
# It also discovers printers added to the bridge after setup.
RECONCILE_INTERVAL_MINUTES = 5

# How often the per-printer event poller ticks (seconds).  The bridge event
# table is a supplement to the WS push path: we poll rather than stream so
# that events written between WS reconnects are never silently dropped.
EVENT_POLL_INTERVAL_SECONDS = 30

# HA event-bus event fired for every bridge WS `event` frame (contract §12).
EVENT_BRIDGE = "bambu_bridge_event"

PLATFORMS: tuple[Platform, ...] = (
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.FAN,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
)

# Print lifecycle phases the bridge emits (contract §6.1).
PHASES: tuple[str, ...] = (
    "idle",
    "preparing",
    "printing",
    "paused",
    "completed",
    "failed",
    "unknown",
)

# Print speed presets — label -> level int for POST /speed (contract §9).
SPEED_LEVELS: dict[str, int] = {
    "silent": 1,
    "standard": 2,
    "sport": 3,
    "ludicrous": 4,
}
