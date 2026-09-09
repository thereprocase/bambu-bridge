"""Typed P1S command builders (spec 5.1 "Command envelope").

Every function returns a ready-to-publish ``{category: {...}}`` envelope built
through :func:`build_command`, so each carries a unique ``sequence_id`` the
printer echoes in its ack.

Wire semantics are reimplemented from OpenBambuAPI / ha-bambulab — **not
imported**. Two honest facts shape this module:

* The P1S has no native MQTT "set temperature / fan / move" command. Those are
  ``print.gcode_line`` (Marlin-ish) under the hood; the helpers below say so
  rather than pretending otherwise.
* The chamber LED is ``system.ledctrl`` with a fixed blink envelope; steady
  on/off is ``led_mode`` with the timing fields zeroed.

Validation raises :class:`ValueError`; the API layer maps that to HTTP 422.

AMS slot numbering
------------------
Tray/slot indices are **0-based protocol indices end-to-end**.  The P1S AMS
labels physical slots 1–4 on its touchscreen; the bridge and firmware both
use 0–3 on the wire.  The API layer accepts 0-based values from clients;
physical-slot conversion (1-based → 0-based) is the *client's* responsibility
for any endpoint where the contract documents physical slots.
"""

from __future__ import annotations

import os
from typing import Any

from bambu_bridge.protocol.models import build_command

Envelope = dict[str, Any]

# Default nozzle temperature ceiling (stainless steel nozzle).  Hardened-steel
# nozzles can go to 300 °C; the API layer applies the conditional clamp and
# passes the already-validated value here.  The builder enforces the
# hardcoded absolute maximum (300 °C) so no client-supplied clamp can exceed
# the physical limit regardless of nozzle type.
NOZZLE_MAX_C = 280             # default / stainless ceiling
NOZZLE_MAX_HARDENED_C = 300   # conditional ceiling; requires nozzle_type=hardened
BED_MAX_C = 120
SPEED_LEVELS = {1: "silent", 2: "standard", 3: "sport", 4: "ludicrous"}
# print.gcode_line fan index per Bambu convention.
_FAN_PARTS = {"part": 1, "aux": 2, "chamber": 3}
AMS_ACTIONS = frozenset({"pause", "resume", "reset"})

# P1S MQTT receive-buffer ceiling (research/02 §4).  Payloads larger than this
# risk silently overflowing the printer's RX buffer and being discarded.
GCODE_LINE_MAX_BYTES = 4096

# ---------------------------------------------------------------------------
# Wave-2: xcam module names confirmed for P1S (control matrix §9).
# buildplate_marker_detector is omitted — LIDAR-dependent on X1; P1S uncertain.
# Whitelist is closed; unknown modules are rejected at the builder level.
# ---------------------------------------------------------------------------
XCAM_MODULES: frozenset[str] = frozenset({
    "first_layer_inspector",
    "spaghetti_detector",
    "airprint_detector",
    "printing_monitor",
    "pileup_detector",
    "clump_detector",
})

# ---------------------------------------------------------------------------
# Wave-2: print_option flag allowlist.
# Only matrix-confirmed P1S flags are accepted; unknown keys are rejected.
# ---------------------------------------------------------------------------
PRINT_OPTION_FLAGS: frozenset[str] = frozenset({
    "auto_recovery",
    "air_print_detect",
    "filament_tangle_detect",
    "nozzle_blob_detect",
    "sound_enable",
})

# ---------------------------------------------------------------------------
# Wave-2: AMS tray material type allowlist (ams_filament_setting).
# ---------------------------------------------------------------------------
AMS_TRAY_TYPES: frozenset[str] = frozenset({
    "PLA", "ABS", "PETG", "TPU", "PA", "PA-CF", "PLA-CF",
    "PETG-CF", "PVA", "ASA", "PC", "HIPS", "ABS-GF",
    "PLA Silk", "PLA Matte",
})

# ---------------------------------------------------------------------------
# AMS drying temperature ceiling (hardware safety limit).
# The AMS heater must not exceed this temperature regardless of what the
# client requests; exceeding it risks damaging the AMS unit and filament.
# ---------------------------------------------------------------------------
AMS_DRYING_MAX_TEMP_C = 75

