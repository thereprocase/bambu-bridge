"""Skip Objects end to end, every way a print can start, on a real PrinterService.

A real PrinterService (legacy P1S reports fed to _handle_report) sits behind
the real skip and files routers, with a real NativeInbox wired through the
real NativeGateway guard, observer and local source (only ensure_idle,
recover_lost_start and expiry are stubbed). The SD card is a dict and MQTT a
recording mock: nothing contacts a printer. Ported from the round-4
verification rig.
"""

from __future__ import annotations

import asyncio
import contextvars
import io
import re
import secrets
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bambu_bridge.api import files as files_api
from bambu_bridge.api import skip_objects
from bambu_bridge.api.auth import require_auth, require_media_auth
from bambu_bridge.native_gateway import NativeGateway
from bambu_bridge.native_inbox import NativeInbox
from bambu_bridge.protocol.models import ReportMessage
from bambu_bridge.service.printer import PrinterService
from bambu_bridge.skip_objects import ENABLE_ENV
from bambu_bridge.slicedoc import project_file_command, sd_filename

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "orca"


def _cli_keys() -> bytes:
    """multi3 shaped like the owner's CLI slices: labelled G-code, no object lists."""
    buf = io.BytesIO()
    with zipfile.ZipFile(FIXTURES / "multi3.gcode.3mf") as src, zipfile.ZipFile(buf, "w") as dst:
        for name in src.namelist():
            body = src.read(name)
            if name == "Metadata/slice_info.config":
                body = re.sub(rb"\s*<object [^>]*/>", b"", body)
            elif name == "Metadata/model_settings.config":
                body = re.sub(rb"\s*<model_instance>.*?</model_instance>", b"", body, flags=re.S)
            dst.writestr(name, body)
    return buf.getvalue()


def _swapped() -> bytes:
    """A same-height (20 layer) re-slice of multi3 with the cube and frame ids swapped."""
    buf = io.BytesIO()
    swap = {b"63": b"85", b"85": b"63"}
    with zipfile.ZipFile(FIXTURES / "multi3.gcode.3mf") as src, zipfile.ZipFile(buf, "w") as dst:
        for name in src.namelist():
            body = src.read(name)
            if name in ("Metadata/plate_1.gcode", "Metadata/slice_info.config"):
                body = re.sub(rb"(?<=[ :=\",])(63|85)(?=[\",\n])", lambda m: swap[m.group(1)], body)
            dst.writestr(name, body)
    return buf.getvalue()


ARCHIVES = {
    "multi3": (FIXTURES / "multi3.gcode.3mf").read_bytes(),     # 63 74 85, 20 layers
    "sparse13": (FIXTURES / "sparse13.gcode.3mf").read_bytes(), # 45 56, 15 layers
    "keys": _cli_keys(),                                        # labels only, 20 layers
}
IDS = {"multi3": [63, 74, 85], "sparse13": [45, 56], "keys": [63, 74, 85]}
LAYERS = {"multi3": 20, "sparse13": 15, "keys": 20}
URL = "/printers/p/skip_objects"
IDENTITY = ("job", "gcode_file", "run_id", "plate", "digest")
BASE = {
    "s_obj": [],
    "print_type": "local",
    "info": {"module": [{"name": "ota", "product_name": "P1S"}]},
}


class FakeStorage:
    def __init__(self):
        self.dirs: dict[str, dict[str, bytes]] = {"": {}, "cache": {}}

    def transfer(self, *args, **kwargs):
        storage = self

        class Ftps:
            async def list_dir(self, remote_dir="", *, strict=False):
                return list(storage.dirs[remote_dir])

            async def upload_bytes(self, data, name, remote_dir=""):
                storage.dirs[remote_dir][name] = data
                return f"/{name}"

        return Ftps()

    async def revision(self, printer_id, ftps, remote_dir, filename):
        data = self.dirs[remote_dir][filename]
        return remote_dir, (len(data), str(hash(data)))   # SIZE/MDTM stand-in

    async def source_bytes(self, printer_id, ftps, remote_dir, filename):
        return self.dirs[remote_dir][filename]


