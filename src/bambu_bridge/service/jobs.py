"""Job state machine + manager (contract §7.4, PR B remap).

::

    queued ─► uploading ─► submitted ─► preparing ─► printing ─► completed
                  │            │           │           │
                  └─► failed ◄─┴───────────┴───────────┘
    queued ─► canceled        (cancel before the printer acks)

`submitted` means dispatch was attempted, NOT a printer acknowledgement.
The durable start operation distinguishes dispatch from observation.
`preparing` is the heat-soak + bed-level window (`gcode_state == RUNNING`
but `layer_num == 0`). `printing` is gated on `layer_num > 0` — the
single most important rule in the contract.

Every transition is persisted to the ``events`` table (event_type
``state_change``); the job row's ``state`` is the latest transition.
Driven by the printer's named bus events (M2):

* ``print_started``   gcode_state -> RUNNING   => submitted -> preparing
* ``print_progress``  layer_num crossed 0      => preparing -> printing
* ``print_completed`` gcode_state -> FINISH    => * -> completed
* ``print_failed`` / ``error``                 => -> failed

A managed start without RUNNING within 60 s remains outcome-unknown and
retains printer ownership. The FED_NO_PROGRESS watchdog (600 s, AMS engagement)
runs from the preparing-onward window — it is the §6.3 hard-fail safety
net independent of the layer_num signal.

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
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from bambu_bridge.db.jobs import EventRepo, Job, JobRepo, JobState
from bambu_bridge.db.starts import StartConflict, StartRepo
from bambu_bridge.protocol.ftps import FtpsTransfer
from bambu_bridge.service.events import Event
from bambu_bridge.service.registry import PrinterNotFoundError, Registry
from bambu_bridge.service.viz_cache import VizCache
from bambu_bridge.slicedoc import project_file_command, sd_filename, validate
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
# the tray engages during the start-gcode load, well within a few minutes.
_FEED_DEADLINE_S = 600

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
    JobState.SUBMITTED: {JobState.PREPARING, JobState.FAILED, JobState.CANCELED},
    JobState.PREPARING: {
        JobState.PRINTING,
        JobState.COMPLETED,
        JobState.FAILED,
        JobState.CANCELED,
    },
    JobState.PRINTING: {JobState.COMPLETED, JobState.FAILED, JobState.CANCELED},
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
        viz_cache: VizCache | None = None,
    ) -> None:
        self._jobs = jobs
        self.starts = StartRepo(jobs._db)
        self._events = events
        self._registry = registry
        self._ftps_port = ftps_port
        self._spaghetti_detection = spaghetti_detection
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
        operation_id: str | None = None,
    ) -> Job:
        """Create a queued job and kick off its lifecycle task."""
        operation_id = operation_id or uuid.uuid4().hex
        payload = {
            "file_name": file_name,
            "ams_mapping": ams_mapping,
            "content_sha256": hashlib.sha256(file_bytes).hexdigest(),
        }
        operation, created = await self._claim(operation_id, printer_id, payload)
        job = await self._jobs.get(operation["job_id"])
        if job is None:
            raise StartConflict("Start identity is retained, but its job history was removed")
        if not created:
            return job
        run = JobRun(
            job,
            file_bytes,
            self._jobs,
            self._events,
            self._registry,
            ftps_port=self._ftps_port,
            ams_mapping=ams_mapping,
            spaghetti_detection=self._spaghetti_detection,
            starts=self.starts,
            operation_id=operation_id,
        )
        self._runs[job.id] = run
        run.start()
        assert run._task is not None
        run._task.add_done_callback(lambda _task: self._runs.pop(job.id, None))
        return job

    @staticmethod
    def require_idle(service: Any) -> None:
        """A connected socket alone is not a fresh printer observation."""
        snapshot = service.snapshot()
        session = snapshot.get("session", {})
        timestamp = session.get("last_telemetry_at")
        try:
            observed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            age = (datetime.now(UTC) - observed).total_seconds()
        except (ValueError, TypeError):
            age = float("inf")
        if (
            not service.connected
            or session.get("connected") is not True
            or not 0 <= age <= 15
            or snapshot.get("phase") not in ("idle", "completed")
        ):
            raise StartConflict("Printer must have fresh, connected idle/finished telemetry")

    async def _claim(
        self,
        operation_id: str,
        printer_id: str,
        payload: dict[str, Any],
        *,
        queue_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        service = self._registry.get(printer_id)
        existing = await self.starts.get(operation_id)
        if existing is None:
            self.require_idle(service)
        return await self.starts.claim(operation_id, printer_id, payload, queue_id=queue_id)

    async def start_stored(
        self,
        operation_id: str,
        printer_id: str,
        file_name: str,
        file_path: str,
        ams_mapping: list[int] | None,
        *,
        queue_id: str | None = None,
        command_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Root-only is the current command contract. Do not silently ignore a
        # directory or normalize a different card file while claiming it.
        if "/" in file_name or "\\" in file_name or file_path != "/" + file_name:
            raise StartConflict("Stored-file starts currently require an exact root file path")
        payload: dict[str, Any] = {
            "file_name": file_name,
            "file_path": file_path,
            "ams_mapping": ams_mapping,
        }
        if command_fields is not None:
            payload["command_fields"] = command_fields
        operation, created = await self._claim(operation_id, printer_id, payload, queue_id=queue_id)
        if not created:
            return operation
        job = await self._jobs.get(operation["job_id"])
        assert job is not None
        run = JobRun(
            job,
            b"",
            self._jobs,
            self._events,
            self._registry,
            ftps_port=self._ftps_port,
            ams_mapping=ams_mapping,
            spaghetti_detection=self._spaghetti_detection,
            starts=self.starts,
            operation_id=operation_id,
            stored_file=True,
            command_fields=command_fields,
        )
        self._runs[job.id] = run
        run.start()
        assert run._task is not None
        run._task.add_done_callback(lambda _task: self._runs.pop(job.id, None))
        return operation

    async def cancel(self, job_id: str) -> Job:
        job = await self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        run = self._runs.get(job_id)
        if run is not None and not job.state.terminal:
            await run.request_cancel()
        return await self._jobs.get(job_id) or job

    async def quiesce_start(self, job_id: str) -> None:
        run = self._runs.get(job_id)
        if run is not None:
            await run.stop()

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

        async def start_raw(fields: dict[str, Any]) -> None:
            # Native/raw clients lack a durable caller-generated intent ID.
            # They still share the same atomic printer reservation. Never
            # silently replace their slice/AMS/options with app defaults.
            url = fields.get("url")
            prefix = "file:///sdcard/"
            if not isinstance(url, str) or not url.startswith(prefix):
                raise StartConflict("Managed starts require a root SD-card file URL")
            name = url[len(prefix) :]
            if not name.endswith(".gcode.3mf") or fields.get("param") != "Metadata/plate_1.gcode":
                raise StartConflict("Managed starts currently support plate_1 in a gcode.3mf")
            forwarded = {k: v for k, v in fields.items() if k not in ("command", "sequence_id")}
            allowed = project_file_command(name, use_ams=False, ams_mapping=[])
            if set(forwarded) - set(allowed):
                raise StartConflict("Unsupported native print options; no command sent")
            sequence = fields.get("sequence_id")
            if sequence is not None:
                if type(sequence) not in (str, int) or not 0 < len(str(sequence)) <= 64:
                    raise StartConflict("Invalid native request sequence")
                forwarded["sequence_id"] = str(sequence)
            mapping = fields.get("ams_mapping")
            if mapping is not None and (
                not isinstance(mapping, list)
                or any(type(slot) is not int or slot < -1 or slot > 3 for slot in mapping)
            ):
                raise StartConflict("Invalid native AMS mapping")
            await self.start_stored(
                uuid.uuid4().hex,
                service.serial,
                name,
                "/" + name,
                mapping,
                command_fields=forwarded,
            )

        service.start_handler = start_raw
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
                    elif ev.name in ("print_completed", "print_failed"):
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
        live = await self._jobs.list(printer_id=printer_id, limit=10)
        for job in live:
            if job.state not in _LIVE_STATES:
                continue
            meta: dict[str, Any] = {}
            if job.metadata_json:
                with contextlib.suppress(ValueError, TypeError):
                    meta = json.loads(job.metadata_json)
            if meta.get("origin") != "external":
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
                await self._jobs.update(
                    job.id,
                    state=JobState.FAILED,
                    finished_at=now,
                    error_code="printer_error",
                )
                await self._events.add(
                    printer_id=printer_id,
                    job_id=job.id,
                    event_type="state_change",
                    payload={
                        "from": job.state.value,
                        "to": JobState.FAILED.value,
                        "trigger": "printer_error",
                    },
                )
            log_.info("jobs.external_closed", job_id=job.id, ev_name=event_name)
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
        starts: StartRepo | None = None,
        operation_id: str | None = None,
        stored_file: bool = False,
        command_fields: dict[str, Any] | None = None,
    ) -> None:
        self._job = job
        self._file_bytes = file_bytes
        self._jobs = jobs
        self._events = events
        self._registry = registry
        self._ftps_port = ftps_port
        self._ams_mapping = ams_mapping
        self._spaghetti_detection = spaghetti_detection
        self._starts = starts
        self._operation_id = operation_id
        self._stored_file = stored_file
        self._command_fields = command_fields
        self._dispatch_started = False
        self._dispatch_at = float("inf")
        self._observed_session: str | None = None
        self._signals: asyncio.Queue[str] = asyncio.Queue()
        self._cancel = asyncio.Event()
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
        - ``completed`` / ``failed`` / ``stopped`` — terminal signals
        """
        async for ev in sub:  # type: Event
            assert isinstance(ev, Event)
            if self._starts:
                if not self._dispatch_started or ev.observed_at < self._dispatch_at:
                    continue
                service = self._registry.get(self._job.printer_id)
                if ev.name == "connection_lost":
                    self._observed_session = None
                    await self._fail("connection_lost")
                    continue
                if ev.name == "print_started" and ev.session_id:
                    expected = (self._command_fields or {}).get("subtask_name") or (
                        sd_filename(self._job.file_name).removesuffix(".gcode.3mf")
                    )
                    if ev.data.get("subtask_name") != expected:
                        await self._fail("uncorrelated_printer_start")
                        continue
                    self._observed_session = ev.session_id
                if (
                    not self._observed_session
                    or ev.session_id != self._observed_session
                    or service.bus.session_id != self._observed_session
                ):
                    continue
            if ev.name == "print_started":
                self._signals.put_nowait("running")
            elif ev.name == "print_progress":
                self._signals.put_nowait("layer_advanced")
            elif ev.name == "print_completed":
                self._signals.put_nowait("completed")
            elif ev.name in ("print_failed", "error"):
                self._signals.put_nowait("failed")
            elif ev.type in ("delta", "snapshot"):
                gs = _gcode_state(ev.data)
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

        if (
            self._starts
            and self._operation_id
            and not await self._starts.transition(self._operation_id, ("accepted",), "validating")
        ):
            return
        if self._stored_file:
            ftps = FtpsTransfer(service.ip, service.access_code, port=self._ftps_port)
            self._file_bytes = await ftps.download_bytes(self._job.file_name, remote_dir="")

        # Gate the .gcode.3mf BEFORE touching the printer. This is the §6.3
        # fix: an inconsistent AMS binding, bad md5, or unsafe temperature is
        # rejected here — not discovered as "printed air" 17 min in.
        report = await asyncio.to_thread(
            validate, self._file_bytes, expected_ams_mapping=self._ams_mapping
        )
        if not report.ok:
            await self._fail(f"invalid_3mf: {'; '.join(report.issues)}")
            return

        if (
            self._starts
            and self._operation_id
            and not await self._starts.transition(self._operation_id, ("validating",), "staging")
        ):
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

        if self._cancel.is_set():
            await self._set(JobState.CANCELED, "canceled_before_dispatch")
            return
        if self._starts and self._operation_id:
            operation = await self._starts.get(self._operation_id)
            if operation is None or time.time() - operation["created_at"] > 300:
                await self._fail("intent_expired_before_dispatch")
                return
            JobManager.require_idle(service)
            if not await self._starts.transition(self._operation_id, ("staging",), "dispatching"):
                return
        self._dispatch_started = True

        def before_dispatch() -> None:
            if self._starts:
                JobManager.require_idle(service)
                if self._cancel.is_set():
                    raise StartConflict("Canceled before publish")
            self._dispatch_at = time.monotonic()

        await self._set(JobState.SUBMITTED, "project_file_dispatch_attempted")
        # url = file:///sdcard/<name>.gcode.3mf — the ONLY confirmed scheme
        # (REPORT §6.2). The old code used ftp://, flagged untested in §9.
        await service.send_command(
            "print",
            "project_file",
            **({"before_publish": before_dispatch} if self._starts else {}),
            **(
                self._command_fields
                if self._command_fields is not None
                else project_file_command(
                    self._job.file_name,
                    use_ams=bool(self._ams_mapping),
                    ams_mapping=self._ams_mapping or [],
                )
            ),
        )
        if self._starts and self._operation_id:
            await self._starts.transition(
                self._operation_id, ("dispatching",), "awaiting_observation"
            )

        # submitted -> preparing  (RUNNING within 60 s, else timeout-fail)
        sig = await self._wait_signal({"running", "cancel", "failed"}, timeout=_RUNNING_TIMEOUT_S)
        if sig == "cancel":
            await self._do_cancel(service, acked=False)
            return
        if sig in (None, "failed"):
            reason = "no_running_within_60s" if sig is None else "printer_error"
            await self._fail(reason)
            return
        await self._set(JobState.PREPARING, "gcode_running")

        # preparing -> printing  (layer_num > 0 OR completion, with the
        # FED_NO_PROGRESS watchdog still in force as the §6.3 hard safety).
        # `layer_advanced` is the canonical "printing began" signal per
        # contract §6.0.1; `progress` (AMS engaged) is the parallel safety
        # check — if neither fires within the deadline, abort. In healthy
        # prints both arrive within seconds of each other.
        sig = await self._wait_signal(
            {"layer_advanced", "progress", "completed", "failed", "cancel"},
            timeout=_FEED_DEADLINE_S,
        )
        if sig is None:
            await self._stop_owned(service)
            await self._fail("FED_NO_PROGRESS")
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
        accept = {"completed", "failed", "cancel"}
        spaghetti = self._start_spaghetti_monitor(service)
        if spaghetti is not None:
            accept.add("spaghetti")
        try:
            sig = await self._wait_signal(accept)
        finally:
            if spaghetti is not None:
                spaghetti.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await spaghetti
        if sig == "completed":
            await self._complete()
        elif sig == "cancel":
            await self._do_cancel(service, acked=True)
        elif sig == "spaghetti":
            await self._stop_owned(service)
            await self._fail("SPAGHETTI_DETECTED")
        else:
            await self._fail("printer_error")

    # ------------------------------------------------------------------ #
    # Transitions
    # ------------------------------------------------------------------ #

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
        if not await self._stop_owned(service):
            await self._fail("cancel_outcome_unknown")
            return
        if acked or self._starts:
            # Printer was printing — wait for it to confirm the stop (spec 8).
            confirmation = await self._wait_signal(
                {"stopped", "completed", "failed"}, timeout=_CANCEL_CONFIRM_S
            )
            if self._starts and confirmation is None:
                await self._fail("stop_not_confirmed")
                return
        await self._set(JobState.CANCELED, "user_cancel")

    async def _stop_owned(self, service: Any) -> bool:
        """A historical job must never stop the next physical session."""
        if self._starts and self._operation_id:
            operation = await self._starts.get(self._operation_id)
            if (
                not operation
                or operation["state"] != "observed_started"
                or not self._observed_session
                or not service.connected
                or service.bus.session_id != self._observed_session
            ):
                return False
        try:

            def before_stop() -> None:
                if not service.connected or service.bus.session_id != self._observed_session:
                    raise StartConflict("Physical session changed before stop")

            await service.send_command(
                "print", "stop", **({"before_publish": before_stop} if self._starts else {})
            )
            return True
        except Exception:
            return False

    async def _complete(self) -> None:
        await self._set(JobState.COMPLETED, "gcode_finish")

    async def _fail(self, reason: str) -> None:
        if self._starts and self._operation_id and self._dispatch_started:
            await self._starts.transition(
                self._operation_id,
                ("dispatching", "awaiting_observation", "observed_started"),
                "outcome_unknown",
                reason=reason.split(":", 1)[0],
            )
            # Do not call a transport/observation failure a physical print
            # failure or free the printer for a retry.
            return
        await self._jobs.update(self._job.id, finished_at=int(time.time()), error_code=reason)
        await self._set(JobState.FAILED, reason)

    async def _set(self, new: JobState, trigger: str) -> None:
        if self._starts and self._dispatch_started and new.terminal:
            service = self._registry.get(self._job.printer_id)
            if (
                not self._observed_session
                or not service.connected
                or service.bus.session_id != self._observed_session
            ):
                await self._fail("terminal_session_not_confirmed")
                return
        cur = self._job.state
        if new != cur and new not in _ALLOWED.get(cur, set()):
            self._log.warning("job.illegal_transition", frm=cur.value, to=new.value)
            return
        fields: dict[str, Any] = {"state": new}
        if new is JobState.COMPLETED:
            now = int(time.time())
            fields.update(
                progress_pct=100.0, finished_at=now, duration_s=now - (self._job.started_at or now)
            )
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
        if self._starts and self._operation_id:
            if new in (JobState.PREPARING, JobState.PRINTING):
                await self._starts.transition(
                    self._operation_id,
                    ("awaiting_observation", "observed_started"),
                    "observed_started",
                )
            elif new.terminal:
                state = (
                    "rejected_before_dispatch"
                    if new is JobState.FAILED
                    else "canceled_before_dispatch"
                    if not self._dispatch_started
                    else new.value
                )
                await self._starts.transition(
                    self._operation_id,
                    (
                        "accepted",
                        "validating",
                        "staging",
                        "awaiting_observation",
                        "observed_started",
                    ),
                    state,
                    release=True,
                )


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
