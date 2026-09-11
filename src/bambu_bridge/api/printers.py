"""Printer CRUD endpoints (contract §3 + §4).

The headline endpoint is :func:`register_printer` — the **three-fork
onboarding contract**: user supplies only ``{host, access_code}`` and the
bridge derives the serial from the leaf cert, then classifies any failure
into E1 (unreachable) / E2 (auth) / E3 (silent) so the APK can render
remediation copy without re-interpreting protocol nouns.

Errors flow through :mod:`bambu_bridge.api.errors` so every response is the
universal envelope shape; never bare ``{"detail": …}``.
"""

from __future__ import annotations

import ipaddress
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bambu_bridge.api import errors
from bambu_bridge.api.auth import require_auth, require_read_or_viz
from bambu_bridge.db.jobs import JobRepo, JobState, PrinterRepo
from bambu_bridge.protocol import discovery
from bambu_bridge.service.printer import PrinterService
from bambu_bridge.service.registry import (
    PrinterExistsError,
    PrinterNotFoundError,
    Registry,
)

router = APIRouter(
    prefix="/printers", tags=["printers"], dependencies=[Depends(require_auth)]
)

# Read-only router: routes here accept EITHER the master key OR the viz token.
# Registered in main.py alongside the main router; routes must not duplicate
# any path already in `router` or FastAPI will use the first match only.
read_router = APIRouter(prefix="/printers", tags=["printers"])


# --------------------------------------------------------------------------- #
# Request / Response models  (contract §3 / §4)
# --------------------------------------------------------------------------- #


class RegisterPrinter(BaseModel):
    """Onboarding request body.

    `extra="forbid"` rejects any unexpected field — most importantly the
    legacy `serial` (contract §3.1: "Sending serial is a 422"). The global
    validation handler reshapes pydantic's 422 into the universal envelope.

    `host` is validated as an IP address to block **SSRF** (Aragorn
    war-council finding): a malicious bearer-holder could otherwise point
    the bridge at cloud metadata (169.254.169.254), the bridge's own loopback,
    or arbitrary external hosts and have it speak TLS+MQTT to them under
    its own identity. We accept private IPv4 (the printer's actual home)
    + IPv6 ULA; reject loopback, link-local, multicast, reserved, unspecified.

    `access_code` is bound to the printer's 8-digit format to prevent
    1MB-body DoS and to refuse obviously-malformed input early.
    """

    model_config = ConfigDict(extra="forbid")

    host: Annotated[str, Field(min_length=1, max_length=64, description="Printer LAN IP")]
    access_code: Annotated[
        str,
        Field(
            min_length=8,
            max_length=8,
            pattern=r"^\d{8}$",
            description="8-digit code from printer Settings ▸ WLAN",
        ),
    ]
    friendly_name: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("host")
    @classmethod
    def _validate_host_is_ip(cls, value: str) -> str:
        """Format check only — confirm it parses as an IP.

        The SSRF policy (block loopback / link-local / multicast / reserved /
        unspecified) lives in the endpoint, gated on
        ``Settings.bridge_allow_loopback_host`` so the in-process test broker
        (which binds 127.0.0.1) can still be registered when the flag is on.
        """
        try:
            ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError(f"host must be an IP address, got {value!r}") from exc
        return value


def _host_blocked_by_ssrf_policy(
    value: str, *, allow_loopback: bool
) -> str | None:
    """Return None if `value` is acceptable, or a stable reason string for the
    envelope context if it's blocked. Aragorn war-council policy:
    block loopback (cloud-metadata pivot), link-local (169.254.x.x), multicast,
    reserved, and unspecified (0.0.0.0). When ``allow_loopback=True`` (test
    mode + bridge dev mode), 127.x.x.x is allowed so the in-process broker is
    reachable but the other unsafe types still are not."""
    ip = ipaddress.ip_address(value)
    if ip.is_loopback and not allow_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_reserved:
        return "reserved"
    if ip.is_unspecified:
        return "unspecified"
    return None


class UpdatePrinter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    friendly_name: str | None = Field(default=None, min_length=1, max_length=64)
    ip: str | None = Field(default=None, min_length=1, max_length=64)
    access_code: str | None = Field(default=None, min_length=8, max_length=8, pattern=r"^[0-9]{8}$")

    @field_validator("ip")
    @classmethod
    def validate_ip(cls, value: str | None) -> str | None:
        return RegisterPrinter._validate_host_is_ip(value) if value is not None else None


