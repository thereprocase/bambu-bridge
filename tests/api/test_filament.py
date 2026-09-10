"""API tests for filament memory endpoints (G3).

PUT/GET/DELETE roundtrip; auth guards; slot validation; body validation.
All tests use the hermetic TestClient against an in-memory DB.

These tests do NOT register a real printer (no MQTT broker needed) —
the API endpoints only need the printer to exist in the registry; we
register it at mqtt_port=1 (unreachable) which is the standard pattern
for CRUD tests in this repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ACCESS_CODE, API_KEY, SERIAL, build_app, patch_discovery_ok

_AUTH = {"Authorization": f"Bearer {API_KEY}"}
_BASE = f"/api/v1/printers/{SERIAL}/filament-memory"


@pytest.fixture(autouse=True)
def _patched_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)


def _register(c: TestClient) -> None:
    r = c.post(
        "/api/v1/printers",
        headers=_AUTH,
        json={"host": "127.0.0.1", "access_code": ACCESS_CODE, "friendly_name": "Workshop P1S"},
    )
    assert r.status_code == 201, r.text


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def test_get_requires_auth(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "fil.db")) as c:
        _register(c)
        r = c.get(_BASE)
        assert r.status_code == 401


def test_put_requires_auth(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "fil.db")) as c:
        _register(c)
        r = c.put(f"{_BASE}/1", json={"make": "Bambu"})
        assert r.status_code == 401


def test_delete_requires_auth(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "fil.db")) as c:
        _register(c)
        r = c.delete(f"{_BASE}/1")
        assert r.status_code == 401


# --------------------------------------------------------------------------- #
# 404 — unknown printer
# --------------------------------------------------------------------------- #


def test_get_unknown_printer_404(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "fil.db")) as c:
        r = c.get("/api/v1/printers/NO-SUCH/filament-memory", headers=_AUTH)
        assert r.status_code == 404


def test_put_unknown_printer_404(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "fil.db")) as c:
        r = c.put("/api/v1/printers/NO-SUCH/filament-memory/1",
                  headers=_AUTH, json={"make": "Bambu"})
        assert r.status_code == 404


def test_delete_unknown_printer_404(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "fil.db")) as c:
        r = c.delete("/api/v1/printers/NO-SUCH/filament-memory/1", headers=_AUTH)
        assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Slot validation (422 for out-of-range or 0)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("slot", [0, 5, 99, -1])
def test_put_invalid_slot_422(tmp_path: Path, slot: int) -> None:
    with TestClient(build_app(tmp_path / f"fil-s{slot}.db")) as c:
        _register(c)
        r = c.put(f"{_BASE}/{slot}", headers=_AUTH, json={"make": "Bambu"})
        assert r.status_code == 422


@pytest.mark.parametrize("slot", [0, 5])
def test_delete_invalid_slot_422(tmp_path: Path, slot: int) -> None:
    with TestClient(build_app(tmp_path / f"fil-d{slot}.db")) as c:
        _register(c)
        r = c.delete(f"{_BASE}/{slot}", headers=_AUTH)
        assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Body validation
# --------------------------------------------------------------------------- #


def test_put_all_null_body_422(tmp_path: Path) -> None:
    """At least one field must be non-empty."""
    with TestClient(build_app(tmp_path / "fil.db")) as c:
        _register(c)
        r = c.put(f"{_BASE}/1", headers=_AUTH, json={})
        assert r.status_code == 422


def test_put_whitespace_only_fields_422(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "fil.db")) as c:
        _register(c)
        r = c.put(f"{_BASE}/1", headers=_AUTH, json={"make": "   ", "model": None})
        assert r.status_code == 422


def test_put_field_too_long_422(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "fil.db")) as c:
        _register(c)
        r = c.put(f"{_BASE}/1", headers=_AUTH, json={"make": "x" * 121})
        assert r.status_code == 422


def test_put_extra_fields_422(tmp_path: Path) -> None:
    """extra='forbid' on the request model."""
    with TestClient(build_app(tmp_path / "fil.db")) as c:
        _register(c)
        r = c.put(f"{_BASE}/1", headers=_AUTH, json={"make": "Bambu", "unexpected": "x"})
        assert r.status_code == 422


# --------------------------------------------------------------------------- #
# PUT / GET / DELETE roundtrip
# --------------------------------------------------------------------------- #


def test_put_get_roundtrip(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "fil.db")) as c:
        _register(c)
        # GET before any PUT → empty slots dict.
        r = c.get(_BASE, headers=_AUTH)
        assert r.status_code == 200
        assert r.json() == {"slots": {}}

        # PUT slot 1.
        r = c.put(f"{_BASE}/1", headers=_AUTH,
                  json={"make": "Bambu", "model": "PLA Matte", "profile": "0.20 Standard"})
        assert r.status_code == 200
        body = r.json()
        assert body["make"] == "Bambu"
        assert body["model"] == "PLA Matte"
        assert body["profile"] == "0.20 Standard"
        assert "tray_type_seen" in body
        assert body["updated_at"] > 0

        # GET shows slot 1.
        r = c.get(_BASE, headers=_AUTH)
        assert r.status_code == 200
        slots = r.json()["slots"]
        assert "1" in slots
        assert slots["1"]["make"] == "Bambu"


def test_put_multiple_slots(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "fil.db")) as c:
        _register(c)
        for slot, make in ((1, "Bambu"), (2, "Polymaker"), (4, "Bambu")):
            r = c.put(f"{_BASE}/{slot}", headers=_AUTH, json={"make": make})
            assert r.status_code == 200
        r = c.get(_BASE, headers=_AUTH)
        slots = r.json()["slots"]
        assert set(slots.keys()) == {"1", "2", "4"}
        assert slots["1"]["make"] == "Bambu"
        assert slots["2"]["make"] == "Polymaker"
        assert slots["4"]["make"] == "Bambu"


def test_put_overwrites_existing(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "fil.db")) as c:
        _register(c)
        c.put(f"{_BASE}/1", headers=_AUTH, json={"make": "Bambu"})
        r = c.put(f"{_BASE}/1", headers=_AUTH, json={"make": "Polymaker", "model": "PETG"})
        assert r.status_code == 200
        assert r.json()["make"] == "Polymaker"
        assert r.json()["model"] == "PETG"

        # Only one entry for slot 1.
        slots = c.get(_BASE, headers=_AUTH).json()["slots"]
        assert len(slots) == 1
        assert slots["1"]["make"] == "Polymaker"


def test_delete_existing_slot(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "fil.db")) as c:
        _register(c)
        c.put(f"{_BASE}/2", headers=_AUTH, json={"make": "Bambu"})
        r = c.delete(f"{_BASE}/2", headers=_AUTH)
        assert r.status_code == 204
        # Slot gone from GET.
        r = c.get(_BASE, headers=_AUTH)
        assert "2" not in r.json()["slots"]


def test_delete_nonexistent_slot_is_no_op(tmp_path: Path) -> None:
    """DELETE on a slot with no label is idempotent — 204 either way."""
    with TestClient(build_app(tmp_path / "fil.db")) as c:
        _register(c)
        r = c.delete(f"{_BASE}/3", headers=_AUTH)
        assert r.status_code == 204


def test_put_all_valid_slots(tmp_path: Path) -> None:
    """Slots 1-4 all accepted."""
    with TestClient(build_app(tmp_path / "fil.db")) as c:
        _register(c)
        for slot in (1, 2, 3, 4):
            r = c.put(f"{_BASE}/{slot}", headers=_AUTH, json={"make": f"M{slot}"})
            assert r.status_code == 200, f"slot {slot}: {r.text}"
        slots = c.get(_BASE, headers=_AUTH).json()["slots"]
        assert set(slots.keys()) == {"1", "2", "3", "4"}


def test_put_partial_fields_accepted(tmp_path: Path) -> None:
    """Only make is required (well — at least one non-empty field)."""
    with TestClient(build_app(tmp_path / "fil.db")) as c:
        _register(c)
        r = c.put(f"{_BASE}/1", headers=_AUTH, json={"profile": "Draft"})
        assert r.status_code == 200
        body = r.json()
        assert body["make"] is None
        assert body["model"] is None
        assert body["profile"] == "Draft"


def test_tray_type_seen_bound_to_live_state(tmp_path: Path) -> None:
    """tray_type_seen reflects the slot's current type from live state
    (which is empty/None for an un-connected printer in tests)."""
    with TestClient(build_app(tmp_path / "fil.db")) as c:
        _register(c)
        r = c.put(f"{_BASE}/1", headers=_AUTH, json={"make": "Bambu"})
        assert r.status_code == 200
        # Printer is not connected → no AMS state → tray_type_seen is None.
        assert r.json()["tray_type_seen"] is None
