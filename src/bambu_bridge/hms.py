"""HMS and stage decoding with phase-aware context.

Restored from the deployed implementation and modified September 9, 2026.
Protocol tables reference ha-bambulab (MIT; notice in LICENSES/) and earlier
Bambu Studio compatibility work (AGPL-3.0). See THIRD_PARTY.md for sources and
firmware limits. Unknown codes remain visible; this is not an authoritative
vendor diagnostic catalog.
"""

from __future__ import annotations

from typing import Literal, TypedDict

Severity = Literal["info", "warn", "error", "unknown"]

# Job context values passed to decode_hms_entry / _hms_list.
# Mirrors the translate-layer job_context root field.
JobContext = Literal["no_job", "printing", "finishing", "done"]


class HmsEntry(TypedDict):
    severity: Severity
    category: str
    user_message: str
    remediation: str | None


# ---------------------------------------------------------------------------
# Sub-stage table
# ---------------------------------------------------------------------------
#
# Source: ha-bambulab const.py CURRENT_STAGE_IDS, retrieved 2026-06-12 from
# https://raw.githubusercontent.com/greghesp/ha-bambulab/main/custom_components/bambu_lab/pybambu/const.py
#
# Hand-transcribed by comprehension. 78 entries total (ids 0–77, plus -1 and
# 255 sentinel values). Keys are the raw stg_cur integer from the P1S MQTT
# push_status payload. 255 and -1 are idle sentinels (P1 returns 255 for idle;
# X1 returns -1). stage_text() returns None for both sentinel values —
# callers interpret None-text as "Idle / no sub-stage active."
#
# NOTE: ids 12 and 18 share the label "calibrating_micro_lidar" in the source
# (marked DUPLICATED upstream). They are transcribed faithfully here.

_STAGE_TEXT: dict[int, str | None] = {
    -1: None,    # idle sentinel (X1 returns -1 for idle; P1 uses 255)
    0: "Printing",
    1: "Auto bed leveling",
    2: "Heatbed preheating",
    3: "Vibration compensation",
    4: "Changing filament",
    5: "M400 pause",
    6: "Paused — filament runout",
    7: "Heating hotend",
    8: "Calibrating extrusion",
    9: "Scanning bed surface",
    10: "Inspecting first layer",
    11: "Identifying build plate type",
    12: "Calibrating micro lidar",
    13: "Homing toolhead",
    14: "Cleaning nozzle tip",
    15: "Checking extruder temperature",
    16: "Paused — user",
    17: "Paused — front cover falling",
    18: "Calibrating micro lidar",
    19: "Calibrating extrusion flow",
    20: "Paused — nozzle temperature malfunction",
    21: "Paused — heat bed temperature malfunction",
    22: "Filament unloading",
    23: "Paused — skipped step",
    24: "Filament loading",
    25: "Calibrating motor noise",
    26: "Paused — AMS lost",
    27: "Paused — low fan speed heat break",
    28: "Paused — chamber temperature control error",
    29: "Cooling chamber",
    30: "Paused — user G-code",
    31: "Motor noise showoff",
    32: "Paused — nozzle filament covered detected",
    33: "Paused — cutter error",
    34: "Paused — first layer error",
    35: "Paused — nozzle clog",
    36: "Check absolute accuracy before calibration",
    37: "Absolute accuracy calibration",
    38: "Check absolute accuracy after calibration",
    39: "Calibrate nozzle offset",
    40: "Bed level high temperature",
    41: "Check quick release",
    42: "Check door and cover",
    43: "Laser calibration",
    44: "Check platform",
    45: "Check birdeye camera position",
    46: "Calibrate birdeye camera",
    47: "Bed level phase 1",
    48: "Bed level phase 2",
    49: "Heating chamber",
    50: "Heated bed cooling",
    51: "Print calibration lines",
    52: "Check material",
    53: "Calibrating live view camera",
    54: "Waiting for heatbed temperature",
    55: "Check material position",
    56: "Calibrating cutter model offset",
    57: "Measuring surface",
    58: "Thermal preconditioning",
    59: "Homing blade holder",
    60: "Calibrating camera offset",
    61: "Calibrating blade holder position",
    62: "Hotend pick/place test",
    63: "Waiting for chamber temperature equalization",
    64: "Preparing hotend",
    65: "Calibrating detection nozzle clumping",
    66: "Purifying chamber air",
    67: "Measuring rotary attachment",
    68: "Moving toolhead above purge chute",
    69: "Cooling nozzle",
    70: "Moving toolhead to center of heatbed",
    71: "Active arc fitting",
    72: "Hotend type detection",
    73: "Build plate alignment detection",
    74: "Heatbed surface foreign object detection",
    75: "Heatbed underside foreign object detection",
    76: "Pre-extrusion before printing",
    77: "Preparing AMS",
    255: None,   # idle sentinel (P1S returns 255 for idle)
}


def stage_text(stage_id: int) -> str | None:
    """Human-readable label for a P1S stg_cur stage id.

    Returns None for unknown stage ids AND for the idle sentinel values
    (255 / -1). Callers treat None-text as "Idle" or "no sub-stage."
    """
    return _STAGE_TEXT.get(stage_id)


