"""Pre-flight discovery against a Bambu P1S.

Two helpers used by the API edge (``api/printers.py``) so that
``POST /printers`` only needs ``{host, access_code}`` — the serial is
derived from the printer's TLS leaf cert (REPORT §1 / OpenBambuAPI
``tls.md``), and we classify auth failures into actionable categories
*before* persisting anything.

The three-fork failure taxonomy (per `docs/API-CONTRACT.md` §3.4 / the
APK's E1/E2/E3 onboarding screens):

* ``tls_handshake`` (E1) — TCP/TLS to :8883 failed. Wrong IP, router
  blocking, printer's network stack is down.
* ``mqtt_connack`` (E2) — TLS OK, MQTT CONNECT rejected (most commonly
  ``CONNACK 5`` *not authorized*). Wrong access code, OR LAN-Only Mode
  is off — the wire can't disambiguate these for us.
* ``mqtt_no_telemetry`` (E3) — CONNECT succeeded (CONNACK 0) but no
  report arrived after ``pushall`` within the watchdog window. Likely
  Developer Mode off; could also be a transient LAN drop. Don't
  over-claim in the user-facing copy.

This module deliberately raises typed results / typed exceptions — the
API layer translates them into the universal error envelope.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from dataclasses import dataclass
from typing import Literal

import aiomqtt
import structlog

from bambu_bridge.protocol.models import pushall_request
from bambu_bridge.protocol.tls import insecure_tls_context, leaf_cert_fingerprint

log = structlog.get_logger(__name__)

DEFAULT_MQTT_PORT = 8883
_TLS_PROBE_TIMEOUT_S = 8.0
_MQTT_CONNECT_TIMEOUT_S = 10.0
_TELEMETRY_WATCHDOG_S = 5.0

# Stable phase strings — these end up under `context.phase` in the API
# envelope (contract §2). Keep in sync with the contract.
DiscoveryPhase = Literal[
    "tls_handshake",
    "mqtt_connack",
    "mqtt_no_telemetry",
]


@dataclass(frozen=True)
class CertProbe:
    """Result of the TLS leaf-cert read at :8883."""

    serial: str  # CN of the leaf cert (REPORT §1)
    raw_subject: str  # full subject for diagnostics / _raw
    fingerprint_sha256: str  # for TOFU storage (contract §3.6)


@dataclass(frozen=True)
class AuthProbe:
    """Result of the MQTT credential probe.

    `failure` is the contract-stable phase string; `connack_code` is
    populated on E2 so the envelope can include it in ``context``.
    """

    ok: bool
    failure: DiscoveryPhase | None = None
    detail: str = ""  # short, stable, safe to surface; never a stack trace
    connack_code: int | None = None


async def extract_serial_from_cert(
    host: str,
    *,
    port: int = DEFAULT_MQTT_PORT,
    timeout: float = _TLS_PROBE_TIMEOUT_S,
) -> CertProbe:
    """Read the printer's leaf cert and return its serial + TOFU fingerprint.

    Thin wrapper over :func:`bambu_bridge.protocol.tls.leaf_cert_fingerprint`
    that derives the serial from the subject CN (REPORT.md §1) and reshapes
    into the discovery contract. PR A.2 lifted the actual TLS handshake to
    :mod:`bambu_bridge.protocol.tls` so the three protocol clients and this
    probe share one code path.
    """
    cert = await leaf_cert_fingerprint(host, port, timeout=timeout)
    cn = cert.common_name
    if not cn:
        raise ConnectionError(
            f"tls_handshake: leaf cert has no CN (subject={cert.subject_rfc4514!r})"
        )
    return CertProbe(
        serial=cn,
        raw_subject=cert.subject_rfc4514,
        fingerprint_sha256=cert.fingerprint_sha256,
    )


async def probe_mqtt_auth(
    host: str,
    serial: str,
    access_code: str,
    *,
    port: int = DEFAULT_MQTT_PORT,
    connect_timeout: float = _MQTT_CONNECT_TIMEOUT_S,
    telemetry_timeout: float = _TELEMETRY_WATCHDOG_S,
) -> AuthProbe:
    """Probe MQTT auth + telemetry. Distinguishes E2 (auth) from E3 (silent).

    Connects, subscribes to ``device/<serial>/report``, publishes
    ``pushall`` (QoS 0 — gotcha #9), and waits up to ``telemetry_timeout``
    for ANY message on the report topic. Outcomes:

    * `MqttCodeError` (CONNACK 5 etc.) → ``failure="mqtt_connack"`` (E2)
    * Other MqttError / TLS / timeout → ``failure="tls_handshake"`` (E1)
    * Connected, no report within window → ``failure="mqtt_no_telemetry"`` (E3)
    * Connected, report arrived → ``ok=True``

    No state is left on the printer — the probe disconnects cleanly.
    """
    request_topic = f"device/{serial}/request"
    report_topic = f"device/{serial}/report"

    try:
        async with aiomqtt.Client(
            hostname=host,
            port=port,
            username="bblp",
            password=access_code,
            identifier=f"bambu-bridge-probe-{serial[:8]}",
            tls_context=insecure_tls_context(),
            keepalive=30,
            clean_session=True,
            timeout=connect_timeout,
        ) as client:
            await client.subscribe(report_topic, qos=0)
            await client.publish(request_topic, json.dumps(pushall_request()), qos=0)
            try:
                async with asyncio.timeout(telemetry_timeout):
                    async for _msg in client.messages:
                        return AuthProbe(ok=True)
            except TimeoutError:
                return AuthProbe(
                    ok=False,
                    failure="mqtt_no_telemetry",
                    detail=(
                        f"no report within {telemetry_timeout:.0f}s of pushall — "
                        "Developer Mode off, or transient LAN drop"
                    ),
                )
    except aiomqtt.MqttCodeError as exc:
        # CONNACK non-zero. 5=not authorised is by far the common case.
        # exc.rc is int|ReasonCode; ReasonCode has .value, ints coerce.
        rc_value = getattr(exc.rc, "value", exc.rc)
        connack_code = int(rc_value) if isinstance(rc_value, int) else None
        return AuthProbe(
            ok=False,
            failure="mqtt_connack",
            detail=f"MQTT CONNACK rc={connack_code} — wrong access code or LAN-Only Mode off",
            connack_code=connack_code,
        )
    except (aiomqtt.MqttError, TimeoutError, OSError, ssl.SSLError) as exc:
        return AuthProbe(
            ok=False,
            failure="tls_handshake",
            detail=f"{type(exc).__name__}: {exc}",
        )
    # Unreachable: the inner block returned or one of the except branches
    # handled the exit. mypy needs a fallback.
    return AuthProbe(ok=False, failure="tls_handshake", detail="unknown")