def _gateway(app, service, tmp_path):
    gateway = object.__new__(NativeGateway)
    gateway.app = app
    gateway.inbox = NativeInbox(tmp_path / "inbox")
    gateway.config = {"printer_id": "p"}
    gateway.inbox_service = service
    gateway.inbox_dispatch_lock = asyncio.Lock()
    gateway.inbox_owner = contextvars.ContextVar("owner", default=None)
    gateway.inbox_cancel = contextvars.ContextVar("cancel", default=None)
    gateway.inbox_wake = asyncio.Event()
    gateway.inbox_expiry = set()
    gateway.inbox_status = []
    gateway.ensure_idle = AsyncMock()
    gateway.recover_lost_start = AsyncMock()
    gateway.arm_inbox_expiry = lambda: None
    service.command_guard = gateway.guard_inbox_command
    service.native_observer = gateway.observe_inbox
    return gateway


class Env(SimpleNamespace):
    def report(self, **fields):
        asyncio.run(self.service._handle_report(ReportMessage.model_validate({"print": fields})))

    def send(self, envelope, owner=None):
        async def go():
            token = self.gw.inbox_owner.set(owner) if (self.gw and owner) else None
            try:
                await self.service.send_raw(envelope)
            finally:
                if token is not None:
                    self.gw.inbox_owner.reset(token)

        asyncio.run(go())
        return envelope

    def published(self):
        calls = self.service._mqtt.publish.await_args_list
        return [c.args[0]["print"] for c in calls if "print" in c.args[0]]

    def ack(self, envelope, result="success"):
        body = envelope["print"]
        self.report(command=body["command"], sequence_id=body["sequence_id"], result=result,
                    param=body.get("param", ""))

    def reconnect(self):
        asyncio.run(self.service._handle_lost())
        asyncio.run(self.service._handle_connected())

    def get(self):
        return self.client.get(URL)

    def post(self, seen, ids, action="skip"):
        return self.client.post(
            URL, json={"obj_list": list(ids), "action": action, **{k: seen[k] for k in IDENTITY}}
        )


@pytest.fixture
def env(monkeypatch, tmp_path, request):
    monkeypatch.setenv(ENABLE_ENV, "1")
    service = PrinterService("SER", "127.0.0.1", "12345678", friendly_name="P1S", model="P1S",
                             mqtt_port=8883)
    service._mqtt.publish = AsyncMock()
    service._tofu_compare = AsyncMock()
    service._connected = True
    storage = FakeStorage()
    monkeypatch.setattr(skip_objects, "FtpsTransfer", storage.transfer)
    monkeypatch.setattr(files_api, "_ftps_for", lambda *a, **k: storage.transfer())
    app = FastAPI()
    app.state.registry = SimpleNamespace(get=lambda _: service)
    app.state.ftps_port = 990
    app.state.viz_cache_obj = SimpleNamespace(
        validate_revision=storage.revision, source_bytes=storage.source_bytes,
        invalidate=lambda *a, **k: None,
    )
    app.dependency_overrides[require_auth] = lambda: None
    app.dependency_overrides[require_media_auth] = lambda: None
    app.include_router(skip_objects.router)
    app.include_router(files_api.router)
    gateway = _gateway(app, service, tmp_path) if getattr(request, "param", True) else None
    app.state.native_gateway = gateway
    with TestClient(app) as client:
        yield Env(client=client, service=service, storage=storage, gw=gateway, app=app)


# --------------------------------------------------------------------------- #
# How a print starts
# --------------------------------------------------------------------------- #


def web_start(env, name, data):
    """POST /jobs or the queue: JobService uploads with sd_filename, then starts."""
    sd = sd_filename(name)
    env.storage.dirs[""][sd] = data
    fields = project_file_command(name, use_ams=True, ams_mapping=[0])
    envelope = {"print": {"command": "project_file",
                          "sequence_id": str(secrets.randbelow(10**6)), **fields}}
    env.send(envelope)
    return sd, fields["subtask_name"], envelope


def printhost_start(env, name, data):
    """Orca print-host: stored as name-<hex> through jobs.submit."""
    return web_start(env, name[: -len(".gcode.3mf")] + "-" + secrets.token_hex(4) + ".gcode.3mf",
                     data)


