"""Skip Objects endpoints: Orca's PartSkipDialog rules, zero publication on refusal.

The printer, FTPS and MQTT are all stand-ins: the project bytes come from
real Orca slice fixtures placed in a fake printer storage, and ``send_raw``
records what would be published.
"""

from __future__ import annotations

import ftplib
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

from bambu_bridge import skip_objects as core
from bambu_bridge.api import skip_objects
from bambu_bridge.api.auth import require_auth, require_media_auth
from bambu_bridge.skip_objects import ENABLE_ENV, PART_SKIP_FUN_BIT

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "orca"
FUN = format(1 << PART_SKIP_FUN_BIT, "X")
MULTI3 = (FIXTURES / "multi3.gcode.3mf").read_bytes()      # objects 63, 74, 85
SPARSE13 = (FIXTURES / "sparse13.gcode.3mf").read_bytes()  # objects 45, 56
MULTI3_IDS = [63, 74, 85]


def _with_plate_gcode(data: bytes, gcode: bytes = b"; no labels\n") -> bytes:
    """gui_pick1.3mf keeps only the GUI slice's metadata; give it a plate member."""
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(buf, "w") as dst:
        for name in src.namelist():
            dst.writestr(name, src.read(name))
        dst.writestr("Metadata/plate_1.gcode", gcode)
    return buf.getvalue()


GUI = _with_plate_gcode((FIXTURES / "gui_pick1.3mf").read_bytes())  # 496, pick map
def _ids(body):
    return [o["id"] for o in body["objects"]]


class FakeStorage:
    """Printer SD card: {"": root files, "cache": /cache files}, with failures."""

    def __init__(self):
        self.dirs = {"": {}, "cache": {}}
        self.fail: dict[str, Exception] = {}
        self.on_read = None

    def transfer(self, *args, **kwargs):
        storage = self

        class Ftps:
            async def list_dir(self, remote_dir="", *, strict=False):
                assert strict, "skip lookups must be strict"
                if remote_dir in storage.fail:
                    raise storage.fail[remote_dir]
                return list(storage.dirs[remote_dir])

        return Ftps()

    async def source_bytes(self, printer_id, ftps, remote_dir, filename):
        if self.on_read:
            self.on_read()
        return self.dirs[remote_dir][filename]


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, "1")
    raw = {
        "gcode_state": "RUNNING",
        "subtask_name": "multi3",
        "gcode_file": "multi3.gcode.3mf",   # the live P1S reports the archive name
        "fun": FUN,
        "info": {"module": [{"name": "ota", "product_name": "P1S"}]},
    }
    snapshot = {
        "printer_id": "p",
        "_raw": raw,
        "session": {"connected": True, "last_telemetry_at": datetime.now(UTC).isoformat()},
    }
    service = SimpleNamespace(
        model=None, connected=True, cert_status="ok", ip="127.0.0.1", access_code="x" * 8,
        snapshot=lambda: snapshot, send_raw=AsyncMock(),
    )
    storage = FakeStorage()
    storage.dirs[""]["multi3.gcode.3mf"] = MULTI3
    monkeypatch.setattr(skip_objects, "FtpsTransfer", storage.transfer)
    app = FastAPI()
    app.state.registry = SimpleNamespace(get=lambda _: service)
    app.state.ftps_port = 990
    app.state.viz_cache_obj = SimpleNamespace(
        validate_revision=AsyncMock(), source_bytes=storage.source_bytes
    )
    app.dependency_overrides[require_auth] = lambda: None
    app.dependency_overrides[require_media_auth] = lambda: None
    app.include_router(skip_objects.router)
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client, service=service, raw=raw, snapshot=snapshot, storage=storage, app=app
        )


def _get(rig):
    return rig.client.get("/printers/p/skip_objects")


IDENTITY = ("job", "gcode_file", "plate", "digest")


def _body(seen, ids=(63,), action="skip", **override):
    return {"obj_list": list(ids), "action": action, **{k: seen[k] for k in IDENTITY}, **override}


def _post(rig, ids, action="skip", **override):
    seen = _get(rig).json()
    return rig.client.post("/printers/p/skip_objects", json=_body(seen, ids, action, **override))


def _sent(rig):
    return [c.args[0]["print"] for c in rig.service.send_raw.await_args_list]


# --------------------------------------------------------------------------- #
# GET
# --------------------------------------------------------------------------- #