# ---------------------------------------------------------------------------
# AMS unit index upper bound (0-based, 0..3 for a 4-unit chain).
# The P1S supports up to 4 AMS units in a chain; indices 4+ are invalid.
# ---------------------------------------------------------------------------
AMS_ID_MAX = 3

# ---------------------------------------------------------------------------
# Wave-3: extrude/retract guards (control matrix §6).
# ---------------------------------------------------------------------------
EXTRUDE_MAX_MM = 100.0
EXTRUDE_FEEDRATE_WHITELIST: frozenset[int] = frozenset({120, 300, 600})
# Minimum nozzle temp before allowing extrusion (cold-extrude protection).
# If temp is unavailable the bridge refuses extrude entirely (conservative).
EXTRUDE_MIN_TEMP_C = 170

# ---------------------------------------------------------------------------
# Wave-3: calibration bitmask — P1S-confirmed bits only (control matrix §8).
# Bit 0 (1) = vibration compensation (NOT LIDAR; P1S has no LIDAR).
# Bit 1 (2) = bed leveling.
# Bit 2 (4) = first layer / flow calibration.
# Any combination of bits 0-2 is valid (1..7); bits 3+ are X1-only (LIDAR)
# and are rejected. 0 is also rejected (no-op that hides intent).
# Valid mask for any P1S-safe value: v & ~0b111 == 0 and v > 0.
# ---------------------------------------------------------------------------
_CALIBRATION_P1S_MASK: int = 0b111  # bits 0-2 are P1S-confirmed

# ---------------------------------------------------------------------------
# Wave-3: raw G-code console gate.
# Controlled by the BRIDGE_ENABLE_RAW_GCODE env var.  Default unset = off.
# ---------------------------------------------------------------------------

def raw_gcode_enabled() -> bool:
    """Return True only when BRIDGE_ENABLE_RAW_GCODE is non-empty in the env."""
    return bool(os.environ.get("BRIDGE_ENABLE_RAW_GCODE", "").strip())


# ---------------------------------------------------------------------------
# Wave-2: set_accessories nozzle type / diameter enums.
# ---------------------------------------------------------------------------
NOZZLE_TYPES: frozenset[str] = frozenset({"stainless_steel", "hardened_steel"})
NOZZLE_DIAMETERS: frozenset[float] = frozenset({0.2, 0.4, 0.6, 0.8})


def _gcode(line: str) -> Envelope:
    """A ``print.gcode_line`` envelope; the printer needs the trailing newline.

    Enforces the :data:`GCODE_LINE_MAX_BYTES` ceiling so callers don't need
    to think about the printer's RX buffer limit.  The check is on the
    *final* payload (after the trailing newline is appended) so the cap is
    an honest byte count of what lands on the wire.
    """
    if not line.endswith("\n"):
        line += "\n"
    if len(line.encode()) > GCODE_LINE_MAX_BYTES:
        raise ValueError(
            f"gcode payload {len(line.encode())} bytes exceeds the "
            f"{GCODE_LINE_MAX_BYTES}-byte MQTT RX buffer limit"
        )
    return build_command("print", "gcode_line", param=line)


# --------------------------------------------------------------------------- #
# Print lifecycle  (print.*)
# --------------------------------------------------------------------------- #


def print_pause() -> Envelope:
    # OpenBambuAPI includes `"param":""` in the stop/pause/resume envelope.
    # The bridge previously omitted it — no observed breakage on fw 01.09 —
    # but aligning removes the discrepancy noted in the control matrix §1.
    return build_command("print", "pause", param="")


def print_resume() -> Envelope:
    return build_command("print", "resume", param="")


def print_stop() -> Envelope:
    return build_command("print", "stop", param="")


def print_speed(level: int) -> Envelope:
    """Speed profile: 1 silent · 2 standard · 3 sport · 4 ludicrous."""
    if level not in SPEED_LEVELS:
        raise ValueError(f"speed level must be one of {sorted(SPEED_LEVELS)}")
    return build_command("print", "print_speed", param=str(level))


def gcode_line(line: str) -> Envelope:
    """Raw G-code passthrough. Multi-line allowed (embed ``\\n``)."""
    if not line.strip():
        raise ValueError("gcode line is empty")
    return _gcode(line)


