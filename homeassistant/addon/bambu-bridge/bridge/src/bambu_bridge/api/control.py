"""Printer control endpoints — the write half of the passthrough.

Everything a phone/web client needs to *do* (not just observe) to the P1S:

* ``POST /printers/{id}/command`` — raw escape hatch. Forwards any
  ``{category, command, params}`` verbatim, so the long firmware-versioned
  tail of the P1S surface is reachable without a server release.
* Typed convenience routes (pause/resume/stop, light, temperature, fan,
  speed, gcode, home, move, AMS, filament) — validated, mapped to
  :mod:`bambu_bridge.protocol.commands` builders so the wire shape lives in
  exactly one place.

Failure mapping: unknown printer → 404; offline → 409 (fail fast, don't wait
out the publish timeout); bad parameters (builder ``ValueError``) → 422;
publish failure → 502.

Jog safety contract (defence-in-depth, crash-prevention) — FAIL CLOSED.

The P1S does NOT report toolhead position over MQTT (``home_flag`` is the only
motion-state field it sends). So we cannot clamp against a printer-reported
position — that clamp was dead code, and four consecutive Z-50 jogs once drove
the bed ~200 mm into the toolhead. Instead the bridge dead-reckons position
(see :class:`PrinterService` motion state) and the guard fails closed whenever
the estimate is unknown.

Guard order (every jog, server-side, no human in the loop):
  1. Step whitelist — distance_mm ∈ ±ALLOWED_STEPS {1, 10, 50}; else 422.
     Closes the "client sends Z-200" attack vector.
  2. Homed gate — the axis ``home_flag`` bit must be set; absent/unknown → 409.
  3. Envelope, FAIL CLOSED on unknown position:
       * Tracked position UNKNOWN (None): REJECT (409, "Home first"). We never
         allow a move from an unknown position — a gap-closing Z- jog could
         crash the bed, and even a gap-opening jog could exit the envelope if
         the bed is already at max. Unknown + any possibility of collision =
         reject.
       * Tracked position KNOWN: clamp the dead-reckoned result to
         Z ∈ [SAFE_Z_FLOOR, 256], X/Y ∈ [0, 256]; out-of-envelope → 409.
  4. The dead-reckon estimate is advanced only after a *successful* publish,
     and reset to unknown on any desync (disconnect / session loss /
     print start-stop) so a stale position can never be trusted.

Sign convention (locked by repo tests): distance_mm > 0 raises the Z
coordinate → bed lowers → gap OPENS (safe direction). distance_mm < 0 lowers
the Z coordinate → bed rises → gap CLOSES (the dangerous, bed-crashing
direction).
"""

from __future__ import annotations

import json as _json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from bambu_bridge.api import errors
from bambu_bridge.api.auth import require_auth
from bambu_bridge.api.printers import get_registry
from bambu_bridge.protocol import commands
from bambu_bridge.protocol.models import build_command
from bambu_bridge.service.printer import PrinterService
from bambu_bridge.service.registry import PrinterNotFoundError, Registry

# Discrete step sizes the UI offers.  Any other value is rejected to prevent
# a malicious or buggy client from sending an unbounded distance (e.g. Z-200).
ALLOWED_STEPS: frozenset[float] = frozenset({1.0, 10.0, 50.0})

# Physical envelope — refuse moves that would exit these bounds even if the
# printer might (expensively) fault-handle them itself.
_AXIS_MIN: dict[str, float] = {"X": 0.0, "Y": 0.0, "Z": 0.0}
_AXIS_MAX: dict[str, float] = {"X": 256.0, "Y": 256.0, "Z": 256.0}

# Hard floor the cumulative dead-reckoned Z must never be driven below, even
# from a known position. Z=0 is the bed at the nozzle (gap fully closed); going
# below means crashing the bed into the toolhead. Equal to the envelope min,
# named separately so the bed-crash floor is explicit at the guard site.
SAFE_Z_FLOOR: float = 0.0