def test_get_lists_the_running_plate_and_its_identity(rig):
    rig.raw["s_obj"] = [74]
    body = _get(rig).json()
    assert (body["job"], body["gcode_file"], body["plate"]) == ("multi3", "multi3.gcode.3mf", 1)
    assert len(body["digest"]) == 64
    assert body["objects"] == [
        {"id": 63, "name": "cube.stl", "skipped": False},
        {"id": 74, "name": "bar.stl", "skipped": True},
        {"id": 85, "name": "frame.stl", "skipped": False},
    ]
    # CLI slices carry no pick image: the map is the G-code footprint.
    assert body["map_source"] == "gcode"
    assert {v for row in body["map"]["rows"] for v in row[0::2]} == {0, 63, 74, 85}
    assert body["available"] is True and body["reason"] is None


def test_get_reports_why_skip_is_withheld(rig, monkeypatch):
    monkeypatch.delenv(ENABLE_ENV)
    body = _get(rig).json()
    assert body["available"] is False and body["reason"] == "Control support under review"
    monkeypatch.setenv(ENABLE_ENV, "1")
    rig.raw.pop("fun")
    body = _get(rig).json()
    assert body["reason"] == "The printer does not report support for skipping objects"


def test_get_without_a_job_is_404(rig):
    rig.raw["subtask_name"] = ""
    rig.raw["gcode_file"] = ""
    assert _get(rig).status_code == 404


def test_pick_map_rows_and_rendered_png(rig):
    rig.storage.dirs[""]["Brushwarden_Base.gcode.3mf"] = GUI
    rig.raw.update(subtask_name="Brushwarden_Base", gcode_file="Metadata/plate_1.gcode")
    body = _get(rig).json()
    assert body["map_source"] == "pick"
    grid = body["map"]
    assert (grid["width"], grid["height"]) == (512, 512) and len(grid["rows"]) == 512
    assert all(sum(row[1::2]) == 512 for row in grid["rows"])
    assert {v for row in grid["rows"] for v in row[0::2]} == {0, 496}
    r = rig.client.get(
        "/printers/p/skip_objects/map.png", params={"checked": "496", "digest": body["digest"]}
    )
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    # The URL may carry the owner key: never cached (as camera.py).
    assert r.headers["cache-control"] == "no-store"
    colours = {tuple(c) for c in np.asarray(Image.open(io.BytesIO(r.content))).reshape(-1, 4)}
    assert (239, 175, 175, 255) in colours and (255, 255, 255, 255) not in colours


def test_map_png_rejects_bad_ids_and_ignores_unlisted_ones(rig):
    url = "/printers/p/skip_objects/map.png"
    for bad in ("x", "-1", "8589934592"):
        assert rig.client.get(url, params={"checked": bad}).status_code == 422, bad
    assert rig.client.get(url, params={"checked": "99999,63"}).status_code == 200


def test_map_png_for_another_file_is_409(rig):
    r = rig.client.get("/printers/p/skip_objects/map.png", params={"digest": "0" * 64})
    assert r.status_code == 409


def test_map_of_a_file_without_any_map_is_404(rig):
    # No pick image and a plate G-code without object labels.
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(GUI)) as src, zipfile.ZipFile(buf, "w") as dst:
        for name in src.namelist():
            if name != "Metadata/pick_1.png":
                dst.writestr(name, src.read(name))
    rig.storage.dirs[""]["Brushwarden_Base.gcode.3mf"] = buf.getvalue()
    rig.raw.update(subtask_name="Brushwarden_Base", gcode_file="Metadata/plate_1.gcode")
    body = _get(rig).json()
    assert body["map"] is None and _ids(body) == [496]
    assert rig.client.get("/printers/p/skip_objects/map.png").status_code == 404


def test_parsed_job_is_reused_for_the_same_file(rig, monkeypatch):
    calls = []
    real = core.read_job
    monkeypatch.setattr(core, "read_job", lambda *a: calls.append(1) or real(*a))
    _get(rig)
    _get(rig)
    assert rig.client.get("/printers/p/skip_objects/map.png").status_code == 200
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# A. The running file, found exactly
# --------------------------------------------------------------------------- #


def test_native_inbox_print_uses_the_verified_copy_not_a_stale_namesake(rig):
    # Orca's tray.gcode.3mf is stored as beluga-<uuid>.gcode.3mf; subtask stays "tray".
    beluga = "beluga-" + "a" * 32 + ".gcode.3mf"
    rig.raw.update(subtask_name="tray", gcode_file=beluga)
    rig.storage.dirs[""]["tray.gcode.3mf"] = SPARSE13       # an older app print
    seen = []
    rig.app.state.native_gateway = SimpleNamespace(
        local_camera_source=lambda snap: seen.append(snap["_raw"]["gcode_file"]) or MULTI3
    )
    assert _ids(_get(rig).json()) == MULTI3_IDS
    assert seen == [beluga]