# ---------------------------------------------------------------------------
# HMS severity / module derivation helpers (for unmapped codes)
# ---------------------------------------------------------------------------
#
# Source: ha-bambulab const.py HMS_SEVERITY_LEVELS / HMS_MODULES, 2026-06-12.
# Severity nibble = code >> 16 (the high 16 bits of the 32-bit code field).
# Module byte     = (attr >> 24) & 0xFF.

_SEVERITY_FROM_CODE: dict[int, Severity] = {
    # ha-bambulab names: fatal=1, serious=2, common=3, info=4
    1: "error",   # fatal
    2: "error",   # serious
    3: "warn",    # common
    4: "info",    # info
}

_MODULE_FROM_ATTR: dict[int, str] = {
    0x03: "mc",
    0x05: "mainboard",
    0x07: "ams",
    0x08: "toolhead",
    0x0C: "xcam",
}

# ---------------------------------------------------------------------------
# HMS table
# ---------------------------------------------------------------------------
#
# Keys are the canonical hex string form "aaaa_bbbb_cccc_dddd" (lowercase).
# Key composition: aaaa = attr>>16, bbbb = attr&0xFFFF,
#                  cccc = code>>16, dddd = code&0xFFFF.
#
# Sources: ha-bambulab hms_en.json.gz + wiki_links.json.gz (2026-06-12).
# Text is hand-transcribed and trimmed to operator-actionable length.
# X1-only lidar codes are omitted; xcam spaghetti/first-layer codes kept
# because they also cover P1S xcam.
#
# Original runout codes promoted from PrinterService._FILAMENT_RUNOUT_CODES
# are retained for backward compatibility.

