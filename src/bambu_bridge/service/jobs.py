"""Job state machine + manager (contract §7.4, PR B remap).

::

    queued ─► uploading ─► submitted ─► preparing ─► printing ─► completed
                  │            │           │           │
                  └─► failed ◄─┴───────────┴───────────┘
    queued ─► canceled        (cancel before the printer acks)

The §6.3 boundary in state form: `submitted` means the printer answered
`result:"success"` for `print.project_file` — NOT that printing began.
`preparing` is the heat-soak + bed-level window (`gcode_state == RUNNING`
but `layer_num == 0`). `printing` is gated on `layer_num > 0` — the
single most important rule in the contract.

Every transition is persisted to the ``events`` table (event_type
``state_change``); the job row's ``state`` is the latest transition.
Driven by the printer's named bus events (M2):

* ``print_started``   gcode_state -> RUNNING   => submitted -> preparing
* ``print_progress``  layer_num crossed 0      => preparing -> printing
* ``print_completed`` gcode_state -> FINISH    => * -> completed
* ``print_failed``    gcode_state -> FAILED    => -> failed

A printer PAUSE is not a failure. The P1S pauses with a non-zero
``print_error`` for things a human settles at the printer (the nozzle-setting
check before a print, filament runout, a door opening) and waits there; the
job keeps its state, gets ``printer_paused`` / ``printer_resumed`` entries in
its event log, and continues when the printer goes RUNNING again (a resume
from PAUSE does not fire ``print_started``, so the FSM watches the
PAUSE -> RUNNING edge itself). A named ``error`` event alone never fails a
job: only a terminal printer state does (FAILED, or IDLE after a stop), or,
before the printer ever runs the job, an error with no RUNNING or PAUSE
within 60 s (the printer refused it).

A ``submitted`` job that never sees RUNNING or PAUSE within 60 s fails (MQTT
timeout, spec 8). The FED_NO_PROGRESS watchdog (``BRIDGE_FEED_DEADLINE_S``,
AMS engagement) runs from the preparing-onward window — it is the §6.3
hard-fail safety net independent of the layer_num signal. It is suspended
while the printer is paused (a person is deciding) and starts a fresh window
on resume.

External prints (screen/SD/Bambu-Studio-direct)
-----------------------------------------------
When the bridge witnesses a ``print_started`` event for printer P and no
live (submitted/preparing/printing) job row exists, it inserts an
"external" row so restart-recovery works for ALL prints — not just ones
submitted through the bridge API.  The row carries ``{"origin":
"external"}`` in ``metadata_json`` so the jobs-history UI can distinguish
it.  On ``print_completed`` / ``print_failed`` the normal close-out path
runs against that row via :meth:`JobManager.attach`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from bambu_bridge.db.jobs import EventRepo, Job, JobRepo, JobState
from bambu_bridge.protocol.ftps import FtpsTransfer
from bambu_bridge.service.events import Event
from bambu_bridge.service.registry import PrinterNotFoundError, Registry
from bambu_bridge.service.viz_cache import VizCache
from bambu_bridge.slicedoc import project_file_command, sd_filename, validate
from bambu_bridge.translate import build_print_error
from bambu_bridge.vision import SpaghettiMonitor

if TYPE_CHECKING:
    from bambu_bridge.service.printer import PrinterService

log = structlog.get_logger(__name__)

_RUNNING_TIMEOUT_S = 60  # started -> printing deadline (spec 8)

# Job states that mean "a print is actively in flight for this printer."
# Mirrors db/jobs._LIVE_JOB_STATES — kept here to avoid importing a private
# symbol across package boundaries. Both sets must be kept in sync.
_LIVE_STATES = frozenset(
    {
        JobState.SUBMITTED,
        JobState.PREPARING,
        JobState.PRINTING,
    }
)
_CANCEL_CONFIRM_S = 30  # bound the wait for the printer to confirm a stop
# Post-RUNNING: the AMS must actually engage a tray (ams.tray_now set) within
# this, else FED_NO_PROGRESS. In §6.3 *and* the 2026-05-19 recurrence tray_now
# was never set while the printer ran 40+ layers of air. On a healthy print
# the tray engages during the start-gcode load — but only after the bed and
# chamber are up to temperature. A 100 °C bed from cold (ASA) took more than
# the original 600 s on 2026-09-18 and the watchdog stopped a healthy print.
# Default 1800 s; override with BRIDGE_FEED_DEADLINE_S (or Settings via
# JobManager(feed_deadline_s=...)). Tests monkeypatch this module value.
_FEED_DEADLINE_S = float(os.environ.get("BRIDGE_FEED_DEADLINE_S", "1800"))

# Legal transitions (contract §7.4 PR B remap). Guarded so a late MQTT
# event can't, say, move a canceled job to completed.
#
# `PREPARING → COMPLETED` is allowed for two cases:
#   1. a short print whose FINISH arrives before the bridge sees the
#      layer_num crossing — uncommon in practice but possible if the
#      printer batches its push_status updates;
#   2. the unit tests' MockPrinter (which simulates `RUNNING → FINISH`
#      without intermediate layer reports).
# The PRINTING transition is still preferred when the layer_advanced /
# progress signals do arrive; this is purely a safety net.
_ALLOWED: dict[JobState, set[JobState]] = {
    JobState.QUEUED: {JobState.UPLOADING, JobState.CANCELED, JobState.FAILED},
    JobState.UPLOADING: {JobState.SUBMITTED, JobState.FAILED, JobState.CANCELED},
    JobState.SUBMITTED: {
        JobState.PREPARING,
        JobState.FAILED,
        JobState.CANCELED,
        JobState.INTERRUPTED,
    },
    JobState.PREPARING: {
        JobState.PRINTING,
        JobState.COMPLETED,
        JobState.INTERRUPTED,
        JobState.FAILED,
        JobState.CANCELED,
    },
    JobState.PRINTING: {
        JobState.COMPLETED,
        JobState.FAILED,
        JobState.CANCELED,
        JobState.INTERRUPTED,
    },
}


class JobManager:
    """Owns all jobs: submission, the lifecycle tasks, history, cancel."""

    def __init__(
        self,
        jobs: JobRepo,
        events: EventRepo,
        registry: Registry,
        *,
        ftps_port: int = 990,
        spaghetti_detection: bool = False,
        feed_deadline_s: float | None = None,
        viz_cache: VizCache | None = None,
    ) -> None:
        self._jobs = jobs
        self._events = events
        self._registry = registry
        self._ftps_port = ftps_port
        self._spaghetti_detection = spaghetti_detection
        self._feed_deadline_s = feed_deadline_s
        self._viz_cache = viz_cache
        self._runs: dict[str, JobRun] = {}
        # Background tasks watching each printer's bus for external prints.
        self._watch_tasks: dict[str, asyncio.Task[None]] = {}

    async def submit(
        self,
        printer_id: str,
        file_bytes: bytes,
        file_name: str,
        *,
        ams_mapping: list[int] | None = None,
    ) -> Job:
        """Create a queued job and kick off its lifecycle task."""
        self._registry.get(printer_id)  # PrinterNotFoundError -> 404 at API
        job = Job(
            id=uuid.uuid4().hex,
            printer_id=printer_id,
            file_name=file_name,
            state=JobState.QUEUED,
            queued_at=int(time.time()),
            metadata_json=None,
        )
        await self._jobs.create(job)
        await self._events.add(
            printer_id=job.printer_id,
            job_id=job.id,
            event_type="job_created",
            payload={"state": JobState.QUEUED.value, "file_name": file_name},
        )
        run = JobRun(
            job,
            file_bytes,
            self._jobs,
            self._events,
            self._registry,
            ftps_port=self._ftps_port,
            ams_mapping=ams_mapping,
            spaghetti_detection=self._spaghetti_detection,
            feed_deadline_s=self._feed_deadline_s,
        )
        self._runs[job.id] = run
        run.start()
        assert run._task is not None
        run._task.add_done_callback(lambda _task: self._runs.pop(job.id, None))
        return job

    async def cancel(self, job_id: str) -> Job:
        job = await self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        run = self._runs.get(job_id)
        if run is not None and not job.state.terminal:
            await run.request_cancel()
        return await self._jobs.get(job_id) or job

    async def get(self, job_id: str) -> Job | None:
        return await self._jobs.get(job_id)

    async def history(self, **filters: Any) -> list[Job]:
        return await self._jobs.list(**filters)

    async def events_for(self, job_id: str) -> list[dict[str, Any]]:
        records = await self._events.list_for_job(job_id)
        return [
            {
                "ts": r.ts,
                "event_type": r.event_type,
                "payload": r.payload,
            }
            for r in records
        ]

    async def attach(self, service: PrinterService) -> None:
        """Registry listener: subscribe to a printer's bus to detect external prints.

        Matches the :data:`~bambu_bridge.service.registry.PrinterListener`
        protocol so the registry can call it the same way it calls
        :meth:`~bambu_bridge.service.event_persister.EventPersister.attach`.
        """
        prior = self._watch_tasks.pop(service.serial, None)
        if prior is not None:
            prior.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await prior
        self._watch_tasks[service.serial] = asyncio.create_task(
            self._watch_printer(service),
            name=f"jobs:watch:{service.serial}",
        )

    async def _watch_printer(self, service: PrinterService) -> None:
        """Watch one printer's bus; create external job rows as needed.

        On ``print_started``: if no live (submitted/preparing/printing) job row
        exists for this printer, insert an "external" row in PRINTING state so
        restart-recovery works for prints started from the printer's screen, SD
        card, or Bambu Studio direct.  Must NOT duplicate rows for
        bridge-submitted jobs (they already have a live row — that is the
        existence check).

        On ``print_completed`` / ``print_failed``: close out any external row
        that is still in a live state (the normal FSM handles bridge-submitted
        jobs; this path handles the external-origin rows only).
        """
        log_ = log.bind(printer_id=service.serial)
        warmed_job: str | None = None
        async with service.bus.subscribe() as sub:
            async for ev in sub:
                try:
                    # On restart the job name may arrive after the RUNNING edge.
                    # Warm as soon as both are present, even without print_started.
                    if self._viz_cache is not None:
                        summary = service.summary()
                        job_name = summary.get("subtask_name") or ""
                        if summary.get("gcode_state") in ("RUNNING", "PREPARE", "PAUSE"):
                            if job_name and job_name != warmed_job:
                                self._viz_cache.schedule_prewarm(
                                    service.serial, service.ip, service.access_code, job_name
                                )
                                warmed_job = job_name
                        else:
                            warmed_job = None
                    if ev.type != "event" or ev.name is None:
                        continue
                    if ev.name == "print_started":
                        # Trigger viz pre-warm (best-effort; any failure is
                        # swallowed inside VizCache.schedule_prewarm).
                        if self._viz_cache is not None:
                            job_name = ev.data.get("subtask_name") or ""
                            if job_name and job_name != warmed_job:
                                self._viz_cache.schedule_prewarm(
                                    service.serial,
                                    service.ip,
                                    service.access_code,
                                    job_name,
                                )
                                warmed_job = job_name
                        await self._maybe_create_external_job(service.serial, ev, log_)
                    elif ev.name in ("print_completed", "print_failed", "print_interrupted"):
                        await self._maybe_close_external_job(service.serial, ev.name, log_)
                except Exception:  # noqa: BLE001 — never let the watch task die
                    log_.exception("jobs.watch.error", ev_name=ev.name)

    async def _maybe_create_external_job(self, printer_id: str, ev: Event, log_: Any) -> None:
        """Insert an external job row if no live row exists for this printer.

        The live-row check fetches the most recent 50 jobs and scans for any
        in a live state (submitted/preparing/printing).  A ``limit=1`` query
        was previously used here but is wrong: if the most-recently-queued row
        is terminal (COMPLETED/FAILED) and an older live row exists, the
        terminal row is returned first and the live one is missed — causing a
        spurious external row to be inserted for a bridge-submitted print.
        50 is far more rows than any printer will realistically have in flight,
        so the scan is effectively exhaustive at normal operating volumes.
        """
        live = await self._jobs.list(printer_id=printer_id, limit=50)
        has_live = any(j.state in _LIVE_STATES for j in live)
        if has_live:
            # A bridge-submitted job is in flight — do not duplicate.
            return

        # Parse started_at from the event payload; fall back to now().
        raw_started = ev.data.get("started_at")
        started_epoch: int
        if raw_started and isinstance(raw_started, str):
            try:
                dt = datetime.strptime(raw_started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                started_epoch = int(dt.timestamp())
            except ValueError:
                started_epoch = int(time.time())
        else:
            started_epoch = int(time.time())

        file_name = ev.data.get("subtask_name") or "external-print"
        job = Job(
            id=uuid.uuid4().hex,
            printer_id=printer_id,
            file_name=str(file_name),
            state=JobState.PRINTING,
            queued_at=started_epoch,
            started_at=started_epoch,
            metadata_json=json.dumps({"origin": "external"}),
        )
        await self._jobs.create(job)
        await self._events.add(
            printer_id=printer_id,
            job_id=job.id,
            event_type="job_created",
            payload={
                "state": JobState.PRINTING.value,
                "file_name": file_name,
                "origin": "external",
            },
        )
        log_.info("jobs.external_created", job_id=job.id, file_name=file_name)

    async def _maybe_close_external_job(self, printer_id: str, event_name: str, log_: Any) -> None:
        """Close an external-origin job row on completion or failure.

        Only acts on rows whose metadata marks them as external and whose state
        is still live.  Bridge-submitted jobs are closed by their own JobRun
        instance; this path must not race with it.
        """
        live = await self._jobs.list(printer_id=printer_id, limit=50)
        for job in live:
            if job.state not in _LIVE_STATES:
                continue
            meta: dict[str, Any] = {}
            if job.metadata_json:
                with contextlib.suppress(ValueError, TypeError):
                    meta = json.loads(job.metadata_json)
            recovered_active = (
                event_name == "print_interrupted"
                and job.state in (JobState.PREPARING, JobState.PRINTING)
                and job.id not in self._runs
            )
            if meta.get("origin") != "external" and not recovered_active:
                continue
            now = int(time.time())
            if event_name == "print_completed":
                started = job.started_at or now
                await self._jobs.update(
                    job.id,
                    state=JobState.COMPLETED,
                    progress_pct=100.0,
                    finished_at=now,
                    duration_s=now - started,
                )
                await self._events.add(
                    printer_id=printer_id,
                    job_id=job.id,
                    event_type="state_change",
                    payload={
                        "from": job.state.value,
                        "to": JobState.COMPLETED.value,
                        "trigger": "gcode_finish",
                    },
                )
            else:
                interrupted = event_name == "print_interrupted"
                await self._jobs.update(
                    job.id,
                    state=JobState.INTERRUPTED if interrupted else JobState.FAILED,
                    finished_at=now,
                    error_code="printer_job_lost" if interrupted else "printer_error",
                )
                await self._events.add(
                    printer_id=printer_id,
                    job_id=job.id,
                    event_type="state_change",
                    payload={
                        "from": job.state.value,
                        "to": JobState.INTERRUPTED.value if interrupted else JobState.FAILED.value,
                        "trigger": "printer_job_lost" if interrupted else "printer_error",
                    },
                )
            log_.info("jobs.external_closed", job_id=job.id, ev_name=event_name)
            if event_name != "print_interrupted":
                break  # only one external job expected per printer at a time

    async def shutdown(self) -> None:
        for task in self._watch_tasks.values():
            task.cancel()
        for task in self._watch_tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._watch_tasks.clear()
        for run in list(self._runs.values()):
            await run.stop()
        self._runs.clear()


class JobNotFoundError(KeyError):
    """No job with that id."""


class JobRun:
    """One job's lifecycle. Subscribes to the printer bus and drives the FSM."""

    def __init__(
        self,
        job: Job,
        file_bytes: bytes,
        jobs: JobRepo,
        events: EventRepo,
        registry: Registry,
        *,
        ftps_port: int,
        ams_mapping: list[int] | None,
        spaghetti_detection: bool = False,
        feed_deadline_s: float | None = None,
    ) -> None:
        self._job = job
        self._file_bytes = file_bytes
        self._jobs = jobs
        self._events = events
        self._registry = registry
        self._ftps_port = ftps_port
        self._ams_mapping = ams_mapping
        self._spaghetti_detection = spaghetti_detection
        self._feed_deadline_s = feed_deadline_s
        self._signals: asyncio.Queue[str] = asyncio.Queue()
        self._cancel = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._log = log.bind(job_id=job.id, printer_id=job.printer_id)
        # Printer-side context the bus reader keeps for the FSM: the last
        # gcode_state seen, the latest structured print_error (from `error` /
        # `print_failed` events or a report's own fields) and the report that
        # carried the latest PAUSE, for the printer_paused event.
        self._last_gcode: str | None = None
        self._last_error: dict[str, Any] | None = None
        self._pause_report: dict[str, Any] = {}

    # ------------------------------------------------------------------ #

    def start(self) -> None:
        self._task = asyncio.create_task(self._guarded())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def request_cancel(self) -> None:
        self._cancel.set()
        self._signals.put_nowait("cancel")

    # ------------------------------------------------------------------ #

    async def _guarded(self) -> None:
        try:
            await self._execute()
        finally:
            self._file_bytes = b""

    async def _execute(self) -> None:
        try:
            service = self._registry.get(self._job.printer_id)
        except PrinterNotFoundError:
            await self._fail("printer_removed")
            return
        async with service.bus.subscribe() as sub:
            reader = asyncio.create_task(self._read_bus(sub))
            try:
                guard = getattr(service, "job_guard", None)
                if guard is not None:
                    async with guard(self._cancel):
                        await self._lifecycle(service)
                else:
                    await self._lifecycle(service)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — any failure -> failed
                self._log.exception("job.crashed")
                await self._fail(f"exception: {exc!s}")
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader

    async def _read_bus(self, sub: Any) -> None:
        """Normalise bus events into FSM signals.

        - ``running`` — gcode_state crossed into RUNNING (submitted → preparing)
        - ``layer_advanced`` — layer_num crossed 0 → positive (preparing → printing)
        - ``progress`` — AMS engaged (FED_NO_PROGRESS watchdog satisfaction)
        - ``paused`` / ``resumed`` — gcode_state entered PAUSE / went PAUSE → RUNNING
        - ``error`` — the printer reported a print_error (kept in ``_last_error``;
          not terminal on its own: the P1S also pauses with one)
        - ``completed`` / ``failed`` / ``stopped`` — terminal signals
        """
        confirmed_active = self._job.state in (JobState.PREPARING, JobState.PRINTING)
        async for ev in sub:  # type: Event
            assert isinstance(ev, Event)
            if ev.name == "print_started":
                self._signals.put_nowait("running")
            elif ev.name == "print_progress":
                self._signals.put_nowait("layer_advanced")
            elif ev.name == "print_completed":
                self._signals.put_nowait("completed")
            elif ev.name == "print_interrupted":
                if confirmed_active:
                    self._signals.put_nowait("interrupted")
            elif ev.name == "print_failed":
                self._last_error = ev.data.get("print_error") or self._last_error
                self._signals.put_nowait("failed")
            elif ev.name == "error":
                self._last_error = ev.data.get("print_error") or self._last_error
                self._signals.put_nowait("error")
            elif ev.type in ("delta", "snapshot"):
                gs = _gcode_state(ev.data)
                prev, self._last_gcode = self._last_gcode, gs or self._last_gcode
                code = ev.data.get("mc_print_error_code") or ev.data.get("print_error")
                if code not in (None, 0, "0", ""):
                    self._last_error = build_print_error(code)
                if gs == "PAUSE" and prev != "PAUSE":
                    self._pause_report = dict(ev.data)
                    self._signals.put_nowait("paused")
                elif gs == "RUNNING" and prev == "PAUSE":
                    self._signals.put_nowait("resumed")
                if gs in ("IDLE", "FAILED", "FINISH"):
                    self._signals.put_nowait("stopped")
                if _ams_engaged(ev.data):
                    self._signals.put_nowait("progress")
                if _layer_advanced(ev.data):
                    self._signals.put_nowait("layer_advanced")

    async def _lifecycle(self, service: Any) -> None:
        if self._cancel.is_set():
            await self._set(JobState.CANCELED, "canceled_before_upload")
            return

        # Gate the .gcode.3mf BEFORE touching the printer. This is the §6.3
        # fix: an inconsistent AMS binding, bad md5, or unsafe temperature is
        # rejected here — not discovered as "printed air" 17 min in.
        report = validate(self._file_bytes, expected_ams_mapping=self._ams_mapping)
        if not report.ok:
            await self._fail(f"invalid_3mf: {'; '.join(report.issues)}")
            return

        # queued -> uploading -> started (spec 5.2: distinct transitions)
        await self._set(JobState.UPLOADING, "ftps_begin")
        ftps = FtpsTransfer(service.ip, service.access_code, port=self._ftps_port)
        sd_name = sd_filename(self._job.file_name)
        try:
            # FTP root == SD card root (REPORT §5) — store at root, not model/.
            remote = await ftps.upload_bytes(self._file_bytes, sd_name, remote_dir="")
        except Exception as exc:  # noqa: BLE001
            await self._fail(f"upload_error: {exc!s}")
            return
        self._file_bytes = b""
        await self._jobs.update(self._job.id, file_path=remote)

        await self._set(JobState.SUBMITTED, "project_file_published")
        # url = file:///sdcard/<name>.gcode.3mf — the ONLY confirmed scheme
        # (REPORT §6.2). The old code used ftp://, flagged untested in §9.
        await service.send_command(
            "print",
            "project_file",
            **project_file_command(
                self._job.file_name,
                use_ams=bool(self._ams_mapping),
                ams_mapping=self._ams_mapping or [],
            ),
        )

        # submitted -> preparing  (RUNNING within 60 s, else timeout-fail). A
        # PAUSE in this window means the printer took the job and stopped for
        # a person (the nozzle-setting check does this before the first move):
        # hold without a deadline until it runs or ends. An `error` alone is
        # remembered, not fatal; with no RUNNING/PAUSE by the deadline the
        # printer refused the job and it fails as printer_error.
        deadline = asyncio.get_running_loop().time() + _RUNNING_TIMEOUT_S
        trigger = "gcode_running"
        while True:
            sig = await self._wait_signal(
                {"running", "resumed", "paused", "error", "cancel", "failed"},
                timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
            )
            if sig == "error":
                continue
            if sig == "paused":
                sig = await self._hold_while_paused()
                trigger = "gcode_running_after_pause"
            break
        if sig == "cancel":
            # after a pause the printer holds the job: wait for it to confirm the stop
            await self._do_cancel(service, acked=trigger != "gcode_running")
            return
        if sig in ("stopped", "completed", "interrupted"):
            await self._end_while_paused(sig)
            return
        if sig in (None, "failed"):
            refused = sig == "failed" or self._last_error is not None
            reason = "printer_error" if refused else "no_running_within_60s"
            await self._fail(reason)
            return
        await self._set(JobState.PREPARING, trigger)

        # preparing -> printing  (layer_num > 0 OR completion, with the
        # FED_NO_PROGRESS watchdog still in force as the §6.3 hard safety).
        # `layer_advanced` is the canonical "printing began" signal per
        # contract §6.0.1; `progress` (AMS engaged) is the parallel safety
        # check — if neither fires within the deadline, abort. In healthy
        # prints both arrive within seconds of each other. A pause suspends the
        # watchdog (a person is deciding at the printer) and a resume starts a
        # fresh window.
        while True:
            sig = await self._wait_signal(
                {
                    "layer_advanced",
                    "progress",
                    "completed",
                    "failed",
                    "cancel",
                    "interrupted",
                    "paused",
                },
                timeout=(
                    self._feed_deadline_s if self._feed_deadline_s is not None else _FEED_DEADLINE_S
                ),
            )
            if sig != "paused":
                break
            sig = await self._hold_while_paused()
            if sig in ("resumed", "running"):
                continue
            if sig in ("stopped", "completed", "interrupted"):
                await self._end_while_paused(sig)
                return
            break
        if sig is None:
            with contextlib.suppress(Exception):
                await service.send_command("print", "stop")
            await self._fail("FED_NO_PROGRESS")
            return
        if sig == "interrupted":
            await self._jobs.update(
                self._job.id, finished_at=int(time.time()), error_code="printer_job_lost"
            )
            await self._set(JobState.INTERRUPTED, "printer_job_lost")
            return
        if sig == "completed":
            await self._complete()
            return
        if sig == "cancel":
            await self._do_cancel(service, acked=True)
            return
        if sig == "failed":
            await self._fail("printer_error")
            return
        # Either layer_advanced (preferred) or progress (AMS) — both signal
        # the §6.3 boundary has been crossed safely. Transition to PRINTING.
        trigger = "layer_started" if sig == "layer_advanced" else "ams_engaged"
        await self._set(JobState.PRINTING, trigger)

        # printing -> completed | failed | canceled | spaghetti
        #
        # Spaghetti detection (opt-in, vision/) runs ONLY here: telemetry is
        # healthy by construction at this point (RUNNING, tray engaged, past
        # FED_NO_PROGRESS) so a sustained chaotic-frame run means the print
        # itself failed. It feeds the same abort path as any other failure.
        accept = {"completed", "failed", "cancel", "interrupted", "paused"}
        spaghetti = self._start_spaghetti_monitor(service)
        if spaghetti is not None:
            accept.add("spaghetti")
        try:
            while True:
                sig = await self._wait_signal(accept)
                if sig != "paused":
                    break
                sig = await self._hold_while_paused()
                if sig in ("resumed", "running"):
                    continue
                if sig in ("stopped", "completed", "interrupted"):
                    await self._end_while_paused(sig)
                    return
                break
        finally:
            if spaghetti is not None:
                spaghetti.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await spaghetti
        if sig == "interrupted":
            await self._jobs.update(
                self._job.id, finished_at=int(time.time()), error_code="printer_job_lost"
            )
            await self._set(JobState.INTERRUPTED, "printer_job_lost")
            return
        if sig == "completed":
            await self._complete()
        elif sig == "cancel":
            await self._do_cancel(service, acked=True)
        elif sig == "spaghetti":
            with contextlib.suppress(Exception):
                await service.send_command("print", "stop")
            await self._fail("SPAGHETTI_DETECTED")
        else:
            await self._fail("printer_error")

    # ------------------------------------------------------------------ #
    # Transitions
    # ------------------------------------------------------------------ #

    async def _hold_while_paused(self) -> str:
        """Wait out a printer PAUSE, with no deadline: a person settles it at the printer.

        Logs ``printer_paused`` (the job's phase, the printer's print_error,
        HMS list, layer and stage) and, on resume, ``printer_resumed``.
        Returns the signal that ended the pause: ``resumed`` / ``running``,
        or ``cancel`` / ``failed`` / ``stopped`` / ``completed`` /
        ``interrupted``. Feed and layer signals that arrive while paused are
        handed back to the queue on resume, so the watchdog after the pause
        still sees them.
        """
        report = self._pause_report
        await self._events.add(
            printer_id=self._job.printer_id,
            job_id=self._job.id,
            event_type="printer_paused",
            payload={
                "phase": self._job.state.value,
                "print_error": self._last_error,
                "hms": report.get("hms"),
                "layer_num": report.get("layer_num"),
                "mc_print_stage": report.get("mc_print_stage"),
            },
        )
        self._log.info(
            "job.printer_paused", phase=self._job.state.value, print_error=self._last_error
        )
        held: list[str] = []
        while True:
            sig = await self._wait_signal(
                {
                    "resumed",
                    "running",
                    "cancel",
                    "failed",
                    "stopped",
                    "completed",
                    "interrupted",
                    "progress",
                    "layer_advanced",
                }
            )
            assert sig is not None  # no timeout
            if sig in ("progress", "layer_advanced"):
                if sig not in held:
                    held.append(sig)
                continue
            break
        if sig in ("resumed", "running"):
            await self._events.add(
                printer_id=self._job.printer_id,
                job_id=self._job.id,
                event_type="printer_resumed",
                payload={"phase": self._job.state.value},
            )
            self._log.info("job.printer_resumed", phase=self._job.state.value)
            for s in held:
                self._signals.put_nowait(s)
        return sig

    async def _end_while_paused(self, sig: str) -> None:
        """The printer left PAUSE without resuming: FINISH completes, a lost job is
        interrupted, FAILED fails as printer_error and IDLE (stopped at the printer's
        screen) as printer_stopped."""
        if sig == "completed" or (sig == "stopped" and self._last_gcode == "FINISH"):
            if self._job.state is JobState.SUBMITTED:  # paused before it ever ran
                await self._set(JobState.PREPARING, "gcode_running_after_pause")
            await self._complete()
        elif sig == "interrupted":
            await self._jobs.update(
                self._job.id, finished_at=int(time.time()), error_code="printer_job_lost"
            )
            await self._set(JobState.INTERRUPTED, "printer_job_lost")
        else:
            await self._fail("printer_error" if self._last_gcode == "FAILED" else "printer_stopped")

    async def _wait_signal(self, accept: set[str], *, timeout: float | None = None) -> str | None:
        """Pull signals until one is in ``accept``; None on timeout."""
        deadline = None if timeout is None else asyncio.get_event_loop().time() + timeout
        while True:
            remaining = None
            if deadline is not None:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return None
            try:
                sig = await asyncio.wait_for(self._signals.get(), timeout=remaining)
            except TimeoutError:
                return None
            if sig in accept:
                return sig

    def _start_spaghetti_monitor(self, service: Any) -> asyncio.Task[None] | None:
        """Opt-in camera failure watch, scoped to the PRINTING phase.

        Returns the running task, or None when disabled / no camera. The
        callback only *enqueues* a signal — the actual abort stays in the
        FSM (one stop path), and the trigger is debounced upstream in the
        pure detector, so a single bad frame can't fail a good print.
        """
        if not self._spaghetti_detection:
            return None
        try:
            camera = service.camera
        except Exception:  # noqa: BLE001 — no camera -> just skip the watch
            return None
        monitor = SpaghettiMonitor(
            camera,
            printing_ok=lambda: self._job.state is JobState.PRINTING,
            on_spaghetti=lambda _c: self._signals.put_nowait("spaghetti"),
            printer_id=self._job.printer_id,
        )
        return asyncio.create_task(monitor.run())

    async def _do_cancel(self, service: Any, *, acked: bool) -> None:
        try:
            await service.send_command("print", "stop")
        except ValueError as exc:
            if str(exc).startswith("BBSTOP_"):
                await self._fail(
                    "cancel_not_confirmed; check the printer and use printer stop controls"
                )
                return
        except Exception:
            pass
        if acked:
            # Printer was printing — wait for it to confirm the stop (spec 8).
            await self._wait_signal({"stopped", "completed", "failed"}, timeout=_CANCEL_CONFIRM_S)
        await self._set(JobState.CANCELED, "user_cancel")

    async def _complete(self) -> None:
        now = int(time.time())
        started = self._job.started_at or now
        await self._jobs.update(
            self._job.id,
            progress_pct=100.0,
            finished_at=now,
            duration_s=now - started,
        )
        await self._set(JobState.COMPLETED, "gcode_finish")

    async def _fail(self, reason: str) -> None:
        await self._jobs.update(self._job.id, finished_at=int(time.time()), error_code=reason)
        extra = {"print_error": self._last_error} if self._last_error else None
        await self._set(JobState.FAILED, reason, extra=extra)

    async def _set(
        self, new: JobState, trigger: str, *, extra: dict[str, Any] | None = None
    ) -> None:
        cur = self._job.state
        if new != cur and new not in _ALLOWED.get(cur, set()):
            self._log.warning("job.illegal_transition", frm=cur.value, to=new.value)
            return
        fields: dict[str, Any] = {"state": new}
        if new is JobState.SUBMITTED and self._job.started_at is None:
            fields["started_at"] = int(time.time())
        updated = await self._jobs.update(self._job.id, **fields)
        if updated is not None:
            self._job = updated
        await self._events.add(
            printer_id=self._job.printer_id,
            job_id=self._job.id,
            event_type="state_change",
            payload={"from": cur.value, "to": new.value, "trigger": trigger, **(extra or {})},
        )
        self._log.info("job.transition", frm=cur.value, to=new.value, trigger=trigger)


