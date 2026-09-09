"""M3: system endpoints (no auth) and Bearer enforcement (spec 6, 10)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import API_KEY, build_app


def _auth(key: str = API_KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_health_and_version_need_no_auth(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "a.db")) as c:
        assert c.get("/api/v1/health").json() == {"status": "ok"}
        body = c.get("/api/v1/version").json()
        assert "version" in body


def test_printers_requires_valid_bearer(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "b.db")) as c:
        assert c.get("/api/v1/printers").status_code == 401
        assert c.get("/api/v1/printers", headers=_auth("wrong")).status_code == 401
        ok = c.get("/api/v1/printers", headers=_auth())
        assert ok.status_code == 200
        assert ok.json() == []


def test_unconfigured_key_fails_closed(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "c.db", api_key="")) as c:
        # System endpoints stay up so the misconfig is observable...
        assert c.get("/api/v1/health").status_code == 200
        # ...but every authenticated route is 503, never open.
        assert c.get("/api/v1/printers", headers=_auth("anything")).status_code == 503