_TABLE: dict[str, HmsEntry] = {

    # ------------------------------------------------------------------
    # Heatbed thermal malfunction
    # ------------------------------------------------------------------
    "0300_0100_0001_0001": {
        "severity": "error",
        "category": "bed",
        "user_message": "Heatbed heater short circuit detected.",
        "remediation": "Power off the printer. Check heatbed heater wiring and contact support.",
    },
    "0300_0100_0001_0002": {
        "severity": "error",
        "category": "bed",
        "user_message": "Heatbed heater open circuit or thermal-switch open.",
        "remediation": "Power off the printer. Check the heatbed heater connections.",
    },
    "0300_0100_0001_0003": {
        "severity": "error",
        "category": "bed",
        "user_message": "Heatbed over-temperature.",
        "remediation": "Power off immediately. Allow the bed to cool before inspecting.",
    },
    "0300_0100_0001_0005": {
        "severity": "error",
        "category": "bed",
        "user_message": "Heatbed heating module may be damaged.",
        "remediation": "Power off immediately. Follow the Wiki to replace the AC board.",
    },

    # ------------------------------------------------------------------
    # Nozzle temperature malfunction
    # ------------------------------------------------------------------
    "0300_0200_0001_0001": {
        "severity": "error",
        "category": "thermal",
        "user_message": "Nozzle heater short circuit detected.",
        "remediation": "Power off; clean filament blobs and inspect heater wiring for damage.",
    },
    "0300_0200_0001_0002": {
        "severity": "error",
        "category": "thermal",
        "user_message": "Nozzle heater open circuit detected.",
        "remediation": "Re-seat the heater connector (check for off-by-one pin). Measure ~12 Ω.",
    },
    "0300_0200_0001_0003": {
        "severity": "error",
        "category": "thermal",
        "user_message": "Nozzle over-temperature.",
        "remediation": "Power off. Reapply thermal paste and ensure the silicone sock is fitted.",
    },
    "0300_0200_0001_0005": {
        "severity": "error",
        "category": "thermal",
        "user_message": "Nozzle heating module may be damaged.",
        "remediation": "Disconnect power immediately and contact customer support.",
    },
    "0300_0200_0001_0006": {
        "severity": "error",
        "category": "thermal",
        "user_message": "Nozzle temperature sensor short circuit.",
        "remediation": "Re-seat the NTC connector and both ends of the toolhead FPC ribbon.",
    },
    "0300_0200_0001_0007": {
        "severity": "error",
        "category": "thermal",
        "user_message": "Nozzle temperature sensor open circuit — thermistor or FPC may be faulty.",
        "remediation": "Re-seat NTC connector and both FPC ribbon ends; inspect leads for fatigue.",
    },
    "0300_0200_0001_0008": {
        "severity": "error",
        "category": "thermal",
        "user_message": "Nozzle cannot reach set temperature.",
        "remediation": "Fit the silicone sock, reapply thermal paste, reduce part-fan for PETG.",
    },

    # ------------------------------------------------------------------
    # Part-cooling fan and heatbreak fan
    # ------------------------------------------------------------------
    "0300_0300_0002_0002": {
        "severity": "error",
        "category": "cooling",
        "user_message": "Hotend cooling fan running too slowly or stopped.",
        "remediation": "Re-seat the micro fan connector; clear filament strands from the blades.",
    },
    "0300_0400_0002_0001": {
        "severity": "error",
        "category": "cooling",
        "user_message": "Part-cooling fan too slow or stopped.",
        "remediation": "Clear blade obstructions; re-route the fan wire and re-seat the connector.",
    },

    # ------------------------------------------------------------------
    # Bed leveling / homing
    # ------------------------------------------------------------------
    "0300_0d00_0002_0001": {
        "severity": "error",
        "category": "bed",
        "user_message": "Heatbed homing abnormal — possible bulge or dirty nozzle tip.",
        "remediation": "Heat nozzle to ~240 °C, brush off filament, re-seat the plate, retry.",
    },
    "0300_1800_0001_0001": {
        "severity": "error",
        "category": "bed",
        "user_message": "Extruder eddy current sensor value too low — nozzle may not be installed.",
        "remediation": "Ensure the nozzle is fully installed and the hotend is assembled.",
    },
    "0300_1800_0001_0008": {
        "severity": "error",
        "category": "bed",
        "user_message": "Nozzle contacts heatbed abnormally during probe.",
        "remediation": "Check for filament residue on the nozzle or foreign matter on the bed.",
    },

    # ------------------------------------------------------------------
    # Nozzle clog / extrusion failure
    # ------------------------------------------------------------------
    "0300_0900_0002_0001": {
        "severity": "error",
        "category": "extruder",
        "user_message": "Extrusion motor overloaded — extruder may be clogged.",
        "remediation": "Cold-pull the hotend, check the PTFE tube, and resume.",
    },
    "0300_0900_0002_0002": {
        "severity": "error",
        "category": "extruder",
        "user_message": "Extrusion resistance abnormal — possible clog or stuck filament.",
        "remediation": "Check the toolhead for jammed filament; perform a cold pull if needed.",
    },
    "0300_1a00_0002_0001": {
        "severity": "error",
        "category": "extruder",
        "user_message": "Nozzle covered with filament or build plate is crooked.",
        "remediation": "Clean the nozzle tip and verify the build plate is seated flat.",
    },
    "0300_1a00_0002_0002": {
        "severity": "error",
        "category": "extruder",
        "user_message": "Nozzle clogged with filament.",
        "remediation": "Perform a cold pull or heat the nozzle and manually clear the blockage.",
    },

    # ------------------------------------------------------------------
    # Motor / homing errors (X/Y/Z axis)
    # ------------------------------------------------------------------
    "0300_0600_0001_0001": {
        "severity": "error",
        "category": "motion",
        "user_message": "Motor open-circuit — possible loose connection or motor failure.",
        "remediation": "Re-seat the motor cable at both ends; inspect for pinch damage.",
    },

    # ------------------------------------------------------------------
    # Toolhead front cover
    # ------------------------------------------------------------------
    "0300_1200_0002_0001": {
        "severity": "error",
        "category": "motion",
        "user_message": "Toolhead front cover fell off or is not seated.",
        "remediation": "Re-snap the front cover. False alarms: tighten TH screw, re-seat 10-pin.",
    },

    # ------------------------------------------------------------------
    # xcam — first-layer inspection and spaghetti detection
    # ------------------------------------------------------------------
    "0c00_0300_0003_0008": {
        "severity": "warn",
        "category": "xcam",
        "user_message": "Possible spaghetti failure detected.",
        "remediation": "Stop the job only if the defect is significant; reduce AI sensitivity.",
    },
    "0c00_0300_0003_001b": {
        "severity": "warn",
        "category": "xcam",
        "user_message": "Possible spaghetti defects detected.",
        "remediation": "Inspect the print surface and stop the job if the defect is significant.",
    },
    "0c00_0300_0002_000e": {
        "severity": "error",
        "category": "xcam",
        "user_message": "Nozzle appears to be covered with jammed or clogged material.",
        "remediation": "Clean the nozzle tip before the next print.",
    },
    "0c00_0300_0002_0010": {
        "severity": "error",
        "category": "xcam",
        "user_message": "Foreign objects detected on heatbed.",
        "remediation": "Check and clean the heatbed surface before resuming.",
    },

    # ------------------------------------------------------------------
    # AMS filament runout / empty slot
    # Key form: 0700_<slot-submodule>_0002_000N
    # Slot submodules: 2000=slot1, 2100=slot2, 2200=slot3, 2300=slot4
    # (original table entries below kept for backward compatibility)
    # ------------------------------------------------------------------

    # Legacy keys from PrinterService._FILAMENT_RUNOUT_CODES —
    # reconciled against ha-bambulab corpus + hms-field-guide 2026-06-12.
    # Keys kept (deletion premature while unattested); content corrected where
    # research contradicted the original descriptions.
    #
    # Not attested in ha-bambulab corpus; pending device capture.
    "0300_0d00_0003_0001": {
        "severity": "warn",
        "category": "ams",
        "user_message": "Filament runout — AMS spool empty.",
        "remediation": "Load a fresh spool into the AMS slot and resume.",
    },
    # ha-bambulab device_error 03008013 = gcode-commanded pause, NOT runout.
    # Category corrected to mainboard; filament_runout gate will not fire.
    "0300_8013_0002_0001": {
        "severity": "warn",
        "category": "mainboard",
        "user_message": "Print paused by a pause command in the print file.",
        "remediation": "Check the print file's pause instructions, then resume when ready.",
    },
    # Not attested in ha-bambulab corpus; 0300_0c00 module may be force-sensor;
    # category assignment unverified — pending device capture.
    "0300_0c00_0003_0001": {
        "severity": "warn",
        "category": "ams",
        "user_message": "Filament tangled in the AMS — feed cannot advance.",
        "remediation": "Open the AMS, untangle, and press Resume.",
    },
    # ha-bambulab: 0300_1100 = Y-axis resonance, not thermal. Legacy code,
    # third group 0001 unattested; attested sibling 0300_1100_0002_0001 added below.
    "0300_1100_0001_0001": {
        "severity": "error",
        "category": "motion",
        "user_message": "Y-axis resonance check fault (legacy code — unverified).",
        "remediation": "Check the hotend cartridge and thermistor wiring.",
    },
    # Attested in ha-bambulab + field guide (Y-axis resonance frequency low).
    "0300_1100_0002_0001": {
        "severity": "error",
        "category": "motion",
        "user_message": "Y-axis resonance frequency too low — timing belt may be loose.",
        "remediation": "Re-tension the Y-axis belt; re-run resonance calibration.",
    },
    # ha-bambulab: 0500_0100 = mainboard/storage, not bed leveling.
    # Bed leveling codes live in 0300_0d00_* / 0300_1800_*.
    "0500_0100_0001_0001": {
        "severity": "error",
        "category": "mainboard",
        "user_message": "Mainboard or storage error (unclassified legacy code).",
        "remediation": "Clean the bed and the probe, then retry.",
    },

    # AMS slot 1 — filament runout and breakage
    "0700_2000_0002_0001": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 1 filament has run out.",
        "remediation": "Check for a snapped stub at the feeder; trim and retry. Reload if empty.",
    },
    "0700_2000_0002_0002": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 1 is empty.",
        "remediation": "Insert filament until the feeder grabs it; press Retry.",
    },
    "0700_2000_0002_0003": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 1 filament may be broken inside the AMS.",
        "remediation": "Open the AMS, press hub release, push fresh filament through the channel.",
    },
    "0700_2000_0002_0004": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 1 filament may be broken in the toolhead.",
        "remediation": "Heat nozzle; remove stub with tweezers. Free the sensor magnet if no stub.",
    },
    "0700_2000_0002_0005": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 1 filament runout — purge of old filament failed.",
        "remediation": "Check whether the toolhead is clogged, then reload and resume.",
    },
    "0700_2000_0002_0009": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 1 extrusion failed — clogged extruder or filament too thin.",
        "remediation": "Check the extruder and toolhead for clogs, then retry.",
    },
    "0700_2000_0002_0015": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 1 filament status abnormal — possible breakage inside the AMS.",
        "remediation": "Open the AMS lid, inspect the filament path, and reload.",
    },

    # AMS slot 2 — filament runout
    "0700_2100_0002_0001": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 2 filament has run out.",
        "remediation": "Check for a snapped stub at the feeder; trim and retry. Reload if empty.",
    },
    # AMS slot 3 — filament runout
    "0700_2200_0002_0001": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 3 filament has run out.",
        "remediation": "Check for a snapped stub at the feeder; trim and retry. Reload if empty.",
    },
    # AMS slot 4 — filament runout
    "0700_2300_0002_0001": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 4 filament has run out.",
        "remediation": "Check for a snapped stub at the feeder; trim and retry. Reload if empty.",
    },

    # AMS filament pull-back / unload failures (MUST-HAVE: post-print AMS jam)
    "0700_2000_0002_0010": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 1 feed-out timeout.",
        "remediation": "Open the AMS lid, free the filament path, then resume.",
    },
    "0700_2000_0002_0011": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 1 filament pull-back timeout.",
        "remediation": "Open the AMS lid, free the filament path, then resume.",
    },
    "0700_2000_0002_0012": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 1 feeder motor stalled — cannot rotate the spool.",
        "remediation": "Open the AMS lid, free the spool, and check for tangles.",
    },
    "0700_2000_0002_0013": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 1 feeder motor has no signal.",
        "remediation": "Reseat the motor connector inside the AMS, or contact support.",
    },
    "0700_2000_0002_0016": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 1 assist motor slipped.",
        "remediation": "Pull out the filament, cut off the worn section, and reload.",
    },
    "0700_2000_0002_0017": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 1 assist motor stalled — tube resistance (AMS to printer).",
        "remediation": "Open the AMS lid, free the filament path, then resume.",
    },
    "0700_2000_0002_0018": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 1 assist motor stalled — excessive resistance in tube near AMS.",
        "remediation": "Open the AMS lid, free the filament path near the AMS port, then resume.",
    },
    "0700_2000_0002_0019": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 1 assist motor stalled — excessive resistance near buffer.",
        "remediation": "Check the PTFE tube between AMS and the buffer, then resume.",
    },
    "0700_2000_0002_0022": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 1 assist motor stalled — excessive resistance near toolhead.",
        "remediation": "Inspect the PTFE tube near the toolhead for kinks or clogs, then resume.",
    },

    # AMS slot 2 — pull-back failures
    "0700_2100_0002_0011": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 2 filament pull-back timeout.",
        "remediation": "Open the AMS lid, free the filament path, then resume.",
    },
    "0700_2100_0002_0012": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 2 feeder motor stalled.",
        "remediation": "Open the AMS lid, free the spool, and check for tangles.",
    },

    # AMS slot 3 — pull-back failures
    "0700_2200_0002_0011": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 3 filament pull-back timeout.",
        "remediation": "Open the AMS lid, free the filament path, then resume.",
    },
    "0700_2200_0002_0012": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 3 feeder motor stalled.",
        "remediation": "Open the AMS lid, free the spool, and check for tangles.",
    },

    # AMS slot 4 — pull-back failures
    "0700_2300_0002_0011": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 4 filament pull-back timeout.",
        "remediation": "Open the AMS lid, free the filament path, then resume.",
    },
    "0700_2300_0002_0012": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 4 feeder motor stalled.",
        "remediation": "Open the AMS lid, free the spool, and check for tangles.",
    },

    # AMS assist motor (hub-level) / buffer
    "0700_0100_0001_0001": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS assist motor slipped — extrusion wheel may be worn.",
        "remediation": "Pull out the filament, cut off the worn section, reload, and resume.",
    },
    "0700_0100_0002_0002": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS assist motor overloaded — filament may be tangled or stuck.",
        "remediation": "Check for a spool tangle and rewind under tension. Avoid cardboard spools.",
    },
    "0700_2000_0002_000a": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS failed to adjust buffer position — filament or buffer may be jammed.",
        "remediation": "Open the AMS lid, free the filament path and buffer, then resume.",
    },

    # AMS slot 1 motor overload (tangle)
    "0700_1000_0002_0002": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS slot 1 motor overloaded — filament may be tangled or stuck.",
        "remediation": "Check for a spool tangle; pull filament and cut past any chewed section.",
    },

    # AMS odometer
    "0700_0200_0001_0001": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS filament odometry error — speed/length sensor may be faulty.",
        "remediation": "Check the odometer connector inside the AMS.",
    },

    # ------------------------------------------------------------------
    # AMS hub-level retract / load failures — HIGH-FREQUENCY P1S+AMS
    # attr_low=7000 = hub-level (not per-slot); attr_low=4500 = cutter.
    # These are the #1 volume complaint on P1S+AMS; all route to the
    # ams-retract context-note family so the 'part is complete' note
    # fires on finishing/done (see _hms_family, _CONTEXT_NOTES).
    # ------------------------------------------------------------------
    "0700_7000_0002_0004": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS failed to pull back filament from the toolhead.",
        "remediation": "Eject fragments from hub PTFE tubes; power-cycle and run manual Unload.",
    },
    "0700_7000_0002_0001": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS failed to pull out the filament from the extruder.",
        "remediation": "Inspect cutter blade seating; heat nozzle and pull filament by hand.",
    },
    "0700_7000_0002_0002": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS failed to feed the filament into the toolhead.",
        "remediation": "Pull back filament, cut a fresh angled tip, and retry.",
    },
    "0700_7000_0002_0005": {
        "severity": "error",
        "category": "ams",
        "user_message": "AMS failed to feed the filament outside — tip may be deformed.",
        "remediation": "Cut the filament end flat, removing 5–10 cm of deformed tip, then retry.",
    },
    "0700_4500_0002_0003": {
        "severity": "error",
        "category": "ams",
        "user_message": "Filament cutter handle not released — blade may be stuck.",
        "remediation": "Inspect blade seating (flipped/worn blades); clear chips from the channel.",
    },

    # ------------------------------------------------------------------
    # System / connectivity
    # ------------------------------------------------------------------
    "0500_0100_0003_0005": {
        "severity": "error",
        "category": "mainboard",
        "user_message": "SD card read/write error — timelapse and jobs may fail.",
        "remediation": "Replace the stock microSD with a name-brand high-endurance card (FAT32).",
    },
    "0500_0300_0001_0002": {
        "severity": "error",
        "category": "mainboard",
        "user_message": "Toolhead board is malfunctioning — restart required.",
        "remediation": "Restart; re-seat the toolhead USB cable and FPC ribbon at the MC board.",
    },
}