# Home-flag bitmask (research/01-mqtt-protocol.md `home_flag`; ha-bambulab /
# OpenBambuAPI): bit 0 = X homed, bit 1 = Y homed, bit 2 = Z homed.
_HOME_BIT: dict[str, int] = {"X": 0x01, "Y": 0x02, "Z": 0x04}

router = APIRouter(
    prefix="/printers", tags=["control"], dependencies=[Depends(require_auth)]
)


def _online(registry: Registry, printer_id: str) -> PrinterService:
    try:
        service = registry.get(printer_id)
    except PrinterNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"printer {printer_id} not found",
        ) from exc
    # PR A.2: TOFU gate — if the leaf cert rotated and the operator has
    # not re-pinned via POST /printers/{id}/trust, refuse the control
    # call. The envelope body carries `actions` with the trust path the
    # APK can render as a button. `errors._http_handler` passes a
    # dict-detail with `error` through unchanged, so the typed enum
    # survives all the way to the wire.
    gate = errors.cert_gate(service)
    if gate is not None:
        # Reuse the same body — JSONResponse stores bytes; re-decode for the
        # HTTPException detail so the handler can pass it through verbatim.
        import json as _json
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
    except ConnectionError as exc:  # dropped between the check and the publish
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    return {"sent": envelope}


def _build(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Run a commands.* builder, turning its ValueError into HTTP 422."""
    try:
        return fn(*args, **kwargs)  # type: ignore[no-any-return]
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


# --------------------------------------------------------------------------- #
# Raw passthrough
# --------------------------------------------------------------------------- #


class RawCommand(BaseModel):
    """Raw command envelope passthrough.

    ``category`` is restricted to the three P1S protocol categories the
    bridge actually understands — narrows the raw escape hatch's surface
    so a future category typo (``categroy: "exec_arbitrary"``) can't be
    sent over the wire just because the bridge happens to forward it
    (Aragorn war-council finding).
    """

    model_config = ConfigDict(extra="forbid")

    category: Literal["print", "system", "info"] = Field(
        description="P1S protocol category"
    )
    command: str = Field(min_length=1, max_length=64, examples=["pause", "ledctrl"])
    params: dict[str, Any] = Field(default_factory=dict)


def _check_raw_params(params: dict[str, Any]) -> None:
    """Guard RawCommand.params against oversized or deeply-nested payloads.

    Limits (matching the P1S MQTT RX ceiling and protocol conventions):
    - Serialised size <= 4096 bytes (prevents HTTP-parse amplification).
    - Nesting depth <= 2 (values may be scalars or one-level dicts/lists;
      deeper nesting indicates abnormal input and could bypass future guards).

    Raises :class:`HTTPException` 422 on violation.
    """
    serialised = _json.dumps(params)
    if len(serialised.encode()) > 4096:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"params serialised size {len(serialised.encode())} bytes exceeds "
                "4096-byte limit"
            ),
        )

    def _depth(obj: Any, current: int = 0) -> int:
        if current > 2:
            return current
        if isinstance(obj, dict):
            return max((_depth(v, current + 1) for v in obj.values()), default=current)
        if isinstance(obj, list):
            return max((_depth(v, current + 1) for v in obj), default=current)
        return current

    if _depth(params) > 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="params nesting depth exceeds 2 levels",
        )


@router.post("/{printer_id}/command")
async def raw_command(
    printer_id: str,
    body: RawCommand,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Forward an arbitrary command envelope (gets a fresh sequence_id)."""
    _check_raw_params(body.params)
    service = _online(registry, printer_id)
    return await _send(
        service, build_command(body.category, body.command, **body.params)
    )


# --------------------------------------------------------------------------- #
# Typed control
# --------------------------------------------------------------------------- #

_PRINT_ACTIONS = {
    "pause": commands.print_pause,
    "resume": commands.print_resume,
    "stop": commands.print_stop,
}


@router.post("/{printer_id}/print/{action}")
async def print_action(
    printer_id: str, action: str, registry: Registry = Depends(get_registry)
) -> dict[str, Any]:
    """Pause / resume / stop the active print."""
    if action not in _PRINT_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"action must be one of {sorted(_PRINT_ACTIONS)}",
        )
    service = _online(registry, printer_id)
    return await _send(service, _PRINT_ACTIONS[action]())


