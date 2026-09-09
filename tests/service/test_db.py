"""M2: PrinterRepo CRUD round-trip on an in-memory database."""

from __future__ import annotations

import pytest

from bambu_bridge.db.jobs import Printer, PrinterRepo
from tests.conftest import ACCESS_CODE, SERIAL


def _printer() -> Printer:
    return Printer(
        id=SERIAL,
        friendly_name="Workshop P1S",
        ip="192.168.1.50",
        access_code=ACCESS_CODE,
        model="P1S",
        added_at=1_700_000_000,
    )


@pytest.mark.asyncio
async def test_add_get_list(printer_repo: PrinterRepo) -> None:
    await printer_repo.add(_printer())
    got = await printer_repo.get(SERIAL)
    assert got is not None
    assert got.friendly_name == "Workshop P1S"
    assert got.model == "P1S"
    assert [p.id for p in await printer_repo.list()] == [SERIAL]


@pytest.mark.asyncio
async def test_get_missing_is_none(printer_repo: PrinterRepo) -> None:
    assert await printer_repo.get("nope") is None


@pytest.mark.asyncio
async def test_update_patches_only_given_fields(printer_repo: PrinterRepo) -> None:
    await printer_repo.add(_printer())
    updated = await printer_repo.update(SERIAL, friendly_name="Renamed", ip="10.0.0.9")
    assert updated is not None
    assert updated.friendly_name == "Renamed"
    assert updated.ip == "10.0.0.9"
    assert updated.access_code == ACCESS_CODE  # untouched


@pytest.mark.asyncio
async def test_touch_last_seen(printer_repo: PrinterRepo) -> None:
    await printer_repo.add(_printer())
    await printer_repo.touch_last_seen(SERIAL, ts=1_700_000_123)
    got = await printer_repo.get(SERIAL)
    assert got is not None and got.last_seen_at == 1_700_000_123


@pytest.mark.asyncio
async def test_delete(printer_repo: PrinterRepo) -> None:
    await printer_repo.add(_printer())
    assert await printer_repo.delete(SERIAL) is True
    assert await printer_repo.get(SERIAL) is None
    assert await printer_repo.delete(SERIAL) is False
