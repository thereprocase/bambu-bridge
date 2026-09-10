"""Shared base entity + a small snapshot-digging helper."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import BambuBridgeCoordinator


def dig(data: Mapping[str, Any] | None, *path: str) -> Any:
    """Safely walk a nested dict — returns None on any missing/typed-out key.

    Snapshots arrive partial (WS deltas, cold seed), so every read is defensive.
    """
    cur: Any = data
    for key in path:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(key)
    return cur


def _firmware(printer: Mapping[str, Any]) -> str | None:
    """Best-effort printer firmware string from the preserved `_raw.info`."""
    modules = dig(printer, "_raw", "info", "module")
    if isinstance(modules, list):
        for mod in modules:
            if isinstance(mod, Mapping) and mod.get("name") in ("ota", "esp32"):
                ver = mod.get("sw_ver") or mod.get("firmware_version")
                if ver:
                    return str(ver)
    return None


class BambuBridgeEntity(CoordinatorEntity[BambuBridgeCoordinator]):
    """Base entity: device identity (one HA device per printer) + availability."""

    _attr_has_entity_name = True
    # Most entities are meaningless when the printer link is down. The
    # connectivity binary sensor flips this off so it can still report "off".
    _requires_connection = True

    def __init__(self, coordinator: BambuBridgeCoordinator, printer_id: str) -> None:
        super().__init__(coordinator)
        self._printer_id = printer_id

    @property
    def printer(self) -> dict[str, Any]:
        """Current translated snapshot for this printer (may be partial)."""
        return self.coordinator.data.get(self._printer_id, {})

    @property
    def device_info(self) -> DeviceInfo:
        printer = self.printer
        serial = printer.get("serial") or self._printer_id
        return DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=printer.get("friendly_name") or f"Bambu {serial}",
            manufacturer=MANUFACTURER,
            model=printer.get("model"),
            serial_number=serial,
            sw_version=_firmware(printer),
            configuration_url=self.coordinator.client.base_url,
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self._printer_id not in self.coordinator.data:
            return False
        if not self._requires_connection:
            return True
        session = self.printer.get("session") or {}
        # Absent during the first seed — treat unknown as available so
        # entities aren't all greyed out before the WS snapshot lands.
        return bool(session.get("connected", True))