class LightBody(BaseModel):
    on: bool


@router.post("/{printer_id}/light")
async def set_light(
    printer_id: str, body: LightBody, registry: Registry = Depends(get_registry)
) -> dict[str, Any]:
    """Chamber LED steady on/off."""
    service = _online(registry, printer_id)
    return await _send(service, commands.chamber_light(body.on))


class WorkLightBody(BaseModel):
    """Work/task light control.

    ``mode`` ∈ {``"on"``, ``"off"``, ``"flashing"``}.
    For ``"flashing"``: ``loop_times`` (default 1; 0 = loop forever) and
    ``interval_time`` ms (default 500).  Ignored for ``"on"``/``"off"``.
    """

    model_config = ConfigDict(extra="forbid")

    mode: str = Field(examples=["on", "off", "flashing"])
    loop_times: int = Field(default=1, ge=0)
    interval_time: int = Field(default=500, gt=0)


@router.post("/{printer_id}/work_light")
async def set_work_light(
    printer_id: str,
    body: WorkLightBody,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Work/task light on/off/flashing.

    Not all P1S hardware revisions include a work light; the printer silently
    ignores the command when the node is absent — the bridge always forwards
    it.
    """
    service = _online(registry, printer_id)
    return await _send(
        service,
        _build(
            commands.work_light,
            body.mode,
            loop_times=body.loop_times,
            interval_time=body.interval_time,
        ),
    )


class TemperatureBody(BaseModel):
    nozzle: int | None = None
    bed: int | None = None


@router.post("/{printer_id}/temperature")
async def set_temperature(
    printer_id: str,
    body: TemperatureBody,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Set nozzle and/or bed target (gcode M104/M140 under the hood).

    Nozzle clamp: 280 °C default (stainless nozzle); 300 °C only when the
    printer row records ``nozzle_type=hardened_steel``.  This is the server-
    side gate — the builder enforces the same ceiling but needs to know
    which cap applies.
    """
    if body.nozzle is None and body.bed is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="provide nozzle and/or bed",
        )
    service = _online(registry, printer_id)
    hardened = getattr(service, "nozzle_type", None) == "hardened_steel"
    sent: list[dict[str, Any]] = []
    if body.nozzle is not None:
        env = _build(commands.set_nozzle_temp, body.nozzle, hardened=hardened)
        sent.append((await _send(service, env))["sent"])
    if body.bed is not None:
        sent.append((await _send(service, _build(commands.set_bed_temp, body.bed)))["sent"])
    return {"sent": sent}


class FanBody(BaseModel):
    part: str = Field(examples=["part", "aux", "chamber"])
    percent: int = Field(ge=0, le=100)


@router.post("/{printer_id}/fan")
async def set_fan(
    printer_id: str, body: FanBody, registry: Registry = Depends(get_registry)
) -> dict[str, Any]:
    service = _online(registry, printer_id)
    return await _send(service, _build(commands.set_fan, body.part, body.percent))


class SpeedBody(BaseModel):
    level: int = Field(ge=1, le=4, description="1 silent · 2 standard · 3 sport · 4 ludicrous")


@router.post("/{printer_id}/speed")
async def set_speed(
    printer_id: str, body: SpeedBody, registry: Registry = Depends(get_registry)
) -> dict[str, Any]:
    service = _online(registry, printer_id)
    return await _send(service, _build(commands.print_speed, body.level))


class GcodeBody(BaseModel):
    line: str = Field(min_length=1, max_length=4096)


@router.post("/{printer_id}/gcode")
async def send_gcode(
    printer_id: str, body: GcodeBody, registry: Registry = Depends(get_registry)
) -> dict[str, Any]:
    """Raw G-code passthrough. Multi-line allowed (embed newlines)."""
    service = _online(registry, printer_id)
    return await _send(service, _build(commands.gcode_line, body.line))


