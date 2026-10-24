"""Advanced printer control endpoints — wave 2 (YELLOW) and wave 3 (RED/BLACK).

Endpoints in this module require the printer to be connected; offline → 409.
All validation raises HTTP 422; guard failures raise HTTP 409.

Wave 2 — YELLOW/GREEN (typed, phase-aware, clamped):
  POST /{id}/xcam                     xcam AI vision module toggles
  POST /{id}/print_option             print detection/behaviour flags
  POST /{id}/ams/filament_setting     write filament profile to AMS slot
  POST /{id}/ams/rfid                 trigger RFID re-read for a slot
  POST /{id}/ams/drying               start AMS filament drying cycle
  POST /{id}/ams/user_setting         configure AMS RFID read behaviour
  POST /{id}/calibration              withheld pending protocol qualification
  POST /{id}/set_accessories/nozzle   set nozzle type + diameter

Wave 3 — RED (hard server-side guards):
  POST /{id}/extrude                  E-axis extrude/retract with cold-extrude guard
  POST /{id}/steppers/off             M84 stepper disable + position reset

BLACK — gated by env var:
  POST /{id}/gcode/raw                raw G-code console (BRIDGE_ENABLE_RAW_GCODE)

Contract §11 (Advanced controls) table:
  See docs/API-CONTRACT.md §11 for the risk tier column and guard list.

Nozzle note:
  set_accessories/nozzle only tells the printer which nozzle is fitted. The
  nozzle type does not set the temperature ceiling (see POST /temperature).

M84 position-state note:
  steppers/off sends M84 and calls ``service.reset_motion_state("M84")`` to
  forget the dead-reckoned position. If future code ever tracks homed/position
  in a file outside this partition (e.g. db/*) the exact one-line change needed
  is to also update that persistent store — see the report section on wave 3.
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from bambu_bridge.api import errors
from bambu_bridge.api.auth import require_auth
from bambu_bridge.api.capabilities import control_capability_gate, fresh_state
from bambu_bridge.api.printers import get_registry
from bambu_bridge.protocol import commands
from bambu_bridge.protocol.commands import AMS_DRYING_MAX_TEMP_C, AMS_ID_MAX
from bambu_bridge.service.printer import PrinterService
from bambu_bridge.service.registry import PrinterNotFoundError, Registry
from bambu_bridge.service.remaining_estimates import request_remaining_report

# Home-flag bitmask — mirrors control.py's _HOME_BIT.
# Extrude requires ALL three axes homed; an unhomed axis means position is
# unknown and the extruder carriage may not be clear of the bed or frame.
# Bit meanings: bit 0 = X homed, bit 1 = Y homed, bit 2 = Z homed.
_HOME_BIT: dict[str, int] = {"X": 0x01, "Y": 0x02, "Z": 0x04}
_ALL_AXES_HOMED_MASK: int = 0x01 | 0x02 | 0x04  # 0x07

log = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/printers",
    tags=["advanced"],
    dependencies=[Depends(require_auth), Depends(control_capability_gate)],
)


# --------------------------------------------------------------------------- #
# Shared helpers (mirrors control.py — same pattern, different module)        #
# --------------------------------------------------------------------------- #


def _online(registry: Registry, printer_id: str) -> PrinterService:
    """Resolve a printer and verify it is connected; raises HTTPException otherwise."""
    try:
        service = registry.get(printer_id)
    except PrinterNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"printer {printer_id} not found",
        ) from exc
    gate = errors.cert_gate(service)
    if gate is not None:
        body = _json.loads(bytes(gate.body).decode("utf-8"))
        raise HTTPException(status_code=gate.status_code, detail=body)
    if not service.connected:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"printer {printer_id} is not connected",
        )
    return service


async def _send(service: PrinterService, envelope: dict[str, Any]) -> dict[str, Any]:
    try:
        await service.send_raw(envelope)
    except ConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return {"sent": envelope, "confirmation": "pending"}


def _build(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Run a commands.* builder; turn its ValueError into HTTP 422."""
    try:
        return fn(*args, **kwargs)  # type: ignore[no-any-return]
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