# --------------------------------------------------------------------------- #
# Climate / motion  (these *are* gcode_line — named for the client's sake)
# --------------------------------------------------------------------------- #


def set_nozzle_temp(celsius: int, *, hardened: bool = False) -> Envelope:
    """Set nozzle target temperature (M104, no-wait).

    ``hardened=False`` (default / stainless nozzle): ceiling is
    :data:`NOZZLE_MAX_C` (280 °C).
    ``hardened=True`` (hardened-steel nozzle confirmed in printer record):
    ceiling is :data:`NOZZLE_MAX_HARDENED_C` (300 °C).

    The API layer reads the nozzle_type from the printer record / service
    state and sets ``hardened`` accordingly.  The builder itself never
    trusts the caller to raise the limit — ``hardened=True`` is only ever
    set by the server-side gate in ``api/control.py``.
    """
    ceiling = NOZZLE_MAX_HARDENED_C if hardened else NOZZLE_MAX_C
    if not 0 <= celsius <= ceiling:
        raise ValueError(
            f"nozzle temp {celsius} out of range 0..{ceiling} "
            f"({'hardened' if hardened else 'stainless'} nozzle)"
        )
    return _gcode(f"M104 S{celsius}")


def set_bed_temp(celsius: int) -> Envelope:
    if not 0 <= celsius <= BED_MAX_C:
        raise ValueError(f"bed temp out of range 0..{BED_MAX_C}")
    return _gcode(f"M140 S{celsius}")


def set_fan(part: str, percent: int) -> Envelope:
    """``part`` ∈ {part, aux, chamber}; ``percent`` 0–100 → M106 S0–255."""
    if part not in _FAN_PARTS:
        raise ValueError(f"fan part must be one of {sorted(_FAN_PARTS)}")
    if not 0 <= percent <= 100:
        raise ValueError("fan percent out of range 0..100")
    return _gcode(f"M106 P{_FAN_PARTS[part]} S{round(percent * 255 / 100)}")


def home() -> Envelope:
    """Home all axes (G28). Moves the toolhead — not a passive command."""
    return _gcode("G28")


def move_axis(axis: str, distance_mm: float, *, feed_mm_min: int = 600) -> Envelope:
    """Relative jog of one axis. Moves the toolhead."""
    axis = axis.upper()
    if axis not in {"X", "Y", "Z"}:
        raise ValueError("axis must be X, Y or Z")
    if feed_mm_min <= 0:
        raise ValueError("feed must be positive")
    return _gcode(f"G91\nG1 {axis}{distance_mm} F{feed_mm_min}\nG90")


# --------------------------------------------------------------------------- #
# Chamber light  (system.ledctrl)
# --------------------------------------------------------------------------- #


def chamber_light(on: bool) -> Envelope:
    """Steady chamber LED on/off — the canonical non-destructive command."""
    return build_command(
        "system",
        "ledctrl",
        led_node="chamber_light",
        led_mode="on" if on else "off",
        led_on_time=500,
        led_off_time=500,
        loop_times=0,
        interval_time=0,
    )


_LED_MODES = frozenset({"on", "off", "flashing"})


def work_light(
    mode: str,
    *,
    loop_times: int = 1,
    interval_time: int = 500,
) -> Envelope:
    """Work/task light control (``work_light`` node).

    ``mode`` ∈ {``"on"``, ``"off"``, ``"flashing"``}.

    For ``"flashing"``:
    * ``loop_times`` — number of blink cycles (default 1; 0 = repeat forever).
    * ``interval_time`` — ms between the on and off edges of each cycle
      (default 500 ms).

    For ``"on"`` and ``"off"`` the timing fields are irrelevant to the printer
    and are zeroed per the observed work_light payload in the control matrix.

    Not all P1S hardware revisions include a work light LED; the printer
    silently ignores the command when the node is absent.
    """
    if mode not in _LED_MODES:
        raise ValueError(f"led mode must be one of {sorted(_LED_MODES)}")
    if mode == "flashing":
        if loop_times < 0:
            raise ValueError("loop_times must be >= 0")
        if interval_time <= 0:
            raise ValueError("interval_time must be > 0")
        return build_command(
            "system",
            "ledctrl",
            led_node="work_light",
            led_mode="flashing",
            led_on_time=interval_time,
            led_off_time=interval_time,
            loop_times=loop_times,
            interval_time=interval_time,
        )
    # Steady on/off: timing fields zeroed per control-matrix §4 work_light payload.
    return build_command(
        "system",
        "ledctrl",
        led_node="work_light",
        led_mode=mode,
        led_on_time=0,
        led_off_time=0,
        loop_times=0,
        interval_time=0,
    )


