"""Owner-requested Orca setup material; never include the bridge owner key."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.hazmat.primitives import serialization

if TYPE_CHECKING:
    from bambu_bridge.native_gateway import NativeGateway


def setup_material(gateway: NativeGateway) -> dict[str, str]:
    if not gateway.servers:
        raise ValueError("Turn on native P1S access first.")
    code = gateway.saved_code()
    if not code:
        raise ValueError("Save your existing native code below before preparing this computer.")
    serial = gateway.service().serial
    if len(serial) != 15 or not serial.isascii() or not serial.isalnum():
        raise ValueError("Orca setup needs the printer's 15-character serial number.")
    pem = (gateway.store.directory / "native-rsa" / "identity.crt").read_text()
    certificate = x509.load_pem_x509_certificate(pem.encode())
    der = certificate.public_bytes(serialization.Encoding.DER)
    fingerprint = hashlib.sha256(der).hexdigest().upper()
    payload = {
        "host": gateway.host,
        "serial": serial,
        "code": code,
        "certificate": base64.b64encode(der).decode(),
        "fingerprint": fingerprint,
        "mqtt_port": gateway.servers[0].sockets[0].getsockname()[1],
        "camera_port": gateway.servers[2].sockets[0].getsockname()[1],
    }
    encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    template = (Path(__file__).parent / "templates" / "orca-windows.ps1").read_text()
    return {
        "script": template.replace("__BRIDGE_SETUP_DATA__", encoded),
        "certificate_pem": pem,
        "certificate_sha256": fingerprint,
    }
