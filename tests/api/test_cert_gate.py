"""PR A.2 — cert_gate enforcement (contract §4.5).

Once :class:`PrinterService` has flipped to ``cert_status="changed"``, the
API edge must refuse reads + control with the typed
``printer_cert_changed`` envelope (403, ``actions`` carrying the POST
/trust path the APK renders as a button). Two surfaces matter:

* ``GET /printers/{id}`` — the dashboard read path.
* ``POST /printers/{id}/command`` — and by extension every typed control
  route, which all funnel through ``_online()``.

The matching ``POST /trust`` must immediately re-pin the in-memory
service so the very next call goes through — without that, the operator
would have to wait for the next MQTT reconnect to clear the 403.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    ACCESS_CODE,
    API_KEY,
    SERIAL,
    build_app,
    patch_discovery_ok,
)

_AUTH = {"Authorization": f"Bearer {API_KEY}"}


def _register(c: TestClient, **overrides: Any) -> Any:
    body = {"host": "127.0.0.1", "access_code": ACCESS_CODE}
    body.update(overrides)
    return c.post("/api/v1/printers", headers=_AUTH, json=body)


def test_changed_cert_blocks_get_and_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL, fingerprint="pin-A")
    with TestClient(build_app(tmp_path / "gate1.db")) as c:
        assert _register(c).status_code == 201

        svc = c.app.state.registry.get(SERIAL)  # type: ignore[attr-defined]
        svc.expected_fingerprint = "pin-A"
        svc.current_fingerprint = "pin-B"
        svc.cert_status = "changed"

        r = c.get(f"/api/v1/printers/{SERIAL}", headers=_AUTH)
        assert r.status_code == 403
        body = r.json()
        assert body["error"] == "printer_cert_changed"
        assert body["context"]["previous_fingerprint"] == "pin-A"
        assert body["context"]["current_fingerprint"] == "pin-B"
        # APK renders `actions` as buttons; the trust action must point at
        # the resolution endpoint with the printer id substituted.
        paths = [a.get("path") for a in body["actions"]]
        assert f"/api/v1/printers/{SERIAL}/trust" in paths

        # Control path — every typed route funnels through _online() which
        # raises HTTPException(403, dict-detail) for the gate; the envelope
        # handler passes the dict body through unchanged.
        r = c.post(
            f"/api/v1/printers/{SERIAL}/command",
            headers=_AUTH,
            json={"category": "system", "command": "ledctrl"},
        )
        assert r.status_code == 403
        assert r.json()["error"] == "printer_cert_changed"


def test_trust_clears_gate_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /trust endpoint re-reads the cert and pins it; the very next
    request must not 403 (without this the gate would only clear on the
    next MQTT reconnect, which is anywhere from 1-60s away)."""
    patch_discovery_ok(monkeypatch, serial=SERIAL, fingerprint="pin-A")
    with TestClient(build_app(tmp_path / "gate2.db")) as c:
        assert _register(c).status_code == 201

        svc = c.app.state.registry.get(SERIAL)  # type: ignore[attr-defined]
        svc.expected_fingerprint = "pin-A"
        svc.current_fingerprint = "pin-B"
        svc.cert_status = "changed"

        # Re-patch discovery so /trust sees the *new* cert as the truth.
        patch_discovery_ok(monkeypatch, serial=SERIAL, fingerprint="pin-B")
        r = c.post(f"/api/v1/printers/{SERIAL}/trust", headers=_AUTH)
        assert r.status_code == 204

        r = c.get(f"/api/v1/printers/{SERIAL}", headers=_AUTH)
        assert r.status_code == 200
        assert r.json()["cert_status"] == "trusted"


def test_legacy_unknown_status_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-A.2 printers (``cert_fingerprint=NULL``) load with
    ``cert_status='unknown'``. The gate must let those through — otherwise
    upgrading the bridge to A.2 would 403 every legacy row until the
    operator hit /trust on each one."""
    patch_discovery_ok(monkeypatch, serial=SERIAL, fingerprint="pin-A")
    with TestClient(build_app(tmp_path / "gate3.db")) as c:
        assert _register(c).status_code == 201

        svc = c.app.state.registry.get(SERIAL)  # type: ignore[attr-defined]
        svc.expected_fingerprint = None
        svc.cert_status = "unknown"

        r = c.get(f"/api/v1/printers/{SERIAL}", headers=_AUTH)
        assert r.status_code == 200
        assert r.json()["cert_status"] == "unknown"