# --------------------------------------------------------------------------- #
# Wave-2: XCam (YELLOW)                                                       #
# --------------------------------------------------------------------------- #


class XcamBody(BaseModel):
    """XCam AI vision module control.

    ``module_name`` must be one of the matrix-confirmed P1S xcam modules.
    ``print_halt=true`` enables auto-pause on detection — the user-facing
    spaghetti/air-print protection behaviour.
    """

    model_config = ConfigDict(extra="forbid")

    module_name: str = Field(
        examples=["spaghetti_detector", "first_layer_inspector"],
        description="xcam module name — must be a matrix-confirmed P1S module",
    )
    enabled: bool = Field(description="true to enable, false to disable")
    print_halt: bool = Field(
        default=False,
        description="true to auto-pause the print on detection",
    )


@router.post("/{printer_id}/xcam")
async def xcam_control(
    printer_id: str,
    body: XcamBody,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Enable or disable an xcam AI inspection module.

    ``module_name`` is whitelist-validated to the matrix-confirmed P1S set;
    unknown names are rejected (422) to prevent silent no-ops or triggering
    unexpected behaviour on future firmware.

    The ``xcam`` MQTT category is blocked by the raw /command escape hatch —
    this typed endpoint is the only path to xcam commands.

    Risk: YELLOW — print-affecting when ``print_halt=true`` (will pause the
    running print on next detection event).
    """
    service = _online(registry, printer_id)
    return await _send(
        service,
        _build(
            commands.xcam_control,
            body.module_name,
            enabled=body.enabled,
            print_halt=body.print_halt,
        ),
    )


# --------------------------------------------------------------------------- #
# Wave-2: print_option flags (YELLOW)                                         #
# --------------------------------------------------------------------------- #


class PrintOptionBody(BaseModel):
    """print_option flag payload.

    Each key must be a matrix-confirmed P1S flag name; all values are booleans.
    Unknown keys are rejected (422).  Multiple flags may be combined.

    Known flags: ``auto_recovery``, ``air_print_detect``,
    ``filament_tangle_detect``, ``nozzle_blob_detect``, ``sound_enable``.
    """

    model_config = ConfigDict(extra="allow")  # extra keys caught in builder


@router.post("/{printer_id}/print_option")
async def set_print_option(
    printer_id: str,
    body: PrintOptionBody,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Set one or more print_option boolean flags.

    Unknown flag names are rejected (422) with the list of allowed flags.
    Multiple flags may be combined in a single call; they are forwarded as
    one MQTT envelope.

    Risk: YELLOW — these are safety-detection toggles (air-print, spaghetti,
    tangle, blob detection). Disabling them reduces automatic safety coverage.
    """
    service = _online(registry, printer_id)
    # body.model_dump() gives us the raw dict including any extra keys.
    # model_config=extra="allow" means we need to pull them explicitly.
    flag_dict: dict[str, bool] = {}
    for key, val in body.model_dump().items():
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"print_option flag {key!r} must be a boolean",
            )
        flag_dict[key] = val
    if not flag_dict:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="print_option requires at least one flag",
        )
    return await _send(service, _build(commands.print_option, **flag_dict))


# --------------------------------------------------------------------------- #
# Wave-2: AMS operations (YELLOW/GREEN)                                       #
# --------------------------------------------------------------------------- #