def get_registry(request: Request) -> Registry:
    return request.app.state.registry  # type: ignore[no-any-return]


def _printer_repo(request: Request) -> PrinterRepo:
    return PrinterRepo(request.app.state.db)


def _job_repo(request: Request) -> JobRepo:
    return JobRepo(request.app.state.db)


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #


@router.get("")
def list_printers(registry: Registry = Depends(get_registry)) -> list[dict[str, Any]]:
    """List registered printers with a last-known state summary."""
    return [s.summary() for s in registry.list()]


@read_router.get(
    "/{printer_id}",
    dependencies=[Depends(require_read_or_viz)],
)
def get_printer(
    printer_id: str, registry: Registry = Depends(get_registry)
) -> Any:
    """Full current state snapshot.

    Auth: master key OR viz token (bearer header or ``?token=`` query param).
    """
    try:
        svc = registry.get(printer_id)
    except PrinterNotFoundError:
        return errors.not_found("printer", printer_id)
    if (gate := errors.cert_gate(svc)) is not None:
        return gate
    return svc.snapshot()


# --------------------------------------------------------------------------- #
# Onboarding — contract §3 / the three-fork register endpoint
# --------------------------------------------------------------------------- #


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_printer(
    body: RegisterPrinter,
    request: Request,
    registry: Registry = Depends(get_registry),
) -> Any:
    """Discover-then-register. Body: ``{host, access_code, friendly_name?}``.

    Probe order: SSRF policy check → TLS handshake → serial-from-cert →
    MQTT auth + telemetry watchdog. Each fork returns the corresponding
    §3.4 envelope.
    """
    # SSRF policy — outside the pydantic validator so it can read app settings.
    settings = request.app.state.settings
    blocked = _host_blocked_by_ssrf_policy(
        body.host, allow_loopback=settings.bridge_allow_loopback_host
    )
    if blocked is not None:
        return errors.invalid_input(
            f"host {body.host!r} is not allowed: {blocked} addresses are blocked.",
            issues=[{"field": "host", "policy": blocked}],
        )

    # E1 — TLS handshake / cert read
    try:
        cert = await discovery.extract_serial_from_cert(body.host)
    except ConnectionError as exc:
        return errors.unreachable(
            host=body.host,
            port=discovery.DEFAULT_MQTT_PORT,
            raw={"exc_type": type(exc).__name__, "exc_str": str(exc)},
        )

    # 409 — already-registered short-circuit (avoids a pointless MQTT probe)
    try:
        existing = registry.get(cert.serial)
    except PrinterNotFoundError:
        pass
    else:
        return errors.conflict(
            message=f"Printer {cert.serial} is already registered.",
            existing_printer_id=existing.serial,
        )

    # E2 / E3 — MQTT credential probe + post-pushall telemetry watchdog
    probe = await discovery.probe_mqtt_auth(body.host, cert.serial, body.access_code)
    if not probe.ok:
        raw = {"exc_str": probe.detail} if probe.detail else None
        if probe.failure == "mqtt_connack":
            return errors.auth_failed(
                serial=cert.serial,
                model=None,
                connack_code=probe.connack_code,
                raw=raw,
            )
        if probe.failure == "mqtt_no_telemetry":
            return errors.no_telemetry(
                serial=cert.serial,
                model=None,
                wait_ms=int(discovery._TELEMETRY_WATCHDOG_S * 1000),  # noqa: SLF001
                raw=raw,
            )
        # tls_handshake on the MQTT path (rare second-fail; treat as E1).
        return errors.unreachable(
            host=body.host, port=discovery.DEFAULT_MQTT_PORT, raw=raw
        )

    # Success — persist + start the long-lived MQTT task
    try:
        service = await registry.add(
            serial=cert.serial,
            ip=body.host,
            access_code=body.access_code,
            friendly_name=body.friendly_name or cert.serial,
            cert_fingerprint=cert.fingerprint_sha256,
        )
    except PrinterExistsError:  # race with another register call
        existing_svc = registry.get(cert.serial)
        return errors.conflict(
            message=f"Printer {cert.serial} is already registered.",
            existing_printer_id=existing_svc.serial,
        )

    return {
        "printer_id": service.serial,
        "serial": service.serial,
        "model": service.model,
        "friendly_name": service.friendly_name,
        "connected": True,  # probe succeeded; long-lived MQTT reconnects within seconds
        "first_telemetry_at": _iso_now(),
    }