# ---------------------------------------------------------------------------
# Wiki URL table
# ---------------------------------------------------------------------------
#
# Source: ha-bambulab wiki_links.json.gz (retrieved 2026-06-12).
# Paths hand-transcribed. Base URL: https://wiki.bambulab.com
# P1S-specific paths used where available; default (empty model list) otherwise.
# Keys match _TABLE keys (lowercase with underscores).

_WIKI_PATHS: dict[str, str] = {
    "0300_0100_0001_0003": "/en/x1/troubleshooting/hmscode/0300_0100_0001_0003",
    "0300_0200_0001_0001": "/en/x1/troubleshooting/hmscode/0300_0200_0001_0001",
    "0300_0200_0001_0002": "/en/x1/troubleshooting/hmscode/0300_0200_0001_0002",
    "0300_0200_0001_0003": "/en/x1/troubleshooting/hmscode/0300_0200_0001_0003",
    "0300_0200_0001_0006": "/en/x1/troubleshooting/hmscode/0300_0200_0001_0006",
    "0300_0200_0001_0008": "/en/h2c/troubleshooting/hmscode/0300_0200_0001_0008",
    "0300_0300_0002_0002": "/en/x1/troubleshooting/hmscode/0300_0300_0002_0002",
    "0300_0400_0002_0001": "/en/x1/troubleshooting/hmscode/0300_0400_0002_0001",
    "0300_0600_0001_0001": "/en/x1/troubleshooting/hmscode/0300_0600_0001_0001",
    "0300_0900_0002_0001": "/en/h2/troubleshooting/hmscode/0300_0900_0002_0001",
    "0300_0900_0002_0002": "/en/h2/troubleshooting/hmscode/0300_0900_0002_0002",
    "0300_0d00_0002_0001": "/en/x1/troubleshooting/hmscode/0300_0D00_0002_0001",
    "0300_1100_0002_0001": "/en/p2s/troubleshooting/hmscode/0300_1100_0002_0001",
    "0300_1800_0001_0001": "/en/a1-mini/troubleshooting/hmscode/0300_1800_0001_0001",
    "0300_1800_0001_0008": "/en/p2s/troubleshooting/hmscode/0300_1800_0001_0008",
    "0300_1a00_0002_0001": "/en/a1-mini/troubleshooting/hmscode/0300_1A00_0002_0001",
    "0300_1a00_0002_0002": "/en/a1-mini/troubleshooting/hmscode/0300_1A00_0002_0002",
    "0700_0100_0001_0001": "/en/x1/troubleshooting/hmscode/0700_0100_0001_0001",
    "0700_0100_0002_0002": "/en/x1/troubleshooting/hmscode/0700_0100_0002_0002",
    "0700_0200_0001_0001": "/en/x1/troubleshooting/hmscode/0700_0200_0001_0001",
    "0700_1000_0002_0002": "/en/x1/troubleshooting/hmscode/0700_1000_0002_0002",
    "0700_2000_0002_0001": "/en/x1/troubleshooting/hmscode/0700_2000_0002_0001",
    "0700_2000_0002_0002": "/en/x1/troubleshooting/hmscode/0700_2000_0002_0002",
    "0700_2000_0002_0003": "/en/x1/troubleshooting/hmscode/0700_2000_0002_0003",
    "0700_2000_0002_0004": "/en/x1/troubleshooting/hmscode/0700_2000_0002_0004",
    "0700_2000_0002_0005": "/en/x1/troubleshooting/hmscode/0700_2000_0002_0005",
    "0700_2000_0002_0009": "/en/h2/troubleshooting/hmscode/0700_2000_0002_0009",
    "0700_2000_0002_0015": "/en/h2/troubleshooting/hmscode/0700_2000_0002_0015",
    "0700_7000_0002_0001": "/en/x1/troubleshooting/hmscode/0700_7000_0002_0001",
    "0700_7000_0002_0002": "/en/x1/troubleshooting/hmscode/0700_7000_0002_0002",
    "0700_7000_0002_0004": "/en/x1/troubleshooting/hmscode/0700_7000_0002_0004",
    "0700_7000_0002_0005": "/en/x1/troubleshooting/hmscode/0700_7000_0002_0005",
    "0700_4500_0002_0003": "/en/x1/troubleshooting/hmscode/0700_4500_0002_0003",
    "0300_1200_0002_0001": "/en/x1/troubleshooting/hmscode/0300_1200_0002_0001",
    "0300_0200_0001_0007": "/en/x1/troubleshooting/hmscode/0300_0200_0001_0007",
    "0500_0100_0003_0005": "/en/x1/troubleshooting/hmscode/0500_0100_0003_0005",
    "0500_0300_0001_0002": "/en/x1/troubleshooting/hmscode/0500_0300_0001_0002",
    "0c00_0100_0002_0008": "/en/x1/troubleshooting/hmscode/0C00_0100_0002_0008",
    "0c00_0300_0002_000e": "/en/h2/troubleshooting/hmscode/0C00_0300_0002_000E",
    "0c00_0300_0003_0008": "/en/x1/troubleshooting/hmscode/0C00_0300_0003_0008",
}