class AmsFilamentSettingBody(BaseModel):
    """Filament profile write to an AMS slot.

    Required for untagged spools (no RFID) or to override the RFID-read
    profile.  All fields are validated server-side before forwarding.
    """

    model_config = ConfigDict(extra="forbid")

    ams_id: int = Field(ge=0, le=AMS_ID_MAX, description="AMS unit index, 0-based (max 3)")
    tray_id: int = Field(ge=0, le=3, description="slot index within AMS unit, 0-based")
    tray_info_idx: str = Field(
        default="",
        description="Bambu filament SKU string (e.g. GFB61); empty string for manual",
    )
    tray_color: str = Field(
        description="8-char hex RRGGBBAA (e.g. FFFFFFFF)",
        min_length=8,
        max_length=8,
    )
    nozzle_temp_min: int = Field(gt=0, description="minimum nozzle temperature (°C)")
    nozzle_temp_max: int = Field(gt=0, description="maximum nozzle temperature (°C)")
    tray_type: str = Field(description="material type (PLA, PETG, ABS, …)")
    # Write a slot that holds an assigned spool or belongs to a refill backup
    # anyway. Never forwarded to the printer. Strict: only JSON true lets it by.
    override: StrictBool = False


async def _slot_in_use(
    request: Request, service: PrinterService, printer_id: str, body: AmsFilamentSettingBody
) -> dict[str, Any] | None:
    """What a raw write would disturb: the slot's assignment and its refill backup.

    None when the slot has neither. The external holder never reaches this
    route (ams_id and tray_id are 0–3). assignment_kept is what the seating
    pass would decide for the slot as the write leaves it (same_spool): only a
    chemistry change ends a stamped assignment; one stored before stamping
    also ends on a product change. refill_state_expected is predicted, since
    the printer has not read the write back: an enabled group turns pending,
    one being restored needs attention, and a write that changes nothing
    leaves it be.
    """
    from bambu_bridge.inventory_sources import (
        declared_identity,
        same_spool,
        source_choices,
        spool_key,
    )
    from bambu_bridge.refill import backups

    inventory = getattr(request.app.state, "inventory", None)
    if inventory is None:
        return None
    wire = body.ams_id * 4 + body.tray_id
    snapshot = await inventory.assignments.snapshot()
    assignment = next(
        (a for a in snapshot.assignments if a.printer_id == printer_id and a.wire_slot == wire),
        None,
    )
    frame = service.material_inventory.snapshot()
    unit: dict[str, Any] = next(
        (
            u
            for u in ((frame.get("ams") or {}).get("ams") or [])
            if str(u.get("id")) == str(body.ams_id)
        ),
        {},
    )
    tray: dict[str, Any] = next(
        (t for t in unit.get("tray") or [] if str(t.get("id")) == str(body.tray_id)), {}
    )
    written = {
        "tray_type": body.tray_type,
        "tray_info_idx": body.tray_info_idx,
        "tray_color": body.tray_color.upper(),
        "nozzle_temp_min": body.nozzle_temp_min,
        "nozzle_temp_max": body.nozzle_temp_max,
    }
    current = next((c for c in source_choices(service) if c["wire_slot"] == wire), None)
    unchanged = current is not None and current["setting"] == {
        **written,
        "tray_type": body.tray_type.strip().upper(),
    }
    refill = None
    controller = getattr(inventory, "refill", None)
    if controller is not None and body.ams_id == 0:
        report = service.refill_reports.snapshot(connected=service.connected)
        status_ = controller.status(printer_id, report)  # read-only
        if status_["state"] != "disabled" and any(s["wire"] == wire for s in status_["steps"]):
            chain = next(iter(backups(status_, report)), None)
            refill = {
                "from_wire": status_["steps"][0]["wire"],
                "to_wire": status_["steps"][1]["wire"],
                "state": chain["state"] if chain else status_["state"],
                "expected": None
                if unchanged
                else ("pending" if status_["enabled"] else "needs_attention"),
            }
    if assignment is None and refill is None:
        return None
    after = {**tray, **written}
    kept = (
        assignment is None
        or same_spool(
            assignment,
            {
                "material": body.tray_type,
                "spool_key": spool_key(after),
                "observed_identity": declared_identity(after),
            },
        )
        is not False
    )
    label = f"Slot {wire + 1}"
    parts = []
    if assignment is not None:
        parts.append(f"holds spool #{assignment.spool_id}")
    if refill is not None:
        parts.append(
            f"is part of the slot {refill['from_wire'] + 1} → slot {refill['to_wire'] + 1} backup"
        )
    if refill is not None:
        cause = "slot_assigned_and_in_refill" if assignment is not None else "slot_in_refill"
        hint = (
            "Remove the backup first; it puts both slots back. Writing now makes the backup "
            "need you."
        )
    else:
        assert assignment is not None
        cause = "slot_assigned"
        hint = (
            "Change the spool in Spoolman or assign another spool; the bridge sets the slot "
            f"to match. Send with override to write it anyway — spool #{assignment.spool_id} "
            + (
                "stays assigned."
                if kept
                else "is unassigned, since the slot then reads as another spool."
            )
        )
    return {
        "error": "slot_in_use",
        "message": f"{label} " + " and ".join(parts) + ".",
        "likely_cause": cause,
        "remediation_hint": hint,
        "context": {
            "printer_id": printer_id,
            "wire_slot": wire,
            "spool_id": assignment.spool_id if assignment is not None else None,
            "refill": {k: v for k, v in refill.items() if k != "expected"} if refill else None,
            "assignment_kept": kept,
            "override_field": "override",
        },
        "_refill_expected": refill["expected"] if refill else None,
    }


