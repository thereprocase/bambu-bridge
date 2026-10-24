"""JobRun reconciles against the printer's current state (audit 2026-10-01).

Each scenario drives a fake printer that holds level state (print_view) and
publishes the bus events PrinterService would; the job must follow the
printer's state, not the event stream.
"""

from __future__ import annotations

import asyncio

import pytest

import bambu_bridge.service.jobs as J
from bambu_bridge.db.jobs import Job, JobState
from bambu_bridge.protocol.job_identity import job_lost
from bambu_bridge.service.events import Event, EventBus


class Repo:
    def __init__(s, job):
        s.job = job

    async def update(s, jid, **f):
        s.job = s.job.model_copy(update=f)
        return s.job

    async def get(s, jid):
        return s.job


class Ev:
    def __init__(s):
        s.rows = []

    async def add(s, **k):
        s.rows.append(k)


class Printer:
    """Fake PrinterService: level state + the bus events PrinterService would publish."""

    ip = "x"
    access_code = "y"

    def __init__(s, stop_exc=None, react=True):
        s.bus = EventBus()
        s.sent = []
        s.stop_exc = stop_exc
        s.react = react
        s.state = {
            "gcode_state": "IDLE",
            "gcode_file": "",
            "subtask_name": "",
            "layer_num": 0,
            "ams": {"tray_now": "255"},
        }
        s.fresh = True

    def print_view(s):
        return {
            "gcode_state": s.state["gcode_state"] if s.fresh else None,
            "layer_num": int(s.state.get("layer_num") or 0),
            "tray_now": s.state["ams"]["tray_now"],
            "lost": job_lost(s.state),
        }

    def push(s, **f):
        prev = s.state["gcode_state"]
        if "tray_now" in f:
            s.state["ams"] = {"tray_now": f.pop("tray_now")}
        s.state.update(f)
        s.bus.publish(Event("delta", dict(f)))
        cur = s.state["gcode_state"]
        if cur != prev and cur == "RUNNING" and prev in ("IDLE", "PREPARE", "FINISH", "FAILED"):
            s.bus.publish(Event("event", {}, name="print_started"))
        if cur != prev and cur == "FINISH":
            s.bus.publish(Event("event", {}, name="print_completed"))
        if cur != prev and cur == "FAILED":
            s.bus.publish(Event("event", {}, name="print_failed"))

    async def send_command(s, *a, **k):
        s.sent.append(a[:2])
        if a[:2] == ("print", "stop"):
            if s.stop_exc:
                raise s.stop_exc
            if s.react:
                s.push(gcode_state="FAILED")


class Reg:
    def __init__(s, svc):
        s.svc = svc

    def get(s, _):
        return s.svc


class FT:
    def __init__(s, *a, **k):
        pass

    async def upload_bytes(s, *a, **k):
        return "/x.gcode.3mf"


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(J, "FtpsTransfer", FT)


async def _run(printer, steps, *, feed=None):
    job = Job(id="j", printer_id="P", file_name="x.gcode.3mf", state=JobState.QUEUED, queued_at=1)
    repo = Repo(job)
    run = J.JobRun(
        job, b"x", repo, Ev(), Reg(printer), ftps_port=990, ams_mapping=None, feed_deadline_s=feed
    )
    run.start()
    await asyncio.sleep(0.05)
    for st in steps:
        await st(printer, run) if asyncio.iscoroutinefunction(st) else st(printer, run)
        await asyncio.sleep(0.03)
    await asyncio.sleep(0.4)
    done = run._task.done()
    await run.stop()
    return repo.job, done


def P(**f):  # noqa: N802 — a step: the printer reports these fields
    return lambda p, r: p.push(**f)


async def CANCEL(p, r):
    await r.request_cancel()


def DROP(p, r):
    p.fresh = False
    p.bus.publish(Event("event", {}, name="connection_lost"))


def RECONNECT(**f):
    def _(p, r):
        if "tray_now" in f:
            p.state["ams"] = {"tray_now": f.pop("tray_now")}
        p.state.update(f)
        p.fresh = True
        p.bus.publish(Event("event", {}, name="connection_restored"))
        p.bus.publish(Event("snapshot", dict(p.state)))

    return _


START = [
    P(gcode_state="RUNNING", subtask_name="x", gcode_file="x.gcode.3mf"),
    P(tray_now="1"),
    P(layer_num=1),
]


async def test_lost_print_interrupts_job():
    job, done = await _run(
        Printer(), START + [P(gcode_state="IDLE", gcode_file="", subtask_name="")]
    )
    assert job.state is JobState.INTERRUPTED and job.error_code == "printer_job_lost" and done


async def test_reconnect_during_prepare_tracks_and_completes():
    job, done = await _run(
        Printer(),
        [
            P(gcode_state="PREPARE", subtask_name="x"),
            DROP,
            RECONNECT(gcode_state="RUNNING", layer_num=3, tray_now="1"),
            P(layer_num=4),
            P(gcode_state="FINISH"),
        ],
    )
    assert job.state is JobState.COMPLETED and done