_WIKI_BASE = "https://wiki.bambulab.com"
_WIKI_FALLBACK = _WIKI_BASE + "/en/hms/home"


def wiki_url(hex_form: str) -> str:
    """Bambu wiki HMS troubleshooting URL for a given canonical hex key.

    ``hex_form`` is the canonical ``aaaa_bbbb_cccc_dddd`` form (lowercase,
    underscore-separated). Returns the per-code wiki page when known, else
    the HMS index page as a fallback.
    """
    path = _WIKI_PATHS.get(hex_form.lower())
    if path is not None:
        return _WIKI_BASE + path
    return _WIKI_FALLBACK


# ---------------------------------------------------------------------------
# decode_hms_entry — structured decode from raw attr+code ints
# ---------------------------------------------------------------------------


class HmsDecoded(TypedDict):
    """Structured decode of one ``{"attr": int, "code": int}`` HMS entry."""
    severity: Severity
    category: str
    user_message: str
    remediation: str | None
    hex: str          # canonical "aaaa_bbbb_cccc_dddd" (lowercase)
    wiki_url: str
    context_note: str | None  # phase-tuned operator advice (null when not applicable)
    stale: bool               # True only when job_context == "no_job" (latched residue)


# ---------------------------------------------------------------------------
# Phase-aware context note table
# ---------------------------------------------------------------------------
#
# Keyed by (family, job_context).  `family` is derived from category +
# the code's hex-prefix groups as a simple categorical label.
#
# Families defined here:
#   "ams-retract"     — AMS pull-back / unload / retract failures
#   "ams-runout-feed" — filament runout / empty slot / feed failures
#   "ams-overload"    — motor overloads (hub assist, slot feeder)
#   "heater"          — nozzle/bed thermal faults
#   "motion"          — motor/homing/belt faults
#   "cooling"         — fan faults
#
# Family assignment is done in _hms_family() below.
# When a (family, job_context) pair is absent, context_note is None.
#
# Notes are one sentence, matter-of-fact, operator-directed.
# Evidence base: docs/hms-field-guide.json phase_notes and
# docs/HMS-FIELD-GUIDE.md §8 phase-muxing rules.