# --------------------------------------------------------------------------- #
# Camera  (camera.*)
# --------------------------------------------------------------------------- #


def ipcam_record_set(enabled: bool) -> Envelope:
    """Enable or disable print video recording to the SD card.

    Uses the ``camera`` category (not ``print`` or ``system``).  The
    raw-command passthrough deliberately blocks the ``camera`` category, so
    this typed builder is the only safe path.
    """
    return build_command(
        "camera",
        "ipcam_record_set",
        control="enable" if enabled else "disable",
    )


def ipcam_timelapse(enabled: bool) -> Envelope:
    """Enable or disable timelapse generation on the SD card."""
    return build_command(
        "camera",
        "ipcam_timelapse",
        control="enable" if enabled else "disable",
    )


# --------------------------------------------------------------------------- #
# AMS / filament  (print.*)
# --------------------------------------------------------------------------- #


def ams_control(action: str) -> Envelope:
    """AMS handshake control: pause | resume | reset (e.g. after a runout)."""
    if action not in AMS_ACTIONS:
        raise ValueError(f"ams action must be one of {sorted(AMS_ACTIONS)}")
    return build_command("print", "ams_control", param=action)


def ams_change_filament(
    target_tray: int, *, cur_temp: int = 220, tar_temp: int = 220
) -> Envelope:
    """Switch the active AMS tray (0-based protocol index)."""
    if target_tray < 0:
        raise ValueError("target tray must be >= 0")
    return build_command(
        "print",
        "ams_change_filament",
        target=target_tray,
        curr_temp=cur_temp,
        tar_temp=tar_temp,
    )


def unload_filament() -> Envelope:
    return build_command("print", "unload_filament")


# --------------------------------------------------------------------------- #
# Device info  (info.get_version) — one-shot module/firmware identity
# --------------------------------------------------------------------------- #


def get_version() -> Envelope:
    return build_command("info", "get_version")


# --------------------------------------------------------------------------- #
# Wave-2: XCam (xcam.xcam_control_set)                                       #
# --------------------------------------------------------------------------- #


def xcam_control(
    module_name: str,
    *,
    enabled: bool,
    print_halt: bool = False,
) -> Envelope:
    """Enable/disable an xcam AI inspection module.

    ``module_name`` must be one of :data:`XCAM_MODULES` (closed set, P1S
    confirmed).  Unknown module names are rejected so a typo or a future
    firmware-only module doesn't silently no-op on the printer.

    ``print_halt=True`` enables auto-pause on detection — the feature users
    typically want for spaghetti protection.

    The ``xcam`` category is blocked by the raw-command passthrough; this
    typed builder is the only path to it.
    """
    if module_name not in XCAM_MODULES:
        raise ValueError(
            f"xcam module {module_name!r} is not in the matrix-confirmed P1S set "
            f"{sorted(XCAM_MODULES)}"
        )
    return build_command(
        "xcam",
        "xcam_control_set",
        module_name=module_name,
        control=enabled,
        print_halt=print_halt,
    )


# --------------------------------------------------------------------------- #
# Wave-2: print_option flags (print.print_option)                             #
# --------------------------------------------------------------------------- #


def print_option(**flags: bool) -> Envelope:
    """Set one or more print_option boolean flags in a single command.

    Only matrix-confirmed P1S flags are accepted (see :data:`PRINT_OPTION_FLAGS`).
    Unknown flag names are rejected with a :class:`ValueError` so the API
    layer turns them into 422s before anything reaches the printer.

    Multiple flags may be combined in one call; they are sent as individual
    boolean fields alongside the ``print_option`` command body.

    Example::

        print_option(air_print_detect=True, auto_recovery=False)
    """
    if not flags:
        raise ValueError("print_option requires at least one flag")
    unknown = set(flags) - PRINT_OPTION_FLAGS
    if unknown:
        raise ValueError(
            f"unknown print_option flags {sorted(unknown)}; "
            f"allowed: {sorted(PRINT_OPTION_FLAGS)}"
        )
    # Cast to dict[str, Any] — build_command's **fields expects Any, not bool.
    fields: dict[str, Any] = dict(flags)
    return build_command("print", "print_option", **fields)


