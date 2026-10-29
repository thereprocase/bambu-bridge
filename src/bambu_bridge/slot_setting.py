"""Set an AMS slot or the external holder to the material of the spool put there.

Untagged spools only tell the printer what they are through this per-slot
setting. When the owner puts a spool in a slot that reports another material,
the bridge writes the spool's material, colour and a nozzle range, waits for
the printer to report it back, and only then assigns. A slot holding an RFID
spool is never overwritten: the tag is the printer's own reading.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from bambu_bridge.protocol import commands

# Nozzle ranges (°C) from OrcaSlicer's MaterialType table, limited to materials
# a P1S can print; the bridge's own ceiling still applies on top.
MATERIAL_NOZZLE_C: dict[str, tuple[int, int]] = {
    "ABS": (190, 300),
    "ABS-CF": (220, 300),
    "ABS-GF": (240, 280),
    "ASA": (220, 300),
    "ASA-CF": (230, 300),
    "ASA-GF": (240, 300),
    "HIPS": (220, 270),
    "PA": (235, 280),
    "PA-CF": (240, 315),
    "PA-GF": (240, 290),
    "PA6-CF": (230, 300),
    "PA6-GF": (260, 300),
    "PC": (240, 300),
    "PC-ABS": (230, 270),
    "PC-CF": (270, 295),
    "PCTG": (220, 300),
    "PET": (200, 290),
    "PET-CF": (240, 320),
    "PET-GF": (280, 320),
    "PETG": (190, 260),
    "PETG-CF": (230, 290),
    "PETG-GF": (210, 270),
    "PLA": (180, 240),
    "PLA-CF": (190, 250),
    "PPA-CF": (260, 300),
    "PPA-GF": (260, 290),
    "PVA": (185, 250),
    "TPU": (175, 260),
}

# Bambu's "Generic <type>" presets (data/stock_materials_v242.json). Orca lists a
# slot only when its preset id resolves, so every material with a stock generic
# preset needs its id here; the rest are written as manual entries.
GENERIC_TRAY_INFO_IDX = {
    "ABS": "GFB99",
    "ASA": "GFB98",
    "HIPS": "GFS98",
    "PA": "GFN99",
    "PA-CF": "GFN98",
    "PC": "GFC99",
    "PCTG": "GFG97",
    "PETG": "GFG99",
    "PETG-CF": "GFG98",
    "PLA": "GFL99",
    "PLA-CF": "GFL98",
    "PPA-CF": "GFN97",
    "PPA-GF": "GFN96",
    "PVA": "GFS99",
    "TPU": "GFU99",
}

REPORT_WAIT_S = 20.0
# The printer can apply the setting after the first full report it sends back.
REPORT_REQUEST_EVERY_S = 3.0


def slot_setting(material: str, color_hex: str | None) -> dict[str, Any]:
    """The ams_filament_setting fields for a spool of this material and colour."""
    tray_type = material.strip().upper()
    if tray_type not in MATERIAL_NOZZLE_C:
        raise ValueError(f"The printer has no slot setting for {material or 'this material'}.")
    low, high = MATERIAL_NOZZLE_C[tray_type]
    high = min(high, commands.NOZZLE_MAX_C)
    if low >= high:
        raise ValueError(f"{tray_type} needs a hotter nozzle than this printer allows.")
    color = (color_hex or "").strip().lstrip("#").upper()
    if len(color) == 6:
        color += "FF"
    if len(color) != 8 or any(c not in "0123456789ABCDEF" for c in color):
        color = "FFFFFFFF"
    return {
        "tray_type": tray_type,
        "tray_info_idx": GENERIC_TRAY_INFO_IDX.get(tray_type, ""),
        "tray_color": color,
        "nozzle_temp_min": low,
        "nozzle_temp_max": high,
    }


async def write_slot(
    service: Any,
    wire: int,
    setting: dict[str, Any],
    reported: Any,
    *,
    wait_s: float | None = None,
) -> bool:
    """Send the setting, then wait until reported() is true; False on timeout."""
    wait_s = REPORT_WAIT_S if wait_s is None else wait_s
    if wire == 254:
        ams_id, tray_id = commands.EXTERNAL_AMS_ID, commands.EXTERNAL_TRAY_ID
    else:
        ams_id, tray_id = divmod(wire, 4)
    envelope = commands.ams_filament_setting(ams_id=ams_id, tray_id=tray_id, **setting)
    async with service.raw_bus.subscribe() as reports:
        await service.send_raw(envelope)
        deadline = asyncio.get_running_loop().time() + wait_s
        while (left := deadline - asyncio.get_running_loop().time()) > 0:
            # A P1S sends full AMS frames only when asked.
            await service.request_full_report()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(min(REPORT_REQUEST_EVERY_S, left)):
                    if reported():
                        return True
                    async for _ in reports:
                        if reported():
                            return True
    return reported()