@router.post("/{printer_id}/ams/filament_setting")
async def ams_filament_setting(
    printer_id: str,
    body: AmsFilamentSettingBody,
    request: Request,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Write a filament profile to an AMS slot.

    Essential for untagged (non-RFID) spools — sets the material type,
    colour, and temperature range so the printer knows how to handle the
    filament. Also allows overriding an incorrect RFID-read profile.

    A slot that holds an assigned spool or belongs to a refill backup is
    refused (409 slot_in_use, saying what the write would do) unless the
    body sets override; the assignment and refill flows keep those right.

    Risk: YELLOW — changes the printer's per-slot filament configuration;
    using incorrect temperatures could damage filament or the printer.
    """
    service = _online(registry, printer_id)
    try:
        in_use = await _slot_in_use(request, service, printer_id, body)
    except Exception:
        # Fail closed: a write that might release a spool or break a backup is
        # not sent on a guess. The override still gets through.
        log.exception("ams_filament_setting.guard_failed", printer=printer_id)
        if not body.override:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The bridge could not check this slot's spool or backup; try again, "
                "or send with override to write it anyway.",
            ) from None
        in_use = None
    expected = in_use.pop("_refill_expected", None) if in_use is not None else None
    if in_use is not None and not body.override:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=in_use)
    sent = await _send(
        service,
        _build(
            commands.ams_filament_setting,
            ams_id=body.ams_id,
            tray_id=body.tray_id,
            tray_info_idx=body.tray_info_idx,
            tray_color=body.tray_color,
            nozzle_temp_min=body.nozzle_temp_min,
            nozzle_temp_max=body.nozzle_temp_max,
            tray_type=body.tray_type,
        ),
    )
    if in_use is not None:
        context = in_use["context"]
        sent["guard"] = {
            "overridden": True,
            "assignment_kept": context["assignment_kept"],
            # Predicted: the printer has not read the write back yet.
            "refill_state_expected": expected,
        }
    return sent


# Printer states in which nothing is printing, so the AMS may turn a slot.
_RFID_READ_STATES = frozenset({"IDLE", "FINISH", "FAILED"})


class AmsRfidBody(BaseModel):
    """Trigger RFID re-read for a specific slot."""

    model_config = ConfigDict(extra="forbid")

    ams_id: int = Field(ge=0, le=AMS_ID_MAX, description="AMS unit index, 0-based (max 3)")
    slot_id: int = Field(ge=0, le=3, description="slot index within AMS unit, 0-based")
    wait_for_report_s: float = Field(default=0, ge=0, le=30)


@router.post("/{printer_id}/ams/rfid")
async def ams_get_rfid(
    printer_id: str,
    body: AmsRfidBody,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Trigger an RFID re-read for a specific AMS slot.

    Useful after swapping a spool to force the printer to re-read the RFID
    tag rather than waiting for the next automatic poll.

    Risk: GREEN — read-only RFID scan; no physical motion or heating. The
    AMS turns the slot to read it, so only while the printer is idle.
    """
    service = _online(registry, printer_id)
    if fresh_state(service) not in _RFID_READ_STATES:
        raise HTTPException(status_code=409, detail="The printer reads tags when it's idle")
    try:
        return await request_remaining_report(
            service,
            _build(commands.ams_get_rfid, ams_id=body.ams_id, slot_id=body.slot_id),
            body.ams_id * 4 + body.slot_id,
            body.wait_for_report_s,
        )
    except ConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class AmsDryingBody(BaseModel):
    """AMS filament drying cycle parameters."""

    model_config = ConfigDict(extra="forbid")

    ams_id: int = Field(ge=0, le=AMS_ID_MAX, description="AMS unit index, 0-based (max 3)")
    temp: int = Field(gt=0, le=AMS_DRYING_MAX_TEMP_C, description="drying temperature (°C; max 75)")
    cooling_temp: int = Field(ge=0, description="cooling target temperature (°C)")
    duration: int = Field(gt=0, description="drying duration in minutes")
    humidity: int = Field(ge=0, le=100, description="target humidity level (0–100)")
    mode: int = Field(default=0, ge=0, description="drying mode (0 = standard)")
    rotate_tray: bool = Field(default=False, description="rotate tray during drying")


@router.post("/{printer_id}/ams/drying")
async def ams_filament_drying(
    printer_id: str,
    body: AmsDryingBody,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Start a filament drying cycle in the AMS.

    Requires AMS firmware support for the drying feature. If the AMS firmware
    does not support drying, the command will be silently ignored.

    Risk: YELLOW — activates a heating process in the AMS unit.
    """
    service = _online(registry, printer_id)
    return await _send(
        service,
        _build(
            commands.ams_filament_drying,
            ams_id=body.ams_id,
            temp=body.temp,
            cooling_temp=body.cooling_temp,
            duration=body.duration,
            humidity=body.humidity,
            mode=body.mode,
            rotate_tray=body.rotate_tray,
        ),
    )


class AmsUserSettingBody(BaseModel):
    """AMS RFID read behaviour configuration."""

    model_config = ConfigDict(extra="forbid")

    ams_id: int = Field(ge=0, le=AMS_ID_MAX, description="AMS unit index, 0-based (max 3)")
    startup_read_option: bool = Field(description="re-read RFID tags on AMS startup")
    tray_read_option: bool = Field(description="read RFID when a tray is inserted")


@router.post("/{printer_id}/ams/user_setting")
async def ams_user_setting(
    printer_id: str,
    body: AmsUserSettingBody,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Configure AMS RFID read behaviour for one AMS unit.

    Controls whether RFID tags are read on AMS startup and when trays are
    inserted.  Disabling startup reads speeds up boot; disabling tray reads
    prevents automatic profile override when a spool is inserted.

    Risk: YELLOW — changes AMS behaviour; disabling reads means the printer
    will not automatically detect spool changes.
    """
    service = _online(registry, printer_id)
    return await _send(
        service,
        _build(
            commands.ams_user_setting,
            ams_id=body.ams_id,
            startup_read_option=body.startup_read_option,
            tray_read_option=body.tray_read_option,
        ),
    )


# --------------------------------------------------------------------------- #
# Wave-2: calibration (RED)                                                   #
# --------------------------------------------------------------------------- #


class CalibrationBody(BaseModel):
    """Manufacturer calibration mask; endpoint withheld pending qualification."""

    model_config = ConfigDict(extra="forbid")

    option: int = Field(description="Manufacturer mask: bed=2, vibration=4; endpoint under review")
    bed_type: int | None = Field(
        default=None,
        ge=0,
        description="Legacy parameter; protocol support under review",
    )


@router.post("/{printer_id}/calibration")
async def run_calibration(
    printer_id: str,
    body: CalibrationBody,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Run printer calibration routines.

    The builder accepts manufacturer bed-leveling bit 2 and vibration bit 4,
    individually or combined as 6. Public access is withheld by the capability
    dependency pending protocol qualification. Risk: RED — machine motion.
    """
    service = _online(registry, printer_id)
    return await _send(
        service,
        _build(commands.calibration, body.option, bed_type=body.bed_type),
    )


# --------------------------------------------------------------------------- #
# Wave-2: set_accessories — nozzle (YELLOW)                                   #
# --------------------------------------------------------------------------- #


class SetNozzleBody(BaseModel):
    """Nozzle type and diameter configuration."""

    model_config = ConfigDict(extra="forbid")

    nozzle_type: Literal["stainless_steel", "hardened_steel"] = Field(
        description="nozzle material type"
    )
    nozzle_diameter: float = Field(
        description="nozzle diameter in mm — must be one of 0.2, 0.4, 0.6, 0.8"
    )


@router.post("/{printer_id}/set_accessories/nozzle")
async def set_nozzle(
    printer_id: str,
    body: SetNozzleBody,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Notify the printer of the installed nozzle type and diameter.

    Sends a ``system.set_accessories`` command to update the printer's
    nozzle profile.

    Risk: YELLOW — incorrect nozzle settings mislead the printer's own
    filament/nozzle compatibility checks.
    """
    service = _online(registry, printer_id)
    result = await _send(
        service,
        _build(
            commands.set_accessories_nozzle,
            nozzle_type=body.nozzle_type,
            nozzle_diameter=body.nozzle_diameter,
        ),
    )
    return result


# --------------------------------------------------------------------------- #
# Wave-3: extrude / retract (RED)                                             #
# --------------------------------------------------------------------------- #

# Nozzle states that allow extrusion (printer is idle or paused).
_EXTRUDE_ALLOWED_STATES: frozenset[str] = frozenset({"IDLE", "PAUSE"})


class ExtrudeBody(BaseModel):
    """E-axis extrude/retract.

    ``distance_mm > 0`` extrudes; ``< 0`` retracts.
    Max ``|distance_mm|`` is 100 mm (EXTRUDE_MAX_MM).
    ``feedrate`` must be one of 120, 300, 600 mm/min.
    """

    model_config = ConfigDict(extra="forbid")

    distance_mm: float = Field(description="positive = extrude, negative = retract; |value| ≤ 100")
    feedrate: int = Field(
        default=300,
        description="feedrate in mm/min — must be one of 120, 300, 600",
    )


@router.post("/{printer_id}/extrude")
async def extrude(
    printer_id: str,
    body: ExtrudeBody,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Manually extrude or retract filament.

    Guards (all server-side, no human confirm):
    1. Printer state must be IDLE or PAUSE — prevents mid-print extrusion
       interference (print active = 409 extrude_state_not_allowed).
    2. ALL axes (X|Y|Z) must be homed — home_flag bitmask 0x07 must be fully
       set (409 jog_not_homed, naming the unhomed axes). home_flag absent,
       non-integer, or any bit clear → 409. A never-homed printer reports
       home_flag=0 (bits present but clear); checking only `is None` is
       insufficient and would pass that case.
    3. Nozzle temperature must be available and ≥ EXTRUDE_MIN_TEMP_C (170 °C)
       — cold-extrude protection (422 nozzle_too_cold). If temperature data
       is not available in the service state, extrusion is refused entirely
       (conservative: unknown temp = could be cold).
    4. |distance_mm| ≤ 100 mm — 422 from builder.
    5. feedrate in {120, 300, 600} mm/min — 422 from builder.

    Risk: RED — moves filament in/out of the hotend; cold extrusion can damage
    the nozzle or jam the extruder.
    """
    service = _online(registry, printer_id)
    state: dict[str, Any] = service._state  # noqa: SLF001 — internal read

    # Guard 1: state must be IDLE or PAUSE.
    gcode_state = state.get("gcode_state")
    if gcode_state not in _EXTRUDE_ALLOWED_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "extrude_state_not_allowed",
                "message": (
                    f"Extrude/retract is only allowed when the printer is IDLE or PAUSE; "
                    f"current state is {gcode_state!r}."
                ),
                "likely_cause": "print_active",
                "context": {"gcode_state": gcode_state},
            },
        )

    # Guard 2: ALL axes must be homed — same bitmask idiom as control.py jog guard.
    # A powered-on never-homed printer reports home_flag=0 (bits present but clear);
    # checking only `is None` passes that case. We must parse the int and require
    # all three axis bits set. Fail closed: absent, non-integer, or any bit clear → 409.
    home_flag_raw = state.get("home_flag")
    home_flag: int | None = None
    if home_flag_raw is not None:
        try:
            home_flag = int(home_flag_raw)
        except (TypeError, ValueError):
            home_flag = None  # unparseable → treat as not homed
    if home_flag is None or (home_flag & _ALL_AXES_HOMED_MASK) != _ALL_AXES_HOMED_MASK:
        # Identify which axes are unhomed so the error names them explicitly.
        if home_flag is not None:
            unhomed = [ax for ax, bit in _HOME_BIT.items() if not (home_flag & bit)]
        else:
            unhomed = list(_HOME_BIT.keys())
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "jog_not_homed",
                "message": (
                    f"Cannot extrude: axes {unhomed} have not been homed. " "Run Home first."
                ),
                "likely_cause": "axis_position_unknown",
                "remediation_hint": "Tap 'Home all' to home all axes before extruding.",
                "context": {
                    "unhomed_axes": unhomed,
                    "home_flag": home_flag_raw,
                },
            },
        )

    # Guard 3: cold-extrude protection.
    nozzle_temp: float | None = None
    raw_temp = state.get("nozzle_temper")
    if raw_temp is not None:
        try:
            nozzle_temp = float(raw_temp)
        except (TypeError, ValueError):
            nozzle_temp = None

    if nozzle_temp is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "nozzle_too_cold",
                "message": (
                    "Nozzle temperature is not available. Cannot extrude safely "
                    "without knowing the current nozzle temperature."
                ),
                "likely_cause": "temp_data_unavailable",
                "remediation_hint": (
                    "Wait for the printer to report nozzle temperature before extruding."
                ),
            },
        )

    if nozzle_temp < commands.EXTRUDE_MIN_TEMP_C:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "nozzle_too_cold",
                "message": (
                    f"Nozzle is too cold to extrude safely: "
                    f"{nozzle_temp:.1f} °C < {commands.EXTRUDE_MIN_TEMP_C} °C minimum. "
                    f"Heat the nozzle first."
                ),
                "likely_cause": "cold_extrude",
                "context": {
                    "nozzle_temp_c": nozzle_temp,
                    "min_temp_c": commands.EXTRUDE_MIN_TEMP_C,
                },
                "remediation_hint": (
                    f"Set nozzle temperature to at least {commands.EXTRUDE_MIN_TEMP_C} °C "
                    f"and wait for it to reach target before extruding."
                ),
            },
        )

    return await _send(
        service,
        _build(commands.extrude, body.distance_mm, feedrate=body.feedrate),
    )


# --------------------------------------------------------------------------- #
# Wave-3: stepper disable (RED)                                               #
# --------------------------------------------------------------------------- #


@router.post("/{printer_id}/steppers/off")
async def steppers_off(
    printer_id: str,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Disable all stepper motors (M84).

    After sending M84, the toolhead is no longer position-controlled by the
    firmware and may drift. The bridge resets its dead-reckoned position to
    UNKNOWN so subsequent jog attempts re-require homing — the same safeguard
    applied on disconnect/session-loss.

    Risk: RED — disables steppers; position tracking is lost. Re-home before
    jogging.

    NOTE on persistent position state: ``reset_motion_state`` clears the
    in-memory dead-reckon estimate. If a future revision stores homed/position
    state in db/* (outside this module's partition), the exact one-line change
    is: ``await registry.clear_motion_state(printer_id)`` — see wave-3 report.
    """
    service = _online(registry, printer_id)
    result = await _send(service, commands.steppers_off())
    # Reset the dead-reckon estimate — toolhead may now drift freely.
    service.reset_motion_state("M84")
    return result


# --------------------------------------------------------------------------- #
# BLACK: raw G-code console (BRIDGE_ENABLE_RAW_GCODE gate)                   #
# --------------------------------------------------------------------------- #


class RawGcodeBody(BaseModel):
    """Raw G-code passthrough.

    ``line`` may be a single G-code command or a multi-line sequence
    (embed newlines). Maximum 4096 bytes after UTF-8 encoding (the P1S
    MQTT RX buffer ceiling).

    WARNING: this is the no-guardrails path. Every command is logged at
    WARNING level. There is no validation of the G-code content beyond the
    length cap. This endpoint is gated by the BRIDGE_ENABLE_RAW_GCODE
    environment variable.
    """

    model_config = ConfigDict(extra="forbid")

    line: str = Field(min_length=1, description="G-code to send (max 4096 bytes)")


@router.post("/{printer_id}/gcode/raw")
async def raw_gcode_console(
    request: Request,
    printer_id: str,
    body: RawGcodeBody,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Raw G-code console endpoint — NO GUARDRAILS.

    !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    BLACK ENDPOINT — GATED BY BRIDGE_ENABLE_RAW_GCODE ENVIRONMENT VARIABLE
    !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

    When BRIDGE_ENABLE_RAW_GCODE is unset or empty (the default), this
    endpoint returns 403 with an explanatory message. Enabling it is an
    explicit operator decision that unlocks arbitrary G-code execution.

    When enabled:
    - Every G-code line is logged at WARNING level (structural log + std-lib
      warning for visibility in log aggregators).
    - The 4096-byte MQTT RX buffer ceiling is inherited from gcode_line.
    - No content validation — any valid G-code (including destructive
      commands like M500/M501/M502 EEPROM ops) is forwarded.

    This is the explicit escape hatch for firmware debugging, factory resets,
    and one-off calibration commands that have no typed endpoint.

    Risk: BLACK — arbitrary motion and configuration changes possible.
    """
    if not commands.raw_gcode_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "raw_gcode_disabled",
                "message": (
                    "Raw G-code console is disabled. "
                    "Set BRIDGE_ENABLE_RAW_GCODE=1 in the bridge environment "
                    "to enable this no-guardrails endpoint."
                ),
                "likely_cause": "feature_gate_not_set",
                "remediation_hint": (
                    "Add BRIDGE_ENABLE_RAW_GCODE=1 to the bridge .env file and restart. "
                    "Read the API contract §11 warning before enabling."
                ),
            },
        )

    service = _online(registry, printer_id)

    # Log at WARNING — raw G-code is the no-guardrails path. Both structlog
    # and stdlib logging so ops pipelines that don't parse structlog JSON see it.
    client = request.client.host if request.client else "unknown"
    log.warning(
        "raw_gcode_console",
        printer_id=printer_id,
        client_host=client,
        line=body.line,
    )
    logging.getLogger(__name__).warning(
        "raw_gcode_console printer=%s client=%s line=%r",
        printer_id,
        client,
        body.line,
    )

    return await _send(service, _build(commands.gcode_line, body.line))