_CONTEXT_NOTES: dict[tuple[str, str], str] = {

    # -----------------------------------------------------------------------
    # AMS retract / unload family
    # (pull-back timeout, pull-out failure, cutter stuck, tip-deformation
    #  rebound, assist-motor stall variants in the pull-back half)
    # -----------------------------------------------------------------------
    ("ams-retract", "finishing"): (
        "Filament cleanup needs attention; confirm that the part finished. "
        "Free the filament path, then clear the error."
    ),
    ("ams-retract", "done"): (
        "Filament cleanup needs attention; confirm that the part finished. "
        "Free the filament path, then clear the error."
    ),
    ("ams-retract", "printing"): (
        "Filament retract failed mid-print — act now to prevent a nozzle blob. "
        "Clear the filament path and resume."
    ),

    # -----------------------------------------------------------------------
    # AMS runout / feed family
    # (filament runout, empty slot, broken-in-AMS/toolhead, feed failures)
    # -----------------------------------------------------------------------
    ("ams-runout-feed", "finishing"): (
        "A runout or feed fault needs attention during cleanup; "
        "confirm that the part finished. Insert filament or cancel at your convenience."
    ),
    ("ams-runout-feed", "done"): (
        "A runout or feed fault needs attention during cleanup; "
        "confirm that the part finished. Insert filament or cancel at your convenience."
    ),
    ("ams-runout-feed", "printing"): (
        "Filament runout or feed fault during printing — insert a new spool or clear "
        "the blockage and resume before the nozzle parks too long."
    ),

    # -----------------------------------------------------------------------
    # AMS overload family — motor overloads during active feed or rewind.
    # Phase note useful for finishing/done (rewind overload at job end)
    # vs printing (active-feed overload).
    # -----------------------------------------------------------------------
    ("ams-overload", "finishing"): (
        "Spool rewind overload during filament pull-back; confirm that the part finished. "
        "Check for a spool tangle or loose filament pile, then clear the error."
    ),
    ("ams-overload", "done"): (
        "Spool rewind overload during filament pull-back; confirm that the part finished. "
        "Check for a spool tangle or loose filament pile, then clear the error."
    ),
    ("ams-overload", "printing"): (
        "Spool or filament path overload during printing — check for a tangle or "
        "crossed winding and resume."
    ),

    # -----------------------------------------------------------------------
    # Heater / thermal family — note only for mid-print (most urgent).
    # No phase difference for finishing/done vs printing in operator action;
    # mid-print over-temp is the scarier case (thermal runaway risk).
    # -----------------------------------------------------------------------
    ("heater", "printing"): (
        "Thermal fault during active printing — the printer has paused for safety. "
        "Check the nozzle/bed heater and thermistor before resuming."
    ),

    # Motion and cooling: no operationally meaningful phase difference.
    # context_note left None for those families.
}


