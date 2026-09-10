"""External-print row creation (screen/SD/Bambu-Studio-direct prints).

When the bridge witnesses a ``print_started`` bus event for a printer that has
no live job row (submitted/preparing/printing), it must insert an "external"
row so restart-recovery works for all prints, not just bridge-submitted ones.

Contract invariants tested here:
1. ``print_started`` with no live row → exactly one row, state=printing,
   origin=external, started_at from event payload.
2. ``print_started`` when a bridge-submitted live row already exists → no
   duplicate row created.
3. Restart-recovery integration: the external row's started_at is recoverable
   via JobRepo.latest_active_started_at → PrinterService._print_started_at
   recovers on seed.
4. ``print_completed`` for the external row → row closes to completed.
5. ``print_failed`` for the external row → row closes to failed.

Additional invariants (gap-fill):
6. Empty-string subtask_name falls back to "external-print" (same as absent).
7. Absent started_at (key not present) falls back to now().
8. state_change EventRecord is written on completion.
9. state_change EventRecord is written on failure.
10. A terminal (completed) job in the list does not prevent external row
    creation when the only non-terminal row would appear after limit=1 ordering.
11. Two rapid ``print_started`` events create exactly one row (idempotency
    within one watcher iteration; second fires while first DB write is pending).
12. Completed external row is not re-closed by a subsequent print_failed.
13. Restart-recovery works when printer is PAUSED at seed time (PAUSE is in
    _ACTIVE_ON_SEED) — the external row's started_at is still recovered.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from bambu_bridge.db.jobs import (
    Database,
    EventRepo,
    Job,
    JobRepo,
    JobState,
    Printer,
    PrinterRepo,
)
from bambu_bridge.service.events import Event, EventBus
from bambu_bridge.service.jobs import JobManager
from bambu_bridge.service.printer import PrinterService
from bambu_bridge.service.registry import Registry
from tests.conftest import ACCESS_CODE, SERIAL, MockPrinter

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _seed_printer(db: Database) -> None:
    await PrinterRepo(db).add(
        Printer(
            id=SERIAL,
            friendly_name="P",
            ip="127.0.0.1",
            access_code=ACCESS_CODE,
            added_at=1,
        )
    )


async def _next_event(sub: Any, predicate: Any, timeout: float = 5.0) -> Event:
    async with asyncio.timeout(timeout):
        while True:
            ev = await sub.get()
            if predicate(ev):
                return ev


def _make_print_started_event(
    subtask_name: str = "benchy",
    started_at: str = "2026-06-11T10:00:00Z",
) -> Event:
    return Event(
        "event",
        {"subtask_name": subtask_name, "started_at": started_at},
        name="print_started",
    )


def _make_print_completed_event() -> Event:
    return Event("event", {"subtask_name": "benchy"}, name="print_completed")


def _make_print_failed_event() -> Event:
    return Event("event", {"print_error": None, "layer_num": 5}, name="print_failed")


class _FakePrinterService:
    """Minimal stand-in for PrinterService: exposes a live EventBus and serial."""

    def __init__(self, serial: str) -> None:
        self.serial = serial
        self.bus = EventBus()


class _FakeRegistry:
    """Minimal Registry stand-in — JobManager only calls .get() for submissions."""

    def get(self, serial: str) -> None:  # noqa: ARG002
        raise RuntimeError("should not be called in external-print tests")


# --------------------------------------------------------------------------- #
# Unit tests — pure JobManager logic, no live MQTT
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_external_print_started_creates_row(database: Database) -> None:
    """print_started with no live row → exactly one external job row."""
    await _seed_printer(database)
    jobs = JobRepo(database)
    events = EventRepo(database)
    manager = JobManager(jobs, events, _FakeRegistry())  # type: ignore[arg-type]

    svc = _FakePrinterService(SERIAL)
    await manager.attach(svc)  # type: ignore[arg-type]

    # Give the watcher task a chance to subscribe before we publish.
    await asyncio.sleep(0)

    svc.bus.publish(_make_print_started_event("my-print", "2026-06-11T10:00:00Z"))

    # Allow the coroutine to process the event.
    await asyncio.sleep(0.1)

    rows = await jobs.list(printer_id=SERIAL)
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    row = rows[0]

    assert row.state is JobState.PRINTING
    assert row.file_name == "my-print"
    assert row.printer_id == SERIAL
    # started_at must come from the event payload epoch, not now().
    expected_epoch = int(
        datetime(2026, 6, 11, 10, 0, 0, tzinfo=UTC).timestamp()
    )
    assert row.started_at == expected_epoch
    assert row.queued_at == expected_epoch

    # Metadata must mark the row as external.
    assert row.metadata_json is not None
    meta = json.loads(row.metadata_json)
    assert meta.get("origin") == "external"

    await manager.shutdown()


@pytest.mark.asyncio
async def test_external_print_started_fallback_file_name(
    database: Database,
) -> None:
    """subtask_name absent or empty → file_name defaults to 'external-print'."""
    await _seed_printer(database)
    jobs = JobRepo(database)
    manager = JobManager(jobs, EventRepo(database), _FakeRegistry())  # type: ignore[arg-type]

    svc = _FakePrinterService(SERIAL)
    await manager.attach(svc)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    # No subtask_name in payload
    svc.bus.publish(
        Event("event", {"started_at": "2026-06-11T10:00:00Z"}, name="print_started")
    )
    await asyncio.sleep(0.1)

    rows = await jobs.list(printer_id=SERIAL)
    assert len(rows) == 1
    assert rows[0].file_name == "external-print"

    await manager.shutdown()


@pytest.mark.asyncio
async def test_external_print_started_fallback_started_at(
    database: Database,
) -> None:
    """Unparseable started_at in payload → fallback to now() (non-None epoch)."""
    await _seed_printer(database)
    jobs = JobRepo(database)
    manager = JobManager(jobs, EventRepo(database), _FakeRegistry())  # type: ignore[arg-type]

    svc = _FakePrinterService(SERIAL)
    await manager.attach(svc)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    before = int(time.time())
    svc.bus.publish(
        Event(
            "event",
            {"subtask_name": "x", "started_at": "not-a-date"},
            name="print_started",
        )
    )
    await asyncio.sleep(0.1)
    after = int(time.time())

    rows = await jobs.list(printer_id=SERIAL)
    assert len(rows) == 1
    assert rows[0].started_at is not None
    assert before <= rows[0].started_at <= after + 1

    await manager.shutdown()


@pytest.mark.asyncio
async def test_no_duplicate_when_bridge_submitted_job_live(
    database: Database,
) -> None:
    """print_started when a bridge-submitted live row already exists → no duplicate."""
    await _seed_printer(database)
    jobs = JobRepo(database)
    events = EventRepo(database)
    manager = JobManager(jobs, events, _FakeRegistry())  # type: ignore[arg-type]

    # Pre-insert a bridge-submitted job in a live state.
    existing = Job(
        id="bridge-job-001",
        printer_id=SERIAL,
        file_name="bridge.3mf",
        state=JobState.PREPARING,
        queued_at=1000,
        started_at=1000,
    )
    await jobs.create(existing)

    svc = _FakePrinterService(SERIAL)
    await manager.attach(svc)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    svc.bus.publish(_make_print_started_event())
    await asyncio.sleep(0.1)

    rows = await jobs.list(printer_id=SERIAL)
    # Still exactly one row — the pre-existing bridge row, no external added.
    assert len(rows) == 1
    assert rows[0].id == "bridge-job-001"

    await manager.shutdown()


@pytest.mark.asyncio
async def test_no_duplicate_when_submitted_state_live(database: Database) -> None:
    """SUBMITTED state is also a live state — no external row created."""
    await _seed_printer(database)
    jobs = JobRepo(database)
    manager = JobManager(jobs, EventRepo(database), _FakeRegistry())  # type: ignore[arg-type]

    existing = Job(
        id="bridge-j2",
        printer_id=SERIAL,
        file_name="x.3mf",
        state=JobState.SUBMITTED,
        queued_at=1000,
        started_at=1000,
    )
    await jobs.create(existing)

    svc = _FakePrinterService(SERIAL)
    await manager.attach(svc)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    svc.bus.publish(_make_print_started_event())
    await asyncio.sleep(0.1)

    rows = await jobs.list(printer_id=SERIAL)
    assert len(rows) == 1
    assert rows[0].id == "bridge-j2"

    await manager.shutdown()


@pytest.mark.asyncio
async def test_external_print_completed_closes_row(database: Database) -> None:
    """print_completed after an external row → state=completed, finished_at set."""
    await _seed_printer(database)
    jobs = JobRepo(database)
    manager = JobManager(jobs, EventRepo(database), _FakeRegistry())  # type: ignore[arg-type]

    svc = _FakePrinterService(SERIAL)
    await manager.attach(svc)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    svc.bus.publish(_make_print_started_event("x", "2026-06-11T09:00:00Z"))
    await asyncio.sleep(0.1)

    rows = await jobs.list(printer_id=SERIAL)
    assert len(rows) == 1
    assert rows[0].state is JobState.PRINTING

    svc.bus.publish(_make_print_completed_event())
    await asyncio.sleep(0.1)

    rows = await jobs.list(printer_id=SERIAL)
    assert len(rows) == 1
    row = rows[0]
    assert row.state is JobState.COMPLETED
    assert row.finished_at is not None
    assert row.duration_s is not None and row.duration_s >= 0
    assert row.progress_pct == 100.0

    await manager.shutdown()


@pytest.mark.asyncio
async def test_external_print_failed_closes_row(database: Database) -> None:
    """print_failed after an external row → state=failed, error_code set."""
    await _seed_printer(database)
    jobs = JobRepo(database)
    manager = JobManager(jobs, EventRepo(database), _FakeRegistry())  # type: ignore[arg-type]

    svc = _FakePrinterService(SERIAL)
    await manager.attach(svc)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    svc.bus.publish(_make_print_started_event())
    await asyncio.sleep(0.1)

    svc.bus.publish(_make_print_failed_event())
    await asyncio.sleep(0.1)

    rows = await jobs.list(printer_id=SERIAL)
    assert len(rows) == 1
    row = rows[0]
    assert row.state is JobState.FAILED
    assert row.finished_at is not None
    assert row.error_code == "printer_error"

    await manager.shutdown()


@pytest.mark.asyncio
async def test_completion_does_not_close_bridge_submitted_row(
    database: Database,
) -> None:
    """print_completed must NOT close a bridge-submitted (non-external) row.

    Bridge rows carry no metadata_json origin tag; they're managed by JobRun.
    The watcher must leave them alone.
    """
    await _seed_printer(database)
    jobs = JobRepo(database)
    manager = JobManager(jobs, EventRepo(database), _FakeRegistry())  # type: ignore[arg-type]

    bridge_row = Job(
        id="bridge-j3",
        printer_id=SERIAL,
        file_name="x.3mf",
        state=JobState.PRINTING,
        queued_at=1000,
        started_at=1000,
    )
    await jobs.create(bridge_row)

    svc = _FakePrinterService(SERIAL)
    await manager.attach(svc)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    svc.bus.publish(_make_print_completed_event())
    await asyncio.sleep(0.1)

    row = await jobs.get("bridge-j3")
    assert row is not None
    # JobRun would normally close it; here we verify the watcher left it alone.
    assert row.state is JobState.PRINTING

    await manager.shutdown()


@pytest.mark.asyncio
async def test_external_print_started_empty_subtask_fallback(
    database: Database,
) -> None:
    """Empty-string subtask_name is falsy and falls back to 'external-print'.

    The code does ``ev.data.get("subtask_name") or "external-print"``.  An
    empty string is a distinct input from a missing key — both must produce
    the same fallback.  (Gap #6: only the absent case was tested before.)
    """
    await _seed_printer(database)
    jobs = JobRepo(database)
    manager = JobManager(jobs, EventRepo(database), _FakeRegistry())  # type: ignore[arg-type]

    svc = _FakePrinterService(SERIAL)
    await manager.attach(svc)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    svc.bus.publish(
        Event(
            "event",
            {"subtask_name": "", "started_at": "2026-06-11T10:00:00Z"},
            name="print_started",
        )
    )
    await asyncio.sleep(0.1)

    rows = await jobs.list(printer_id=SERIAL)
    assert len(rows) == 1
    assert rows[0].file_name == "external-print"

    await manager.shutdown()


@pytest.mark.asyncio
async def test_external_print_started_absent_started_at_fallback(
    database: Database,
) -> None:
    """Payload with no started_at key at all falls back to now().

    The existing fallback test only covers an unparseable *string*.  This
    covers the distinct branch where the key is not present (returns None,
    which is not a str so the ``isinstance(raw_started, str)`` guard skips
    straight to the fallback).  (Gap #7.)
    """
    await _seed_printer(database)
    jobs = JobRepo(database)
    manager = JobManager(jobs, EventRepo(database), _FakeRegistry())  # type: ignore[arg-type]

    svc = _FakePrinterService(SERIAL)
    await manager.attach(svc)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    before = int(time.time())
    svc.bus.publish(Event("event", {"subtask_name": "my-print"}, name="print_started"))
    await asyncio.sleep(0.1)
    after = int(time.time())

    rows = await jobs.list(printer_id=SERIAL)
    assert len(rows) == 1
    assert rows[0].started_at is not None
    assert before <= rows[0].started_at <= after + 1

    await manager.shutdown()


@pytest.mark.asyncio
async def test_external_print_completed_writes_state_change_event(
    database: Database,
) -> None:
    """print_completed must write a state_change EventRecord in the events table.

    The code calls EventRepo.add() on close-out; previously only the job-row
    state was asserted, not the event log entry.  (Gap #8.)
    """
    await _seed_printer(database)
    jobs = JobRepo(database)
    events = EventRepo(database)
    manager = JobManager(jobs, events, _FakeRegistry())  # type: ignore[arg-type]

    svc = _FakePrinterService(SERIAL)
    await manager.attach(svc)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    svc.bus.publish(_make_print_started_event("x", "2026-06-11T09:00:00Z"))
    await asyncio.sleep(0.1)

    rows = await jobs.list(printer_id=SERIAL)
    assert len(rows) == 1
    job_id = rows[0].id

    svc.bus.publish(_make_print_completed_event())
    await asyncio.sleep(0.1)

    event_log = await events.list_for_job(job_id)
    # Expect two records: job_created (on insert) + state_change (on close-out).
    types = [e.event_type for e in event_log]
    assert "state_change" in types, f"state_change not found; events={types}"
    sc = next(e for e in event_log if e.event_type == "state_change")
    assert sc.payload.get("to") == JobState.COMPLETED.value
    assert sc.payload.get("trigger") == "gcode_finish"

    await manager.shutdown()


@pytest.mark.asyncio
async def test_external_print_failed_writes_state_change_event(
    database: Database,
) -> None:
    """print_failed must write a state_change EventRecord in the events table.

    Mirrors the completion case above for the failure branch.  (Gap #9.)
    """
    await _seed_printer(database)
    jobs = JobRepo(database)
    events = EventRepo(database)
    manager = JobManager(jobs, events, _FakeRegistry())  # type: ignore[arg-type]

    svc = _FakePrinterService(SERIAL)
    await manager.attach(svc)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    svc.bus.publish(_make_print_started_event())
    await asyncio.sleep(0.1)

    rows = await jobs.list(printer_id=SERIAL)
    job_id = rows[0].id

    svc.bus.publish(_make_print_failed_event())
    await asyncio.sleep(0.1)

    event_log = await events.list_for_job(job_id)
    types = [e.event_type for e in event_log]
    assert "state_change" in types, f"state_change not found; events={types}"
    sc = next(e for e in event_log if e.event_type == "state_change")
    assert sc.payload.get("to") == JobState.FAILED.value
    assert sc.payload.get("trigger") == "printer_error"

    await manager.shutdown()


@pytest.mark.asyncio
async def test_terminal_job_does_not_shadow_live_job_from_newer_queued(
    database: Database,
) -> None:
    """A COMPLETED row with a higher queued_at than a live row must not prevent
    external row creation.

    ``_maybe_create_external_job`` fetches ``limit=1`` ordered by
    ``queued_at DESC``.  If the most-recently-queued row is terminal
    (COMPLETED/FAILED) and the live row has an older timestamp, the list
    query returns only the terminal row — ``any(j.state in _LIVE_STATES)``
    is False — and an external row would be wrongly created.  This test pins
    the correct invariant: the bridge must NOT duplicate when a live row exists
    regardless of ordering.  (Gap #10 / latent bug probe.)

    Implementation note: the current code uses ``limit=1``, so this test
    *will expose a bug if one exists* — treat a failure here as a blocker.
    """
    await _seed_printer(database)
    jobs = JobRepo(database)
    manager = JobManager(jobs, EventRepo(database), _FakeRegistry())  # type: ignore[arg-type]

    # Live PREPARING row — older timestamp.
    live_row = Job(
        id="live-job",
        printer_id=SERIAL,
        file_name="x.3mf",
        state=JobState.PREPARING,
        queued_at=500,
        started_at=500,
    )
    await jobs.create(live_row)

    # Completed row — newer timestamp; will be top of ``queued_at DESC`` list.
    completed_row = Job(
        id="done-job",
        printer_id=SERIAL,
        file_name="prev.3mf",
        state=JobState.COMPLETED,
        queued_at=1000,
        started_at=500,
        finished_at=800,
    )
    await jobs.create(completed_row)

    svc = _FakePrinterService(SERIAL)
    await manager.attach(svc)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    svc.bus.publish(_make_print_started_event())
    await asyncio.sleep(0.1)

    rows = await jobs.list(printer_id=SERIAL)
    # Must still be exactly two rows — the pre-existing ones; no external added.
    assert len(rows) == 2, (
        f"expected 2 rows (no external duplicate), got {len(rows)}: {rows}"
    )
    ids = {r.id for r in rows}
    assert ids == {"live-job", "done-job"}

    await manager.shutdown()


@pytest.mark.asyncio
async def test_two_rapid_print_started_events_create_exactly_one_row(
    database: Database,
) -> None:
    """Two ``print_started`` events fired back-to-back create exactly one row.

    The watcher processes events sequentially (single asyncio task), so the
    second event runs only after the first DB write completes.  That means the
    second ``_maybe_create_external_job`` call will find one live row and bail.
    This test locks in that idempotency guarantee.  (Gap #11.)
    """
    await _seed_printer(database)
    jobs = JobRepo(database)
    manager = JobManager(jobs, EventRepo(database), _FakeRegistry())  # type: ignore[arg-type]

    svc = _FakePrinterService(SERIAL)
    await manager.attach(svc)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    svc.bus.publish(_make_print_started_event("p1", "2026-06-11T10:00:00Z"))
    svc.bus.publish(_make_print_started_event("p2", "2026-06-11T10:00:01Z"))
    # Give the single watcher task enough time to drain both events.
    await asyncio.sleep(0.3)

    rows = await jobs.list(printer_id=SERIAL)
    assert len(rows) == 1, f"expected 1 row (idempotent), got {len(rows)}: {rows}"
    assert rows[0].file_name == "p1"  # first event wins

    await manager.shutdown()


@pytest.mark.asyncio
async def test_print_failed_after_completed_does_not_reopen_row(
    database: Database,
) -> None:
    """A ``print_failed`` arriving after the external row is already COMPLETED
    must leave the row untouched.

    The close-out guard checks ``job.state not in _LIVE_STATES``; COMPLETED is
    terminal so the row is skipped.  This test pins that COMPLETED rows are
    immune to a late failure signal.  (Gap #12.)
    """
    await _seed_printer(database)
    jobs = JobRepo(database)
    manager = JobManager(jobs, EventRepo(database), _FakeRegistry())  # type: ignore[arg-type]

    svc = _FakePrinterService(SERIAL)
    await manager.attach(svc)  # type: ignore[arg-type]
    await asyncio.sleep(0)

    svc.bus.publish(_make_print_started_event())
    await asyncio.sleep(0.1)

    svc.bus.publish(_make_print_completed_event())
    await asyncio.sleep(0.1)

    rows = await jobs.list(printer_id=SERIAL)
    assert rows[0].state is JobState.COMPLETED

    # Late failure signal — must not re-close the completed row.
    svc.bus.publish(_make_print_failed_event())
    await asyncio.sleep(0.1)

    rows = await jobs.list(printer_id=SERIAL)
    assert len(rows) == 1
    assert rows[0].state is JobState.COMPLETED  # unchanged

    await manager.shutdown()


# --------------------------------------------------------------------------- #
# Restart-recovery integration: external row → PrinterService._print_started_at
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_restart_recovery_works_for_external_row(
    mqtt_broker: int, mock_printer: MockPrinter, database: Database
) -> None:
    """The external row's started_at is recoverable via jobs.db after a restart.

    Sequence:
    1. Create an external job row in PRINTING state (simulating what
       _maybe_create_external_job would have written in the prior process).
    2. Instantiate PrinterService with recover_started_at wired to JobRepo.
    3. Seed fires (mock_printer seeds RUNNING) → recovery hook is called →
       PrinterService._print_started_at is set from the row's started_at.
    """
    await _seed_printer(database)
    jobs = JobRepo(database)

    # The epoch the "prior process" stamped — fixed so we can assert equality.
    started_epoch = int(datetime(2026, 6, 11, 8, 30, 0, tzinfo=UTC).timestamp())

    # Insert the external row as if the prior bridge process had created it.
    external_row = Job(
        id="ext-row-001",
        printer_id=SERIAL,
        file_name="screen-print",
        state=JobState.PRINTING,
        queued_at=started_epoch,
        started_at=started_epoch,
        metadata_json=json.dumps({"origin": "external"}),
    )
    await jobs.create(external_row)

    async def _recover() -> datetime | None:
        epoch = await jobs.latest_active_started_at(SERIAL)
        if epoch is None:
            return None
        return datetime.fromtimestamp(epoch, tz=UTC)

    service = PrinterService(
        SERIAL,
        "127.0.0.1",
        ACCESS_CODE,
        friendly_name="Workshop P1S",
        model="P1S",
        mqtt_port=mqtt_broker,
        recover_started_at=_recover,
    )
    async with service.bus.subscribe() as sub:
        await service.start()
        try:
            # mock_printer seeds RUNNING → _maybe_recover_started_at fires.
            async with asyncio.timeout(10.0):
                while True:
                    ev = await sub.get()
                    if ev.type == "snapshot":
                        break
            # The snapshot must expose the recovered started_at, not None.
            snap = service.snapshot()
            assert snap["job"]["started_at"] == "2026-06-11T08:30:00Z"
        finally:
            await service.stop()


@pytest.mark.asyncio
async def test_restart_recovery_works_when_paused_at_seed(
    mqtt_broker: int, database: Database
) -> None:
    """Restart-recovery works when the printer is PAUSED at seed time.

    ``_ACTIVE_ON_SEED`` includes both RUNNING and PAUSE, so a mid-print deploy
    where the operator had paused the job should still recover started_at.
    The existing RUNNING-seed test covers RUNNING; this test covers PAUSE.
    (Gap #13.)
    """
    # Build a custom MockPrinter that seeds PAUSE instead of RUNNING.
    pause_report: dict[str, Any] = {
        "print": {
            "command": "push_status",
            "sequence_id": "0",
            "gcode_state": "PAUSE",
            "mc_percent": 55,
            "subtask_name": "paused-job",
        }
    }

    await _seed_printer(database)
    jobs = JobRepo(database)

    started_epoch = int(datetime(2026, 6, 11, 7, 0, 0, tzinfo=UTC).timestamp())
    external_row = Job(
        id="ext-paused-001",
        printer_id=SERIAL,
        file_name="paused-job",
        state=JobState.PRINTING,
        queued_at=started_epoch,
        started_at=started_epoch,
        metadata_json=json.dumps({"origin": "external"}),
    )
    await jobs.create(external_row)

    async def _recover() -> datetime | None:
        epoch = await jobs.latest_active_started_at(SERIAL)
        if epoch is None:
            return None
        return datetime.fromtimestamp(epoch, tz=UTC)

    # Use a fresh MockPrinter that seeds the PAUSE report.
    pause_mock = MockPrinter("127.0.0.1", mqtt_broker, pause_report)
    await pause_mock.start()
    try:
        service = PrinterService(
            SERIAL,
            "127.0.0.1",
            ACCESS_CODE,
            friendly_name="Workshop P1S",
            model="P1S",
            mqtt_port=mqtt_broker,
            recover_started_at=_recover,
        )
        async with service.bus.subscribe() as sub:
            await service.start()
            try:
                async with asyncio.timeout(10.0):
                    while True:
                        ev = await sub.get()
                        if ev.type == "snapshot":
                            break
                snap = service.snapshot()
                assert snap["job"]["started_at"] == "2026-06-11T07:00:00Z", (
                    f"PAUSE-seed recovery failed; got started_at={snap['job']['started_at']}"
                )
            finally:
                await service.stop()
    finally:
        await pause_mock.stop()


# --------------------------------------------------------------------------- #
# Integration: live MQTT path — external print_started creates a row
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_live_external_print_creates_row_via_mqtt(
    mqtt_broker: int, printing_mock: MockPrinter, database: Database
) -> None:
    """End-to-end: a printer that starts a print (no bridge-submitted job) →
    the watcher creates the external row and starts recovery works.

    Uses a real PrinterService + MockPrinter (IDLE-seed, simulate_print=True).
    The JobManager is wired as a listener and the printer bus is live.
    """
    await _seed_printer(database)
    jobs_repo = JobRepo(database)
    events_repo = EventRepo(database)

    # Build a Registry with just the one printer (no auto-load from DB here —
    # we construct PrinterService directly and attach the manager manually).
    registry = Registry(PrinterRepo(database), mqtt_port=mqtt_broker)
    await registry.load()  # loads the printer row and starts the MQTT task

    manager = JobManager(jobs_repo, events_repo, registry)
    service = registry.get(SERIAL)
    await manager.attach(service)

    try:
        # Wait for the PrinterService to seed (IDLE snapshot).
        async with service.bus.subscribe() as sub:
            async with asyncio.timeout(10.0):
                while True:
                    ev = await sub.get()
                    if ev.type == "snapshot":
                        break

            # Trigger a print via the mock (simulates screen-start by pushing
            # RUNNING without a bridge-submitted job row existing).
            await service.send_command("print", "project_file", param="x.3mf")

            # Wait for print_started to propagate and the watcher to process it.
            async with asyncio.timeout(5.0):
                while True:
                    ev = await sub.get()
                    if ev.name == "print_started":
                        break

        # Give the watcher coroutine time to write the row.
        await asyncio.sleep(0.2)

        rows = await jobs_repo.list(printer_id=SERIAL)
        assert len(rows) == 1, f"expected 1 external row, got {len(rows)}: {rows}"
        row = rows[0]
        assert row.state is JobState.PRINTING
        assert row.metadata_json is not None
        assert json.loads(row.metadata_json).get("origin") == "external"
        assert row.started_at is not None
    finally:
        await manager.shutdown()
        await registry.shutdown()