@router.post("/{printer_id}/home")
async def home(
    printer_id: str, registry: Registry = Depends(get_registry)
) -> dict[str, Any]:
    """Home all axes (G28). Moves the toolhead."""
    service = _online(registry, printer_id)
    result = await _send(service, commands.home())
    # Publish succeeded → seed the dead-reckon estimate to the known post-home
    # position so subsequent jogs have a position to clamp against. (If the
    # publish raised, _send already converted it to an HTTPException and we
    # never reach here — the estimate stays whatever it was.)
    service.mark_homed()
    return result


def _jog_raise(resp: Any) -> None:
    """Decode a JSONResponse envelope and raise it as an HTTPException.

    The ``_http_handler`` in errors.py recognises ``detail`` dicts that have
    an ``"error"`` key and passes them through verbatim, so the typed error
    enum survives all the way to the wire.
    """
    body = _json.loads(bytes(resp.body).decode())
    raise HTTPException(status_code=resp.status_code, detail=body)


def _check_jog(
    service: PrinterService, axis: str, distance_mm: float
) -> None:
    """Enforce the jog safety contract — FAIL CLOSED (crash-prevention).

    Raises :class:`fastapi.HTTPException` with a contract-§2 envelope body on
    any violation so ``_http_handler`` passes it through unchanged. The server
    is the last line of defence: there is no human confirm, so a client that
    fires 100 jogs must be stopped here alone.

    Order:
      1. Step whitelist — distance_mm must be ±ALLOWED_STEPS (else 422).
      2. Homed gate — the axis ``home_flag`` bit must be set (else 409).
      3. Envelope, fail closed:
           * tracked position UNKNOWN → reject 409 (never move from unknown).
           * tracked position KNOWN → clamp the dead-reckoned result to
             Z ∈ [SAFE_Z_FLOOR, 256], X/Y ∈ [0, 256]; else 409.
    """
    axis = axis.upper()

    # 1. Step whitelist — reject arbitrary distances (closes "client sends
    #    Z-200"). Done first so a bad magnitude never reaches the printer.
    if abs(distance_mm) not in ALLOWED_STEPS:
        _jog_raise(errors.jog_step_not_allowed(distance_mm, ALLOWED_STEPS))

    # 2. Homed gate — read the homing bitmask from the printer's raw state.
    #    `home_flag` is the ONLY motion-state signal the P1S sends; it carries
    #    no position. Absent / non-integer / axis-bit-clear → unknown → reject.
    state: dict[str, Any] = service._state  # noqa: SLF001 – internal read only
    home_flag_raw = state.get("home_flag")
    if home_flag_raw is None:
        _jog_raise(errors.jog_not_homed(axis))
    try:
        home_flag = int(home_flag_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        _jog_raise(errors.jog_not_homed(axis))
        return  # unreachable — _jog_raise always raises; satisfies type-checker
    bit = _HOME_BIT.get(axis, 0)
    if not (home_flag & bit):
        _jog_raise(errors.jog_not_homed(axis))

    # 3. Envelope — clamp against the bridge's DEAD-RECKONED position, because
    #    the printer never reports one. Fail closed on unknown.
    current = service.tracked_position(axis)
    if current is None:
        # UNKNOWN POSITION + ANY POSSIBILITY OF COLLISION = REJECT.
        # We do not distinguish direction here on purpose: a gap-closing Z-
        # jog could ram the bed, and a gap-opening jog could already be at the
        # envelope max — from an unknown position both are unsafe. The fix is
        # always the same: re-home so the bridge regains a known estimate.
        _jog_raise(errors.jog_not_homed(axis))
        return  # unreachable — satisfies the type-checker

    # Known position: clamp the dead-reckoned result.
    proposed = current + distance_mm
    lo = SAFE_Z_FLOOR if axis == "Z" else _AXIS_MIN[axis]
    hi = _AXIS_MAX[axis]
    if proposed < lo or proposed > hi:
        limit = hi if proposed > hi else lo
        _jog_raise(errors.jog_out_of_envelope(axis, current, proposed, limit))


class MoveBody(BaseModel):
    axis: str = Field(examples=["X", "Y", "Z"])
    distance_mm: float
    feed_mm_min: int = Field(default=600, gt=0)


@router.post("/{printer_id}/move")
async def move(
    printer_id: str, body: MoveBody, registry: Registry = Depends(get_registry)
) -> dict[str, Any]:
    """Relative single-axis jog. Moves the toolhead.

    Safety guards (defence-in-depth, fail closed):
    - step must be in ALLOWED_STEPS (1, 10, 50 mm)
    - axis must be homed (home_flag bitmask in printer state)
    - the bridge's dead-reckoned position must be known and the result must
      stay within Z ∈ [SAFE_Z_FLOOR, 256], X/Y ∈ [0, 256] mm; an unknown
      position is rejected (re-home to recover).
    """
    service = _online(registry, printer_id)
    axis = body.axis.upper()
    _check_jog(service, axis, body.distance_mm)
    result = await _send(
        service,
        _build(
            commands.move_axis,
            axis,
            body.distance_mm,
            feed_mm_min=body.feed_mm_min,
        ),
    )
    # Publish succeeded → advance the dead-reckon estimate by the signed delta
    # so the next jog clamps against the new position. Only reached when both
    # the guard passed and the publish did not raise.
    service.apply_jog(axis, body.distance_mm)
    return result


class AmsControlBody(BaseModel):
    action: str = Field(examples=["pause", "resume", "reset"])


@router.post("/{printer_id}/ams/control")
async def ams_control(
    printer_id: str,
    body: AmsControlBody,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    service = _online(registry, printer_id)
    return await _send(service, _build(commands.ams_control, body.action))


class AmsChangeBody(BaseModel):
    target_tray: int = Field(ge=0, description="0-based AMS protocol index")
    cur_temp: int = 220
    tar_temp: int = 220


@router.post("/{printer_id}/ams/change")
async def ams_change(
    printer_id: str,
    body: AmsChangeBody,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    service = _online(registry, printer_id)
    return await _send(
        service,
        _build(
            commands.ams_change_filament,
            body.target_tray,
            cur_temp=body.cur_temp,
            tar_temp=body.tar_temp,
        ),
    )


@router.post("/{printer_id}/filament/unload")
async def unload_filament(
    printer_id: str, registry: Registry = Depends(get_registry)
) -> dict[str, Any]:
    service = _online(registry, printer_id)
    return await _send(service, commands.unload_filament())


# --------------------------------------------------------------------------- #
# Camera recording / timelapse  (camera.*)
# --------------------------------------------------------------------------- #


class IpcamBody(BaseModel):
    enabled: bool


@router.post("/{printer_id}/ipcam/record")
async def ipcam_record(
    printer_id: str,
    body: IpcamBody,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Enable or disable print video recording to the printer's SD card.

    This sends a ``camera.ipcam_record_set`` command, which the raw
    ``/command`` escape hatch cannot reach (``camera`` category is blocked).
    """
    service = _online(registry, printer_id)
    return await _send(service, commands.ipcam_record_set(body.enabled))


@router.post("/{printer_id}/ipcam/timelapse")
async def ipcam_timelapse(
    printer_id: str,
    body: IpcamBody,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Enable or disable timelapse generation on the printer's SD card."""
    service = _online(registry, printer_id)
    return await _send(service, commands.ipcam_timelapse(body.enabled))


# --------------------------------------------------------------------------- #
# Device version info  (info.get_version)
# --------------------------------------------------------------------------- #


@router.post("/{printer_id}/get_version")
async def get_version(
    printer_id: str, registry: Registry = Depends(get_registry)
) -> dict[str, Any]:
    """Request a firmware/module version refresh from the printer.

    The printer echoes its module list in the ``info.module[]`` response,
    which the bridge stores under ``state["info"]``.  Clients that want the
    latest version string should call this and then read the state snapshot
    (or watch the WS for the ``info`` category update).

    Note: ``get_version`` is also fired automatically on every MQTT
    (re)connect — this endpoint lets clients trigger a refresh on demand.
    """
    service = _online(registry, printer_id)
    return await _send(service, commands.get_version())