# --------------------------------------------------------------------------- #
# Wave-2: skip_objects (print.skip_objects)                                   #
# --------------------------------------------------------------------------- #


def skip_objects(obj_list: list[int]) -> Envelope:
    """Cancel specific objects mid-print without stopping the whole job.

    ``obj_list`` must be a non-empty list of integer Bambu object IDs (from
    the slice, not user-facing indices).  The ``timestamp`` field is the
    current Unix epoch (seconds, integer) as required by the wire protocol
    (OpenBambuAPI / ha-bambulab).
    """
    if not obj_list:
        raise ValueError("obj_list must be non-empty")
    for i, oid in enumerate(obj_list):
        if not isinstance(oid, int):
            raise ValueError(
                f"obj_list[{i}] must be an int, got {type(oid).__name__!r}"
            )
    import time as _time
    return build_command(
        "print",
        "skip_objects",
        timestamp=int(_time.time()),
        obj_list=obj_list,
    )


# --------------------------------------------------------------------------- #
# Wave-2: AMS operations                                                      #
# --------------------------------------------------------------------------- #


def ams_filament_setting(
    *,
    ams_id: int,
    tray_id: int,
    tray_info_idx: str,
    tray_color: str,
    nozzle_temp_min: int,
    nozzle_temp_max: int,
    tray_type: str,
) -> Envelope:
    """Write a filament profile to an AMS slot (essential for untagged spools).

    Parameters
    ----------
    ams_id:
        AMS unit index, 0-based.
    tray_id:
        Slot index within the AMS unit, 0-based (0–3 for a single-unit P1S).
    tray_info_idx:
        Bambu filament SKU string (e.g. ``"GFB61"``).  May be empty string
        for a fully manual entry.
    tray_color:
        8-character hex RRGGBBAA (e.g. ``"FFFFFFFF"``).
    nozzle_temp_min:
        Minimum nozzle temperature for this filament (°C).
    nozzle_temp_max:
        Maximum nozzle temperature for this filament (°C).
    tray_type:
        Material type string — must be in :data:`AMS_TRAY_TYPES`.
    """
    if ams_id < 0:
        raise ValueError("ams_id must be >= 0")
    if ams_id > AMS_ID_MAX:
        raise ValueError(f"ams_id must be <= {AMS_ID_MAX} (P1S supports up to 4 AMS units)")
    if tray_id < 0:
        raise ValueError("tray_id must be >= 0")
    if len(tray_color) != 8 or not all(c in "0123456789ABCDEFabcdef" for c in tray_color):
        raise ValueError("tray_color must be 8-char hex RRGGBBAA")
    if nozzle_temp_min >= nozzle_temp_max:
        raise ValueError(
            f"nozzle_temp_min ({nozzle_temp_min}) must be < nozzle_temp_max ({nozzle_temp_max})"
        )
    if nozzle_temp_max > NOZZLE_MAX_HARDENED_C:
        raise ValueError(
            f"nozzle_temp_max {nozzle_temp_max} exceeds absolute ceiling {NOZZLE_MAX_HARDENED_C}"
        )
    if tray_type not in AMS_TRAY_TYPES:
        raise ValueError(
            f"tray_type {tray_type!r} not in known materials {sorted(AMS_TRAY_TYPES)}"
        )
    return build_command(
        "print",
        "ams_filament_setting",
        ams_id=ams_id,
        tray_id=tray_id,
        tray_info_idx=tray_info_idx,
        tray_color=tray_color,
        nozzle_temp_min=nozzle_temp_min,
        nozzle_temp_max=nozzle_temp_max,
        tray_type=tray_type,
    )