def native_start(env, name, data, url_form="ftp"):
    """Orca LAN print through the bridge: STOR into the inbox, deliver, start."""
    inbox = env.gw.inbox
    row = inbox.reserve("p", f"/{name}", len(data))

    async def receive():
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        return await inbox.receive(reader, row, len(data))

    asyncio.run(receive())
    subtask = name[: -len(".gcode.3mf")]
    url = f"ftp://{name}" if url_form == "ftp" else f"file:///sdcard/{name}"
    orca = {"print": {"command": "project_file", "param": "Metadata/plate_1.gcode", "url": url,
                      "subtask_name": subtask, "sequence_id": "20000", "use_ams": True,
                      "ams_mapping": [0]}}
    held = inbox.hold_start("p", orca)
    assert held is not None
    remote = held["remote"]
    inbox.transition(held["id"], "stored", "delivering", "BBDELIVERY_PENDING")
    env.storage.dirs[""][remote.lstrip("/")] = data
    inbox.transition(held["id"], "delivering", "delivered", "BBDELIVERY_OK")
    payload = inbox.claim_start(held["id"])
    env.send(payload, owner=held["id"])
    inbox.dispatched(held["id"], "sent")
    return remote.lstrip("/"), subtask, payload


def replay_start(env, data):
    """Library replay: a staged copy started through the inbox."""
    inbox = env.gw.inbox
    rid = uuid.uuid4().hex
    fields = project_file_command(f"replay-{rid}", use_ams=True, ams_mapping=[0])
    command = {"print": {"command": "project_file", "sequence_id": rid, **fields}}
    row = inbox.reserve("p", f"/replay-{rid}.gcode.3mf", len(data), replay_request_id=rid,
                        replay_command=command)

    async def receive():
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        return await inbox.receive(reader, row, len(data))

    asyncio.run(receive())
    inbox.queue_replay(rid)
    inbox.transition(rid, "stored", "delivering", "BBDELIVERY_PENDING")
    env.storage.dirs[""][row["remote"].lstrip("/")] = data
    inbox.transition(rid, "delivering", "delivered", "BBDELIVERY_OK")
    payload = inbox.claim_start(rid)
    env.send(payload, owner=rid)
    inbox.dispatched(rid, "sent")
    return row["remote"].lstrip("/"), fields["subtask_name"], payload


def card_start(env, name, data):
    """The printer's screen, or an Orca direct LAN print: the file is just on the card."""
    env.storage.dirs[""][name] = data
    return name, name[: -len(".gcode.3mf")], None


MODES = {
    "web": lambda e, fx: web_start(e, f"{fx}.gcode.3mf", ARCHIVES[fx]),
    "queue": lambda e, fx: web_start(e, f"{fx} copy.gcode.3mf", ARCHIVES[fx]),
    "printhost": lambda e, fx: printhost_start(e, f"{fx}.gcode.3mf", ARCHIVES[fx]),
    "native": lambda e, fx: native_start(e, f"{fx}.gcode.3mf", ARCHIVES[fx]),
    "native-sd-url": lambda e, fx: native_start(e, f"{fx}.gcode.3mf", ARCHIVES[fx], "sd"),
    "replay": lambda e, fx: replay_start(e, ARCHIVES[fx]),
    "card": lambda e, fx: card_start(e, f"{fx}.gcode.3mf", ARCHIVES[fx]),
}
NEEDS_GATEWAY = {"native", "native-sd-url", "replay"}


def _start_running(env, mode, fx, shape):
    env.report(msg=0, gcode_state="FINISH", subtask_name="old", gcode_file="old.gcode.3mf",
               total_layer_num=5, layer_num=5, mc_percent=100, **BASE)
    name, subtask, envelope = MODES[mode](env, fx)
    gcode_file = name if shape == "archive" else "Metadata/plate_1.gcode"
    if envelope is not None:
        env.ack(envelope)
    env.report(gcode_state="PREPARE", subtask_name=subtask, gcode_file=gcode_file,
               total_layer_num=LAYERS[fx], layer_num=0, mc_percent=0, s_obj=[])
    env.report(gcode_state="RUNNING", layer_num=1, mc_percent=1)
    env.report(layer_num=3, mc_percent=12)
    return subtask, gcode_file


CASES = [
    pytest.param(gw, mode, fx, shape, id=f"{mode}-{fx}-{shape}-{'gw' if gw else 'nogw'}")
    for mode in MODES
    for fx in ARCHIVES
    for shape in ("archive", "member")
    for gw in ((True,) if mode in NEEDS_GATEWAY else (True, False))
]


