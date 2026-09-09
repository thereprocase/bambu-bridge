"""M2 acceptance: subscribe to a printer's events and observe state changes
when the (mock) printer publishes new status — snapshot, deltas, named events.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

import pytest

from bambu_bridge.service.events import Event
from bambu_bridge.service.printer import PrinterService
from tests.conftest import ACCESS_CODE, SERIAL, MockPrinter

_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


async def _next(sub, predicate, timeout: float = 10.0) -> Event:
    """Return the next event matching ``predicate`` within ``timeout``."""
    async with asyncio.timeout(timeout):
        while True:
            ev = await sub.get()
            if predicate(ev):
                return ev


def _service(port: int) -> PrinterService:
    return PrinterService(
        SERIAL,
        "127.0.0.1",
        ACCESS_CODE,
        friendly_name="Workshop P1S",
        model="P1S",
        mqtt_port=port,
    )


@pytest.mark.asyncio
async def test_seed_snapshot_then_delta_then_named_events(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Seed observations are silent; only real transitions emit named events
    (Sauron war-council CRIT #2 — a printer that was already RUNNING when
    the bridge connected MUST NOT replay print_started/completed/error as
    if it had just transitioned)."""
    service = _service(mqtt_broker)
    async with service.bus.subscribe() as sub:
        await service.start()
        try:
            # pushall is answered with SAMPLE_PUSH_STATUS -> a full snapshot.
            snap = await _next(sub, lambda e: e.type == "snapshot")
            assert snap.data["gcode_state"] == "RUNNING"
            assert snap.data["mc_percent"] == 42

            # New status with a changed field -> delta carries only that field
            # and emits NO spurious print_started (seed was already RUNNING).
            await mock_printer.push_report({"print": {"mc_percent": 99}})
            delta = await _next(
                sub, lambda e: e.type == "delta" and "mc_percent" in e.data
            )
            assert delta.data == {"mc_percent": 99}

            # Real transition RUNNING -> FINISH emits print_completed.
            await mock_printer.push_report({"print": {"gcode_state": "FINISH"}})
            done = await _next(sub, lambda e: e.name == "print_completed")
            assert done.data["subtask_name"] == "benchy"
        finally:
            await service.stop()


@pytest.mark.asyncio
async def test_print_started_fires_on_real_transition_not_seed(
    mqtt_broker: int, printing_mock: MockPrinter
) -> None:
    """IDLE-seeded printer -> a project_file submission flips to RUNNING ->
    THAT transition emits print_started. The seed itself stays silent."""
    service = _service(mqtt_broker)
    async with service.bus.subscribe() as sub:
        await service.start()
        try:
            snap = await _next(sub, lambda e: e.type == "snapshot")
            assert snap.data["gcode_state"] == "IDLE"
            # No print_started yet — seed was IDLE.
            # Trigger the simulation: a project_file command flips the mock
            # to RUNNING which IS a real transition.
            await service.send_command("print", "project_file", param="x.3mf")
            started = await _next(
                sub, lambda e: e.name == "print_started", timeout=5.0
            )
            assert started.type == "event"
        finally:
            await service.stop()


