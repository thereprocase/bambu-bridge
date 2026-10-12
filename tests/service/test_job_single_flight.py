"""One bridge job per printer at a time (audit 2026-10-01, finding 9)."""

from __future__ import annotations

import asyncio

import pytest

import bambu_bridge.service.jobs as jobs_mod
from bambu_bridge.db.jobs import Database, EventRepo, JobRepo, Printer, PrinterRepo
from bambu_bridge.service.events import EventBus
from bambu_bridge.service.jobs import JobManager, PrinterBusyError
from tests.conftest import ACCESS_CODE, SERIAL


class _Printer:
    ip = "127.0.0.1"
    access_code = ACCESS_CODE

    def __init__(self) -> None:
        self.bus = EventBus()
        self.sent: list[tuple[str, str]] = []
        self.upload = asyncio.Event()

    async def send_command(self, category: str, command: str, **_: object) -> None:
        self.sent.append((category, command))


class _Registry:
    def __init__(self, printer: _Printer) -> None:
        self.printer = printer

    def get(self, _serial: str) -> _Printer:
        return self.printer


@pytest.mark.asyncio
async def test_second_submit_refused_until_first_run_ends(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await PrinterRepo(database).add(
        Printer(id=SERIAL, friendly_name="P", ip="127.0.0.1", access_code=ACCESS_CODE, added_at=1)
    )
    printer = _Printer()

    class _Ftps:
        def __init__(self, *_a: object, **_k: object) -> None: ...

        async def upload_bytes(self, *_a: object, **_k: object) -> str:
            await printer.upload.wait()  # a slow FTPS upload
            raise OSError("upload failed")

    monkeypatch.setattr(jobs_mod, "FtpsTransfer", _Ftps)
    manager = JobManager(JobRepo(database), EventRepo(database), _Registry(printer))  # type: ignore[arg-type]

    first = await manager.submit(SERIAL, b"x", "a.gcode.3mf")
    with pytest.raises(PrinterBusyError):
        await manager.submit(SERIAL, b"x", "b.gcode.3mf")
    assert len(await JobRepo(database).list(printer_id=SERIAL)) == 1

    printer.upload.set()  # first run fails its upload and ends
    for _ in range(100):
        if (await manager.get(first.id)).state.terminal:  # type: ignore[union-attr]
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0)
    second = await manager.submit(SERIAL, b"x", "c.gcode.3mf")
    assert second.id != first.id
    printer.upload.set()
    await manager.shutdown()
    assert ("print", "project_file") not in printer.sent