# --------------------------------------------------------------------------- #
# Update / delete
# --------------------------------------------------------------------------- #


@router.patch("/{printer_id}")
async def update_printer(
    printer_id: str,
    body: UpdatePrinter,
    request: Request,
    registry: Registry = Depends(get_registry),
) -> Any:
    try:
        registry.get(printer_id)
    except PrinterNotFoundError:
        return errors.not_found("printer", printer_id)
    if body.ip is not None:
        blocked = _host_blocked_by_ssrf_policy(
            body.ip, allow_loopback=request.app.state.settings.bridge_allow_loopback_host
        )
        if blocked is not None:
            return errors.invalid_input(
                f"ip {body.ip!r} is not allowed: {blocked} addresses are blocked.",
                issues=[{"field": "ip", "policy": blocked}],
            )
    service = await registry.update(
        printer_id,
        friendly_name=body.friendly_name,
        ip=body.ip,
        access_code=body.access_code,
    )
    return service.summary()


@router.delete("/{printer_id}")
async def delete_printer(
    printer_id: str,
    request: Request,
    cascade_jobs: bool = False,
    registry: Registry = Depends(get_registry),
) -> Response:
    """``?cascade_jobs=true`` deletes any non-terminal jobs; default false
    returns 409 with ``active_job_ids`` if any non-terminal job exists."""
    try:
        registry.get(printer_id)
    except PrinterNotFoundError:
        return errors.not_found("printer", printer_id)

    if await request.app.state.jobs.starts.active(printer_id):
        return errors.conflict(message="Resolve the active start before removing this printer")
    if not cascade_jobs:
        repo = _job_repo(request)
        active = [
            j.id
            for j in await repo.list(printer_id=printer_id)
            if j.state not in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELED}
        ]
        if active:
            return errors.conflict(
                message=(
                    f"{len(active)} active job(s) on this printer; "
                    "pass ?cascade_jobs=true to delete them too."
                ),
                active_job_ids=active,
            )

    try:
        await registry.remove(printer_id)
    except PrinterNotFoundError:
        return errors.not_found("printer", printer_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# TOFU re-trust — contract §4.5
# --------------------------------------------------------------------------- #


@router.post("/{printer_id}/trust", status_code=status.HTTP_204_NO_CONTENT)
async def trust_printer(
    printer_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
) -> Response:
    """Re-fetch the leaf cert and store its fingerprint as the new TOFU pin.

    Empty body. Idempotent. Resolves the ``printer_cert_changed`` (403)
    state that fires after a printer firmware update rotates the leaf cert.
    """
    try:
        svc = registry.get(printer_id)
    except PrinterNotFoundError:
        return errors.not_found("printer", printer_id)
    try:
        cert = await discovery.extract_serial_from_cert(svc.ip)
    except ConnectionError as exc:
        return errors.unreachable(
            host=svc.ip,
            port=discovery.DEFAULT_MQTT_PORT,
            raw={"exc_type": type(exc).__name__, "exc_str": str(exc)},
        )
    if cert.serial != printer_id:
        # Different printer answering at this IP — refuse the re-trust.
        return errors.conflict(
            message=(
                f"Host {svc.ip} now answers as serial {cert.serial}, "
                f"not {printer_id}. Re-register instead."
            ),
            discovered_serial=cert.serial,
        )
    await _printer_repo(request).update_cert_fingerprint(
        printer_id, cert.fingerprint_sha256
    )
    # PR A.2: also update the live service's expected pin + cert_status so
    # the gate clears immediately — without this the operator would have to
    # wait for the next MQTT reconnect for `cert_status` to flip back to
    # "trusted" and reads/control would stay 403 in the meantime.
    svc.expected_fingerprint = cert.fingerprint_sha256
    svc.current_fingerprint = cert.fingerprint_sha256
    svc.cert_status = "trusted"
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _iso_now() -> str:
    """ISO-8601 UTC with millisecond precision (contract §2 time rule)."""
    from datetime import UTC, datetime

    return (
        datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _lookup(registry: Registry, printer_id: str) -> PrinterService:
    """Compat shim still imported by other api modules (control/jobs/etc.)."""
    return registry.get(printer_id)
