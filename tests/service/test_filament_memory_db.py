"""FilamentMemoryRepo CRUD tests (G3).

All tests run against an in-memory SQLite database. No network, no MQTT,
no FastAPI — pure repo layer.
"""

from __future__ import annotations

import pytest

from bambu_bridge.db.jobs import Database, FilamentMemory, FilamentMemoryRepo


@pytest.fixture
async def repo(database: Database) -> FilamentMemoryRepo:
    return FilamentMemoryRepo(database)


# --------------------------------------------------------------------------- #
# get_all
# --------------------------------------------------------------------------- #


async def test_get_all_empty(repo: FilamentMemoryRepo) -> None:
    result = await repo.get_all("PRINTER-1")
    assert result == {}


async def test_get_all_returns_only_for_printer(repo: FilamentMemoryRepo) -> None:
    """get_all must not leak rows from a different printer_id."""
    await repo.upsert(
        "PRINTER-A", 1, make="Bambu", model="PLA Matte", profile="0.20 Standard",
        tray_type_seen="PLA"
    )
    await repo.upsert(
        "PRINTER-B", 1, make="Polymaker", model="PETG", profile=None,
        tray_type_seen="PETG"
    )
    result_a = await repo.get_all("PRINTER-A")
    assert list(result_a.keys()) == [1]
    assert result_a[1].make == "Bambu"

    result_b = await repo.get_all("PRINTER-B")
    assert list(result_b.keys()) == [1]
    assert result_b[1].make == "Polymaker"


# --------------------------------------------------------------------------- #
# upsert
# --------------------------------------------------------------------------- #


async def test_upsert_creates_row(repo: FilamentMemoryRepo) -> None:
    entry = await repo.upsert(
        "P1", 2, make="Bambu", model="PLA Matte", profile="0.20 Standard",
        tray_type_seen="PLA"
    )
    assert entry.slot == 2
    assert entry.make == "Bambu"
    assert entry.model == "PLA Matte"
    assert entry.profile == "0.20 Standard"
    assert entry.tray_type_seen == "PLA"
    assert entry.updated_at > 0


async def test_upsert_replaces_existing_row(repo: FilamentMemoryRepo) -> None:
    await repo.upsert("P1", 1, make="Bambu", model="PLA", profile=None, tray_type_seen="PLA")
    updated = await repo.upsert(
        "P1", 1, make="Polymaker", model="PETG", profile="0.28 Draft",
        tray_type_seen="PETG"
    )
    assert updated.make == "Polymaker"
    assert updated.model == "PETG"
    assert updated.profile == "0.28 Draft"
    assert updated.tray_type_seen == "PETG"

    # Verify only one row exists.
    all_mem = await repo.get_all("P1")
    assert len(all_mem) == 1
    assert all_mem[1].make == "Polymaker"


async def test_upsert_allows_partial_fields(repo: FilamentMemoryRepo) -> None:
    """make/model/profile are all optional at the DB level."""
    entry = await repo.upsert("P1", 3, make="Bambu", model=None, profile=None,
                               tray_type_seen="ABS")
    assert entry.make == "Bambu"
    assert entry.model is None
    assert entry.profile is None


async def test_upsert_null_tray_type(repo: FilamentMemoryRepo) -> None:
    """tray_type_seen may be None (slot is empty when label is written)."""
    entry = await repo.upsert("P1", 4, make="M", model=None, profile=None,
                               tray_type_seen=None)
    assert entry.tray_type_seen is None


async def test_upsert_multiple_slots(repo: FilamentMemoryRepo) -> None:
    await repo.upsert("P1", 1, make="A", model=None, profile=None, tray_type_seen="PLA")
    await repo.upsert("P1", 2, make="B", model=None, profile=None, tray_type_seen="PETG")
    await repo.upsert("P1", 4, make="C", model=None, profile=None, tray_type_seen="ABS")
    result = await repo.get_all("P1")
    assert set(result.keys()) == {1, 2, 4}
    assert result[1].make == "A"
    assert result[2].make == "B"
    assert result[4].make == "C"


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #


async def test_delete_existing_row(repo: FilamentMemoryRepo) -> None:
    await repo.upsert("P1", 1, make="Bambu", model=None, profile=None, tray_type_seen="PLA")
    removed = await repo.delete("P1", 1)
    assert removed is True
    assert await repo.get_all("P1") == {}


async def test_delete_nonexistent_row_returns_false(repo: FilamentMemoryRepo) -> None:
    removed = await repo.delete("P1", 2)
    assert removed is False


async def test_delete_only_removes_target_slot(repo: FilamentMemoryRepo) -> None:
    await repo.upsert("P1", 1, make="A", model=None, profile=None, tray_type_seen="PLA")
    await repo.upsert("P1", 2, make="B", model=None, profile=None, tray_type_seen="PETG")
    await repo.delete("P1", 1)
    remaining = await repo.get_all("P1")
    assert set(remaining.keys()) == {2}


async def test_delete_only_removes_target_printer(repo: FilamentMemoryRepo) -> None:
    await repo.upsert("P1", 1, make="A", model=None, profile=None, tray_type_seen="PLA")
    await repo.upsert("P2", 1, make="B", model=None, profile=None, tray_type_seen="PLA")
    await repo.delete("P1", 1)
    assert await repo.get_all("P1") == {}
    assert len(await repo.get_all("P2")) == 1


# --------------------------------------------------------------------------- #
# get_all returns FilamentMemory objects
# --------------------------------------------------------------------------- #


async def test_get_all_returns_filament_memory_models(repo: FilamentMemoryRepo) -> None:
    await repo.upsert("P1", 1, make="Bambu", model="PLA Matte", profile="0.20 Standard",
                      tray_type_seen="PLA")
    result = await repo.get_all("P1")
    assert isinstance(result[1], FilamentMemory)
    assert result[1].make == "Bambu"
    assert result[1].model == "PLA Matte"
    assert result[1].profile == "0.20 Standard"
    assert result[1].tray_type_seen == "PLA"