def test_native_inbox_print_without_a_local_copy_reads_its_own_file(rig):
    beluga = "beluga-" + "b" * 32 + ".gcode.3mf"
    rig.raw.update(subtask_name="tray", gcode_file=beluga)
    rig.storage.dirs[""]["tray.gcode.3mf"] = SPARSE13
    rig.app.state.native_gateway = SimpleNamespace(local_camera_source=lambda snap: None)
    assert _get(rig).status_code == 404                     # never the namesake
    rig.storage.dirs[""][beluga] = MULTI3
    assert _ids(_get(rig).json()) == MULTI3_IDS


def test_dotted_subtask_is_never_stem_truncated(rig):
    rig.raw.update(subtask_name="bracket.v2", gcode_file="Metadata/plate_1.gcode")
    rig.storage.dirs[""]["bracket.gcode.3mf"] = SPARSE13
    rig.storage.dirs[""]["bracket.v2.gcode.3mf"] = MULTI3
    assert _ids(_get(rig).json()) == MULTI3_IDS


def test_subtask_prefers_the_sliced_gcode_3mf(rig):
    rig.raw.update(subtask_name="part", gcode_file="Metadata/plate_1.gcode")
    rig.storage.dirs[""]["part.3mf"] = SPARSE13
    rig.storage.dirs[""]["part.gcode.3mf"] = MULTI3
    assert _ids(_get(rig).json()) == MULTI3_IDS


def test_same_name_in_root_and_cache_is_refused(rig):
    seen = _get(rig).json()
    rig.storage.dirs["cache"]["multi3.gcode.3mf"] = SPARSE13
    assert _get(rig).status_code == 409
    assert rig.client.post("/printers/p/skip_objects", json=_body(seen)).status_code == 409
    rig.service.send_raw.assert_not_awaited()
    # A gcode_file that says /cache settles it.
    rig.raw["gcode_file"] = "/cache/multi3.gcode.3mf"
    assert _ids(_get(rig).json()) == [45, 56]


def test_failed_root_listing_never_falls_back_to_cache(rig):
    rig.storage.dirs["cache"]["multi3.gcode.3mf"] = SPARSE13
    rig.storage.fail[""] = TimeoutError("LIST timed out")
    assert _get(rig).status_code == 502
    rig.storage.fail[""] = ftplib.error_perm("550 denied")
    assert _get(rig).status_code == 502


def test_missing_cache_directory_counts_as_empty(rig):
    rig.storage.fail["cache"] = ftplib.error_perm("550 no such directory")
    assert _ids(_get(rig).json()) == MULTI3_IDS


# --------------------------------------------------------------------------- #
# POST
# --------------------------------------------------------------------------- #


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
    r = _post(rig, [74, 85], action="stop")
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "stop"
    (sent,) = _sent(rig)
    assert (sent["command"], sent["param"]) == ("stop", "")


@pytest.mark.parametrize(
    "s_obj,ids,confirmed",
    [([63], [74, 85], "skip"), ([], [74], "stop")],
)
def test_action_must_match_what_the_user_confirmed(rig, s_obj, ids, confirmed):
    rig.raw["s_obj"] = s_obj
    r = _post(rig, ids, action=confirmed)
    assert r.status_code == 409
    assert "reopen Skip Objects" in r.json()["detail"]["message"]
    rig.service.send_raw.assert_not_awaited()


def test_ids_sent_but_not_yet_echoed_count_as_skipped(rig):
    # Orca's dirty filter: the request that empties the plate becomes stop.
    assert _post(rig, [63]).status_code == 200
    assert _post(rig, [74, 85], action="skip").status_code == 409
    assert _post(rig, [63], action="skip").status_code == 422     # already sent
    assert _post(rig, [74, 85], action="stop").status_code == 200
    assert [p["command"] for p in _sent(rig)] == ["skip_objects", "stop"]


def test_pending_ids_clear_when_echoed_or_the_job_changes(rig):
    assert _post(rig, [63]).status_code == 200
    rig.raw["s_obj"] = [63]                           # echoed
    assert _post(rig, [74]).status_code == 200
    rig.storage.dirs[""]["next.gcode.3mf"] = MULTI3
    rig.raw.update(subtask_name="next", gcode_file="next.gcode.3mf", s_obj=[])
    assert _post(rig, [63, 74]).status_code == 200    # a new job: nothing pending


@pytest.mark.parametrize(
    "field,value",
    [("job", "other"), ("gcode_file", "other.gcode.3mf"), ("plate", 2), ("digest", "0" * 64)],
)
def test_a_selection_from_another_job_is_refused(rig, field, value):
    r = _post(rig, [63], **{field: value})
    assert r.status_code == 409
    assert r.json()["detail"]["message"] == "The print changed; reopen Skip Objects"
    rig.service.send_raw.assert_not_awaited()


