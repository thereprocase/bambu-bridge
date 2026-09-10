"""PR A.2 — unified `protocol.tls` module.

Before A.2 each of mqtt.py / ftps.py / camera.py / discovery.py shipped its
own copy of the same six-line ``_insecure_tls_context`` helper; the three
were verified by separate tests that asserted the same four invariants
(TLSv1.2 floor + ceiling, CERT_NONE, check_hostname=False). Those tests
still exist and now exercise the lifted function; this file adds the
fingerprint helper test that didn't have a home before.
"""

from __future__ import annotations

import asyncio
import hashlib
import ssl

import pytest

from bambu_bridge.protocol import tls as tls_mod


def test_insecure_tls_context_is_pinned_to_tls_1_2() -> None:
    """One assertion for the four invariants every protocol module relied on.

    Drift here breaks all three TLS-speaking clients at once; a single
    regression test prevents the silent class of "context drifted on one
    client only" bugs the duplicated helpers used to allow.
    """
    ctx = tls_mod.insecure_tls_context()
    assert ctx.minimum_version is ssl.TLSVersion.TLSv1_2
    assert ctx.maximum_version is ssl.TLSVersion.TLSv1_2
    assert ctx.verify_mode is ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_leaf_cert_common_name_dataclass_field() -> None:
    """``common_name`` is now its own field on :class:`LeafCert` — the helper
    parses the DER directly because ``getpeercert()`` returns ``{}`` whenever
    the SSL context is in ``CERT_NONE`` mode (Python 3.12 made that explicit;
    the old subject-tuple path would have returned None for every P1S probe)."""
    cert = tls_mod.LeafCert(
        der=b"\x00\x01\x02",
        fingerprint_sha256="abc",
        common_name="00M00A000000000",
        subject_rfc4514="CN=00M00A000000000",
    )
    assert cert.common_name == "00M00A000000000"


def test_leaf_cert_no_cn_returns_none() -> None:
    """No CN means no serial → caller raises tls_handshake (E1)."""
    cert = tls_mod.LeafCert(
        der=b"",
        fingerprint_sha256="abc",
        common_name=None,
        subject_rfc4514="O=Bambu Lab",
    )
    assert cert.common_name is None


@pytest.mark.asyncio
async def test_leaf_cert_fingerprint_unreachable_host_raises_tls_handshake() -> None:
    """Connection failures must surface as a ``tls_handshake:``-prefixed
    ConnectionError so the API layer can map to E1 the same way
    ``discovery.extract_serial_from_cert`` does."""
    # 127.0.0.1:1 is closed on a unix box; quick TCP refusal, no DNS hop.
    with pytest.raises(ConnectionError, match=r"^tls_handshake:"):
        await tls_mod.leaf_cert_fingerprint("127.0.0.1", 1, timeout=2.0)


@pytest.mark.asyncio
async def test_leaf_cert_fingerprint_against_self_signed_server() -> None:
    """Round-trip the fingerprint against a tiny ad-hoc TLS server using a
    real self-signed cert generated in-test. Verifies the helper actually
    reads the DER cert + hashes it consistently with what other tools see."""
    pytest.importorskip("cryptography")
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "00M00A000000000")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    der = cert.public_bytes(serialization.Encoding.DER)
    expected_fp = hashlib.sha256(der).hexdigest()

    import pathlib
    import tempfile
    tmp = pathlib.Path(tempfile.mkdtemp())
    cert_pem = tmp / "cert.pem"
    key_pem = tmp / "key.pem"
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    server_ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    server_ctx.load_cert_chain(certfile=str(cert_pem), keyfile=str(key_pem))

    server = await asyncio.start_server(
        lambda r, w: w.close(), "127.0.0.1", 0, ssl=server_ctx
    )
    port = server.sockets[0].getsockname()[1]
    try:
        async with server:
            probed = await tls_mod.leaf_cert_fingerprint("127.0.0.1", port, timeout=5.0)
    finally:
        server.close()
        await server.wait_closed()

    assert probed.fingerprint_sha256 == expected_fp
    assert probed.common_name == "00M00A000000000"