def _gcode_state(data: dict[str, Any]) -> str | None:
    """Pull gcode_state out of a delta/snapshot payload (nested under state)."""
    if "gcode_state" in data:
        gs = data["gcode_state"]
        return str(gs) if gs is not None else None
    state = data.get("state")
    if isinstance(state, dict) and state.get("gcode_state") is not None:
        return str(state["gcode_state"])
    return None


def _layer_advanced(data: dict[str, Any]) -> bool:
    """Has layer_num crossed into positive (the contract §6.0.1 boundary)?

    True only when the report carries an explicit positive ``layer_num``.
    Deltas where the key is absent return False (no signal). The watcher
    in :meth:`_read_bus` is idempotent — repeated True observations just
    re-publish the signal, which the FSM ignores once PREPARING has
    advanced.
    """
    if "layer_num" in data:
        ln = data["layer_num"]
    else:
        state = data.get("state")
        ln = state.get("layer_num") if isinstance(state, dict) else None
    if ln is None:
        return False
    try:
        return int(ln) > 0
    except (TypeError, ValueError):
        return False


def _ams_engaged(data: dict[str, Any]) -> bool:
    """Has the feed system actually engaged a source (the real progress
    signal for FED_NO_PROGRESS)?

    Hard lesson, 2026-05-19: ``mc_percent``/``layer_num`` *advance during a
    dry run* (the air-print hit layer 43 at mc_percent 32) — REPORT §7 said
    so and I ignored it. The signal that actually discriminates printing from
    air is ``ams.tray_now``: in **both** §6.3 and the recurrence it was
    *never set*; on a healthy print it is set to the loaded tray during the
    start-gcode material load, *before* the first layer. So gate on that.

    ``255`` = nothing selected; ``""``/absent = not engaged. A real AMS tray
    (0–15) or the external/VT tray (254) both count as "the feed engaged".
    """
    ams = data.get("ams")
    if not isinstance(ams, dict):
        state = data.get("state")
        ams = state.get("ams") if isinstance(state, dict) else None
    if not isinstance(ams, dict):
        return False
    tn = ams.get("tray_now")
    if tn is None:
        return False
    s = str(tn).strip()
    return s.isdigit() and s != "255"
