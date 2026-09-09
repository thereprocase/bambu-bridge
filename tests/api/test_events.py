"""Per-printer event feed + dismiss + clear (contract §8.4–§8.6)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bambu_bridge.db.jobs import Database, EventRepo
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
            "friendly_name": "P1S",
        },
    )
    assert r.status_code == 201, r.text


async def _seed_events(db_path: Path) -> dict[str, int]:
    """Insert one of each event type the feed renders. Returns the row ids."""
    db = Database(str(db_path))
    await db.connect()
    repo = EventRepo(db)
    ids: dict[str, int] = {}
    ids["print_started"] = await repo.add(
        printer_id=SERIAL,
        event_type="print_started",
        payload={"subtask_name": "Benchy.gcode.3mf", "started_at": "2026-05-22T10:00:00Z"},
        severity="info",
    )
    ids["filament_runout"] = await repo.add(
        printer_id=SERIAL,
        event_type="filament_runout",
        payload={"code": "0700_2000_0002_0001", "slot": 2},
        severity="warn",
    )
    ids["print_failed"] = await repo.add(
        printer_id=SERIAL,
        event_type="print_failed",
        payload={
            "print_error": {
                "code": "0300_1100_0001_0001",
                "hex": "0300_1100_0001_0001",
                "text": "Hotend over-temp",
                "category": "thermal",
                "severity": "error",
                "remediation": "Let the hotend cool, then retry.",
            },
            "layer_num": 42,
        },
        severity="error",
    )
    # Bookkeeping (must be filtered out of the user feed).
    ids["state_change"] = await repo.add(
        printer_id=SERIAL,
        event_type="state_change",
        payload={"from": "queued", "to": "uploading", "trigger": "ftps_begin"},
    )
    await db.close()
    return ids


@pytest.mark.asyncio
async def test_list_events_returns_wire_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    db_path = tmp_path / "events.db"
    app = build_app(db_path)
    with TestClient(app) as c:
        _register(c)
        ids = await _seed_events(db_path)
        r = c.get(f"/api/v1/printers/{SERIAL}/events", headers=_AUTH)
        assert r.status_code == 200
        items = r.json()
        # Bookkeeping `state_change` row is filtered out.
        kinds = [i["kind"] for i in items]
        assert "state_change" not in kinds
        assert {"print_started", "filament_runout", "print_failed"} <= set(kinds)
        # Each item has the full §8.4 envelope.
        first = items[0]
        assert {"id", "ts", "severity", "kind", "title", "detail", "context",
                "job_id", "dismissed"} <= set(first.keys())
        # Specific rendering: filament_runout (§12.2 {code, slot}) names the
        # engaged slot in `detail` and the code in `context`.
        runout = next(i for i in items if i["kind"] == "filament_runout")
        assert runout["severity"] == "warn"
        assert "Slot 2" in runout["detail"]
        assert "0700_2000" in runout["context"]
        # print_failed (§12.2 nested print_error) surfaces text + code.
        failed = next(i for i in items if i["kind"] == "print_failed")
        assert failed["severity"] == "error"
        assert "Hotend over-temp" in failed["detail"]
        assert "0300_1100" in failed["context"]
        # ids returned are the same ones we inserted.
        assert {i["id"] for i in items} == {
            ids["print_started"], ids["filament_runout"], ids["print_failed"]
        }


@pytest.mark.asyncio
async def test_list_filtered_by_severity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    db_path = tmp_path / "events.db"
    app = build_app(db_path)
    with TestClient(app) as c:
        _register(c)
        await _seed_events(db_path)
        r = c.get(
            f"/api/v1/printers/{SERIAL}/events?severity=error", headers=_AUTH
        )
        assert r.status_code == 200
        items = r.json()
        assert [i["kind"] for i in items] == ["print_failed"]


@pytest.mark.asyncio
async def test_invalid_severity_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    app = build_app(tmp_path / "events.db")
    with TestClient(app) as c:
        _register(c)
        r = c.get(
            f"/api/v1/printers/{SERIAL}/events?severity=critical", headers=_AUTH
        )
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_dismiss_event_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    db_path = tmp_path / "events.db"
    app = build_app(db_path)
    with TestClient(app) as c:
        _register(c)
        ids = await _seed_events(db_path)
        target = ids["filament_runout"]
        # First dismiss → 204
        r1 = c.post(
            f"/api/v1/printers/{SERIAL}/events/{target}/dismiss", headers=_AUTH
        )
        assert r1.status_code == 204
        # Confirm the row is now marked dismissed
        items = c.get(f"/api/v1/printers/{SERIAL}/events", headers=_AUTH).json()
        runout = next(i for i in items if i["id"] == target)
        assert runout["dismissed"] is True
        # Second dismiss → still 204 (idempotent)
        r2 = c.post(
            f"/api/v1/printers/{SERIAL}/events/{target}/dismiss", headers=_AUTH
        )
        assert r2.status_code == 204


@pytest.mark.asyncio
async def test_dismiss_filters_when_include_dismissed_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    db_path = tmp_path / "events.db"
    app = build_app(db_path)
    with TestClient(app) as c:
        _register(c)
        ids = await _seed_events(db_path)
        c.post(
            f"/api/v1/printers/{SERIAL}/events/{ids['filament_runout']}/dismiss",
            headers=_AUTH,
        )
        r = c.get(
            f"/api/v1/printers/{SERIAL}/events?include_dismissed=false",
            headers=_AUTH,
        )
        items = r.json()
        assert {i["kind"] for i in items} == {"print_started", "print_failed"}


@pytest.mark.asyncio
async def test_clear_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    db_path = tmp_path / "events.db"
    app = build_app(db_path)
    with TestClient(app) as c:
        _register(c)
        await _seed_events(db_path)
        r = c.post(
            f"/api/v1/printers/{SERIAL}/events/clear", headers=_AUTH
        )
        assert r.status_code == 204
        items = c.get(
            f"/api/v1/printers/{SERIAL}/events?include_dismissed=false",
            headers=_AUTH,
        ).json()
        assert items == []


@pytest.mark.asyncio
async def test_unknown_printer_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build_app(tmp_path / "events.db")
    with TestClient(app) as c:
        r = c.get("/api/v1/printers/ghost/events", headers=_AUTH)
        assert r.status_code == 404
        r2 = c.post(
            "/api/v1/printers/ghost/events/1/dismiss", headers=_AUTH
        )
        assert r2.status_code == 404
        r3 = c.post(
            "/api/v1/printers/ghost/events/clear", headers=_AUTH
        )
        assert r3.status_code == 404


@pytest.mark.asyncio
async def test_dismiss_404_for_event_from_other_printer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    db_path = tmp_path / "events.db"
    app = build_app(db_path)
    with TestClient(app) as c:
        _register(c)
        await _seed_events(db_path)
        # Existing event id but wrong printer in URL → 404
        r = c.post(
            "/api/v1/printers/wrong-printer/events/9999/dismiss", headers=_AUTH
        )
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_iso_since_until_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_discovery_ok(monkeypatch, serial=SERIAL)
    db_path = tmp_path / "events.db"
    app = build_app(db_path)
    with TestClient(app) as c:
        _register(c)
        await _seed_events(db_path)
        # A future `since` returns []
        r = c.get(
            f"/api/v1/printers/{SERIAL}/events"
            "?since=2099-01-01T00:00:00Z",
            headers=_AUTH,
        )
        assert r.status_code == 200
        assert r.json() == []
        # Invalid ISO → 422
        r2 = c.get(
            f"/api/v1/printers/{SERIAL}/events?since=garbage", headers=_AUTH
        )
        assert r2.status_code == 422
