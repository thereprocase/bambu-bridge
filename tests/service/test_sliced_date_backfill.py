"""Tests for the startup backfill path in main.py lifespan.

The backfill schedules a viz pre-warm for any printer that has a live job
row in the database at startup.  This covers the restart-during-print hole
where the sliced-date memo was previously lost on bridge restart.

Invariants tested:

1. When a printer has a live job row and the printer's state carries a
   subtask_name, schedule_prewarm is called with the correct arguments.
2. When there is no live job row (printer idle), no pre-warm is scheduled.
3. When the printer's subtask_name is empty / None, no pre-warm is scheduled
   (no job name → nothing to look up on FTPS).
4. A printer with a live job but no subtask_name does not crash lifespan.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from bambu_bridge.config import Settings
from bambu_bridge.db.jobs import Database, Job, JobRepo, JobState, Printer, PrinterRepo
from bambu_bridge.main import create_app
from bambu_bridge.service.printer import PrinterService
from bambu_bridge.service.viz_cache import VizCache
from tests.conftest import (
    ACCESS_CODE,
    API_KEY,
    SERIAL,
    patch_discovery_ok,
)

_AUTH = {"Authorization": f"Bearer {API_KEY}"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _seed_printer(db_path: str) -> None:
    """Synchronous helper extracted so async tests stay focused."""
    # We run DB ops synchronously via a dedicated asyncio run.
    import asyncio as _asyncio

    async def _run() -> None:
        db = Database(db_path)
        await db.connect()
        await PrinterRepo(db).add(
            Printer(
                id=SERIAL,
                friendly_name="TestPrinter",
                ip="127.0.0.1",
                access_code=ACCESS_CODE,
                model="P1S",
                added_at=int(time.time()),
            )
        )
        await db.close()

    _asyncio.run(_run())


# --------------------------------------------------------------------------- #
# Test 1 — schedule_prewarm called when live job + subtask_name present
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_backfill_schedules_prewarm_for_live_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a printer has a live job row, lifespan backfills a pre-warm."""
    db_path = str(tmp_path / "backfill.db")
    patch_discovery_ok(monkeypatch, serial=SERIAL)

    # Seed the DB with a live PRINTING job before the app starts.
    db_seed = Database(db_path)
    await db_seed.connect()
    await PrinterRepo(db_seed).add(
        Printer(
            id=SERIAL,
            friendly_name="TestPrinter",
            ip="127.0.0.1",
            access_code=ACCESS_CODE,
            model="P1S",
            added_at=int(time.time()),
        )
    )
    jobs_repo = JobRepo(db_seed)
    await jobs_repo.create(
        Job(
            id="testjob001",
            printer_id=SERIAL,
            file_name="benchy.gcode.3mf",
            state=JobState.PRINTING,
            queued_at=int(time.time()),
            started_at=int(time.time()),
        )
    )
    await db_seed.close()

    scheduled: list[tuple[str, str]] = []

    def _mock_schedule_prewarm(
        self: VizCache,
        printer_id: str,
        ip: str,
        access_code: str,
        job_name: str,
    ) -> None:
        scheduled.append((printer_id, job_name))
        # Do NOT call the real method — we just want to capture the call.

    monkeypatch.setattr(VizCache, "schedule_prewarm", _mock_schedule_prewarm)

    # The app's MQTT will try to connect; stub subtask_name into the printer
    # state after the registry loads.  We use a state-injection hook via a
    # monkeypatched registry.load().
    _original_start = PrinterService.start

    async def _patched_start(self: PrinterService) -> None:
        await _original_start(self)
        # Inject a subtask_name into the printer's _state dict so the
        # backfill loop can read it via summary().
        self._state["subtask_name"] = "benchy.gcode.3mf"

    monkeypatch.setattr(PrinterService, "start", _patched_start)

    settings = Settings(
        bridge_api_key=API_KEY,
        bridge_db_path=db_path,
        bridge_log_level="warning",
        bridge_log_format="console",
        bridge_allow_loopback_host=True,
    )
    app = create_app(settings, mqtt_port=1, ftps_port=990)

    def run() -> None:
        from fastapi.testclient import TestClient

        with TestClient(app):
            pass  # just start and stop the lifespan

    await asyncio.to_thread(run)

    # schedule_prewarm must have been called for this printer with the right job.
    assert any(
        pid == SERIAL and jname == "benchy.gcode.3mf"
        for pid, jname in scheduled
    ), f"expected backfill pre-warm; got: {scheduled}"


