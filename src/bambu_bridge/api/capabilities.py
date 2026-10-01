"""Server enforcement for the qualified P1S adapter.

Manufacturer hardware support and bridge command qualification are separate.
Identity refresh stays available while model discovery is pending.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request


def model_of(service: Any) -> str:
    snapshot = service.snapshot() if callable(getattr(service, "snapshot", None)) else {}
    raw = snapshot.get("_raw") or {}
    info = raw.get("info") or {}
    modules = info.get("module") or []
    reported = next(
        (m.get("product_name") for m in modules if isinstance(m, dict) and m.get("name") == "ota"),
        None,
    )
    model = str(reported or snapshot.get("model") or getattr(service, "model", None) or "").strip()
    return "P1S" if model.upper() in {"P1S", "BAMBU LAB P1S", "C12"} else model


def unavailable(message: str) -> None:
    raise HTTPException(409, {"error": "capability_unavailable", "message": message})


def require_p1s(service: Any) -> None:
    if model_of(service) != "P1S":
        unavailable("Bridge control support requires a verified P1S identity")


def reported_nozzle(service: Any) -> float:
    try:
        diameter = float((service.snapshot().get("_raw") or {}).get("nozzle_diameter"))
    except (TypeError, ValueError):
        diameter = 0
    if diameter not in {0.2, 0.4, 0.6, 0.8}:
        unavailable("Reported nozzle diameter required for file compatibility")
    return diameter


def fresh_state(service: Any) -> str:
    snapshot = service.snapshot()
    session = snapshot.get("session") or {}
    try:
        stamp = datetime.fromisoformat(session["last_telemetry_at"].replace("Z", "+00:00"))
        age = (datetime.now(UTC) - stamp).total_seconds()
    except (KeyError, TypeError, ValueError):
        age = float("inf")
    if not session.get("connected") or not -5 <= age <= 15:
        unavailable("Fresh printer status required")
    return str((snapshot.get("_raw") or {}).get("gcode_state", "UNKNOWN"))


# A P1S keeps reporting FAILED after a stopped or failed print until the next
# job starts; the screen's dismiss does not return it to IDLE. Treat it like
# FINISH, as the native gateway, Orca route and job watcher already do. Whether
# the plate is clear is the operator's call, not a telemetry state.
START_READY_STATES = frozenset({"IDLE", "FINISH", "FAILED"})


def require_start_ready(service: Any, what: str = "a file") -> None:
    # A print never starts on a printer whose certificate no longer matches
    # its TOFU pin; the same 403 the control routes return (contract §4.5).
    from bambu_bridge.api import errors

    gate = errors.cert_gate(service)
    if gate is not None:
        import json

        raise HTTPException(gate.status_code, json.loads(bytes(gate.body)))
    if fresh_state(service) not in START_READY_STATES:
        unavailable(f"Printer must be idle before starting {what}")


async def control_capability_gate(request: Request) -> None:
    """Protect every typed/raw route in the control routers before publication."""
    from bambu_bridge.api.printers import get_registry
    from bambu_bridge.service.registry import PrinterNotFoundError

    registry = get_registry(request)
    try:
        service = registry.get(request.path_params["printer_id"])
    except PrinterNotFoundError:
        raise HTTPException(404, "Printer not found") from None
    from bambu_bridge.api import errors

    gate = errors.cert_gate(service)
    if gate is not None:
        import json

        raise HTTPException(gate.status_code, json.loads(bytes(gate.body)))
    suffix = request.url.path.split(f"/printers/{request.path_params['printer_id']}/", 1)[-1]
    if suffix == "get_version":
        return
    # Raw routes require an explicit operator opt-in, including the legacy alias.
    if suffix in {"command", "gcode", "gcode/raw"}:
        from bambu_bridge.protocol.commands import raw_gcode_enabled

        if not raw_gcode_enabled():
            raise HTTPException(
                403,
                {
                    "error": "raw_gcode_disabled",
                    "message": "Raw control requires BRIDGE_ENABLE_RAW_GCODE=1",
                },
            )
    require_p1s(service)
    if suffix in {
        "xcam",
        "calibration",
        "set_accessories/nozzle",
        "ams/drying",
        "home",
        "move",
        "extrude",
        "steppers/off",
        "filament/unload",
        "ams/change",
        "skip_objects",
    }:
        unavailable("Control support under review")
    state = fresh_state(service)
    allowed = {
        "print/pause": {"RUNNING", "PREPARE"},
        "print/resume": {"PAUSE"},
        "print/stop": {"RUNNING", "PREPARE", "PAUSE"},
    }
    if suffix in allowed and state not in allowed[suffix]:
        unavailable(f"Printer state: {state}")
    raw = service.snapshot().get("_raw") or {}
    if suffix == "work_light" and not any(
        isinstance(n, dict) and n.get("node") == "work_light" for n in raw.get("lights_report", [])
    ):
        unavailable("Not available on P1S")
    if suffix.startswith("ams/"):
        units = (raw.get("ams") or {}).get("ams") or []
        body = await request.json()
        unit = next((u for u in units if str(u.get("id")) == str(body.get("ams_id"))), None)
        if unit is None:
            unavailable("Selected AMS requires a current hardware report")
        if suffix in {"ams/rfid", "ams/filament_setting"}:
            slot = body.get("slot_id", body.get("tray_id"))
            if not any(str(t.get("id")) == str(slot) for t in unit.get("tray", [])):
                unavailable("Selected tray requires a current hardware report")
    if suffix == "print_option":
        body = await request.json()
        flag = raw.get("home_flag")
        for name in body:
            if name == "auto_recovery":
                continue
            bit = {"filament_tangle_detect": 19, "nozzle_blob_detect": 25, "sound_enable": 18}.get(
                name
            )
            if bit is None or not isinstance(flag, int) or not flag & (1 << bit):
                unavailable(f"Control support unavailable: {name}")