def _hms_family(hex_key: str, category: str) -> str:
    """Derive the context-note family label from a decoded HMS entry.

    Families are used as the first element of the _CONTEXT_NOTES key.
    The hex_key is "aaaa_bbbb_cccc_dddd" (lowercase).
    """
    # AMS module prefix: attr_high = "0700" means AMS.
    if hex_key.startswith("07"):
        # Retract family: keys with second group 7000 (hub-level retract),
        # or 4500 (cutter), or slot-level pull-back codes (x002_0010..0019,
        # 0022, and the assist-motor stall codes 0016..0019, 0022).
        attr_low = hex_key[5:9]   # positions 5-8 = bbbb group
        code_low_str = hex_key[15:19]  # dddd group (last 4 hex digits)
        try:
            code_low = int(code_low_str, 16)
        except ValueError:
            code_low = 0

        if attr_low == "7000":
            # Hub-level retract: all sub-codes are retract family
            return "ams-retract"
        if attr_low == "4500":
            # Cutter stuck/sensor
            return "ams-retract"
        if attr_low in ("2000", "2100", "2200", "2300"):
            # Per-slot codes: runout/empty/broken = feed family (0x0001..0x0005,
            # 0x0009, 0x0015); pull-back/motor = retract family (0x0010..0x0022);
            # overload = overload family (0x0017, 0x0018, 0x0019, 0x0022 are
            # assist-motor stalls — still retract because it's the pull-back path).
            if 0x0010 <= code_low <= 0x0022:
                return "ams-retract"
            return "ams-runout-feed"
        if attr_low in ("1000", "6000"):
            # Slot feeder motor overload, hub overload
            return "ams-overload"
        if attr_low in ("0100", "0200"):
            # Assist motor (hub-level) overload/slip/odometry
            return "ams-overload"
        if attr_low in ("4000", "5000"):
            # Buffer comms, AMS comms — no phase-specific note
            return "ams-comms"
        # Default AMS
        return "ams-runout-feed"

    # Thermal/heater codes: 0300_02xx (nozzle) and 0300_01xx (bed).
    # Both nozzle (category="thermal") and bed (category="bed") thermal faults
    # belong to the "heater" family — the previous combined predicate
    # (`"0001" in hex_key[5:9]`) was dead code for all known bed keys, so bed
    # entries silently fell through to "other" and never received a context note.
    if hex_key.startswith("03"):
        if category in ("thermal", "bed"):
            return "heater"
        if category == "cooling":
            return "cooling"
        if category == "motion":
            return "motion"
        return "other"

    return "other"