@pytest.mark.parametrize(("env", "mode", "fx", "shape"), CASES, indirect=["env"])
def test_every_start_mode_skips_then_stops(env, mode, fx, shape):
    ids = IDS[fx]
    subtask, gcode_file = _start_running(env, mode, fx, shape)
    r = env.get()
    assert r.status_code == 200, r.text
    seen = r.json()
    assert [o["id"] for o in seen["objects"]] == ids
    assert seen["map"] and seen["available"] is True, seen["reason"]
    assert env.client.get(f"{URL}/map.png", params={"digest": seen["digest"]}).status_code == 200
    first = ids[0]
    assert env.post(seen, [first]).status_code == 200
    assert env.published()[-1] == {**env.published()[-1], "command": "skip_objects",
                                   "obj_list": [first]}
    # Before the echo the sheet already shows it skipped (Orca's dirty filter).
    assert {o["id"]: o["skipped"] for o in env.get().json()["objects"]}[first] is True
    env.report(s_obj=[first], layer_num=4, mc_percent=15)
    seen = env.get().json()
    # A reconnect: the open sheet must reload, not act.
    env.reconnect()
    assert env.post(seen, ids[1:], "stop").status_code == 409
    assert env.published()[-1]["command"] != "stop"
    env.report(msg=0, gcode_state="RUNNING", subtask_name=subtask, gcode_file=gcode_file,
               total_layer_num=LAYERS[fx], layer_num=5, mc_percent=18, s_obj=[first],
               **{k: v for k, v in BASE.items() if k != "s_obj"})
    seen2 = env.get().json()
    assert seen2["run_id"] != seen["run_id"] and seen2["available"] is True, seen2["reason"]
    assert env.post(seen2, ids[1:], "stop").status_code == 200
    assert env.published()[-1]["command"] == "stop"


# --------------------------------------------------------------------------- #
# The pin follows the reported job (round 4 #2, #4, #10, #12)
# --------------------------------------------------------------------------- #


def _finish_sparse13(env):
    card_start(env, "sparse13.gcode.3mf", ARCHIVES["sparse13"])
    env.report(msg=0, gcode_state="RUNNING", subtask_name="sparse13",
               gcode_file="sparse13.gcode.3mf", total_layer_num=15, layer_num=14, **BASE)
    env.report(gcode_state="FINISH", layer_num=15)


def test_a_sheet_open_at_finish_does_not_poison_the_next_print(env):
    # A1: B goes PREPARE -> PAUSE -> RUNNING, never a fresh RUNNING edge.
    _finish_sparse13(env)
    env.get()
    _, subtask, start = web_start(env, "multi3.gcode.3mf", ARCHIVES["multi3"])
    env.ack(start)
    env.report(gcode_state="PREPARE", subtask_name=subtask, gcode_file="multi3.gcode.3mf",
               total_layer_num=20, layer_num=0, s_obj=[])
    env.report(gcode_state="PAUSE")
    env.report(gcode_state="RUNNING", layer_num=2)
    r = env.get()
    assert r.status_code == 200 and r.json()["available"] is True, r.text


def test_state_reported_before_identity(env):
    # A2: the P1S can split gcode_state and the job identity across reports.
    _finish_sparse13(env)
    env.report(gcode_state="IDLE")
    _, subtask, start = web_start(env, "multi3.gcode.3mf", ARCHIVES["multi3"])
    env.ack(start)
    env.report(gcode_state="RUNNING", layer_num=0)
    env.get()                                          # still names sparse13
    env.report(subtask_name=subtask, gcode_file="multi3.gcode.3mf", total_layer_num=20, s_obj=[])
    env.report(layer_num=1)
    r = env.get()
    assert r.status_code == 200 and r.json()["available"] is True, r.text


def test_a_read_in_the_reconnect_window_does_not_poison_the_next_print(env):
    # A3: the link drops; A finishes and B starts unseen.
    card_start(env, "sparse13.gcode.3mf", ARCHIVES["sparse13"])
    env.report(msg=0, gcode_state="RUNNING", subtask_name="sparse13",
               gcode_file="sparse13.gcode.3mf", total_layer_num=15, layer_num=14, **BASE)
    asyncio.run(env.service._handle_lost())
    card_start(env, "multi3.gcode.3mf", ARCHIVES["multi3"])
    asyncio.run(env.service._handle_connected())
    env.get()                                          # before the first report: not pinned
    env.report(msg=0, gcode_state="RUNNING", subtask_name="multi3", gcode_file="multi3.gcode.3mf",
               total_layer_num=20, layer_num=1, **BASE)
    r = env.get()
    assert r.status_code == 200 and r.json()["available"] is True, r.text


