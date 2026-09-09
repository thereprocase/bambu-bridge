"""M4: JobRepo / EventRepo persistence + filtered history."""

from __future__ import annotations

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
from tests.conftest import ACCESS_CODE, SERIAL


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


def _job(job_id: str, state: JobState, queued_at: int = 100) -> Job:
    return Job(
        id=job_id,
        printer_id=SERIAL,
        file_name="benchy.3mf",
        state=state,
        queued_at=queued_at,
    )


@pytest.mark.asyncio
async def test_job_create_get_update(database: Database) -> None:
    await _seed_printer(database)
    repo = JobRepo(database)
    await repo.create(_job("j1", JobState.QUEUED))

    got = await repo.get("j1")
    assert got is not None and got.state is JobState.QUEUED

    updated = await repo.update("j1", state=JobState.PRINTING, progress_pct=42.0)
    assert updated is not None
    assert updated.state is JobState.PRINTING
    assert updated.progress_pct == 42.0


@pytest.mark.asyncio
async def test_job_history_filters(database: Database) -> None:
    await _seed_printer(database)
    repo = JobRepo(database)
    await repo.create(_job("a", JobState.COMPLETED, queued_at=100))
    await repo.create(_job("b", JobState.FAILED, queued_at=200))
    await repo.create(_job("c", JobState.COMPLETED, queued_at=300))

    newest_first = [j.id for j in await repo.list()]
    assert newest_first == ["c", "b", "a"]

    completed = [j.id for j in await repo.list(state=JobState.COMPLETED)]
    assert completed == ["c", "a"]

    windowed = [j.id for j in await repo.list(since=150, until=250)]
    assert windowed == ["b"]

    page = [j.id for j in await repo.list(limit=1, offset=1)]
    assert page == ["b"]


@pytest.mark.asyncio
async def test_event_log_for_job(database: Database) -> None:
    await _seed_printer(database)
    events = EventRepo(database)
    await events.add(
        printer_id=SERIAL,
        job_id="j1",
        event_type="state_change",
        payload={"from": "queued", "to": "uploading"},
        ts_ms=1000,
    )
    await events.add(
        printer_id=SERIAL,
        job_id="j1",
        event_type="state_change",
        payload={"from": "uploading", "to": "started"},
        ts_ms=2000,
    )
    log = await events.list_for_job("j1")
    assert [e.payload["to"] for e in log] == ["uploading", "started"]
    assert log[0].ts == 1000


@pytest.mark.asyncio
async def test_jobs_cascade_delete_with_printer(database: Database) -> None:
    await _seed_printer(database)
    await JobRepo(database).create(_job("j1", JobState.QUEUED))
    assert await PrinterRepo(database).delete(SERIAL) is True
    # FK ON + ON DELETE CASCADE removes the job too.
    assert await JobRepo(database).get("j1") is None