def ams_get_rfid(*, ams_id: int, slot_id: int) -> Envelope:
    """Trigger an RFID re-read for a specific AMS slot.

    Useful after swapping a spool to force the printer to re-read the tag
    rather than waiting for the next periodic poll.

    ``ams_id`` and ``slot_id`` are both 0-based protocol indices.
    """
    if ams_id < 0:
        raise ValueError("ams_id must be >= 0")
    if ams_id > AMS_ID_MAX:
        raise ValueError(f"ams_id must be <= {AMS_ID_MAX} (P1S supports up to 4 AMS units)")
    if slot_id < 0:
        raise ValueError("slot_id must be >= 0")
    return build_command("print", "ams_get_rfid", ams_id=ams_id, slot_id=slot_id)


def ams_filament_drying(
    *,
    ams_id: int,
    temp: int,
    cooling_temp: int,
    duration: int,
    humidity: int,
    mode: int = 0,
    rotate_tray: bool = False,
) -> Envelope:
    """Start a filament drying cycle in the AMS.

    Parameters
    ----------
    ams_id:
        AMS unit index, 0-based.
    temp:
        Drying temperature in °C.
    cooling_temp:
        Cooling target temperature after drying (°C).
    duration:
        Drying duration in minutes.
    humidity:
        Target humidity level (0–100).
    mode:
        Drying mode integer (firmware-defined; 0 = standard).
    rotate_tray:
        Whether to rotate the tray during drying.
    """
    if ams_id < 0:
        raise ValueError("ams_id must be >= 0")
    if ams_id > AMS_ID_MAX:
        raise ValueError(f"ams_id must be <= {AMS_ID_MAX} (P1S supports up to 4 AMS units)")
    if temp <= 0:
        raise ValueError("drying temp must be > 0")
    if temp > AMS_DRYING_MAX_TEMP_C:
        raise ValueError(
            f"drying temp {temp} °C exceeds AMS hardware safety ceiling "
            f"{AMS_DRYING_MAX_TEMP_C} °C"
        )
    if cooling_temp < 0:
        raise ValueError("cooling_temp must be >= 0")
    if duration <= 0:
        raise ValueError("duration must be > 0 minutes")
    if not 0 <= humidity <= 100:
        raise ValueError("humidity must be 0–100")
    return build_command(
        "print",
        "ams_filament_drying",
        ams_id=ams_id,
        temp=temp,
        cooling_temp=cooling_temp,
        duration=duration,
        humidity=humidity,
        mode=mode,
        rotate_tray=rotate_tray,
    )


def ams_user_setting(
    *,
    ams_id: int,
    startup_read_option: bool,
    tray_read_option: bool,
) -> Envelope:
    """Configure AMS RFID-read behaviour for one AMS unit.

    ``startup_read_option``: whether to re-read RFID on AMS startup.
    ``tray_read_option``: whether to read RFID when a tray is inserted.
    """
    if ams_id < 0:
        raise ValueError("ams_id must be >= 0")
    if ams_id > AMS_ID_MAX:
        raise ValueError(f"ams_id must be <= {AMS_ID_MAX} (P1S supports up to 4 AMS units)")
    return build_command(
        "print",
        "ams_user_setting",
        ams_id=ams_id,
        startup_read_option=startup_read_option,
        tray_read_option=tray_read_option,
    )


# --------------------------------------------------------------------------- #
# Wave-2: set_accessories — nozzle type/diameter (system.set_accessories)     #
# --------------------------------------------------------------------------- #


def set_accessories_nozzle(
    *,
    nozzle_type: str,
    nozzle_diameter: float,
) -> Envelope:
    """Notify the printer of the installed nozzle type and diameter.

    ``nozzle_type`` must be ``"stainless_steel"`` or ``"hardened_steel"``.
    ``nozzle_diameter`` must be one of 0.2, 0.4, 0.6, or 0.8 mm.

    This command updates the printer's own nozzle profile and is what
    populates ``nozzle_type`` upstream in the telemetry.  The bridge also
    updates the in-memory service attribute (and queues a DB write-back;
    see wave-2 report note on persistent write-back).
    """
    if nozzle_type not in NOZZLE_TYPES:
        raise ValueError(
            f"nozzle_type {nozzle_type!r} must be one of {sorted(NOZZLE_TYPES)}"
        )
    if nozzle_diameter not in NOZZLE_DIAMETERS:
        raise ValueError(
            f"nozzle_diameter {nozzle_diameter} must be one of {sorted(NOZZLE_DIAMETERS)}"
        )
    return build_command(
        "system",
        "set_accessories",
        accessory_type="nozzle",
        nozzle_diameter=nozzle_diameter,
        nozzle_type=nozzle_type,
    )


