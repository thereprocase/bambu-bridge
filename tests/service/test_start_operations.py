"""Synthetic, independent-connection start admission and crash-boundary tests."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bambu_bridge.db.jobs import Database, EventRepo, JobRepo, Printer, PrinterRepo, QueueRepo
from bambu_bridge.db.starts import StartConflict, StartRepo
from bambu_bridge.service.events import Event, EventBus
from bambu_bridge.service.jobs import JobManager, JobRun

PAYLOAD = {"file_name": "fixture.gcode.3mf", "file_path": "/fixture.gcode.3mf", "ams_mapping": [1]}


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "starts.db"))
    await database.connect()
    await PrinterRepo(database).add(
        Printer(
            id="synthetic",
            friendly_name="Test",
            ip="127.0.0.1",
            access_code="fixture",
            added_at=1,
        )
    )
    yield database
    await database.close()


async def test_concurrent_same_key_replays_one_job(db):
    a, b = StartRepo(db), StartRepo(db)
    results = await asyncio.gather(*(r.claim("intent-1", "synthetic", PAYLOAD) for r in (a, b)))
    assert sum(created for _, created in results) == 1
    assert results[0][0]["job_id"] == results[1][0]["job_id"]
    assert len(await JobRepo(db).list()) == 1


async def test_concurrent_different_keys_admit_one(db):
    results = await asyncio.gather(
        StartRepo(db).claim("intent-1", "synthetic", PAYLOAD),
        StartRepo(db).claim("intent-2", "synthetic", PAYLOAD),
        return_exceptions=True,
    )
    assert sum(isinstance(r, StartConflict) for r in results) == 1
    assert len(await JobRepo(db).list()) == 1


async def test_reused_key_changed_input_conflicts(db):
    repo = StartRepo(db)
    await repo.claim("intent", "synthetic", PAYLOAD)
    with pytest.raises(StartConflict):
        await repo.claim("intent", "synthetic", {**PAYLOAD, "ams_mapping": [2]})


async def test_queue_claim_and_replay_survive_consumption(db):
    queue = QueueRepo(db)
    await queue.add(item_id="q", printer_id="synthetic", added_at=1, notes=None, **PAYLOAD)
    operation, created = await StartRepo(db).claim("queue-q", "synthetic", PAYLOAD, queue_id="q")
    assert created and await queue.get("q") is None
    replay, created = await StartRepo(db).claim("queue-q", "synthetic", PAYLOAD, queue_id="q")
    assert not created and replay["job_id"] == operation["job_id"]


async def test_dispatch_cas_is_at_most_once(db):
    repo = StartRepo(db)
    await repo.claim("intent", "synthetic", PAYLOAD)
    results = await asyncio.gather(
        *(repo.transition("intent", ("accepted",), "dispatching") for _ in range(2))
    )
    assert sorted(results) == [False, True]
    await repo.recover()
    assert (await repo.get("intent"))["state"] == "outcome_unknown"
    assert not await repo.transition("intent", ("accepted",), "dispatching")
    with pytest.raises(StartConflict):
        await repo.claim("copy-two", "synthetic", PAYLOAD)


async def test_restart_before_dispatch_cancels_without_replaying(db):
    repo = StartRepo(db)
    operation, _ = await repo.claim("intent", "synthetic", PAYLOAD)
    await repo.recover()
    assert (await repo.get("intent"))["holds_printer"] == 0
    assert (await JobRepo(db).get(operation["job_id"])).state == "canceled"
    replay, created = await repo.claim("intent", "synthetic", PAYLOAD)
    assert not created and replay["state"] == "canceled_before_dispatch"
    _, created = await repo.claim("explicit-copy-two", "synthetic", PAYLOAD)
    assert created


async def test_deleting_missing_queue_does_not_create_partial_job(db):
    with pytest.raises(StartConflict):
        await StartRepo(db).claim("queue-missing", "synthetic", PAYLOAD, queue_id="missing")
    assert not await JobRepo(db).list()
    assert await StartRepo(db).active("synthetic") is None


async def test_resolution_requires_current_unknown_revision_and_keeps_identity(db):
    repo = StartRepo(db)
    operation, _ = await repo.claim("intent", "synthetic", PAYLOAD)
    with pytest.raises(StartConflict):
        await repo.resolve_unknown("intent", operation["revision"])
    await repo.transition("intent", ("accepted",), "dispatching")
    await repo.recover()
    current = await repo.get("intent")
    with pytest.raises(StartConflict):
        await repo.resolve_unknown("intent", current["revision"] - 1)
    await repo.resolve_unknown("intent", current["revision"])
    replay, created = await repo.claim("intent", "synthetic", PAYLOAD)
    assert not created and replay["state"] == "resolved_unknown"
    assert (await JobRepo(db).get(operation["job_id"])).error_code == "tracking_resolved_unknown"
    _, created = await repo.claim("explicit-next-copy", "synthetic", PAYLOAD)
    assert created


async def test_owner_cannot_be_deleted_but_terminal_identity_survives(db):
    import aiosqlite

    repo = StartRepo(db)
    await repo.claim("intent", "synthetic", PAYLOAD)
    with pytest.raises(aiosqlite.IntegrityError, match="unresolved_start_owner"):
        await db.conn.execute("DELETE FROM printers WHERE id='synthetic'")
    await db.conn.rollback()
    await repo.recover()
    await db.conn.execute("DELETE FROM printers WHERE id='synthetic'")
    await db.conn.commit()
    replay, created = await repo.claim("intent", "synthetic", PAYLOAD)
    assert not created and replay["state"] == "canceled_before_dispatch"


async def make_run(db):
    starts = StartRepo(db)
    operation, _ = await starts.claim("intent", "synthetic", PAYLOAD)
    service = SimpleNamespace(connected=True, bus=EventBus(), send_command=AsyncMock())
    registry = SimpleNamespace(get=lambda _: service)
    run = JobRun(
        await JobRepo(db).get(operation["job_id"]),
        b"",
        JobRepo(db),
        EventRepo(db),
        registry,
        ftps_port=990,
        ams_mapping=[1],
        starts=starts,
        operation_id="intent",
    )
    return run, service, starts


async def test_cancel_cannot_stop_a_different_physical_session(db):
    run, service, starts = await make_run(db)
    await starts.transition("intent", ("accepted",), "observed_started")
    run._dispatch_started = True
    run._observed_session = "old-session"
    service.bus.session_id = "new-session"
    await run._do_cancel(service, acked=True)
    service.send_command.assert_not_awaited()
    assert (await starts.get("intent"))["state"] == "outcome_unknown"
    assert (await starts.get("intent"))["holds_printer"] == 1


async def test_stop_timeout_is_not_success_and_does_not_release(db, monkeypatch):
    run, service, starts = await make_run(db)
    await starts.transition("intent", ("accepted",), "observed_started")
    run._dispatch_started = True
    run._observed_session = service.bus.session_id = "session"
    monkeypatch.setattr(run, "_wait_signal", AsyncMock(return_value=None))
    await run._do_cancel(service, acked=True)
    service.send_command.assert_awaited_once()
    assert (await starts.get("intent"))["reason"] == "stop_not_confirmed"
    assert (await starts.get("intent"))["holds_printer"] == 1


async def test_old_start_event_is_ignored_after_publication_boundary(db):
    run, service, _ = await make_run(db)
    run._dispatch_started = True
    run._dispatch_at = 20.0
    service.bus.session_id = "session"

    async def events():
        yield Event(
            "event",
            {"subtask_name": "fixture"},
            name="print_started",
            session_id="session",
            observed_at=19.0,
        )

    await run._read_bus(events())
    assert run._signals.empty()
    assert run._observed_session is None


def test_idle_requires_recent_telemetry_not_only_an_open_socket():
    service = SimpleNamespace(connected=True)
    service.snapshot = lambda: {"phase": "idle", "session": {"connected": True}}
    with pytest.raises(StartConflict):
        JobManager.require_idle(service)
    service.snapshot = lambda: {
        "phase": "idle",
        "session": {"connected": True, "last_telemetry_at": datetime.now(UTC).isoformat()},
    }
    JobManager.require_idle(service)


async def test_native_start_keeps_settings_and_uses_managed_admission(db, monkeypatch):
    from bambu_bridge.slicedoc import project_file_command

    service = SimpleNamespace(serial="synthetic", bus=EventBus())
    manager = JobManager(JobRepo(db), EventRepo(db), SimpleNamespace(get=lambda _: service))
    start = AsyncMock()
    monkeypatch.setattr(manager, "start_stored", start)
    await manager.attach(service)
    fields = project_file_command(
        "fixture.gcode.3mf", use_ams=True, ams_mapping=[0], bed_leveling=False, timelapse=True
    )
    await service.start_handler({"command": "project_file", "sequence_id": "77", **fields})
    assert start.call_args.kwargs["command_fields"] == {**fields, "sequence_id": "77"}
    assert start.call_args.args[1:] == ("synthetic", "fixture.gcode.3mf", "/fixture.gcode.3mf", [0])
    with pytest.raises(StartConflict):
        await service.start_handler({**fields, "param": "Metadata/plate_2.gcode"})
    assert start.await_count == 1
    await manager.shutdown()
