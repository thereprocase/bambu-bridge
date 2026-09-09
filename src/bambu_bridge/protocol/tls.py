"""Unified TLS surface for the Bambu P1S (gotchas #7 / #8).

The P1S speaks TLS on three different ports — MQTT 8883, FTPS 990, and the
chamber camera on 6000 — and every one of them is the same flavour of TLS:

* self-signed cert that **rotates on firmware update** → CERT_NONE, never pin
  the public key (gotcha #7);
* server only speaks **TLS 1.2** → cap *and* floor the context at 1.2 so the
  first ClientHello is the version the printer can answer (gotcha #8).
  ``PROTOCOL_TLS_CLIENT`` defaults to offering 1.3 first; on the P1S that
  draws a ``handshake_failure`` alert and the connect hangs to the socket
  timeout (REPORT.md §1; verified on hardware 2026-05-19).

Before PR A.2 each protocol module re-implemented the same six-line context
helper — drifting was the only thing keeping me up at night. This module
centralises it; the three protocol clients (and the discovery probe) now
import :func:`insecure_tls_context` and inherit any future tweak for free.

Also exposes :func:`leaf_cert_fingerprint` — a thin async wrapper around the
same handshake that returns the SHA-256 of the DER-encoded leaf cert, used
by :mod:`bambu_bridge.service.printer` for TOFU compare-on-connect (contract
§4.5; war-council PR A.2 finding).
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
import ssl
from dataclasses import dataclass, field

from cryptography import x509
from cryptography.x509.oid import NameOID

_TLS_PROBE_TIMEOUT_S = 8.0


def insecure_tls_context() -> ssl.SSLContext:
    """TLS context tuned for the P1S's rotating self-signed cert.

    Order matters: ``check_hostname`` must be cleared *before* setting
    ``verify_mode`` to ``CERT_NONE`` (the stdlib raises otherwise — gotcha
    #7). Both bounds pinned to TLS 1.2 (gotcha #8) so the ClientHello is one
    the printer accepts; this is the context-level equivalent of
    ``mosquitto --tls-version tlsv1.2``.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


@dataclass(frozen=True)
class LeafCert:
    """Result of a leaf-cert read.

    ``subject_rfc4514`` is the cert subject in RFC-4514 form, e.g.
    ``"CN=00M00A000000000"`` — handy for diagnostics. ``common_name``
    is parsed out of the DER ourselves because ``ssl.SSLSocket.getpeercert()``
    returns ``{}`` whenever the context is in ``CERT_NONE`` mode (and we
    must use ``CERT_NONE`` for the P1S — gotcha #7). Python 3.12 changed
    behaviour here; the old discovery path lifted the dict, found nothing,
    and would have produced ``"leaf cert has no CN"`` on every probe. With
    DER-side parsing the helper is correct regardless of validation mode.
    """

    der: bytes  # raw DER for fingerprinting
    fingerprint_sha256: str  # lowercase hex sha256 of the DER cert
    common_name: str | None  # first CN in the subject, or None
    subject_rfc4514: str = ""  # diagnostic form of the full subject
    # Kept for callers that already pattern-matched on the legacy
    # ``subject`` shape — empty tuple under CERT_NONE so they degrade
    # gracefully rather than KeyError. Don't use this for new code.
    subject: tuple[tuple[tuple[str, str], ...], ...] = field(default_factory=tuple)


async def leaf_cert_fingerprint(
    host: str, port: int, *, timeout: float = _TLS_PROBE_TIMEOUT_S
) -> LeafCert:
    """Open a one-shot TLS handshake and read the printer's leaf cert.

    Raises :class:`ConnectionError` whose message starts with the stable
    ``tls_handshake:`` phase string (so the API layer can map to E1 the same
    way :func:`bambu_bridge.protocol.discovery.extract_serial_from_cert`
    does). Runs the blocking socket work in a worker thread.
    """
    try:
        der = await asyncio.to_thread(_sync_probe, host, port, timeout)
    except (OSError, ssl.SSLError, TimeoutError) as exc:
        raise ConnectionError(f"tls_handshake: {type(exc).__name__}: {exc}") from exc

    cn: str | None = None
    subject_str = ""
    try:
        cert = x509.load_der_x509_certificate(der)
        attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if attrs:
            cn = str(attrs[0].value)
        subject_str = cert.subject.rfc4514_string()
    except Exception:  # noqa: BLE001 — diagnostic-only fallback; never break the probe
        pass

    return LeafCert(
        der=der,
        fingerprint_sha256=hashlib.sha256(der).hexdigest(),
        common_name=cn,
        subject_rfc4514=subject_str,
    )


def _sync_probe(host: str, port: int, timeout: float) -> bytes:
    """Synchronous TLS handshake; returns the raw DER leaf-cert bytes.

    ``ssl.SSLSocket.getpeercert(binary_form=True)`` returns the cert even
    when the context is in ``CERT_NONE`` mode (unlike the parsed-dict form,
    which is ``{}``). Parsing the DER for CN/subject is the caller's job.
    """
    ctx = insecure_tls_context()
    with (
        socket.create_connection((host, port), timeout=timeout) as raw,
        ctx.wrap_socket(raw, server_hostname=host) as wrapped,
    ):
        wrapped.settimeout(timeout)
        der = wrapped.getpeercert(binary_form=True)
    if der is None:  # CERT_NONE-and-no-cert is extremely unusual; treat as E1
        raise ConnectionError("tls_handshake: server presented no certificate")
    return der
