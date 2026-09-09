"""M2: Registry lifecycle — add/get/list/remove, persistence, reload."""

from __future__ import annotations

import asyncio

import pytest

from bambu_bridge.db.jobs import PrinterRepo
from bambu_bridge.service.registry import (
    PrinterExistsError,
    PrinterNotFoundError,
    Registry,
)
from tests.conftest import ACCESS_CODE, SERIAL, MockPrinter


async def _wait(predicate, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_add_connects_and_persists(
    printer_repo: PrinterRepo, mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    registry = Registry(printer_repo, mqtt_port=mqtt_broker)
    try:
        service = await registry.add(
            serial=SERIAL,
            ip="127.0.0.1",
            access_code=ACCESS_CODE,
            friendly_name="Shop",
        )
        assert registry.get(SERIAL) is service
        assert [s.serial for s in registry.list()] == [SERIAL]
        # Persisted to the DB...
        assert await printer_repo.get(SERIAL) is not None
        # ...and the live link seeds state + bumps last_seen_at.
        await _wait(lambda: service.connected and bool(service.snapshot()["_raw"]))
        async with asyncio.timeout(10):
            while True:
                seen = await printer_repo.get(SERIAL)
                if seen is not None and seen.last_seen_at is not None:
                    break
                await asyncio.sleep(0.02)
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_duplicate_add_rejected(printer_repo: PrinterRepo) -> None:
    registry = Registry(printer_repo, mqtt_port=1)  # never connects; fine
    try:
        await registry.add(
            serial=SERIAL, ip="127.0.0.1", access_code=ACCESS_CODE, friendly_name="A"
        )
        with pytest.raises(PrinterExistsError):
            await registry.add(
                serial=SERIAL,
                ip="127.0.0.1",
                access_code=ACCESS_CODE,
                friendly_name="B",
            )
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_remove_stops_and_deletes(printer_repo: PrinterRepo) -> None:
    registry = Registry(printer_repo, mqtt_port=1)
    try:
        await registry.add(
            serial=SERIAL, ip="127.0.0.1", access_code=ACCESS_CODE, friendly_name="A"
        )
        await registry.remove(SERIAL)
        assert await printer_repo.get(SERIAL) is None
        with pytest.raises(PrinterNotFoundError):
            registry.get(SERIAL)
        with pytest.raises(PrinterNotFoundError):
            await registry.remove(SERIAL)
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_load_recreates_persisted_printers(printer_repo: PrinterRepo) -> None:
    # Seed the DB through one registry, then load with a fresh one.
    seed = Registry(printer_repo, mqtt_port=1)
    await seed.add(
        serial=SERIAL, ip="127.0.0.1", access_code=ACCESS_CODE, friendly_name="A"
    )
    await seed.shutdown()

    fresh = Registry(printer_repo, mqtt_port=1)
    try:
        await fresh.load()
        assert [s.serial for s in fresh.list()] == [SERIAL]
    finally:
        await fresh.shutdown()
