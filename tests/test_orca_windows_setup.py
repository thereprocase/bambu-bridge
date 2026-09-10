"""Run the shipped setup command against disposable Windows profiles and TLS."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from bambu_bridge.pairing import identity


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell setup integration")
def test_windows_setup_preserves_profiles_and_certificate_bundle(tmp_path):
    serial = "SETUPFIXTURE001"
    key, cert, _ = identity(tmp_path / "tls", common_name=serial, key_kind="rsa", host="127.0.0.1")
    _, other, _ = identity(tmp_path / "other-ca")
    original_certificates = other.read_bytes()
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                with connection, context.wrap_socket(connection, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    data = {
        "host": "127.0.0.1",
        "serial": serial,
        "code": "FIXTURE1",
        "certificate": base64.b64encode(der).decode(),
        "fingerprint": hashlib.sha256(der).hexdigest().upper(),
        "mqtt_port": listener.getsockname()[1],
        "camera_port": listener.getsockname()[1],
    }
    template = (
        Path(__file__).parents[1] / "src/bambu_bridge/templates/orca-windows.ps1"
    ).read_text()
    script = template.replace(
        "__BRIDGE_SETUP_DATA__", base64.b64encode(json.dumps(data).encode()).decode()
    )
    try:
        for existing in [False, True]:
            case = tmp_path / str(existing)
            profile = case / "appdata/OrcaSlicer"
            profile.mkdir(parents=True)
            directory = case / "programs/OrcaSlicer/resources/cert"
            directory.mkdir(parents=True)
            bundle = directory / "printer.cer"
            bundle.write_bytes(original_certificates)
            original = {
                "app": {"unicode": "café", "nested": [True, False, 16777216]},
                "local_machines": {"OTHERFIXTURE001": {"dev_ip": "192.0.2.2", "dev_name": "Other"}},
                "user_access_code": {"OTHERFIXTURE001": "OTHER123"},
            }
            if existing:
                original["local_machines"][serial] = {"dev_ip": "192.0.2.33", "extra": "preserve"}
            body = json.dumps(original, ensure_ascii=False, indent="\t")
            before = (
                body + "\n# MD5 checksum " + hashlib.md5(body.encode()).hexdigest().upper() + "\n"
            ).encode()
            config = profile / "OrcaSlicer.conf"
            config.write_bytes(before)
            env = {
                **os.environ,
                "APPDATA": str(case / "appdata"),
                "PROGRAMFILES": str(case / "programs"),
            }
            wrong_der = ssl.PEM_cert_to_DER_cert(other.read_text())
            wrong = {
                **data,
                "certificate": base64.b64encode(wrong_der).decode(),
                "fingerprint": hashlib.sha256(wrong_der).hexdigest().upper(),
            }
            untrusted = template.replace(
                "__BRIDGE_SETUP_DATA__", base64.b64encode(json.dumps(wrong).encode()).decode()
            )
            rejected = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "function Get-Process { }\n" + untrusted,
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert "1/3 Checking" in rejected.stdout and "Setup stopped:" in rejected.stdout
            assert "2/3" not in rejected.stdout
            assert config.read_bytes() == before and bundle.read_bytes() == original_certificates
            for _ in range(2):
                result = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        "function Get-Process { }\n" + script,
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
                assert result.returncode == 0 and "Ready! Open Orca" in result.stdout, (
                    result.stdout + result.stderr
                )
                updated = config.read_text(encoding="utf-8")
                raw = updated[: updated.rfind("}") + 1]
                parsed = json.loads(raw)
                assert parsed["app"] == original["app"]
                assert (
                    parsed["local_machines"]["OTHERFIXTURE001"]
                    == original["local_machines"]["OTHERFIXTURE001"]
                )
                assert parsed["user_access_code"]["OTHERFIXTURE001"] == "OTHER123"
                assert parsed["local_machines"][serial]["dev_ip"] == "127.0.0.1"
                assert (
                    parsed["access_code"][serial]
                    == parsed["user_access_code"][serial]
                    == "FIXTURE1"
                )
                if existing:
                    assert parsed["local_machines"][serial]["extra"] == "preserve"
                assert (
                    updated.split("# MD5 checksum ")[1].strip()
                    == hashlib.md5(raw.encode()).hexdigest().upper()
                )
            assert any(p.read_bytes() == before for p in profile.glob("*.bak"))
            assert bundle.read_bytes().startswith(original_certificates)
            assert len(list(directory.glob("*.bak"))) == 1
            assert not list(case.rglob("*.tmp"))
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=3)