@pytest.mark.parametrize("reconnect", [False, True])
def test_an_overwrite_after_the_first_read_is_refused(env, reconnect):
    # Round 4 #1/#5: the pin survives a reconnect when the same print goes on.
    name, subtask, _ = card_start(env, "multi3.gcode.3mf", ARCHIVES["multi3"])
    env.report(msg=0, gcode_state="RUNNING", subtask_name=subtask, gcode_file=name,
               total_layer_num=20, layer_num=5, **BASE)
    assert env.get().status_code == 200
    env.storage.dirs[""][name] = _swapped()
    if reconnect:
        env.reconnect()
        env.report(msg=0, gcode_state="RUNNING", subtask_name=subtask, gcode_file=name,
                   total_layer_num=20, layer_num=6, **BASE)
    r = env.get()
    assert r.status_code == 409 and "changed during this print" in r.text


def test_pending_skips_survive_a_reconnect_within_the_same_print(env):
    # Round 4 #8: the last object must still become stop, as Orca would.
    name, subtask, _ = card_start(env, "multi3.gcode.3mf", ARCHIVES["multi3"])
    env.report(msg=0, gcode_state="RUNNING", subtask_name=subtask, gcode_file=name,
               total_layer_num=20, layer_num=7, **BASE)
    seen = env.get().json()
    assert env.post(seen, [63, 74]).status_code == 200            # not echoed yet
    env.reconnect()
    env.report(msg=0, gcode_state="RUNNING", subtask_name=subtask, gcode_file=name,
               total_layer_num=20, layer_num=7, **BASE)
    seen = env.get().json()
    assert [o["skipped"] for o in seen["objects"]] == [True, True, False]
    assert env.post(seen, [85], "skip").status_code == 409
    assert env.post(seen, [85], "stop").status_code == 200
    assert env.published()[-1]["command"] == "stop"


def test_orca_relayed_skip_is_pending(env):
    name, subtask, _ = card_start(env, "multi3.gcode.3mf", ARCHIVES["multi3"])
    env.report(msg=0, gcode_state="RUNNING", subtask_name=subtask, gcode_file=name,
               total_layer_num=20, layer_num=2, **BASE)
    seen = env.get().json()
    env.send({"print": {"command": "skip_objects", "obj_list": [63], "sequence_id": "77"}})
    assert env.post(seen, [74, 85], "skip").status_code == 409
    assert env.post(env.get().json(), [74, 85], "stop").status_code == 200


# --------------------------------------------------------------------------- #
# Writes over the printing file; bare names; multi-plate starts
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("upload", ["multi3.gcode.3mf", "MULTI3.gcode.3mf", "multi3.GCODE.3MF"])
def test_files_refuses_the_printing_file_in_any_case(env, upload):
    name, subtask, _ = card_start(env, "multi3.gcode.3mf", ARCHIVES["multi3"])
    env.report(msg=0, gcode_state="RUNNING", subtask_name=subtask, gcode_file=name,
               total_layer_num=20, layer_num=2, **BASE)
    sent = {"file": (upload, b"x", "application/octet-stream")}
    r = env.client.post("/printers/p/files", files=sent)
    assert r.status_code == 409
    assert env.storage.dirs[""][name] == ARCHIVES["multi3"]


def test_an_open_start_narrows_a_bare_name_to_its_folder(env):
    # Round 4 #14: a web start of root multi3 while /cache has an old namesake.
    env.storage.dirs["cache"]["multi3.gcode.3mf"] = ARCHIVES["sparse13"]
    _, subtask, start = web_start(env, "multi3.gcode.3mf", ARCHIVES["multi3"])
    env.ack(start)
    env.report(msg=0, gcode_state="RUNNING", subtask_name=subtask, gcode_file="multi3.gcode.3mf",
               total_layer_num=20, layer_num=2, **BASE)
    body = env.get().json()
    assert [o["id"] for o in body["objects"]] == [63, 74, 85]


@pytest.mark.parametrize("env", [False], indirect=True)
def test_without_an_open_start_a_bare_name_in_both_folders_refuses(env):
    env.storage.dirs["cache"]["multi3.gcode.3mf"] = ARCHIVES["sparse13"]
    name, subtask, _ = card_start(env, "multi3.gcode.3mf", ARCHIVES["multi3"])
    env.report(msg=0, gcode_state="RUNNING", subtask_name=subtask, gcode_file=name,
               total_layer_num=20, layer_num=2, **BASE)
    assert env.get().status_code == 409