def decode_hms_entry(
    attr: int,
    code: int,
    job_context: JobContext | None = None,
) -> HmsDecoded:
    """Decode one MQTT ``hms[]`` entry into the structured contract shape.

    Accepts raw ``attr`` and ``code`` as ints from the MQTT payload.

    Key composition (verified against ha-bambulab models.py HMSNotification):
        attr_high16 = attr >> 16
        attr_low16  = attr & 0xFFFF
        code_high16 = code >> 16
        code_low16  = code & 0xFFFF
        key = f"{attr_high16:04x}_{attr_low16:04x}_{code_high16:04x}_{code_low16:04x}"

    For unmapped codes, severity and module are derived from the bit fields:
        severity_nibble = code >> 16  (mapped via _SEVERITY_FROM_CODE)
        module_byte     = (attr >> 24) & 0xFF  (mapped via _MODULE_FROM_ATTR)

    Optional ``job_context`` parameter:
        When supplied, two additional fields are populated:
        - ``context_note``: one-sentence phase-tuned operator advice (or None when
          there is no phase-specific guidance for this code family).
        - ``stale``: True only when job_context == "no_job", meaning the entry
          survived into idle and is almost certainly latched residue from a past job.
        When ``job_context`` is None (the default, for backward-compatible callers),
        both fields default: context_note=None, stale=False.
    """
    attr_high = (attr >> 16) & 0xFFFF
    attr_low = attr & 0xFFFF
    code_high = (code >> 16) & 0xFFFF
    code_low = code & 0xFFFF
    hex_key = f"{attr_high:04x}_{attr_low:04x}_{code_high:04x}_{code_low:04x}"

    entry = _TABLE.get(hex_key)
    if entry is not None:
        category = entry["category"]
        base: HmsDecoded = {
            "severity": entry["severity"],
            "category": category,
            "user_message": entry["user_message"],
            "remediation": entry["remediation"],
            "hex": hex_key,
            "wiki_url": wiki_url(hex_key),
            "context_note": None,
            "stale": False,
        }
    else:
        # Unmapped: derive severity from code high-word, module from attr high-byte
        derived_severity: Severity = _SEVERITY_FROM_CODE.get(code_high, "unknown")
        derived_module = _MODULE_FROM_ATTR.get((attr >> 24) & 0xFF, "unknown")
        category = derived_module
        base = {
            "severity": derived_severity,
            "category": category,
            "user_message": hex_key,
            "remediation": None,
            "hex": hex_key,
            "wiki_url": wiki_url(hex_key),
            "context_note": None,
            "stale": False,
        }

    if job_context is not None:
        # stale: True only when the printer is idle with no active job
        base["stale"] = job_context == "no_job"
        # context_note: look up by family × job_context
        family = _hms_family(hex_key, category)
        note = _CONTEXT_NOTES.get((family, job_context))
        base["context_note"] = note

    return base


# ---------------------------------------------------------------------------
# Existing lookup() API — unchanged, used by print_error channel
# ---------------------------------------------------------------------------


def lookup(code: str | int | None) -> HmsEntry:
    """Decode an HMS / print_error code into a structured entry.

    Accepts:
    - the canonical hex string ``"0300_0d00_0003_0001"``
    - an int (the raw ``print_error`` from MQTT) — decoded to its 4-group hex form
    - ``None`` or ``0`` → ``info / cleared`` (callers usually filter these out
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
    """Normalise to the ``aaaa_bbbb_cccc_dddd`` lowercase hex form."""
    if isinstance(code, int):
        # 32-bit packed: high16 | low16 (P1S sometimes ships a single int)
        # but more commonly the 128-bit form arrives as four 32-bit words.
        # Best-effort: render as 8-digit hex split 4/4. Treat as one 32-bit
        # group + three zero groups so unknown codes still round-trip.
        hex8 = f"{code & 0xFFFFFFFF:08x}"
        return f"{hex8[:4]}_{hex8[4:]}_0000_0000"
    return code.lower().replace("-", "_")
