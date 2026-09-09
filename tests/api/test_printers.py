"""M3 / Contract §3: printer CRUD + the three-fork onboarding contract.

The endpoint is rewritten in PR A: body is ``{host, access_code, friendly_name?}``;
serial is derived from the leaf cert; failures classify into E1/E2/E3.

Tests patch :mod:`bambu_bridge.protocol.discovery` to deterministically
exercise each fork without standing up a real TLS+MQTT printer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bambu_bridge.protocol import discovery
from tests.conftest import (
    ACCESS_CODE,
    API_KEY,
    SERIAL,
    build_app,
    patch_discovery_ok,
)

_AUTH = {"Authorization": f"Bearer {API_KEY}"}


def _post_register(c: TestClient, **overrides: Any) -> Any:
    body = {"host": "192.168.1.50", "access_code": ACCESS_CODE, "friendly_name": "Workshop P1S"}
    body.update(overrides)
    return c.post("/api/v1/printers", headers=_AUTH, json=body)


# --------------------------------------------------------------------------- #
# Happy-path CRUD (discovery mocked OK)
# --------------------------------------------------------------------------- #


def test_register_list_get_patch_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    with TestClient(build_app(tmp_path / "crud.db")) as c:
        r = _post_register(c)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["serial"] == SERIAL
        assert body["printer_id"] == SERIAL
        assert body["connected"] is True
        assert body["friendly_name"] == "Workshop P1S"
        assert "first_telemetry_at" in body

        listing = c.get("/api/v1/printers", headers=_AUTH).json()
        assert [p["serial"] for p in listing] == [SERIAL]
        # Contract §4.1: every summary MUST carry printer_id == serial so that
        # the HA coordinator (and any other consumer) can key entries without
        # sniffing the 'serial' field separately.
        assert [p["printer_id"] for p in listing] == [SERIAL]

        snap = c.get(f"/api/v1/printers/{SERIAL}", headers=_AUTH).json()
        assert snap["serial"] == SERIAL
        # PR B: snapshot is the translated §6 shape; pre-telemetry state
        # shows up as an empty `_raw` block with `phase: unknown`.
        assert snap["_raw"] == {}
        assert snap["phase"] == "unknown"

        patched = c.patch(
            f"/api/v1/printers/{SERIAL}", headers=_AUTH, json={"friendly_name": "Renamed"}
        )
        assert patched.status_code == 200
        assert patched.json()["friendly_name"] == "Renamed"

        assert c.delete(f"/api/v1/printers/{SERIAL}", headers=_AUTH).status_code == 204
        assert c.get(f"/api/v1/printers/{SERIAL}", headers=_AUTH).status_code == 404


def test_friendly_name_defaults_to_serial_when_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    with TestClient(build_app(tmp_path / "fn.db")) as c:
        r = c.post(
            "/api/v1/printers",
            headers=_AUTH,
            json={"host": "192.168.1.50", "access_code": ACCESS_CODE},
        )
        assert r.status_code == 201
        assert r.json()["friendly_name"] == SERIAL


# --------------------------------------------------------------------------- #
# Three-fork contract (§3.4): E1 / E2 / E3
# --------------------------------------------------------------------------- #


def test_e1_tls_handshake_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(host: str, *, port: int = 8883, timeout: float = 0) -> Any:  # noqa: ARG001
        raise ConnectionError("tls_handshake: ConnectionRefusedError: refused")

    monkeypatch.setattr(discovery, "extract_serial_from_cert", _boom)
    with TestClient(build_app(tmp_path / "e1.db")) as c:
        r = _post_register(c, host="192.168.1.99")
        assert r.status_code == 502, r.text
        body = r.json()
        assert body["error"] == "printer_unreachable"
        assert body["context"]["phase"] == "tls_handshake"
        assert body["context"]["host"] == "192.168.1.99"
        assert "remediation_hint" in body


def test_e2_auth_failed_returns_discovered_serial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _cert(host: str, *, port: int = 8883, timeout: float = 0) -> Any:  # noqa: ARG001
        return discovery.CertProbe(
            serial=SERIAL, raw_subject=f"CN={SERIAL}", fingerprint_sha256="aa"
        )

    async def _probe(host: str, serial: str, access_code: str, **_kw: Any) -> Any:  # noqa: ARG001
        return discovery.AuthProbe(ok=False, failure="mqtt_connack", connack_code=5, detail="rc=5")

    monkeypatch.setattr(discovery, "extract_serial_from_cert", _cert)
    monkeypatch.setattr(discovery, "probe_mqtt_auth", _probe)
    with TestClient(build_app(tmp_path / "e2.db")) as c:
        r = _post_register(c)
        # Contract §3.4 + war-council Frodo: printer auth failure is 403, not
        # 401 (401 is the bridge's own bearer-key failure; collision avoided).
        assert r.status_code == 403, r.text
        body = r.json()
        assert body["error"] == "printer_auth_failed"
        assert body["likely_cause"] == "wrong_access_code_or_lan_mode_off"
        assert body["context"]["phase"] == "mqtt_connack"
        assert body["context"]["connack_code"] == 5
        # discovered.serial is the UX win — APK shows "We found your printer (…)"
        assert body["discovered"]["serial"] == SERIAL


def test_e3_no_telemetry_returns_discovered_serial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _cert(host: str, *, port: int = 8883, timeout: float = 0) -> Any:  # noqa: ARG001
        return discovery.CertProbe(
            serial=SERIAL, raw_subject=f"CN={SERIAL}", fingerprint_sha256="bb"
        )

    async def _probe(host: str, serial: str, access_code: str, **_kw: Any) -> Any:  # noqa: ARG001
        return discovery.AuthProbe(ok=False, failure="mqtt_no_telemetry", detail="no report in 5s")

    monkeypatch.setattr(discovery, "extract_serial_from_cert", _cert)
    monkeypatch.setattr(discovery, "probe_mqtt_auth", _probe)
    with TestClient(build_app(tmp_path / "e3.db")) as c:
        r = _post_register(c)
        assert r.status_code == 502, r.text
        body = r.json()
        assert body["error"] == "mqtt_no_telemetry"
        assert body["likely_cause"] == "developer_mode_off_or_lan_drop"
        assert body["context"]["phase"] == "mqtt_no_telemetry"
        assert body["context"]["post_pushall_wait_ms"] == 5000
        assert body["discovered"]["serial"] == SERIAL


# --------------------------------------------------------------------------- #
# §3.5 — other failures
# --------------------------------------------------------------------------- #


def test_duplicate_register_returns_409_with_existing_printer_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    with TestClient(build_app(tmp_path / "dup.db")) as c:
        assert _post_register(c).status_code == 201
        r = _post_register(c, host="10.0.0.1", friendly_name="again")
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["error"] == "conflict"
        assert body["existing_printer_id"] == SERIAL


def test_sending_serial_field_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    with TestClient(build_app(tmp_path / "extra.db")) as c:
        r = c.post(
            "/api/v1/printers",
            headers=_AUTH,
            json={"host": "1.2.3.4", "access_code": ACCESS_CODE, "serial": SERIAL},
        )
        assert r.status_code == 422
        assert r.json()["error"] == "invalid_input"


def test_unknown_printer_is_404(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "miss.db")) as c:
        assert c.get("/api/v1/printers/nope", headers=_AUTH).status_code == 404
        assert c.delete("/api/v1/printers/nope", headers=_AUTH).status_code == 404
        assert (
            c.patch(
                "/api/v1/printers/nope", headers=_AUTH, json={"ip": "1.2.3.4"}
            ).status_code
            == 404
        )


def test_blank_host_and_blank_access_code_rejected(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "blank.db")) as c:
        r = c.post(
            "/api/v1/printers",
            headers=_AUTH,
            json={"host": "", "access_code": ""},
        )
        assert r.status_code == 422
        assert r.json()["error"] == "invalid_input"


def test_non_ip_host_rejected(tmp_path: Path) -> None:
    """Pydantic field validator: host must parse as an IP address."""
    with TestClient(build_app(tmp_path / "host_dns.db")) as c:
        r = c.post(
            "/api/v1/printers",
            headers=_AUTH,
            json={"host": "printer.local", "access_code": "12345678"},
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"] == "invalid_input"


def test_access_code_must_be_8_digits(tmp_path: Path) -> None:
    """Aragorn war-council: format-constrain access_code to block 1MB-body
    DoS and obviously-malformed input."""
    with TestClient(build_app(tmp_path / "accode.db")) as c:
        for bad in ("abcd1234", "1234567", "123456789", "12345 78"):
            r = c.post(
                "/api/v1/printers",
                headers=_AUTH,
                json={"host": "192.168.1.50", "access_code": bad},
            )
            assert r.status_code == 422, f"accepted {bad!r}"


def test_ssrf_loopback_blocked_when_setting_off(tmp_path: Path) -> None:
    """Aragorn war-council SSRF policy: loopback rejected unless
    `bridge_allow_loopback_host` is on (off in production)."""
    from bambu_bridge.config import Settings
    from bambu_bridge.main import create_app

    settings = Settings(
        bridge_api_key=API_KEY,
        bridge_db_path=str(tmp_path / "ssrf.db"),
        bridge_log_level="warning",
        bridge_log_format="console",
        bridge_allow_loopback_host=False,  # production default
    )
    with TestClient(create_app(settings, mqtt_port=1)) as c:
        r = c.post(
            "/api/v1/printers",
            headers=_AUTH,
            json={"host": "127.0.0.1", "access_code": "12345678"},
        )
        assert r.status_code == 422
        body = r.json()
        assert body["error"] == "invalid_input"
        assert any(i.get("policy") == "loopback" for i in body.get("issues", []))


def test_ssrf_link_local_metadata_blocked(tmp_path: Path) -> None:
    """169.254.169.254 (AWS/GCP cloud-metadata) must be blocked even with
    `bridge_allow_loopback_host=True` — link-local stays blocked always."""
    with TestClient(build_app(tmp_path / "meta.db")) as c:
        r = c.post(
            "/api/v1/printers",
            headers=_AUTH,
            json={"host": "169.254.169.254", "access_code": "12345678"},
        )
        assert r.status_code == 422
        body = r.json()
        assert body["error"] == "invalid_input"
        assert any(i.get("policy") == "link_local" for i in body.get("issues", []))


# --------------------------------------------------------------------------- #
# §4.5 — TOFU /trust
# --------------------------------------------------------------------------- #


def test_trust_updates_stored_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL, fingerprint="old-fp")
    db_path = tmp_path / "trust.db"
    with TestClient(build_app(db_path)) as c:
        assert _post_register(c).status_code == 201
        # firmware update rotates the cert -> new fingerprint
        patch_discovery_ok(monkeypatch, serial=SERIAL, fingerprint="new-fp")
        r = c.post(f"/api/v1/printers/{SERIAL}/trust", headers=_AUTH)
        assert r.status_code == 204
        # Persisted? Read it back via PrinterRepo (closed app reopens DB).
    import asyncio

    from bambu_bridge.db.jobs import Database, PrinterRepo

    async def _check() -> str | None:
        db = Database(str(db_path))
        await db.connect()
        try:
            row = await PrinterRepo(db).get(SERIAL)
            return row.cert_fingerprint if row else None
        finally:
            await db.close()

    assert asyncio.run(_check()) == "new-fp"


def test_trust_refuses_if_different_serial_now_at_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    with TestClient(build_app(tmp_path / "swap.db")) as c:
        assert _post_register(c).status_code == 201
        # Different printer now answers at the same IP — refuse.
        patch_discovery_ok(monkeypatch, serial="DIFFERENT_SERIAL")
        r = c.post(f"/api/v1/printers/{SERIAL}/trust", headers=_AUTH)
        assert r.status_code == 409
        body = r.json()
        assert body["error"] == "conflict"
        assert body["discovered_serial"] == "DIFFERENT_SERIAL"
