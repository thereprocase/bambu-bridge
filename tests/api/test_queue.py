"""Print queue CRUD + reorder + start (contract §16 v0)."""

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


def _register(c: TestClient) -> None:
    r = c.post(
        "/api/v1/printers",
        headers=_AUTH,
        json={
            "host": "192.168.1.50",
            "access_code": ACCESS_CODE,
            "friendly_name": "Workshop P1S",
        },
    )
    assert r.status_code == 201, r.text


def _add(c: TestClient, *, file_name: str, ams: list[int] | None = None) -> Any:
    body: dict[str, Any] = {
        "file_path": f"/model/{file_name}",
        "file_name": file_name,
    }
    if ams is not None:
        body["ams_mapping"] = ams
    r = c.post(
        f"/api/v1/printers/{SERIAL}/queue", headers=_AUTH, json=body
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_queue_empty_for_new_printer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    with TestClient(build_app(tmp_path / "queue.db")) as c:
        _register(c)
        r = c.get(f"/api/v1/printers/{SERIAL}/queue", headers=_AUTH)
        assert r.status_code == 200
        assert r.json() == []


def test_add_assigns_dense_positions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    with TestClient(build_app(tmp_path / "queue.db")) as c:
        _register(c)
        a = _add(c, file_name="a.gcode.3mf")
        b = _add(c, file_name="b.gcode.3mf")
        cc = _add(c, file_name="c.gcode.3mf")
        assert (a["position"], b["position"], cc["position"]) == (0, 1, 2)
        # ams_mapping defaults to None and round-trips as null.
        assert a["ams_mapping"] is None


def test_ams_mapping_validated_1_to_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    with TestClient(build_app(tmp_path / "queue.db")) as c:
        _register(c)
        # 0 is forbidden — physical slots are 1-4
        r = c.post(
            f"/api/v1/printers/{SERIAL}/queue",
            headers=_AUTH,
            json={"file_path": "/model/x.3mf", "file_name": "x.3mf", "ams_mapping": [0]},
        )
        assert r.status_code == 422
        body = r.json()
        assert body["error"] == "invalid_input"
        msgs = [
            (i["message"] if isinstance(i, dict) else str(i))
            for i in body.get("issues", [])
        ]
        assert any("physical_slot" in m for m in msgs)

        # 5 also rejected
        r = c.post(
            f"/api/v1/printers/{SERIAL}/queue",
            headers=_AUTH,
            json={"file_path": "/model/x.3mf", "file_name": "x.3mf", "ams_mapping": [5]},
        )
        assert r.status_code == 422

        # 1-4 accepted
        ok = _add(c, file_name="ok.3mf", ams=[1, 3])
        assert ok["ams_mapping"] == [1, 3]


def test_reorder_sends_to_front_and_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    with TestClient(build_app(tmp_path / "queue.db")) as c:
        _register(c)
        a = _add(c, file_name="a.3mf")
        _b = _add(c, file_name="b.3mf")
        cc = _add(c, file_name="c.3mf")

        # Send c to front
        r = c.patch(f"/api/v1/queue/{cc['id']}", headers=_AUTH, json={"position": 0})
        assert r.status_code == 200
        order = [i["file_name"] for i in c.get(
            f"/api/v1/printers/{SERIAL}/queue", headers=_AUTH
        ).json()]
        assert order == ["c.3mf", "a.3mf", "b.3mf"]

        # Send a to back — position 99 clamps to end
        r = c.patch(f"/api/v1/queue/{a['id']}", headers=_AUTH, json={"position": 99})
        assert r.status_code == 200
        order = [i["file_name"] for i in c.get(
            f"/api/v1/printers/{SERIAL}/queue", headers=_AUTH
        ).json()]
        assert order == ["c.3mf", "b.3mf", "a.3mf"]


def test_delete_renumbers_dense(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    with TestClient(build_app(tmp_path / "queue.db")) as c:
        _register(c)
        _a = _add(c, file_name="a.3mf")
        b = _add(c, file_name="b.3mf")
        _cc = _add(c, file_name="c.3mf")

        r = c.delete(f"/api/v1/queue/{b['id']}", headers=_AUTH)
        assert r.status_code == 204

        items = c.get(
            f"/api/v1/printers/{SERIAL}/queue", headers=_AUTH
        ).json()
        positions = [i["position"] for i in items]
        names = [i["file_name"] for i in items]
        assert positions == [0, 1]  # dense after delete
        assert names == ["a.3mf", "c.3mf"]


def test_unknown_printer_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    with TestClient(build_app(tmp_path / "queue.db")) as c:
        r = c.get("/api/v1/printers/nope/queue", headers=_AUTH)
        assert r.status_code == 404
        assert r.json()["error"] == "not_found"


def test_unknown_queue_item_404(tmp_path: Path) -> None:
    with TestClient(build_app(tmp_path / "queue.db")) as c:
        r = c.get("/api/v1/queue/nope", headers=_AUTH)
        assert r.status_code == 404
        assert r.json()["error"] == "not_found"


def test_extra_field_forbidden_on_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    with TestClient(build_app(tmp_path / "queue.db")) as c:
        _register(c)
        r = c.post(
            f"/api/v1/printers/{SERIAL}/queue",
            headers=_AUTH,
            json={
                "file_path": "/model/x.3mf",
                "file_name": "x.3mf",
                "evil_field": "bad",
            },
        )
        assert r.status_code == 422
        assert r.json()["error"] == "invalid_input"


def test_cascade_delete_clears_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting a printer with ?cascade_jobs=true must also drop its queue
    rows (ON DELETE CASCADE on the print_queue table FK)."""
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    with TestClient(build_app(tmp_path / "queue.db")) as c:
        _register(c)
        _add(c, file_name="ghost.3mf")
        r = c.delete(
            f"/api/v1/printers/{SERIAL}?cascade_jobs=true", headers=_AUTH
        )
        assert r.status_code == 204
        # Re-register and confirm queue is empty (no orphans)
        _register(c)
        r = c.get(f"/api/v1/printers/{SERIAL}/queue", headers=_AUTH)
        assert r.status_code == 200
        assert r.json() == []
