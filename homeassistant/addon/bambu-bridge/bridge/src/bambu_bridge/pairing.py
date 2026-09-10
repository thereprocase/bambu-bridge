"""Local, out-of-band pairing. Secrets are hashed at rest; identity never auto-resets.

The QR must be obtained from the bridge host's trusted installer/console, not
from an unauthenticated HTTP page. It authenticates the TLS public key before
the one-use invitation is sent. Each phone gets a separately revocable token.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def validate_base(value: str) -> str:
    u = urlsplit(value)
    if (
        u.scheme != "https"
        or not u.hostname
        or u.username
        or u.password
        or u.query
        or u.fragment
        or u.path.rstrip("/") != "/api/v1"
    ):
        raise ValueError("Use an HTTPS base URL ending in /api/v1")
    _ = u.port  # validates the port
    return value.rstrip("/")


class PairingStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise ValueError("Pairing directory must not be a symlink")
        self.directory.chmod(0o700)
        self.path = self.directory / "devices.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Pairing database must not be a symlink")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS invitations (
                    hash TEXT PRIMARY KEY, expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, hash TEXT UNIQUE NOT NULL,
                    created INTEGER NOT NULL, revoked INTEGER);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def invite(self, ttl: int = 600) -> tuple[str, int]:
        if not 30 <= ttl <= 600:
            raise ValueError("Pairing invitations last 30 to 600 seconds")
        secret = secrets.token_urlsafe(32)
        now = int(time.time())
        with self.connect() as db:
            db.execute("DELETE FROM invitations WHERE expires <= ?", (now,))
            db.execute("INSERT INTO invitations VALUES (?, ?)", (digest(secret), now + ttl))
        return secret, now + ttl

    def claim(self, secret: str, name: str) -> dict[str, Any] | None:
        if not 32 <= len(secret) <= 128:
            return None
        token = "bbd_" + secrets.token_urlsafe(32)
        device_id = secrets.token_hex(12)
        now = int(time.time())
        with self.connect() as db:
            # A single transaction consumes the invitation and adds the device.
            # Concurrent claims cannot both succeed.
            used = db.execute(
                "DELETE FROM invitations WHERE hash = ? AND expires > ?", (digest(secret), now)
            ).rowcount
            if used != 1:
                return None
            db.execute(
                "INSERT INTO devices VALUES (?, ?, ?, ?, NULL)",
                (device_id, name, digest(token), now),
            )
        return {"device_id": device_id, "token": token, "name": name}

    def authenticate(self, token: str | None) -> str | None:
        if not token or not token.startswith("bbd_") or len(token) > 128:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM devices WHERE hash = ? AND revoked IS NULL", (digest(token),)
            ).fetchone()
        return str(row[0]) if row else None

    def invitation_active(self, invitation_id: str) -> bool:
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM invitations WHERE hash = ? AND expires > ?",
                (invitation_id, int(time.time())),
            ).fetchone() is not None

    def cancel_invitation(self, invitation_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM invitations WHERE hash = ?", (invitation_id,))

    def devices(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id, name, created, revoked FROM devices ORDER BY created"
                )
            ]

    def revoke(self, device_id: str) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    "UPDATE devices SET revoked = ? WHERE id = ? AND revoked IS NULL",
                    (int(time.time()), device_id),
                ).rowcount
                == 1
            )


def identity(directory: Path, *, common_name: str = "Bambu Bridge local") -> tuple[Path, Path, str]:
    """Persist a P-256 TLS identity. Renew the certificate with the SAME key.

    The app trusts the scanned SPKI, not a DNS/CA claim. Its native transport
    additionally restricts this trust to the explicitly paired HTTPS origin.
    """
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    key_path, cert_path = directory / "identity.key", directory / "identity.crt"
    if key_path.is_symlink() or cert_path.is_symlink():
        raise ValueError("Identity files must not be symlinks")
    if not key_path.exists():
        if cert_path.exists():
            raise ValueError("Identity key missing; restore backup before starting")
        key = ec.generate_private_key(ec.SECP256R1())
        data = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        temporary = directory / ("key-" + secrets.token_hex(8) + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            with suppress(FileExistsError):
                os.link(temporary, key_path)  # publish only a fully written identity
        finally:
            temporary.unlink()
    loaded_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(loaded_key, ec.EllipticCurvePrivateKey):
        raise ValueError("Unexpected identity key type")
    key = loaded_key
    public = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    pin = base64.b64encode(hashlib.sha256(public).digest()).decode()
    renew = not cert_path.exists()
    if not renew:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        if (
            cert.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            != public
        ):
            raise ValueError("Certificate and identity key do not match")
        renew = cert.not_valid_after_utc < datetime.now(UTC) + timedelta(days=30)
        renew |= cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value != common_name
    if renew:
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(minutes=5))
            .not_valid_after(datetime.now(UTC) + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        temporary = cert_path.with_name("certificate-" + secrets.token_hex(8) + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(cert.public_bytes(serialization.Encoding.PEM))
        os.replace(temporary, cert_path)
    key_path.chmod(0o600)
    return key_path, cert_path, pin


def invitation_payload(store: PairingStore, base: str) -> str:
    base = validate_base(base)
    _, _, pin = identity(store.directory)
    secret, expires = store.invite()
    return json.dumps(
        {
            "type": "bambu-bridge-pair",
            "version": 1,
            "base_url": base,
            "spki": pin,
            "secret": secret,
            "expires": expires,
        },
        separators=(",", ":"),
    )
