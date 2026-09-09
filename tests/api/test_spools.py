"""Off-AMS spool cabinet inventory CRUD (contract §16 v0)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import API_KEY, build_app

_AUTH = {"Authorization": f"Bearer {API_KEY}"}


def _make(c: TestClient, **overrides: Any) -> Any:
    body: dict[str, Any] = {
        "name": "PLA Matte Charcoal",
        "material": "PLA",
        "color_hex": "#2a2a2e",
        "brand": "Bambu",
        "total_g": 1000.0,
        "remaining_g": 928.0,
    }
    body.update(overrides)
    r = c.post("/api/v1/spools", headers=_AUTH, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_list_empty(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "spools.db")) as c:
        r = c.get("/api/v1/spools", headers=_AUTH)
        assert r.status_code == 200
        assert r.json() == []


def test_create_get_update_delete(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "spools.db")) as c:
        created = _make(c)
        assert created["id"]
        assert created["name"] == "PLA Matte Charcoal"
        assert created["color_hex"] == "#2a2a2e"
        assert created["added_at"] > 0

        # GET
        r = c.get(f"/api/v1/spools/{created['id']}", headers=_AUTH)
        assert r.status_code == 200
        assert r.json()["id"] == created["id"]

        # PATCH remaining_g (user used some filament)
        r = c.patch(
            f"/api/v1/spools/{created['id']}",
            headers=_AUTH,
            json={"remaining_g": 854.0, "notes": "used on benchy"},
        )
        assert r.status_code == 200
        assert r.json()["remaining_g"] == 854.0
        assert r.json()["notes"] == "used on benchy"

        # LIST shows the updated row
        r = c.get("/api/v1/spools", headers=_AUTH)
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["remaining_g"] == 854.0

        # DELETE
        r = c.delete(f"/api/v1/spools/{created['id']}", headers=_AUTH)
        assert r.status_code == 204

        r = c.get(f"/api/v1/spools/{created['id']}", headers=_AUTH)
        assert r.status_code == 404
        assert r.json()["error"] == "not_found"


def test_color_hex_validation(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "spools.db")) as c:
        # Missing '#'
        r = c.post(
            "/api/v1/spools",
            headers=_AUTH,
            json={"name": "x", "material": "PLA", "color_hex": "ff0000"},
        )
        assert r.status_code == 422
        assert r.json()["error"] == "invalid_input"

        # Right length, wrong chars
        r = c.post(
            "/api/v1/spools",
            headers=_AUTH,
            json={"name": "x", "material": "PLA", "color_hex": "#zzzzzz"},
        )
        assert r.status_code == 422

        # 6-digit OK
        ok = _make(c, color_hex="#FF0000")
        assert ok["color_hex"] == "#FF0000"

        # 8-digit OK (RGBA)
        ok2 = _make(c, color_hex="#FF0000FF")
        assert ok2["color_hex"] == "#FF0000FF"


def test_negative_remaining_rejected(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "spools.db")) as c:
        r = c.post(
            "/api/v1/spools",
            headers=_AUTH,
            json={
                "name": "x",
                "material": "PLA",
                "color_hex": "#000000",
                "remaining_g": -5,
            },
        )
        assert r.status_code == 422


def test_extra_field_forbidden(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "spools.db")) as c:
        r = c.post(
            "/api/v1/spools",
            headers=_AUTH,
            json={
                "name": "x",
                "material": "PLA",
                "color_hex": "#000000",
                "rogue": "value",
            },
        )
        assert r.status_code == 422


def test_partial_update_only_changes_provided_fields(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "spools.db")) as c:
        s = _make(c, total_g=1000.0, remaining_g=500.0, brand="Bambu")
        # PATCH only remaining_g
        r = c.patch(
            f"/api/v1/spools/{s['id']}",
            headers=_AUTH,
            json={"remaining_g": 250.0},
        )
        assert r.status_code == 200
        updated = r.json()
        assert updated["remaining_g"] == 250.0
        assert updated["total_g"] == 1000.0  # unchanged
        assert updated["brand"] == "Bambu"  # unchanged


def test_list_ordered_by_added_at(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "spools.db")) as c:
        a = _make(c, name="A")
        b = _make(c, name="B")
        cc = _make(c, name="C")
        names = [s["name"] for s in c.get("/api/v1/spools", headers=_AUTH).json()]
        # ascending by added_at => insertion order
        assert names == ["A", "B", "C"]
        # ids unique
        assert len({a["id"], b["id"], cc["id"]}) == 3


def test_unknown_spool_404(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "spools.db")) as c:
        r = c.get("/api/v1/spools/nope", headers=_AUTH)
        assert r.status_code == 404
        r = c.patch("/api/v1/spools/nope", headers=_AUTH, json={"name": "x"})
        assert r.status_code == 404
        r = c.delete("/api/v1/spools/nope", headers=_AUTH)
        assert r.status_code == 404


def test_unauthenticated_rejected(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "spools.db")) as c:
        r = c.get("/api/v1/spools")
        assert r.status_code == 401
