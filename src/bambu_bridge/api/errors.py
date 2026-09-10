"""Universal API error envelope (contract §2).

Every 4xx/5xx response in the bridge uses the shape:

    {
      "error": "<stable_enum>",
      "message": "<short user-facing sentence>",
      "likely_cause": "<optional enum>",
      "remediation_hint": "<optional human sentence with menu paths>",
      "context": { "phase": "...", "printer_id": "...", ... },
      "discovered": { "serial": "...", "model": "..." },  # onboarding only
      "_raw": { "exc_type": "...", "exc_str": "..." }     # dev mode
    }

This module provides:

* :func:`envelope` — build a typed JSONResponse with this shape.
* :func:`install` — global handlers for pydantic validation, the generic
  HTTPException FastAPI uses internally, and unhandled exceptions; each
  one is reshaped into the universal envelope so no endpoint can leak a
  bare ``{"detail": "…"}`` body anymore.
* Tiny per-error builders (:func:`unreachable`, :func:`auth_failed`,
  :func:`no_telemetry`, :func:`conflict`, :func:`not_found`, …) so the
  endpoint code stays declarative.

`error` strings are reserved in contract §2.1 — keep this module's
constants in sync.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# --------------------------------------------------------------------------- #
# Reserved error enums (mirror contract §2.1; keep flat, namespaced)
# --------------------------------------------------------------------------- #

ERR_AUTH_MISSING = "auth_missing"
ERR_AUTH_INVALID = "auth_invalid"
ERR_AUTH_NOT_CONFIGURED = "auth_not_configured"
ERR_NOT_FOUND = "not_found"
ERR_CONFLICT = "conflict"
ERR_INVALID_INPUT = "invalid_input"
ERR_INTERNAL = "internal_error"

ERR_PRINTER_OFFLINE = "printer_offline"
ERR_PRINTER_UNREACHABLE = "printer_unreachable"
ERR_PRINTER_AUTH_FAILED = "printer_auth_failed"
ERR_PRINTER_CERT_CHANGED = "printer_cert_changed"
ERR_MQTT_NO_TELEMETRY = "mqtt_no_telemetry"

ERR_FTPS_FAILED = "ftps_failed"
ERR_FTPS_AUTH_FAILED = "ftps_auth_failed"

ERR_INVALID_3MF = "invalid_3mf"
ERR_PRINT_SUBMIT_FAILED = "print_submit_failed"
ERR_PRINT_COMMAND_FAILED = "print_command_failed"

ERR_CAMERA_UNAVAILABLE = "camera_unavailable"
ERR_CAMERA_NO_FRAME = "camera_no_frame"

ERR_JOG_NOT_HOMED = "jog_not_homed"
ERR_JOG_OUT_OF_ENVELOPE = "jog_out_of_envelope"
ERR_JOG_STEP_NOT_ALLOWED = "jog_step_not_allowed"


# --------------------------------------------------------------------------- #
# Core builder
# --------------------------------------------------------------------------- #


def envelope(
    *,
    error: str,
    message: str,
    status_code: int,
    likely_cause: str | None = None,
    remediation_hint: str | None = None,
    context: Mapping[str, Any] | None = None,
    discovered: Mapping[str, Any] | None = None,
    raw: Mapping[str, Any] | None = None,
    actions: Iterable[Mapping[str, Any]] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> JSONResponse:
    """Build a contract-§2-shaped JSONResponse.

    Optional fields are dropped when empty so the wire stays tidy.
    ``extra`` lets callers attach per-endpoint keys (e.g. ``issues`` for
    3MF validate, ``existing_printer_id`` for 409 conflict,
    ``active_job_ids`` for DELETE cascade conflict) at the envelope root.
    """
    body: dict[str, Any] = {"error": error, "message": message}
    if likely_cause:
        body["likely_cause"] = likely_cause
    if remediation_hint:
        body["remediation_hint"] = remediation_hint
    if context:
        body["context"] = dict(context)
    if discovered:
        body["discovered"] = dict(discovered)
    if raw:
        body["_raw"] = dict(raw)
    if actions:
        body["actions"] = [dict(a) for a in actions]
    if extra:
        for k, v in extra.items():
            if k in body:
                raise ValueError(f"envelope: extra key collides with reserved: {k!r}")
            body[k] = v
    return JSONResponse(status_code=status_code, content=body)


# --------------------------------------------------------------------------- #
# Onboarding (contract §3.4) — the three-fork builders
# --------------------------------------------------------------------------- #


def unreachable(host: str, port: int, raw: Mapping[str, Any] | None = None) -> JSONResponse:
    """E1 — TCP/TLS unreachable."""
    return envelope(
        error=ERR_PRINTER_UNREACHABLE,
        message=f"Can't reach the printer at {host}.",
        status_code=status.HTTP_502_BAD_GATEWAY,
        remediation_hint=(
            "Check that the printer is powered on, the screen is awake, and "
            "this device is on the same Wi-Fi network (not a Guest network). "
            "The IP can change — confirm at Settings ▸ WLAN ▸ IP on the printer."
        ),
        context={"phase": "tls_handshake", "host": host, "port": port},
        raw=raw,
    )


def auth_failed(
    serial: str, model: str | None, connack_code: int | None, raw: Mapping[str, Any] | None = None
) -> JSONResponse:
    """E2 — MQTT CONNACK non-zero. The cert handshake succeeded so the serial
    is known; include it in ``discovered`` so the APK can confirm-the-printer
    inside the error.

    Returns **403** not 401 (Frodo war-council finding): the bridge's own
    Bearer auth uses 401 for missing/invalid API keys, and the APK would
    otherwise have to body-sniff to tell them apart. The printer-side auth
    failure is semantically distinct → its own status code.

    Remediation order: LAN-Only Mode mentioned first (more common
    fresh-printer cause), access code second.
    """
    return envelope(
        error=ERR_PRINTER_AUTH_FAILED,
        message="The printer rejected the access code.",
        status_code=status.HTTP_403_FORBIDDEN,
        likely_cause="wrong_access_code_or_lan_mode_off",
        remediation_hint=(
            "On the printer, confirm Settings ▸ Network ▸ LAN-Only Mode is on. "
            "Then check Settings ▸ WLAN ▸ Access Code (case-sensitive, "
            "regenerable)."
        ),
        context={"phase": "mqtt_connack", "connack_code": connack_code},
        discovered={"serial": serial, **({"model": model} if model else {})},
        raw=raw,
    )


def no_telemetry(
    serial: str,
    model: str | None,
    wait_ms: int,
    raw: Mapping[str, Any] | None = None,
) -> JSONResponse:
    """E3 — connected but no report within the post-pushall watchdog."""
    return envelope(
        error=ERR_MQTT_NO_TELEMETRY,
        message=f"Connected to your {model or 'printer'}, but it isn't sending status.",
        status_code=status.HTTP_502_BAD_GATEWAY,
        likely_cause="developer_mode_off_or_lan_drop",
        remediation_hint=(
            "On the printer, enable Settings ▸ Network ▸ LAN-Only Mode (and "
            "Developer Mode if shown). Then try again."
        ),
        context={"phase": "mqtt_no_telemetry", "post_pushall_wait_ms": wait_ms},
        discovered={"serial": serial, **({"model": model} if model else {})},
        raw=raw,
    )


def cert_changed(
    serial: str, previous: str, current: str, printer_id: str
) -> JSONResponse:
    """403 — leaf cert rotated (post-firmware-update TOFU prompt)."""
    return envelope(
        error=ERR_PRINTER_CERT_CHANGED,
        message=(
            "Your printer's security key changed. "
            "This is normal right after a firmware update."
        ),
        status_code=status.HTTP_403_FORBIDDEN,
        remediation_hint=f"If you just updated firmware on {serial}, tap Trust.",
        context={
            "printer_id": printer_id,
            "previous_fingerprint": previous,
            "current_fingerprint": current,
        },
        actions=[
            {
                "id": "trust",
                "label": "Trust this printer",
                "method": "POST",
                "path": f"/api/v1/printers/{printer_id}/trust",
            },
            {"id": "deny", "label": "Not now", "method": None},
        ],
    )


# --------------------------------------------------------------------------- #
# Common boring builders
# --------------------------------------------------------------------------- #


def not_found(what: str, ident: str) -> JSONResponse:
    return envelope(
        error=ERR_NOT_FOUND,
        message=f"{what} {ident!r} not found.",
        status_code=status.HTTP_404_NOT_FOUND,
        context={"what": what, "id": ident},
    )


def conflict(message: str, **extra: Any) -> JSONResponse:
    return envelope(
        error=ERR_CONFLICT,
        message=message,
        status_code=status.HTTP_409_CONFLICT,
        extra=extra or None,
    )


def invalid_input(message: str, **extra: Any) -> JSONResponse:
    return envelope(
        error=ERR_INVALID_INPUT,
        message=message,
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        extra=extra or None,
    )


def cert_gate(svc: Any) -> JSONResponse | None:
    """Refuse reads/control when the printer's leaf cert no longer matches
    the recorded TOFU pin (PR A.2; contract §4.5).

    Returns the ``printer_cert_changed`` envelope when ``svc.cert_status``
    is ``"changed"``; ``None`` when the route should proceed (``"trusted"``
    or ``"unknown"`` — legacy pre-pin rows are allowed through, the operator
    can post /trust to upgrade them at any time).

    Endpoints that resolve the mismatch (``POST /printers/{id}/trust``)
    must NOT call this — they're the cure, not subject to the gate.
    """
    if getattr(svc, "cert_status", "unknown") != "changed":
        return None
    return cert_changed(
        serial=svc.serial,
        previous=svc.expected_fingerprint or "",
        current=svc.current_fingerprint or "",
        printer_id=svc.serial,
    )


def jog_not_homed(axis: str) -> JSONResponse:
    """409 — jog rejected because the axis has not been homed."""
    return envelope(
        error=ERR_JOG_NOT_HOMED,
        message=f"Cannot jog {axis}: axis has not been homed. Run Home first.",
        status_code=status.HTTP_409_CONFLICT,
        likely_cause="axis_position_unknown",
        remediation_hint="Tap 'Home all' to home all axes before jogging.",
        context={"axis": axis},
    )


def jog_out_of_envelope(
    axis: str, current_mm: float, proposed_mm: float, limit_mm: float
) -> JSONResponse:
    """409 — jog would take the axis outside the safe envelope."""
    return envelope(
        error=ERR_JOG_OUT_OF_ENVELOPE,
        message=(
            f"Cannot jog {axis}: move to {proposed_mm:.1f} mm exceeds "
            f"safe limit ({limit_mm:.1f} mm)."
        ),
        status_code=status.HTTP_409_CONFLICT,
        likely_cause="position_near_limit",
        context={"axis": axis, "current_mm": current_mm, "proposed_mm": proposed_mm,
                 "limit_mm": limit_mm},
    )


def jog_step_not_allowed(
    distance_mm: float,
    allowed: frozenset[float] | None = None,
) -> JSONResponse:
    """422 — step size is not in the allowed discrete set.

    ``allowed`` defaults to {1.0, 10.0, 50.0} — the UI-offered steps.
    Callers can pass the real set to keep the message in sync if the
    whitelist ever changes.
    """
    _allowed = sorted(allowed or frozenset({1.0, 10.0, 50.0}))
    return envelope(
        error=ERR_JOG_STEP_NOT_ALLOWED,
        message=f"Step size {distance_mm} mm is not allowed. Use {_allowed}.",
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        likely_cause="client_sent_arbitrary_distance",
        context={"distance_mm": distance_mm, "allowed": _allowed},
    )


def offline(printer_id: str) -> JSONResponse:
    return envelope(
        error=ERR_PRINTER_OFFLINE,
        message="Printer is offline.",
        status_code=status.HTTP_409_CONFLICT,
        likely_cause="bridge_lost_mqtt",
        remediation_hint="The bridge is trying to reconnect; try again in a few seconds.",
        context={"printer_id": printer_id},
    )


# --------------------------------------------------------------------------- #
# Global exception handlers — install once at app startup
# --------------------------------------------------------------------------- #


def install(app: FastAPI) -> None:
    """Wire the universal envelope into every error path FastAPI raises.

    Without this, pydantic validation errors leak FastAPI's ``{"detail": [...]}``
    format and bare ``HTTPException(detail=...)`` calls produce
    ``{"detail": "string"}`` — neither is contract §2 shape. The handlers
    here keep every endpoint honest.
    """

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(  # noqa: ARG001
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Compress the multi-error pydantic detail into a flat list the APK
        # can render: ["body.host: field required", …]. Keep the contract
        # envelope shape; stash a JSON-safe version of pydantic's detail
        # under `_raw` (raw `exc.errors()` contains non-serialisable
        # objects like the raised ValueError instance in `ctx`).
        issues = [
            f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', '')}"
            for err in exc.errors()
        ]
        # Validation inputs can contain pairing secrets and printer credentials.
        # Field locations and messages diagnose the problem without echoing them.
        safe_errors = [
            {k: _json_safe(v) for k, v in err.items() if k not in {"input", "ctx"}}
            for err in exc.errors()
        ]
        return envelope(
            error=ERR_INVALID_INPUT,
            message="Request body is malformed.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            extra={"issues": issues},
            raw={"pydantic_errors": safe_errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(  # noqa: ARG001
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # If a route raised an HTTPException with a dict detail that already
        # looks like an envelope (has `error`), pass it through unchanged
        # — never let _status_to_error overwrite the route's typed `error`
        # enum with a generic one (Frodo war-council finding).
        if isinstance(exc.detail, Mapping) and "error" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=dict(exc.detail))
        # Empty-string trap: bare `HTTPException(401)` raised by auth dep
        # has detail=None → str(None) was "None"; old code shipped that to
        # the APK. Use _default_message when detail is empty/None/empty-str.
        detail_str = str(exc.detail) if exc.detail not in (None, "") else ""
        message = detail_str or _default_message(exc.status_code)
        return envelope(
            error=_status_to_error(exc.status_code),
            message=message,
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def _unhandled(  # noqa: ARG001
        request: Request, exc: Exception
    ) -> JSONResponse:
        return envelope(
            error=ERR_INTERNAL,
            message="Bridge hit an unexpected error.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            raw={"exc_type": type(exc).__name__, "exc_str": str(exc) or repr(exc)},
        )


def _status_to_error(code: int) -> str:
    return {
        status.HTTP_401_UNAUTHORIZED: ERR_AUTH_INVALID,
        status.HTTP_403_FORBIDDEN: ERR_AUTH_INVALID,
        status.HTTP_404_NOT_FOUND: ERR_NOT_FOUND,
        status.HTTP_409_CONFLICT: ERR_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_ENTITY: ERR_INVALID_INPUT,
        status.HTTP_503_SERVICE_UNAVAILABLE: ERR_AUTH_NOT_CONFIGURED,
    }.get(code, ERR_INTERNAL)


def _json_safe(value: Any) -> Any:
    """Coerce to a JSON-serialisable representation. Used to scrub pydantic
    error ``ctx`` values that sometimes carry raised exception instances."""
    if isinstance(value, str | int | float | bool | type(None)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return str(value)


def _default_message(code: int) -> str:
    return {
        status.HTTP_401_UNAUTHORIZED: "Authentication required.",
        status.HTTP_403_FORBIDDEN: "Forbidden.",
        status.HTTP_404_NOT_FOUND: "Not found.",
        status.HTTP_409_CONFLICT: "Conflict.",
        status.HTTP_422_UNPROCESSABLE_ENTITY: "Invalid request.",
        status.HTTP_503_SERVICE_UNAVAILABLE: "Service unavailable.",
    }.get(code, "Unexpected error.")