@pytest.mark.asyncio
async def test_connection_lost_event_on_link_drop(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    service = _service(mqtt_broker)
    async with service.bus.subscribe() as sub:
        await service.start()
        try:
            await _next(sub, lambda e: e.type == "snapshot")
            assert service.connected is True
            await service.stop()  # cancels the MQTT task -> connection_lost
            lost = await _next(sub, lambda e: e.name == "connection_lost")
            assert lost.type == "event"
            # §12.2: connection_lost carries the drop timestamp.
            assert isinstance(lost.data.get("at"), str)
            assert service.connected is False
        finally:
            await service.stop()


@pytest.mark.asyncio
async def test_error_event_emitted_once_per_transition(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    service = _service(mqtt_broker)
    async with service.bus.subscribe() as sub:
        await service.start()
        try:
            await _next(sub, lambda e: e.type == "snapshot")
            await mock_printer.push_report(
                {"print": {"mc_print_error_code": "0500_0100_0001_0001"}}
            )
            err = await _next(sub, lambda e: e.name == "error")
            # §12.2: `error` carries the structured print_error object.
            assert err.data["print_error"]["code"] == "0500_0100_0001_0001"

            # Same error repeated in the next push must NOT re-emit.
            await mock_printer.push_report(
                {"print": {"mc_print_error_code": "0500_0100_0001_0001", "msg": 1}}
            )
            with pytest.raises(TimeoutError):
                await _next(sub, lambda e: e.name == "error", timeout=1.0)
        finally:
            await service.stop()


@pytest.mark.asyncio
async def test_filament_runout_event_is_code_and_slot(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """A runout-category HMS code emits `filament_runout` with the §12.2
    `{code, slot}` shape — not the generic `error` envelope."""
    service = _service(mqtt_broker)
    async with service.bus.subscribe() as sub:
        await service.start()
        try:
            await _next(sub, lambda e: e.type == "snapshot")
            await mock_printer.push_report(
                {"print": {"mc_print_error_code": "0300_0d00_0003_0001"}}
            )
            runout = await _next(sub, lambda e: e.name == "filament_runout")
            assert set(runout.data.keys()) == {"code", "slot"}
            assert runout.data["code"] == "0300_0d00_0003_0001"
            # slot is a physical 1-4 int, "external", or None — never a raw id.
            assert runout.data["slot"] in (None, "external", 1, 2, 3, 4)
        finally:
            await service.stop()


@pytest.mark.asyncio
async def test_print_failed_event_carries_structured_print_error(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """gcode_state → FAILED emits `print_failed` with the §12.2 nested
    `print_error` object (the shared `build_print_error` builder)."""
    service = _service(mqtt_broker)
    async with service.bus.subscribe() as sub:
        await service.start()
        try:
            await _next(sub, lambda e: e.type == "snapshot")
            await mock_printer.push_report(
                {"print": {
                    "gcode_state": "FAILED",
                    "mc_print_error_code": "0300_1100_0001_0001",
                }}
            )
            failed = await _next(sub, lambda e: e.name == "print_failed")
            err = failed.data["print_error"]
            assert err["code"] == "0300_1100_0001_0001"
            assert err["category"] == "thermal"
            assert err["severity"] == "error"
            assert "layer_num" in failed.data
        finally:
            await service.stop()


# --------------------------------------------------------------------------- #
# Bridge-synthesized job.started_at (Sauron started_at bridge)
#
# The P1S firmware ships NO start-time field, so the bridge stamps started_at
# on the gcode_state→RUNNING transition, holds it across pause/resume, clears
# it on completion, and recovers it from jobs.db after a mid-print restart.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_running_transition_stamps_started_at_in_snapshot(
    mqtt_broker: int, printing_mock: MockPrinter
) -> None:
    """A real IDLE→RUNNING transition stamps started_at; the snapshot exposes
    it as an ISO-8601 UTC `…Z` string even though the raw push has no start key.
    """
    service = _service(mqtt_broker)
    async with service.bus.subscribe() as sub:
        await service.start()
        try:
            await _next(sub, lambda e: e.type == "snapshot")
            # No print yet — IDLE seed carries no synthesized start.
            assert service.snapshot()["job"]["started_at"] is None
            before = datetime.now(UTC)
            await service.send_command("print", "project_file", param="x.3mf")
            started = await _next(
                sub, lambda e: e.name == "print_started", timeout=5.0
            )
            after = datetime.now(UTC)
            # Event payload carries the synthesized value...
            iso = started.data["started_at"]
            assert iso is not None and _ISO_Z.match(iso)
            # ...and the snapshot exposes the same start time.
            snap_iso = service.snapshot()["job"]["started_at"]
            assert snap_iso == iso
            # Sanity: the stamp lands within the transition window (±1s rounding).
            stamped = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            assert before.replace(microsecond=0) <= stamped
            assert stamped <= after.replace(microsecond=0) + _ONE_SECOND
        finally:
            await service.stop()


_ONE_SECOND = (datetime(2000, 1, 1, 0, 0, 1, tzinfo=UTC)
               - datetime(2000, 1, 1, 0, 0, 0, tzinfo=UTC))


@pytest.mark.asyncio
async def test_pause_resume_does_not_change_started_at(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """PAUSE→RUNNING is a resume, not a fresh start — started_at must not move.

    (PAUSE is deliberately excluded from _INACTIVE_STATES so the RUNNING
    branch that re-stamps is never re-entered on resume.)
    """
    service = _service(mqtt_broker)
    async with service.bus.subscribe() as sub:
        await service.start()
        try:
            await _next(sub, lambda e: e.type == "snapshot")
            # Seed is already RUNNING (silent) → no synthesized start yet.
            # Drive a genuine FINISH→RUNNING fresh start to stamp one.
            await mock_printer.push_report({"print": {"gcode_state": "FINISH"}})
            await _next(sub, lambda e: e.name == "print_completed")
            await mock_printer.push_report({"print": {"gcode_state": "RUNNING"}})
            await _next(sub, lambda e: e.name == "print_started")
            stamped = service.snapshot()["job"]["started_at"]
            assert stamped is not None

            # Pause, then resume — neither transition may alter started_at.
            await mock_printer.push_report({"print": {"gcode_state": "PAUSE"}})
            await _next(sub, lambda e: e.type == "delta")
            assert service.snapshot()["job"]["started_at"] == stamped
            await mock_printer.push_report({"print": {"gcode_state": "RUNNING"}})
            await _next(sub, lambda e: e.type == "delta")
            assert service.snapshot()["job"]["started_at"] == stamped
        finally:
            await service.stop()


@pytest.mark.asyncio
async def test_completion_clears_started_at(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """FINISH clears the synthesized start so the next idle snapshot is null."""
    service = _service(mqtt_broker)
    async with service.bus.subscribe() as sub:
        await service.start()
        try:
            await _next(sub, lambda e: e.type == "snapshot")
            # Fresh start to stamp a value.
            await mock_printer.push_report({"print": {"gcode_state": "FINISH"}})
            await _next(sub, lambda e: e.name == "print_completed")
            await mock_printer.push_report({"print": {"gcode_state": "RUNNING"}})
            await _next(sub, lambda e: e.name == "print_started")
            assert service.snapshot()["job"]["started_at"] is not None
            # Completion clears it.
            await mock_printer.push_report({"print": {"gcode_state": "FINISH"}})
            await _next(sub, lambda e: e.name == "print_completed")
            assert service.snapshot()["job"]["started_at"] is None
        finally:
            await service.stop()


@pytest.mark.asyncio
async def test_restart_recovery_uses_jobs_db_start_time(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Mid-print restart: the seed finds the printer already RUNNING with no
    in-memory start time → recover the start time from the jobs.db hook.

    The recovery callable stands in for Registry's jobs.db lookup. mock_printer
    seeds RUNNING (the deploy-mid-print case), so the seed path must consult it.
    """
    recovered_dt = datetime(2026, 5, 20, 3, 14, 1, tzinfo=UTC)

    async def _recover() -> datetime | None:
        return recovered_dt

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
            await _next(sub, lambda e: e.type == "snapshot")
            # Recovered from the (mock) jobs.db row, not stamped as now().
            assert service.snapshot()["job"]["started_at"] == "2026-05-20T03:14:01Z"
        finally:
            await service.stop()


@pytest.mark.asyncio
async def test_restart_recovery_leaves_null_when_no_job_row(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """A screen/SD-started print has no jobs.db row → recovery yields None →
    started_at stays null (honest unknown). We never fabricate now()."""

    async def _recover() -> datetime | None:
        return None

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
            await _next(sub, lambda e: e.type == "snapshot")
            assert service.snapshot()["job"]["started_at"] is None
        finally:
            await service.stop()
