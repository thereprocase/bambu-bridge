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

Reconciled, not signalled. Like OrcaSlicer's monitor (StatusPanel::
update_subtask), a job derives its phase from the printer's *current* state
(``PrinterService.print_view()``) on every report, reconnect, cancel or
deadline, so a missed, dropped or reordered event cannot strand it:

* PREPARE / RUNNING / PAUSE      => submitted -> preparing (Orca PrintJob
  wait_fn: the printer accepted the job). PREPARE has no deadline.
* layer_num > 0 or a tray newly engaged after RUNNING => preparing -> printing
* FINISH => completed; FAILED => failed; IDLE with an empty job => interrupted
* print_error / HMS codes never end a job (a pause is resumable); they only
  feed notifications (Orca StatusPanel::update_error_message).

A ``submitted`` job the printer does not accept within 60 s fails. The
FED_NO_PROGRESS watchdog (default 1800 s from RUNNING) stops a print that
never progresses. Cancel, FED_NO_PROGRESS and spaghetti share one stop
path: ``print stop`` is retried until delivered, and the job ends only
when the printer reports it stopped.

External prints and restarts
----------------------------
When the bridge witnesses a ``print_started`` event for printer P and no
live (submitted/preparing/printing) job row exists, it inserts an
"external" row (``{"origin": "external"}`` in ``metadata_json``) so
restart-recovery works for ALL prints. Live rows no JobRun owns (external
prints, and bridge jobs left by a restart) are reconciled against the
printer's state by :meth:`JobManager._close_orphans`.
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
from bambu_bridge.slicedoc import project_file_command, sd_filename, subtask_name
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
        # Printers with a job between submit() and the end of its run: one at a
        # time. Checked and claimed with no await in between (asyncio-atomic).
        self._busy: set[str] = set()
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
        """Create a queued job and kick off its lifecycle task.

        Raises :class:`PrinterBusyError` while another bridge job for the
        printer is still running (a double POST or two queue starts).
        """
        self._registry.get(printer_id)  # PrinterNotFoundError -> 404 at API
        if printer_id in self._busy:
            raise PrinterBusyError(printer_id)
        self._busy.add(printer_id)
        try:
            return await self._start(printer_id, file_bytes, file_name, ams_mapping)
        except BaseException:
            self._busy.discard(printer_id)
            raise

    async def _start(
        self, printer_id: str, file_bytes: bytes, file_name: str, ams_mapping: list[int] | None
    ) -> Job:
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

        def _done(_task: asyncio.Task[None]) -> None:
            self._runs.pop(job.id, None)
            self._busy.discard(printer_id)

        run._task.add_done_callback(_done)
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

        On every change of the printer's state, reconcile the live rows that
        no JobRun owns (external prints, and bridge jobs from before a
        restart) against it: see :meth:`_close_orphans`.
        """
        log_ = log.bind(printer_id=service.serial)
        warmed_job: str | None = None
        seen: tuple[Any, ...] | None = None
        async with service.bus.subscribe() as sub:
            async for ev in sub:
                try:
                    view = service.print_view()
                    key = (view["gcode_state"], view["lost"], view.get("subtask_name"))
                    if view["gcode_state"] is not None and key != seen:
                        seen = key
                        await self._close_orphans(service.serial, view, log_)
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

    async def recover(self) -> None:
        """Startup: a QUEUED/UPLOADING row has no run in a new process and can
        never progress (its bytes are gone), so it fails instead of staying
        live forever."""
        for state in (JobState.QUEUED, JobState.UPLOADING):
            for job in await self._jobs.list(state=state, limit=200):
                if job.id not in self._runs:
                    await self._close_row(job, JobState.FAILED, "bridge_restarted")

    async def _close_orphans(self, printer_id: str, view: dict[str, Any], log_: Any) -> None:
        """Reconcile live rows that no JobRun owns against the printer's state.

        Level-triggered (Orca is_printing_finished: FINISH/FAILED): external
        prints and bridge jobs left by a restart end when the printer says so,
        whether or not the bridge saw the edge.
        """
        gs, lost, current = view["gcode_state"], view["lost"], view.get("subtask_name")
        active = gs in ("PREPARE", "RUNNING", "PAUSE")
        for job in await self._jobs.list(printer_id=printer_id, limit=50):
            if job.state not in _LIVE_STATES or job.id in self._runs:
                continue
            if current and gs != "IDLE" and subtask_name(job.file_name) != current:
                # The printer is on another job (Orca names it by subtask_name),
                # so this row's print is gone, whatever state the printer is in.
                await self._close_row(job, JobState.INTERRUPTED, "printer_job_replaced")
                log_.info("jobs.orphan_closed", job_id=job.id, gcode_state=gs)
                continue
            if job.state is JobState.SUBMITTED:
                # Sent before a restart: the printer either took it or it is gone.
                if active:
                    await self._close_row(job, JobState.PREPARING, "printer_preparing")
                elif time.time() - (job.started_at or 0) > _RUNNING_TIMEOUT_S:
                    await self._close_row(job, JobState.FAILED, "no_running_within_60s")
                continue
            if gs == "FINISH":
                await self._close_row(job, JobState.COMPLETED, "gcode_finish")
            elif gs == "FAILED":
                await self._close_row(job, JobState.FAILED, "printer_error")
            elif lost:
                await self._close_row(job, JobState.INTERRUPTED, "printer_job_lost")
            else:
                continue
            log_.info("jobs.orphan_closed", job_id=job.id, gcode_state=gs)

    async def _close_row(self, job: Job, state: JobState, trigger: str) -> None:
        now = int(time.time())
        fields: dict[str, Any] = {"state": state}
        if state is JobState.COMPLETED:
            fields.update(progress_pct=100.0, duration_s=now - (job.started_at or now))
        elif state is not JobState.PREPARING:
            fields["error_code"] = trigger
        if state.terminal:
            fields["finished_at"] = now
        await self._jobs.update(job.id, **fields)
        await self._events.add(
            printer_id=job.printer_id,
            job_id=job.id,
            event_type="state_change",
            payload={"from": job.state.value, "to": state.value, "trigger": trigger},
        )

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


class PrinterBusyError(RuntimeError):
    """A bridge job is already running on this printer."""


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
        self._cancel = asyncio.Event()
        self._wake = asyncio.Event()
        self._stop_reason: str | None = None  # user_cancel | FED_NO_PROGRESS | SPAGHETTI_DETECTED
        self._stop_sent = False
        self._stop_failed = False
        self._task: asyncio.Task[None] | None = None
        self._log = log.bind(job_id=job.id, printer_id=job.printer_id)

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
        self._request_stop("user_cancel")

    def _request_stop(self, reason: str) -> None:
        if self._stop_reason is None:  # the first reason wins; one stop path
            self._stop_reason = reason
            self._wake.set()

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
            reader = asyncio.create_task(self._wake_on_reports(sub))
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

    async def _wake_on_reports(self, sub: Any) -> None:
        """Every report (or reconnect) re-runs the reconcile; content is not used."""
        async for _ev in sub:
            self._wake.set()

    async def _lifecycle(self, service: Any) -> None:
        if self._cancel.is_set():
            await self._set(JobState.CANCELED, "canceled_before_upload")
            return

        # Every submit() caller (POST /jobs, queue start, Orca upload) has
        # already run slicedoc.validate() on these exact bytes.
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
        if self._cancel.is_set():  # canceled during the upload: never start it
            await self._set(JobState.CANCELED, "canceled_before_start")
            return

        await self._set(JobState.SUBMITTED, "project_file_published")
        before = service.print_view()
        # url = file:///sdcard/<name>.gcode.3mf — the ONLY confirmed scheme
        # (REPORT §6.2). The old code used ftp://, flagged untested in §9.
        await service.send_command(
            "print",
            "project_file",
            **project_file_command(
                self._job.file_name,
                # Orca SelectMachineDialog: use_ams iff some filament is on
                # an AMS tray; an all -1 mapping means the external spool.
                use_ams=any(tray >= 0 for tray in self._ams_mapping or []),
                ams_mapping=self._ams_mapping or [],
            ),
        )
        await self._track(service, before)

    async def _track(self, service: Any, before: dict[str, Any]) -> None:
        """Reconcile the job against the printer's current state until it ends.

        Level-triggered, like OrcaSlicer's StatusPanel::update_subtask: every
        report, reconnect, cancel or deadline re-reads ``service.print_view()``
        and derives the job's phase from it, so a missed or reordered event
        cannot strand the job.
        """
        loop = asyncio.get_running_loop()
        ack_by = loop.time() + _RUNNING_TIMEOUT_S
        feed_s = self._feed_deadline_s if self._feed_deadline_s is not None else _FEED_DEADLINE_S
        feed_by: float | None = None
        ran = False  # the printer reported RUNNING/PAUSE for this job
        spaghetti: asyncio.Task[None] | None = None
        try:
            while True:
                self._wake.clear()
                await self._deliver_stop(service)
                if self._job.state.terminal:
                    return
                v = service.print_view()
                gs, now = v["gcode_state"], loop.time()
                state = self._job.state

                if state is JobState.SUBMITTED:
                    # Orca PrintJob wait_fn: the job is accepted once the printer is in a
                    # printing status (PREPARE/RUNNING/PAUSE).
                    if self._stop_sent:
                        await self._set(JobState.CANCELED, "user_cancel")
                        return
                    if gs in ("PREPARE", "RUNNING", "PAUSE"):
                        trigger = "printer_preparing" if gs == "PREPARE" else "gcode_running"
                        await self._set(JobState.PREPARING, trigger)
                        continue
                    if gs == "FAILED" and before["gcode_state"] != "FAILED":
                        await self._fail("printer_error")
                        return
                    elif now >= ack_by:
                        await self._fail("no_running_within_60s")
                        return
                elif gs is not None:
                    done = (
                        gs in ("FINISH", "FAILED")
                        or v["lost"]
                        or (gs == "IDLE" and (self._stop_sent or not ran))
                    )
                    if done:
                        await self._close(gs, lost=v["lost"], ran=ran)
                        return
                    if gs in ("RUNNING", "PAUSE"):
                        ran = True
                        if feed_by is None:
                            feed_by = now + feed_s
                    # FED_NO_PROGRESS (§6.3): after RUNNING, either layer_num > 0 or a
                    # tray newly engaged since the submit counts as progress. Layers
                    # also advance during an air print (2026-05-19: layer 43, tray
                    # never set); the owner chose to keep this rule so external-spool
                    # prints work and to rely on the printer's own air-print
                    # detection for that case.
                    if state is JobState.PREPARING and ran:
                        progressed = v["layer_num"] > 0 or _tray_engaged_since(before, v)
                        if progressed:
                            trigger = "layer_started" if v["layer_num"] > 0 else "ams_engaged"
                            await self._set(JobState.PRINTING, trigger)
                            spaghetti = self._start_spaghetti_monitor(service)
                        elif feed_by is not None and now >= feed_by:
                            self._request_stop("FED_NO_PROGRESS")

                deadlines = []
                if self._job.state is JobState.SUBMITTED:
                    deadlines.append(ack_by)
                if self._job.state is JobState.PREPARING and feed_by and not self._stop_reason:
                    deadlines.append(feed_by)
                timeout = max(0.0, min(deadlines) - loop.time()) if deadlines else None
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout)
        finally:
            if spaghetti is not None:
                spaghetti.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await spaghetti

    async def _deliver_stop(self, service: Any) -> None:
        """Send ``print stop`` once for a pending stop; retried on the next wake if it fails."""
        if self._stop_reason is None or self._stop_sent:
            return
        try:
            await service.send_command("print", "stop")
        except Exception as exc:  # noqa: BLE001 — link down: keep the job live, retry
            self._log.warning("job.stop_not_delivered", reason=self._stop_reason, error=str(exc))
            if not self._stop_failed:  # record the first failure, not every retry
                self._stop_failed = True
                await self._event("stop_not_delivered", reason=self._stop_reason, error=str(exc))
            return
        self._stop_sent = True
        # The job ends only when the printer reports the stop (Orca
        # command_task_abort keeps no local "canceled" state either).
        await self._event("stop_sent", reason=self._stop_reason)

    async def _event(self, event_type: str, **payload: Any) -> None:
        await self._events.add(
            printer_id=self._job.printer_id,
            job_id=self._job.id,
            event_type=event_type,
            payload=payload,
        )

    async def _close(self, gs: str, *, lost: bool, ran: bool) -> None:
        """The printer left the job: record how (Orca is_printing_finished = FINISH/FAILED)."""
        if self._stop_sent:
            if self._stop_reason == "user_cancel":
                await self._set(JobState.CANCELED, "user_cancel")
            else:
                await self._fail(self._stop_reason or "printer_error")
        elif gs == "FINISH":
            await self._complete()
        elif lost and ran:
            await self._jobs.update(
                self._job.id, finished_at=int(time.time()), error_code="printer_job_lost"
            )
            await self._set(JobState.INTERRUPTED, "printer_job_lost")
        else:
            await self._fail("printer_error")

    def _start_spaghetti_monitor(self, service: Any) -> asyncio.Task[None] | None:
        """Opt-in camera failure watch, scoped to the PRINTING phase.

        Returns the running task, or None when disabled / no camera. The
        callback only requests a stop — the abort stays in the reconcile
        loop (one stop path), and the trigger is debounced upstream in the
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
            on_spaghetti=lambda _c: self._request_stop("SPAGHETTI_DETECTED"),
            printer_id=self._job.printer_id,
        )
        return asyncio.create_task(monitor.run())

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
        await self._set(JobState.FAILED, reason)

    async def _set(self, new: JobState, trigger: str) -> None:
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
            payload={"from": cur.value, "to": new.value, "trigger": trigger},
        )
        self._log.info("job.transition", frm=cur.value, to=new.value, trigger=trigger)


def _tray_engaged_since(before: dict[str, Any], now: dict[str, Any]) -> bool:
    """The feed engaged a tray during this job (FED_NO_PROGRESS satisfaction).

    Level form of the old edge signal: a tray_now already engaged before the
    project_file does not count, a change to an engaged tray does.
    """
    return _ams_engaged({"ams": {"tray_now": now["tray_now"]}}) and (
        now["tray_now"] != before["tray_now"]
    )


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