async def test_fed_armed_after_reconnect_into_running():
    job, done = await _run(
        Printer(), [P(gcode_state="PREPARE"), DROP, RECONNECT(gcode_state="RUNNING")], feed=0.2
    )
    assert job.state is JobState.FAILED and job.error_code == "FED_NO_PROGRESS" and done


async def test_prepare_has_no_deadline():
    job, done = await _run(Printer(), [P(gcode_state="PREPARE")], feed=0.05)
    assert job.state is JobState.PREPARING and not done


async def test_recoverable_pause_with_error_keeps_tracking():
    p = Printer()
    job, done = await _run(
        p,
        START
        + [
            P(gcode_state="PAUSE", print_error=50364435),
            lambda p, r: p.bus.publish(Event("event", {}, name="error")),
        ],
    )
    assert job.state is JobState.PRINTING and not done
    assert ("print", "stop") not in p.sent


async def test_resume_then_finish_completes():
    job, done = await _run(
        Printer(),
        START
        + [
            P(gcode_state="PAUSE", print_error=50364435),
            P(gcode_state="RUNNING", print_error=0),
            P(gcode_state="FINISH"),
        ],
    )
    assert job.state is JobState.COMPLETED


async def test_cancel_not_recorded_while_stop_undeliverable():
    job, done = await _run(Printer(stop_exc=ConnectionError("down")), START + [CANCEL])
    assert job.state is JobState.PRINTING and not done


async def test_cancel_retries_after_reconnect_then_confirms():
    p = Printer(stop_exc=ConnectionError("down"))

    def heal(p, r):
        p.stop_exc = None

    job, done = await _run(p, START + [CANCEL, DROP, heal, RECONNECT()])
    assert job.state is JobState.CANCELED and done


async def test_unconfirmed_stop_is_not_canceled():
    job, done = await _run(Printer(react=False), START + [CANCEL])
    assert job.state is JobState.PRINTING and not done


async def test_cancel_confirmed_is_canceled():
    job, done = await _run(Printer(), START + [CANCEL])
    assert job.state is JobState.CANCELED and done


async def test_normal_completion():
    job, done = await _run(Printer(), START + [P(gcode_state="FINISH")])
    assert job.state is JobState.COMPLETED and done


async def test_stale_finish_before_ack_ignored_then_running():
    p = Printer()
    p.state["gcode_state"] = "FINISH"
    job, done = await _run(p, START + [P(gcode_state="FINISH")])
    assert job.state is JobState.COMPLETED


async def test_prepare_then_idle_fails():
    job, done = await _run(Printer(), [P(gcode_state="PREPARE"), P(gcode_state="IDLE")])
    assert job.state is JobState.FAILED and job.error_code == "printer_error"


async def test_fed_no_progress_stops_and_fails():
    p = Printer()
    job, done = await _run(p, [P(gcode_state="RUNNING")], feed=0.1)
    assert ("print", "stop") in p.sent and job.state is JobState.FAILED
    assert job.error_code == "FED_NO_PROGRESS"


async def test_stale_engaged_tray_does_not_satisfy_fed():
    p = Printer()
    p.state["ams"] = {"tray_now": "1"}
    job, done = await _run(p, [P(gcode_state="RUNNING")], feed=0.1)
    assert job.error_code == "FED_NO_PROGRESS"


async def test_unconfirmed_fed_stop_does_not_spin():
    """FED fires once; with the printer ignoring the stop the job stays live, idle."""
    p = Printer(react=False)
    job, done = await _run(p, [P(gcode_state="RUNNING")], feed=0.05)
    assert p.sent.count(("print", "stop")) == 1
    assert job.state is JobState.PREPARING and not done


async def test_cancel_during_upload_never_starts_the_print(monkeypatch):
    gate = asyncio.Event()

    class SlowFtps(FT):
        async def upload_bytes(s, *a, **k):
            await gate.wait()
            return "/x.gcode.3mf"

    monkeypatch.setattr(J, "FtpsTransfer", SlowFtps)
    p = Printer()

    async def release(p, r):
        gate.set()

    job, done = await _run(p, [CANCEL, release])
    assert job.state is JobState.CANCELED and done
    assert ("print", "project_file") not in p.sent


async def test_use_ams_follows_mapping(monkeypatch):
    captured = {}
    import bambu_bridge.service.jobs as mod

    real = mod.project_file_command

    def spy(name, **k):
        captured.update(k)
        return real(name, **k)

    monkeypatch.setattr(mod, "project_file_command", spy)
    job = Job(id="j", printer_id="P", file_name="x.gcode.3mf", state=JobState.QUEUED, queued_at=1)
    run = J.JobRun(job, b"x", Repo(job), Ev(), Reg(Printer()), ftps_port=990, ams_mapping=[-1])
    run.start()
    await asyncio.sleep(0.1)
    await run.stop()
    assert captured["use_ams"] is False