@pytest.mark.parametrize(
    "change,message",
    [
        ({"subtask_name": "next"}, "The print changed; reopen Skip Objects"),
        ({"gcode_file": "next.gcode.3mf"}, "The print changed; reopen Skip Objects"),
        ({"gcode_state": "FINISH"}, "Printer state: FINISH"),
        ({"gcode_state": "PREPARE"}, "Printer state: PREPARE"),
    ],
)
def test_printer_is_judged_again_after_the_slow_load(rig, change, message):
    seen = _get(rig).json()
    rig.storage.on_read = lambda: rig.raw.update(change)
    body = _body(seen)
    r = rig.client.post("/printers/p/skip_objects", json=body)
    assert r.status_code == 409
    assert r.json()["detail"]["message"] == message
    rig.service.send_raw.assert_not_awaited()


def test_telemetry_that_goes_stale_during_the_load_is_refused(rig):
    seen = _get(rig).json()
    rig.storage.on_read = lambda: rig.snapshot["session"].update(
        last_telemetry_at="2000-01-01T00:00:00Z"
    )
    body = _body(seen)
    assert rig.client.post("/printers/p/skip_objects", json=body).status_code == 409
    rig.service.send_raw.assert_not_awaited()


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
    seen = _get(rig).json()
    rig.raw.update(change)
    body = _body(seen)
    assert rig.client.post("/printers/p/skip_objects", json=body).status_code == 409
    rig.service.send_raw.assert_not_awaited()


def test_calibration_subtask_is_refused(rig):
    rig.storage.dirs[""]["flow_rate_coarse_calib_mode.gcode.3mf"] = MULTI3
    rig.raw.update(subtask_name="flow_rate_coarse_calib_mode", gcode_file="Metadata/plate_1.gcode")
    assert _get(rig).json()["reason"] == "Calibration prints cannot skip objects"
    assert _post(rig, [63]).status_code == 409
    rig.service.send_raw.assert_not_awaited()


def test_unlabelled_job_is_refused(rig):
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(MULTI3)) as src, zipfile.ZipFile(buf, "w") as dst:
        for name in src.namelist():
            body = src.read(name)
            if name == "Metadata/slice_info.config":
                body = body.replace(b'enabled" value="true"', b'enabled" value="false"')
            dst.writestr(name, body)
    rig.storage.dirs[""]["multi3.gcode.3mf"] = buf.getvalue()
    r = _post(rig, [63])
    assert r.status_code == 409
    assert r.json()["detail"]["message"] == "The current print job cannot be skipped"
    rig.service.send_raw.assert_not_awaited()


# --------------------------------------------------------------------------- #
# D. Plates
# --------------------------------------------------------------------------- #


def _two_plates() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(MULTI3)) as src, zipfile.ZipFile(buf, "w") as dst:
        for name in src.namelist():
            dst.writestr(name, src.read(name))
        dst.writestr("Metadata/plate_2.gcode", src.read("Metadata/plate_1.gcode"))
    return buf.getvalue()


def test_several_plates_and_no_name_is_refused(rig):
    rig.storage.dirs[""]["multi3.gcode.3mf"] = _two_plates()
    body = _get(rig).json()
    assert body["plate"] == 0 and body["objects"] == [] and body["map"] is None
    assert body["reason"] == core.UNKNOWN_PLATE
    assert _post(rig, [63]).status_code == 409
    rig.service.send_raw.assert_not_awaited()


def test_several_plates_named_by_plate_idx(rig):
    rig.storage.dirs[""]["multi3.gcode.3mf"] = _two_plates()
    rig.raw["plate_idx"] = 1
    body = _get(rig).json()
    assert body["plate"] == 1 and _ids(body) == MULTI3_IDS
    assert _post(rig, [63]).status_code == 200


# --------------------------------------------------------------------------- #
# Gate and body
# --------------------------------------------------------------------------- #


def test_withheld_without_operator_opt_in(rig, monkeypatch):
    seen = _get(rig).json()
    monkeypatch.delenv(ENABLE_ENV)
    body = _body(seen)
    r = rig.client.post("/printers/p/skip_objects", json=body)
    assert r.status_code == 409
    assert r.json()["detail"]["message"] == "Control support under review"
    rig.service.send_raw.assert_not_awaited()


def test_stale_telemetry_is_refused(rig):
    rig.snapshot["session"]["last_telemetry_at"] = "2000-01-01T00:00:00Z"
    assert _post(rig, [63]).status_code == 409
    rig.service.send_raw.assert_not_awaited()


def test_body_limits(rig):
    assert _post(rig, []).status_code == 422
    assert _post(rig, list(range(65))).status_code == 422
    assert _post(rig, [63], x=1).status_code == 422
    assert _post(rig, [63], action="maybe").status_code == 422
    missing = rig.client.post("/printers/p/skip_objects", json={"obj_list": [63], "action": "skip"})
    assert missing.status_code == 422
    rig.service.send_raw.assert_not_awaited()
