"""Skip Objects endpoints: Orca's PartSkipDialog rules, zero publication on refusal.

The printer, FTPS and MQTT are all stand-ins: the project bytes come from a
real Orca slice fixture, and ``send_raw`` records what would be published.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from bambu_bridge.api import skip_objects
from bambu_bridge.api.auth import require_auth, require_media_auth
from bambu_bridge.skip_objects import ENABLE_ENV, PART_SKIP_FUN_BIT

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "orca"
FUN = format(1 << PART_SKIP_FUN_BIT, "X")


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, "1")
    raw = {
        "gcode_state": "RUNNING",
        "subtask_name": "multi3",
        "gcode_file": "/data/Metadata/plate_1.gcode",
        "fun": FUN,
        "info": {"module": [{"name": "ota", "product_name": "P1S"}]},
    }
    snapshot = {
        "_raw": raw,
        "session": {"connected": True, "last_telemetry_at": datetime.now(UTC).isoformat()},
    }
    service = SimpleNamespace(
        model=None, connected=True, cert_status="ok", ip="127.0.0.1", access_code="x" * 8,
        snapshot=lambda: snapshot, send_raw=AsyncMock(),
    )
    source = AsyncMock(return_value=(FIXTURES / "multi3.gcode.3mf").read_bytes())
    app = FastAPI()
    app.state.registry = SimpleNamespace(get=lambda _: service)
    app.state.ftps_port = 990
    app.state.viz_cache_obj = SimpleNamespace(acquire_source=source)
    app.dependency_overrides[require_auth] = lambda: None
    app.dependency_overrides[require_media_auth] = lambda: None
    app.include_router(skip_objects.router)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, service=service, raw=raw, source=source)


def _post(rig, ids):
    return rig.client.post("/printers/p/skip_objects", json={"obj_list": ids})


def test_get_lists_the_running_plate(rig):
    rig.raw["s_obj"] = [74]
    body = rig.client.get("/printers/p/skip_objects").json()
    assert body["job"] == "multi3" and body["plate"] == 1
    assert body["objects"] == [
        {"id": 63, "name": "cube.stl", "skipped": False},
        {"id": 74, "name": "bar.stl", "skipped": True},
        {"id": 85, "name": "frame.stl", "skipped": False},
    ]
    assert body["map"] is None   # CLI slices carry no pick image
    assert body["available"] is True and body["reason"] is None
    # Looked up by the printer's subtask_name, like the viewer.
    assert rig.source.await_args.args[2] == "multi3"


def test_get_reports_why_skip_is_withheld(rig, monkeypatch):
    monkeypatch.delenv(ENABLE_ENV)
    body = rig.client.get("/printers/p/skip_objects").json()
    assert body["available"] is False and body["reason"] == "Control support under review"
    monkeypatch.setenv(ENABLE_ENV, "1")
    rig.raw.pop("fun")
    body = rig.client.get("/printers/p/skip_objects").json()
    assert body["reason"] == "The printer does not report support for skipping objects"


def test_get_without_a_job_is_404(rig):
    rig.raw["subtask_name"] = ""
    assert rig.client.get("/printers/p/skip_objects").status_code == 404


def test_pick_map_rows_and_rendered_png(rig):
    rig.source.return_value = (FIXTURES / "gui_pick1.3mf").read_bytes()
    rig.raw["subtask_name"] = "Brushwarden_Base"
    body = rig.client.get("/printers/p/skip_objects").json()
    grid = body["map"]
    assert (grid["width"], grid["height"]) == (512, 512) and len(grid["rows"]) == 512
    assert all(sum(row[1::2]) == 512 for row in grid["rows"])
    assert {v for row in grid["rows"] for v in row[0::2]} == {0, 496}
    r = rig.client.get("/printers/p/skip_objects/map.png", params={"checked": "496"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    colours = {tuple(c) for c in np.asarray(Image.open(io.BytesIO(r.content))).reshape(-1, 4)}
    assert (239, 175, 175, 255) in colours and (255, 255, 255, 255) not in colours
    bad = rig.client.get("/printers/p/skip_objects/map.png", params={"checked": "x"})
    assert bad.status_code == 422


def test_map_of_a_file_without_pick_image_is_404(rig):
    assert rig.client.get("/printers/p/skip_objects/map.png").status_code == 404


def test_skip_publishes_orcas_exact_command(rig):
    r = _post(rig, [85, 63])
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "skip"
    rig.service.send_raw.assert_awaited_once()
    sent = rig.service.send_raw.await_args.args[0]
    assert set(sent) == {"print"}
    assert set(sent["print"]) == {"command", "obj_list", "sequence_id"}
    assert sent["print"]["command"] == "skip_objects"
    assert sent["print"]["obj_list"] == [85, 63]


def test_paused_print_may_skip(rig):
    rig.raw["gcode_state"] = "PAUSE"
    assert _post(rig, [63]).status_code == 200


def test_skipping_every_remaining_object_stops_the_print(rig):
    rig.raw["s_obj"] = [63]
    r = _post(rig, [74, 85])
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "stop"
    sent = rig.service.send_raw.await_args.args[0]["print"]
    assert sent["command"] == "stop" and sent["param"] == ""


@pytest.mark.parametrize(
    "ids,s_obj",
    [([99], []), ([63, 99], []), ([74], [74])],
)
def test_ids_must_be_unskipped_objects_of_this_plate(rig, ids, s_obj):
    rig.raw["s_obj"] = s_obj
    assert _post(rig, ids).status_code == 422
    rig.service.send_raw.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        {"gcode_state": "PREPARE"},
        {"gcode_state": "FINISH"},
        {"gcode_state": "IDLE"},
        {"fun": "0"},
        {"print_type": "system"},
    ],
)
def test_printer_side_refusals_publish_nothing(rig, change):
    rig.raw.update(change)
    assert _post(rig, [63]).status_code == 409
    rig.service.send_raw.assert_not_awaited()


def test_unlabelled_job_is_refused(rig):
    buf = io.BytesIO()
    with zipfile.ZipFile(FIXTURES / "multi3.gcode.3mf") as src, zipfile.ZipFile(buf, "w") as dst:
        for name in src.namelist():
            body = src.read(name)
            if name == "Metadata/slice_info.config":
                body = body.replace(b'enabled" value="true"', b'enabled" value="false"')
            dst.writestr(name, body)
    rig.source.return_value = buf.getvalue()
    r = _post(rig, [63])
    assert r.status_code == 409
    assert r.json()["detail"]["message"] == "The current print job cannot be skipped"
    rig.service.send_raw.assert_not_awaited()


def test_withheld_without_operator_opt_in(rig, monkeypatch):
    monkeypatch.delenv(ENABLE_ENV)
    r = _post(rig, [63])
    assert r.status_code == 409
    assert r.json()["detail"]["message"] == "Control support under review"
    rig.service.send_raw.assert_not_awaited()
    rig.source.assert_not_awaited()


def test_stale_telemetry_is_refused(rig):
    rig.service.snapshot()["session"]["last_telemetry_at"] = "2000-01-01T00:00:00Z"
    assert _post(rig, [63]).status_code == 409
    rig.service.send_raw.assert_not_awaited()


def test_body_limits(rig):
    assert _post(rig, []).status_code == 422
    assert _post(rig, list(range(65))).status_code == 422
    extra = rig.client.post("/printers/p/skip_objects", json={"obj_list": [1], "x": 1})
    assert extra.status_code == 422
    rig.service.send_raw.assert_not_awaited()
