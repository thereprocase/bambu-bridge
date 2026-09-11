"""Warm current prints after restart; reuse paths without skipping revisions."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import Mock

from bambu_bridge.db.jobs import Database, EventRepo, JobRepo
from bambu_bridge.service.events import Event, EventBus
from bambu_bridge.service.jobs import JobManager
from bambu_bridge.service.viz_cache import VizCache


async def test_cached_path_still_invalidates_on_same_size_replacement() -> None:
    cache = VizCache()
    printer, filename = "STARTUPFIXTURE1", "fixture.gcode.3mf"
    cache.put_toolpath((printer, filename, 42), Mock())
    cache._revisions[(printer, filename)] = ("/cache", (42, "old"))
    assert cache.cached_location(printer, filename) == ("/cache", filename)

    class Metadata:
        async def file_revision(self, name: str, *, remote_dir: str) -> tuple[int, str]:
            assert name == filename and remote_dir == "/cache"
            return 42, "new"

    await cache.validate_revision(printer, Metadata(), "/cache", filename)  # type: ignore[arg-type]
    assert cache.lookup_toolpath(printer, filename) is None
    assert cache.cached_location(printer, filename) is None


async def test_delayed_job_name_warms_once_without_print_started(database: Database) -> None:
    class Printer:
        serial = "STARTUPFIXTURE1"
        ip = "127.0.0.1"
        access_code = "12345678"
        bus = EventBus()
        name: str | None = None

        def summary(self) -> dict[str, Any]:
            return {"gcode_state": "RUNNING", "subtask_name": self.name}

    cache = Mock(spec=VizCache)
    printer = Printer()
    manager = JobManager(JobRepo(database), EventRepo(database), Mock(), viz_cache=cache)
    await manager.attach(printer)  # type: ignore[arg-type]
    try:
        await asyncio.sleep(0)
        printer.bus.publish(Event("snapshot", {}))
        await asyncio.sleep(0)
        cache.schedule_prewarm.assert_not_called()
        printer.name = "fixture.gcode.3mf"
        for _ in range(3):
            printer.bus.publish(Event("delta", {}))
            await asyncio.sleep(0)
        cache.schedule_prewarm.assert_called_once_with(
            printer.serial, printer.ip, printer.access_code, printer.name
        )
    finally:
        await manager.shutdown()
