"""Small HMS compatibility lookup and raw-code fallback.

Earlier documentation cited Bambu Studio and ha-bambulab. See THIRD_PARTY.md
for license provenance and firmware-validation limits. This is not a complete
or authoritative vendor error catalog.
"""

from __future__ import annotations

from typing import Literal, TypedDict

Severity = Literal["info", "warn", "error", "unknown"]


class HmsEntry(TypedDict):
    severity: Severity
    category: str
    user_message: str
    remediation: str | None


# Codes are stored as the canonical hex string form, e.g. "0300_0d00_0003_0001".
# The runout codes are the ones PrinterService._FILAMENT_RUNOUT_CODES already
# inlined; we promote them here so there's one source of truth.
_TABLE: dict[str, HmsEntry] = {
    "0300_0d00_0003_0001": {
        "severity": "warn",
        "category": "ams",
        "user_message": "Filament runout — AMS spool empty.",
        "remediation": "Load a fresh spool into the AMS slot and resume.",
    },
    "0300_8013_0002_0001": {
        "severity": "warn",
        "category": "ams",
        "user_message": "Filament runout — external spool empty.",
        "remediation": "Reload the external spool and resume.",
    },
    "0300_0c00_0003_0001": {
        "severity": "warn",
        "category": "ams",
        "user_message": "Filament tangled in the AMS — feed cannot advance.",
        "remediation": "Open the AMS, untangle, and press Resume.",
    },
    "0300_1100_0001_0001": {
        "severity": "error",
        "category": "thermal",
        "user_message": "Nozzle temperature out of range.",
        "remediation": "Check the hotend cartridge and thermistor wiring.",
    },
    "0500_0100_0001_0001": {
        "severity": "error",
        "category": "bed",
        "user_message": "Bed leveling failed — the printer couldn't probe the surface.",
        "remediation": "Clean the bed and the probe, then retry.",
    },
}


def lookup(code: str | int | None) -> HmsEntry:
    """Decode an HMS / print_error code into a structured entry.

    Accepts:
    - the canonical hex string `"0300_0d00_0003_0001"`
    - an int (the raw `print_error` from MQTT) — decoded to its 4-group hex form
    - `None` or `0` → `info / cleared` (callers usually filter these out
      before lookup, but the function is total so it doesn't crash)
    """
    if code is None or code == 0 or code == "0" or code == "":
        return {
            "severity": "info",
            "category": "cleared",
            "user_message": "No active error.",
            "remediation": None,
        }
    canonical = _canonical(code)
    entry = _TABLE.get(canonical)
    if entry is not None:
        return entry
    return {
        "severity": "unknown",
        "category": "unmapped",
        "user_message": canonical,
        "remediation": None,
    }


def _canonical(code: str | int) -> str:
    """Normalise to the `aaaa_bbbb_cccc_dddd` lowercase hex form."""
    if isinstance(code, int):
        # 32-bit packed: high16 | low16 (P1S sometimes ships a single int)
        # but more commonly the 128-bit form arrives as four 32-bit words.
        # Best-effort: render as 8-digit hex split 4/4. Treat as one 32-bit
        # group + three zero groups so unknown codes still round-trip.
        hex8 = f"{code & 0xFFFFFFFF:08x}"
        return f"{hex8[:4]}_{hex8[4:]}_0000_0000"
    return code.lower().replace("-", "_")
