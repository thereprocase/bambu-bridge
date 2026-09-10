"""FastAPI app factory + lifespan (spec 3 ``main.py``).

The lifespan owns process-wide resources: it connects the DB, builds the
Registry, reloads persisted printers, and tears everything down on shutdown.
Routers are thin; all domain state hangs off ``app.state``.

``app`` is created at import so ``uvicorn bambu_bridge.main:app`` works
(matches the systemd unit and README).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import APIRouter, FastAPI

from bambu_bridge import __version__
from bambu_bridge.api import (
    advanced,
    camera,
    control,
    events,
    filament,
    files,
    jobs,
    pairing,
    printers,
    queue,
    spools,
    status,
    viz,
)
from bambu_bridge.api import errors as api_errors
from bambu_bridge.config import Settings, configure_logging
from bambu_bridge.db.jobs import (
    Database,
    EventRepo,
    FilamentMemoryRepo,
    JobRepo,
    NotificationPrefsRepo,
    PrinterRepo,
    SlicedDateRepo,
)
from bambu_bridge.pairing import PairingStore
from bambu_bridge.protocol.ftps import SlicedDateMemo
from bambu_bridge.push.ntfy import NotificationService, NtfyDispatcher
from bambu_bridge.service.event_persister import EventPersister
from bambu_bridge.service.jobs import JobManager
from bambu_bridge.service.registry import Registry
from bambu_bridge.service.viz_cache import VizCache

log = structlog.get_logger(__name__)

_BRIDGE_API_KEY_MIN_LEN = 32  # bytes; anything shorter is likely weak / default


def create_app(
    settings: Settings | None = None,
    *,
    mqtt_port: int = 8883,
    ftps_port: int = 990,
    camera_port: int = 6000,
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.bridge_log_level, settings.bridge_log_format,
                      secrets=(settings.bridge_api_key, settings.bridge_viz_token))

    # Warn loudly when the API key is set but suspiciously short — empty is
    # handled by auth.py (fail-closed 503); a short key is a misconfiguration
    # that could pass auth checks but provide weak entropy.
    if settings.bridge_api_key and len(settings.bridge_api_key) < _BRIDGE_API_KEY_MIN_LEN:
        log.warning(
            "bridge.weak_api_key",
            msg=(
                f"BRIDGE_API_KEY has only {len(settings.bridge_api_key)} characters; "
                f"minimum recommended is {_BRIDGE_API_KEY_MIN_LEN}. "
                "Use a randomly-generated key of at least 32 characters."
            ),
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings.bridge_db_path)
        await db.connect()
        registry = Registry(
            PrinterRepo(db),
            jobs=JobRepo(db),
            filament_memory=FilamentMemoryRepo(db),
            mqtt_port=mqtt_port,
            camera_port=camera_port,
            camera_linger_s=settings.bridge_camera_linger_s,
        )
        dispatcher = NtfyDispatcher(settings.ntfy_url, settings.ntfy_topic)
        notifier = NotificationService(dispatcher, NotificationPrefsRepo(db))
        # The events persister (PR B) — writes each named bus event into
        # the `events` table so the NotificationsScreen can list them.
        # Registered before `load()` so persisted printers attach too.
        persister = EventPersister(EventRepo(db))
        registry.add_listener(persister.attach)
        registry.add_listener(notifier.attach)
        sliced_date_memo = SlicedDateMemo(repo=SlicedDateRepo(db))
        viz_cache = VizCache(ftps_port=ftps_port, sliced_date_memo=sliced_date_memo)
        job_manager = JobManager(
            JobRepo(db),
            EventRepo(db),
            registry,
            ftps_port=ftps_port,
            spaghetti_detection=settings.bridge_spaghetti_detection,
            viz_cache=viz_cache,
        )
        # Must be registered before registry.load() so persisted printers
        # get the external-print watcher attached on startup (same as
        # EventPersister and NotificationService).
        registry.add_listener(job_manager.attach)
        await registry.load()

        # Startup backfill: if any printer has a live job (submitted/preparing/
        # printing), schedule a viz pre-warm immediately.  This closes the
        # restart-mid-print hole where SlicedDateMemo was lost on restart.
        # The pre-warm fills the sliced-date memo for the current file via
        # the same opportunistic path used during normal operation.
        job_repo_for_backfill = JobRepo(db)
        for printer_service in registry.list():
            started_at = await job_repo_for_backfill.latest_active_started_at(
                printer_service.serial
            )
            if started_at is not None:
                job_name = printer_service.summary().get("subtask_name") or ""
                if job_name:
                    log.info(
                        "bridge.startup_backfill",
                        printer_id=printer_service.serial,
                        job_name=job_name,
                    )
                    viz_cache.schedule_prewarm(
                        printer_service.serial,
                        printer_service.ip,
                        printer_service.access_code,
                        job_name,
                    )

        app.state.settings = settings
        app.state.pairing = (PairingStore(settings.bridge_pairing_dir)
                             if settings.bridge_pairing_dir else None)
        app.state.db = db
        app.state.registry = registry
        app.state.jobs = job_manager
        app.state.notifier = notifier
        app.state.event_persister = persister
        app.state.ftps_port = ftps_port
        # The shared VizCache is on app.state so the HTTP endpoints can find it
        # via _get_viz_cache(request); JobManager already holds the same object.
        app.state.viz_cache_obj = viz_cache
        # Sliced-date memo: filled opportunistically when the bridge downloads a
        # .gcode.3mf for viz pre-warm.  Exposed on app.state so the files
        # listing endpoint can consult it without a direct dependency on VizCache.
        app.state.sliced_date_memo = sliced_date_memo
        log.info("bridge.started", version=__version__)
        try:
            yield
        finally:
            await job_manager.shutdown()
            await persister.shutdown()
            await notifier.shutdown()
            await registry.shutdown()
            await db.close()
            log.info("bridge.stopped")

    app = FastAPI(title="Bambu Bridge", version=__version__, lifespan=lifespan)
    api_errors.install(app)  # universal envelope on every error path (contract §2)

    # One app (shared app.state); a prefixed router rather than a mounted
    # sub-app so request.app / websocket.app stays the main app.
    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(printers.router)
    v1.include_router(printers.read_router)
    v1.include_router(status.router)
    v1.include_router(control.router)
    v1.include_router(advanced.router)
    v1.include_router(files.router)
    v1.include_router(jobs.router)
    v1.include_router(camera.router)
    v1.include_router(queue.router)
    v1.include_router(spools.router)
    v1.include_router(events.router)
    v1.include_router(viz.router)
    v1.include_router(filament.router)
    v1.include_router(pairing.router)

    @v1.get("/health", tags=["system"])  # no auth (spec 6)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @v1.get("/version", tags=["system"])  # no auth (spec 6)
    def version() -> dict[str, str]:
        return {"version": __version__}

    app.include_router(v1)
    # Root-level SPA shell (/, /app, /app/{path}); unauthenticated static files.
    # Included AFTER v1 so /api/v1/* always wins on any path overlap.
    app.include_router(viz.app_shell_router)
    return app


app = create_app()


def run() -> None:
    """Console-script entrypoint (`bambu-bridge`)."""
    from bambu_bridge.local_server import cli

    cli()


if __name__ == "__main__":
    run()