# --------------------------------------------------------------------------- #
# Wave-2: calibration (print.calibration) — P1S-confirmed bits only           #
# --------------------------------------------------------------------------- #


def calibration(
    option: int,
    *,
    bed_type: int = 1,
) -> Envelope:
    """Run calibration routines specified by the bitmask ``option``.

    P1S-confirmed bit meanings (control matrix §8; source-conflict resolved):
    * bit 0 (1) = vibration compensation  (NOT LIDAR — P1S has no LIDAR)
    * bit 1 (2) = bed leveling (ABL)
    * bit 2 (4) = first-layer / flow calibration (extrudes purge material)
    * 7 = all three (1|2|4)

    Any bitwise combination of bits 0-2 is accepted (i.e. 1..7); all three
    individual bits and their combinations (3 = vibration+bed, 5 = vibration+flow,
    6 = bed+flow, 7 = all three) are P1S-safe per docs/P1S-CONTROL-MATRIX.md §8.
    0 is rejected (no-op that hides intent).  Bits 3+ are X1-only (LIDAR) and
    are rejected with a 422 that names the control matrix as authority.

    ``bed_type``: selects the leveling mesh profile (1 = textured plate,
    default; documented in ha-bambulab but not independently confirmed on P1S).
    """
    if option <= 0 or (option & ~_CALIBRATION_P1S_MASK) != 0:
        raise ValueError(
            f"calibration option {option} is not a valid P1S bitmask "
            f"(docs/P1S-CONTROL-MATRIX.md §8). "
            "Accepted: any non-zero combination of bits 0-2 (values 1–7). "
            "Bits 3+ may be X1-only (LIDAR); rejected to prevent firmware misbehaviour."
        )
    return build_command(
        "print",
        "calibration",
        option=option,
        bed_type=bed_type,
    )


# --------------------------------------------------------------------------- #
# Wave-3: extrude / retract (print.gcode_line — E axis)                       #
# --------------------------------------------------------------------------- #


def extrude(
    distance_mm: float,
    *,
    feedrate: int = 300,
) -> Envelope:
    """Manually extrude or retract filament (E-axis move).

    ``distance_mm > 0`` extrudes; ``distance_mm < 0`` retracts.
    ``|distance_mm|`` must be in (0, :data:`EXTRUDE_MAX_MM`].
    ``feedrate`` must be in :data:`EXTRUDE_FEEDRATE_WHITELIST` (mm/min).

    Cold-extrude protection is applied by the API layer (nozzle temp check)
    before calling this builder.  The builder itself enforces bounds and the
    feedrate whitelist.

    Gcode emitted: ``M83\\nG1 E{distance} F{feedrate}\\nM82``
    (relative extrusion mode, move, restore absolute mode).
    """
    if distance_mm == 0.0:
        raise ValueError("extrude distance must be non-zero")
    if abs(distance_mm) > EXTRUDE_MAX_MM:
        raise ValueError(
            f"|distance_mm| {abs(distance_mm)} exceeds maximum {EXTRUDE_MAX_MM} mm"
        )
    if feedrate not in EXTRUDE_FEEDRATE_WHITELIST:
        raise ValueError(
            f"feedrate {feedrate} not in whitelist {sorted(EXTRUDE_FEEDRATE_WHITELIST)}"
        )
    return _gcode(f"M83\nG1 E{distance_mm} F{feedrate}\nM82")


# --------------------------------------------------------------------------- #
# Wave-3: stepper disable (M84)                                                #
# --------------------------------------------------------------------------- #


def steppers_off() -> Envelope:
    """Disable all stepper motors (M84).

    After sending, the toolhead position is no longer maintained by the
    firmware and may drift — the bridge's dead-reckoned position becomes
    invalid.  The API layer MUST call ``service.reset_motion_state("M84")``
    after a successful publish so subsequent jog attempts re-require homing.
    """
    return _gcode("M84")
