"""Registry — the set of live printers (spec 3 ``service/registry.py``).

Maps ``serial -> PrinterService`` and is the single owner of printer
lifecycle: persistence (via :class:`PrinterRepo`) and the MQTT task. The API
layer holds one Registry (created in the FastAPI lifespan, M3) and never talks
to a :class:`PrinterService` it didn't get from here.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog

from bambu_bridge.db.jobs import FilamentMemory, FilamentMemoryRepo, JobRepo, Printer, PrinterRepo
from bambu_bridge.service.printer import PrinterService

PrinterListener = Callable[[PrinterService], Awaitable[None]]

log = structlog.get_logger(__name__)


class PrinterNotFoundError(KeyError):
    """No registered printer with that serial."""


class PrinterExistsError(ValueError):
    """A printer with that serial is already registered."""


class Registry:
    def __init__(
        self,
        repo: PrinterRepo,
        *,
        jobs: JobRepo | None = None,
        filament_memory: FilamentMemoryRepo | None = None,
        mqtt_port: int = 8883,
        camera_port: int = 6000,
        camera_linger_s: float = 10.0,
    ) -> None:
        self._repo = repo
        # Optional jobs repo, used only to give each PrinterService a
        # restart-recovery hook for job.started_at (the P1S firmware reports no
        # start time, so the bridge synthesizes it; after a deploy mid-print
        # the in-memory value is gone and is recovered from jobs.db). None in
        # tests/contexts that don't wire jobs — recovery is then a no-op.
        self._jobs = jobs
        # Optional filament memory repo (G3). None in tests/contexts that
        # don't wire the DB — per-slot filament labels are then a no-op.
        self._filament_memory = filament_memory
        self._printers: dict[str, PrinterService] = {}
        # Non-standard ports: only set away from 8883/6000 for test servers or
        # a TLS-terminating proxy in front of the printer.
        self._mqtt_port = mqtt_port
        self._camera_port = camera_port
        self._camera_linger_s = camera_linger_s
        # Fired with each PrinterService as it comes up (load/add). The
        # notification service uses this to attach without the registry
        # importing push code.
        self._listeners: list[PrinterListener] = []

    def add_listener(self, listener: PrinterListener) -> None:
        self._listeners.append(listener)

    async def _announce(self, service: PrinterService) -> None:
        for listener in self._listeners:
            await listener(service)

    # ----------------------------------------------------------------- #
    # Lifecycle
    # ----------------------------------------------------------------- #

    async def load(self) -> None:
        """Recreate services for every persisted printer and start them."""
        for record in await self._repo.list():
            service = self._make_service(record)
            self._printers[record.id] = service
            await service.start()
            await self._announce(service)
        log.info("registry.loaded", count=len(self._printers))

    async def shutdown(self) -> None:
        for service in self._printers.values():
            await service.stop()
        self._printers.clear()
        log.info("registry.shutdown")

    # ----------------------------------------------------------------- #
    # CRUD
    # ----------------------------------------------------------------- #

    async def add(
        self,
        *,
        serial: str,
        ip: str,
        access_code: str,
        friendly_name: str,
        model: str | None = None,
        cert_fingerprint: str | None = None,
    ) -> PrinterService:
        if serial in self._printers:
            raise PrinterExistsError(serial)
        record = Printer(
            id=serial,
            friendly_name=friendly_name,
            ip=ip,
            access_code=access_code,
            model=model,
            added_at=int(time.time()),
            cert_fingerprint=cert_fingerprint,
        )
        await self._repo.add(record)
        service = self._make_service(record)
        self._printers[serial] = service
        await service.start()
        await self._announce(service)
        return service

    def get(self, serial: str) -> PrinterService:
        try:
            return self._printers[serial]
        except KeyError as exc:
            raise PrinterNotFoundError(serial) from exc

    def list(self) -> list[PrinterService]:
        return list(self._printers.values())

    async def update(
        self,
        serial: str,
        *,
        friendly_name: str | None = None,
        ip: str | None = None,
        access_code: str | None = None,
    ) -> PrinterService:
        service = self.get(serial)
        await self._repo.update(
            serial,
            friendly_name=friendly_name,
            ip=ip,
            access_code=access_code,
        )
        if friendly_name is not None:
            service.friendly_name = friendly_name
        # ip / access_code change requires a live reconnect (open Q #4).
        link_changed = False
        if ip is not None and ip != service.ip:
            service.ip = ip
            link_changed = True
        if access_code is not None and access_code != service.access_code:
            service.access_code = access_code
            link_changed = True
        if link_changed:
            await service.reconnect()
        return service

    async def remove(self, serial: str) -> None:
        service = self._printers.pop(serial, None)
        if service is None:
            raise PrinterNotFoundError(serial)
        await service.stop()
        await self._repo.delete(serial)
        log.info("registry.removed", printer_id=serial)

    # ----------------------------------------------------------------- #
    # Internals
    # ----------------------------------------------------------------- #

    def _make_service(self, record: Printer) -> PrinterService:
        async def _touch() -> None:
            await self._repo.touch_last_seen(record.id)

        recover = self._build_recover_started_at(record.id)
        load_mem, invalidate_mem = self._build_filament_memory_hooks(record.id)

        return PrinterService(
            record.id,
            record.ip,
            record.access_code,
            friendly_name=record.friendly_name,
            model=record.model,
            on_seen=_touch,
            mqtt_port=self._mqtt_port,
            camera_port=self._camera_port,
            camera_linger_s=self._camera_linger_s,
            # PR A.2: forward the stored TOFU pin so the service can
            # compare-on-connect and surface cert_status. None for legacy
            # rows persisted before the cert_fingerprint column existed.
            expected_fingerprint=record.cert_fingerprint,
            recover_started_at=recover,
            load_filament_memory=load_mem,
            invalidate_filament_memory=invalidate_mem,
            # Wave-1: nozzle_type gates the 300 °C allowance in api/control.py.
            # None for legacy rows (stainless fallback → 280 °C ceiling).
            nozzle_type=record.nozzle_type,
        )

    def _build_filament_memory_hooks(
        self, printer_id: str
    ) -> tuple[
        Callable[[], Awaitable[dict[int, FilamentMemory]]] | None,
        Callable[[int], Awaitable[None]] | None,
    ]:
        """G3 — loader + invalidator closures for one printer, or (None, None)
        when no filament_memory repo is wired (tests, legacy contexts)."""
        mem = self._filament_memory
        if mem is None:
            return None, None

        async def _load() -> dict[int, FilamentMemory]:
            return await mem.get_all(printer_id)

        async def _invalidate(slot: int) -> None:
            await mem.delete(printer_id, slot)

        return _load, _invalidate

    def _build_recover_started_at(
        self, printer_id: str
    ) -> Callable[[], Awaitable[datetime | None]] | None:
        """Per-printer jobs.db started_at recovery hook, or None when no jobs
        repo is wired (recovery then degrades to honest-unknown)."""
        jobs = self._jobs
        if jobs is None:
            return None

        async def _recover() -> datetime | None:
            epoch = await jobs.latest_active_started_at(printer_id)
            if epoch is None:
                return None
            return datetime.fromtimestamp(epoch, tz=UTC)

        return _recover