# --------------------------------------------------------------------------- #
# Test 1b/1c — backfill also fires for the other live states (SUBMITTED,
# PREPARING).  All three of _LIVE_JOB_STATES must trigger a pre-warm; only
# PRINTING was covered above, leaving the SUBMITTED/PREPARING branches untested.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "live_state",
    [JobState.SUBMITTED, JobState.PREPARING],
)
async def test_backfill_schedules_prewarm_for_other_live_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_state: JobState,
) -> None:
    """SUBMITTED and PREPARING job rows also trigger a backfill pre-warm.

    Both states are in ``_LIVE_JOB_STATES``: a bridge restart mid-heat-soak
    (PREPARING) or right after the project file landed (SUBMITTED) must still
    re-warm the viz caches, exactly like the PRINTING case.
    """
    db_path = str(tmp_path / f"backfill_{live_state.value}.db")
    patch_discovery_ok(monkeypatch, serial=SERIAL)

    # Seed the DB with a live job in the parametrized state before app start.
    db_seed = Database(db_path)
    await db_seed.connect()
    await PrinterRepo(db_seed).add(
        Printer(
            id=SERIAL,
            friendly_name="TestPrinter",
            ip="127.0.0.1",
            access_code=ACCESS_CODE,
            model="P1S",
            added_at=int(time.time()),
        )
    )
    await JobRepo(db_seed).create(
        Job(
            id=f"livejob_{live_state.value}",
            printer_id=SERIAL,
            file_name="benchy.gcode.3mf",
            state=live_state,
            queued_at=int(time.time()),
            started_at=int(time.time()),
        )
    )
    await db_seed.close()

    scheduled: list[tuple[str, str]] = []

    def _mock_schedule_prewarm(
        self: VizCache,
        printer_id: str,
        ip: str,
        access_code: str,
        job_name: str,
    ) -> None:
        scheduled.append((printer_id, job_name))

    monkeypatch.setattr(VizCache, "schedule_prewarm", _mock_schedule_prewarm)

    _original_start = PrinterService.start

    async def _patched_start(self: PrinterService) -> None:
        await _original_start(self)
        self._state["subtask_name"] = "benchy.gcode.3mf"

    monkeypatch.setattr(PrinterService, "start", _patched_start)

    settings = Settings(
        bridge_api_key=API_KEY,
        bridge_db_path=db_path,
        bridge_log_level="warning",
        bridge_log_format="console",
        bridge_allow_loopback_host=True,
    )
    app = create_app(settings, mqtt_port=1, ftps_port=990)

    def run() -> None:
        from fastapi.testclient import TestClient

        with TestClient(app):
            pass

    await asyncio.to_thread(run)

    assert any(
        pid == SERIAL and jname == "benchy.gcode.3mf"
        for pid, jname in scheduled
    ), f"expected backfill pre-warm for {live_state.value}; got: {scheduled}"


# --------------------------------------------------------------------------- #
# Test 2 — no pre-warm when printer has no live job
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_backfill_no_prewarm_when_no_live_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifespan backfill skips pre-warm when the printer has no live job row."""
    db_path = str(tmp_path / "idle.db")
    patch_discovery_ok(monkeypatch, serial=SERIAL)

    # Seed DB with only a COMPLETED job (terminal state — not live).
    db_seed = Database(db_path)
    await db_seed.connect()
    await PrinterRepo(db_seed).add(
        Printer(
            id=SERIAL,
            friendly_name="TestPrinter",
            ip="127.0.0.1",
            access_code=ACCESS_CODE,
            model="P1S",
            added_at=int(time.time()),
        )
    )
    await JobRepo(db_seed).create(
        Job(
            id="done001",
            printer_id=SERIAL,
            file_name="benchy.gcode.3mf",
            state=JobState.COMPLETED,
            queued_at=int(time.time()),
            started_at=int(time.time()),
            finished_at=int(time.time()),
        )
    )
    await db_seed.close()

    scheduled: list[tuple[str, str]] = []

    def _mock_schedule_prewarm(
        self: VizCache,
        printer_id: str,
        ip: str,
        access_code: str,
        job_name: str,
    ) -> None:
        scheduled.append((printer_id, job_name))

    monkeypatch.setattr(VizCache, "schedule_prewarm", _mock_schedule_prewarm)

    settings = Settings(
        bridge_api_key=API_KEY,
        bridge_db_path=db_path,
        bridge_log_level="warning",
        bridge_log_format="console",
        bridge_allow_loopback_host=True,
    )
    app = create_app(settings, mqtt_port=1, ftps_port=990)

    def run() -> None:
        from fastapi.testclient import TestClient

        with TestClient(app):
            pass

    await asyncio.to_thread(run)

    # No backfill pre-warm for an idle printer.
    assert scheduled == [], f"unexpected pre-warm calls: {scheduled}"


# --------------------------------------------------------------------------- #
# Test 3 — no pre-warm when subtask_name is empty
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_backfill_no_prewarm_when_subtask_name_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backfill skips pre-warm when the printer's current subtask_name is empty."""
    db_path = str(tmp_path / "noname.db")
    patch_discovery_ok(monkeypatch, serial=SERIAL)

    db_seed = Database(db_path)
    await db_seed.connect()
    await PrinterRepo(db_seed).add(
        Printer(
            id=SERIAL,
            friendly_name="TestPrinter",
            ip="127.0.0.1",
            access_code=ACCESS_CODE,
            model="P1S",
            added_at=int(time.time()),
        )
    )
    await JobRepo(db_seed).create(
        Job(
            id="livenoname001",
            printer_id=SERIAL,
            file_name="mystery.gcode.3mf",
            state=JobState.PRINTING,
            queued_at=int(time.time()),
            started_at=int(time.time()),
        )
    )
    await db_seed.close()

    scheduled: list[tuple[str, str]] = []

    def _mock_schedule_prewarm(
        self: VizCache,
        printer_id: str,
        ip: str,
        access_code: str,
        job_name: str,
    ) -> None:
        scheduled.append((printer_id, job_name))

    monkeypatch.setattr(VizCache, "schedule_prewarm", _mock_schedule_prewarm)

    # Do NOT inject subtask_name — it stays empty / None in the printer state.

    settings = Settings(
        bridge_api_key=API_KEY,
        bridge_db_path=db_path,
        bridge_log_level="warning",
        bridge_log_format="console",
        bridge_allow_loopback_host=True,
    )
    app = create_app(settings, mqtt_port=1, ftps_port=990)

    def run() -> None:
        from fastapi.testclient import TestClient

        with TestClient(app):
            pass

    await asyncio.to_thread(run)

    # No pre-warm: subtask_name is empty so nothing to look up.
    assert scheduled == [], f"unexpected pre-warm calls: {scheduled}"
