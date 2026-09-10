"""Tests for SlicedDateRepo and the DB-backed SlicedDateMemo.

Covers:
- SlicedDateRepo.put / get round-trip.
- SlicedDateRepo.get — missing key returns None.
- SlicedDateRepo.put upsert: re-inserting same (filename, size) updates sliced_at.
- SlicedDateRepo.latest_by_names — returns the maximum sliced_at per filename
  across multiple size variants.
- SlicedDateRepo.latest_by_names — omits filenames with no stored entry.
- SlicedDateRepo.latest_by_names — empty input returns empty dict.
- SlicedDateRepo pruning: table is pruned when count exceeds _SLICED_DATES_MAX.
- SlicedDateMemo with repo=None (no-op DB path, LRU only).
- SlicedDateMemo.put updates LRU only (DB write requires aput).
- SlicedDateMemo.aput writes to both LRU and DB.
- SlicedDateMemo.latest_by_names_async with repo: LRU overlay covers the
  window before aput DB write (fast path via LRU after put).
- SlicedDateMemo.latest_by_names_async with repo: DB result survives cold-
  start (empty LRU, data in DB only).
- SlicedDateMemo.latest_by_names_async: LRU wins when strictly newer than DB.
- SlicedDateMemo.latest_by_names_async: DB wins when strictly newer than LRU.
- sort_files_newest_first with name_map parameter.
- sort_files_newest_first name_map takes priority over memo+sizes.
- sort_files_newest_first name_map=None falls back to memo+sizes as before.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import bambu_bridge.db.jobs as jobs_mod
from bambu_bridge.db.jobs import Database, SlicedDateRepo
from bambu_bridge.protocol.ftps import (
    FileEntry,
    SlicedDateMemo,
    sort_files_newest_first,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _dt(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


@pytest.fixture
async def repo(database: Database) -> SlicedDateRepo:
    """A SlicedDateRepo backed by the in-memory test database."""
    return SlicedDateRepo(database)


# --------------------------------------------------------------------------- #
# SlicedDateRepo CRUD
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_repo_put_and_get(repo: SlicedDateRepo) -> None:
    """Basic write-through: put then get returns the same datetime."""
    dt = _dt(2026, 5, 19, 7, 37, 44)
    await repo.put("benchy.gcode.3mf", 12345, dt)
    result = await repo.get("benchy.gcode.3mf", 12345)
    assert result == dt


@pytest.mark.asyncio
async def test_repo_get_missing_returns_none(repo: SlicedDateRepo) -> None:
    """get() on an unknown key returns None (not an exception)."""
    assert await repo.get("nosuchfile.gcode.3mf", 99999) is None


@pytest.mark.asyncio
async def test_repo_put_upsert_updates_sliced_at(repo: SlicedDateRepo) -> None:
    """Putting the same (filename, size) twice keeps only the latest row."""
    old = _dt(2026, 1, 1)
    new = _dt(2026, 6, 1)
    await repo.put("f.gcode.3mf", 100, old)
    await repo.put("f.gcode.3mf", 100, new)
    # Row count must still be 1.
    async with repo._db.conn.execute(
        "SELECT COUNT(*) FROM sliced_dates WHERE filename = 'f.gcode.3mf' AND size_bytes = 100"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None and int(row[0]) == 1
    assert await repo.get("f.gcode.3mf", 100) == new


@pytest.mark.asyncio
async def test_repo_different_sizes_are_separate_entries(repo: SlicedDateRepo) -> None:
    """Different sizes for the same filename are independent rows."""
    dt1 = _dt(2026, 1, 1)
    dt2 = _dt(2026, 6, 1)
    await repo.put("multi.gcode.3mf", 1000, dt1)
    await repo.put("multi.gcode.3mf", 2000, dt2)
    assert await repo.get("multi.gcode.3mf", 1000) == dt1
    assert await repo.get("multi.gcode.3mf", 2000) == dt2


# --------------------------------------------------------------------------- #
# SlicedDateRepo.latest_by_names
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_latest_by_names_returns_max_sliced_at(repo: SlicedDateRepo) -> None:
    """latest_by_names returns MAX(sliced_at) per filename across all sizes."""
    old = _dt(2026, 1, 1)
    new = _dt(2026, 6, 1)
    await repo.put("test.gcode.3mf", 1000, old)
    await repo.put("test.gcode.3mf", 2000, new)  # newer slice, bigger file

    result = await repo.latest_by_names(["test.gcode.3mf"])
    assert result.get("test.gcode.3mf") == new


@pytest.mark.asyncio
async def test_latest_by_names_omits_missing_files(repo: SlicedDateRepo) -> None:
    """latest_by_names omits filenames that have no DB entry."""
    await repo.put("present.gcode.3mf", 100, _dt(2026, 5, 1))
    result = await repo.latest_by_names(["present.gcode.3mf", "absent.gcode.3mf"])
    assert "present.gcode.3mf" in result
    assert "absent.gcode.3mf" not in result


@pytest.mark.asyncio
async def test_latest_by_names_empty_input(repo: SlicedDateRepo) -> None:
    """latest_by_names with an empty name list returns an empty dict."""
    assert await repo.latest_by_names([]) == {}


@pytest.mark.asyncio
async def test_latest_by_names_multiple_files_single_query(repo: SlicedDateRepo) -> None:
    """latest_by_names resolves multiple filenames in one call."""
    await repo.put("a.gcode.3mf", 100, _dt(2026, 3, 1))
    await repo.put("b.gcode.3mf", 200, _dt(2026, 4, 1))
    await repo.put("c.gcode.3mf", 300, _dt(2026, 5, 1))

    result = await repo.latest_by_names(["a.gcode.3mf", "b.gcode.3mf", "c.gcode.3mf"])
    assert result["a.gcode.3mf"] == _dt(2026, 3, 1)
    assert result["b.gcode.3mf"] == _dt(2026, 4, 1)
    assert result["c.gcode.3mf"] == _dt(2026, 5, 1)


# --------------------------------------------------------------------------- #
# SlicedDateRepo pruning
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_repo_pruning_fires_at_cap(repo: SlicedDateRepo) -> None:
    """Inserting _SLICED_DATES_MAX + 1 rows triggers pruning.

    After pruning the count must be strictly below the (patched) cap.  We
    test with a small-cap override to avoid inserting 2001 rows.
    """
    original_cap = jobs_mod._SLICED_DATES_MAX
    jobs_mod._SLICED_DATES_MAX = 10
    try:
        for i in range(12):  # 2 over the patched cap
            await repo.put(f"file{i:03d}.gcode.3mf", i, _dt(2026, 1, 1))

        async with repo._db.conn.execute("SELECT COUNT(*) FROM sliced_dates") as cur:
            row = await cur.fetchone()
        count = int(row[0]) if row else 0
        # Must not exceed the (patched) cap.
        assert count <= 10, f"expected ≤10 rows after prune, got {count}"
    finally:
        jobs_mod._SLICED_DATES_MAX = original_cap


# --------------------------------------------------------------------------- #
# SlicedDateMemo — no-repo mode (LRU only)
# --------------------------------------------------------------------------- #


def test_memo_no_repo_put_and_get() -> None:
    """SlicedDateMemo without a repo uses the in-process LRU only."""
    memo = SlicedDateMemo(repo=None)
    dt = _dt(2026, 5, 19, 7, 37, 44)
    memo.put("file.gcode.3mf", 100, dt)
    assert memo.get("file.gcode.3mf", 100) == dt


def test_memo_no_repo_get_missing_returns_none() -> None:
    memo = SlicedDateMemo(repo=None)
    assert memo.get("nope.gcode.3mf", 1) is None


def test_memo_no_repo_lru_eviction_at_cap() -> None:
    """LRU eviction works without a repo (the old behaviour)."""
    memo = SlicedDateMemo(repo=None)
    for i in range(SlicedDateMemo._MEMO_MAX + 5):
        memo.put(f"file{i}.gcode.3mf", i, _dt(2026, 1, 1))
    assert len(memo) == SlicedDateMemo._MEMO_MAX


def test_memo_no_repo_latest_by_names_lru() -> None:
    """latest_by_names (sync) returns the max sliced_at per name from LRU."""
    memo = SlicedDateMemo(repo=None)
    old = _dt(2026, 1, 1)
    new = _dt(2026, 6, 1)
    memo.put("test.gcode.3mf", 100, old)
    memo.put("test.gcode.3mf", 200, new)
    result = memo.latest_by_names(["test.gcode.3mf"])
    assert result["test.gcode.3mf"] == new


# --------------------------------------------------------------------------- #
# SlicedDateMemo — DB-backed mode
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_memo_put_lru_overlay_visible_before_aput(
    database: Database,
) -> None:
    """put() populates only the LRU; latest_by_names_async returns value via
    the LRU overlay (fast path — no DB write needed for the read)."""
    repo = SlicedDateRepo(database)
    memo = SlicedDateMemo(repo=repo)
    dt = _dt(2026, 5, 19, 7, 37, 44)

    memo.put("known.gcode.3mf", 5000, dt)
    result = await memo.latest_by_names_async(["known.gcode.3mf"])
    assert result.get("known.gcode.3mf") == dt


@pytest.mark.asyncio
async def test_memo_aput_writes_to_db_and_lru(
    database: Database,
) -> None:
    """aput() persists to both the in-memory LRU and the DB repo."""
    repo = SlicedDateRepo(database)
    memo = SlicedDateMemo(repo=repo)
    dt = _dt(2026, 5, 19, 7, 37, 44)

    await memo.aput("aput_test.gcode.3mf", 7000, dt)

    # LRU populated.
    assert memo.get("aput_test.gcode.3mf", 7000) == dt

    # DB row written — verify via a fresh memo (empty LRU).
    fresh = SlicedDateMemo(repo=repo)
    result = await fresh.latest_by_names_async(["aput_test.gcode.3mf"])
    assert result.get("aput_test.gcode.3mf") == dt


@pytest.mark.asyncio
async def test_memo_with_repo_db_persists_after_lru_cleared(
    database: Database,
) -> None:
    """After a fresh memo (empty LRU) is created, DB rows are still visible."""
    repo = SlicedDateRepo(database)
    dt = _dt(2026, 5, 19, 7, 37, 44)

    # Write directly to the repo (simulating a previous bridge run).
    await repo.put("persist.gcode.3mf", 3000, dt)

    # A fresh memo (empty LRU) with the same repo should find the DB row.
    fresh_memo = SlicedDateMemo(repo=repo)
    result = await fresh_memo.latest_by_names_async(["persist.gcode.3mf"])
    assert result.get("persist.gcode.3mf") == dt


@pytest.mark.asyncio
async def test_memo_lru_wins_when_newer_than_db(
    database: Database,
) -> None:
    """LRU entry beats a DB entry when the LRU value is strictly newer.

    This covers the common case where put() was called with a newer sliced_at
    but the aput DB write hasn't been issued yet from this session.
    """
    repo = SlicedDateRepo(database)
    old = _dt(2026, 1, 1)
    new = _dt(2026, 6, 1)

    # Seed the DB with the old value.
    await repo.put("resliced.gcode.3mf", 100, old)

    # Create a memo and put the new value (LRU only — simulates a newer
    # in-session put that hasn't been persisted via aput yet).
    memo = SlicedDateMemo(repo=repo)
    memo.put("resliced.gcode.3mf", 100, new)

    result = await memo.latest_by_names_async(["resliced.gcode.3mf"])
    assert result.get("resliced.gcode.3mf") == new


@pytest.mark.asyncio
async def test_db_wins_when_newer_than_lru(
    database: Database,
) -> None:
    """DB entry beats a stale LRU entry when the DB value is strictly newer."""
    repo = SlicedDateRepo(database)
    old = _dt(2026, 1, 1)
    new = _dt(2026, 6, 1)

    # Seed the LRU with the old value (simulating a stale in-memory entry).
    memo = SlicedDateMemo(repo=repo)
    memo.put("f.gcode.3mf", 100, old)

    # Out-of-band DB write with the new value (simulates another process
    # or an aput from a different path).
    await repo.put("f.gcode.3mf", 100, new)

    result = await memo.latest_by_names_async(["f.gcode.3mf"])
    assert result.get("f.gcode.3mf") == new


@pytest.mark.asyncio
async def test_memo_missing_name_absent_from_result(
    database: Database,
) -> None:
    """latest_by_names_async omits names with no DB entry and no LRU entry."""
    repo = SlicedDateRepo(database)
    memo = SlicedDateMemo(repo=repo)
    result = await memo.latest_by_names_async(["ghost.gcode.3mf"])
    assert "ghost.gcode.3mf" not in result


# --------------------------------------------------------------------------- #
# sort_files_newest_first with name_map parameter
# --------------------------------------------------------------------------- #


def test_sort_name_map_wins_over_modified_at() -> None:
    """name_map sliced_at beats a newer modified_at for the same file."""
    sliced_dt = _dt(2026, 6, 1)
    modified_dt = _dt(2026, 1, 1)  # older than sliced
    entries = [FileEntry("file.gcode.3mf", modified_at=modified_dt)]
    sorted_e = sort_files_newest_first(entries, name_map={"file.gcode.3mf": sliced_dt})
    assert sorted_e[0].sort_basis == "sliced"


def test_sort_name_map_sets_sort_basis_sliced() -> None:
    """sort_basis is 'sliced' when name_map supplies the timestamp."""
    dt = _dt(2026, 5, 1)
    entries = [FileEntry("a.gcode.3mf")]
    sorted_e = sort_files_newest_first(entries, name_map={"a.gcode.3mf": dt})
    assert sorted_e[0].sort_basis == "sliced"


def test_sort_name_map_file_not_in_map_falls_through_to_modified() -> None:
    """A file absent from name_map still uses modified_at normally."""
    dt = _dt(2026, 5, 1)
    entries = [FileEntry("b.gcode.3mf", modified_at=dt)]
    sorted_e = sort_files_newest_first(entries, name_map={})
    assert sorted_e[0].sort_basis == "modified"


def test_sort_name_map_beats_memo_plus_sizes() -> None:
    """name_map takes priority over the memo+sizes path when both are supplied."""
    memo_dt = _dt(2026, 1, 1)
    map_dt = _dt(2026, 12, 1)  # newer

    memo = SlicedDateMemo(repo=None)
    memo.put("same.gcode.3mf", 100, memo_dt)

    # Both name_map and memo+sizes supplied: name_map wins.
    entries = [
        FileEntry("same.gcode.3mf"),
        FileEntry("newer.gcode.3mf", modified_at=_dt(2026, 6, 1)),
    ]
    sorted_with_map = sort_files_newest_first(
        entries,
        memo=memo,
        sizes={"same.gcode.3mf": 100},
        name_map={"same.gcode.3mf": map_dt},
    )
    # map_dt (2026-12) > newer.gcode.3mf modified (2026-06): same.gcode.3mf sorts first.
    assert sorted_with_map[0].name == "same.gcode.3mf"
    assert sorted_with_map[0].sort_basis == "sliced"


def test_sort_name_map_none_falls_back_to_memo_sizes() -> None:
    """Passing name_map=None preserves the original memo+sizes behaviour."""
    memo = SlicedDateMemo(repo=None)
    dt = _dt(2026, 5, 1)
    memo.put("f.gcode.3mf", 100, dt)

    entries = [FileEntry("f.gcode.3mf")]
    sorted_e = sort_files_newest_first(
        entries,
        memo=memo,
        sizes={"f.gcode.3mf": 100},
        name_map=None,
    )
    assert sorted_e[0].sort_basis == "sliced"


def test_sort_name_map_preserves_undated_sort_last() -> None:
    """Files not in name_map and without any timestamp still sort last."""
    dt = _dt(2026, 5, 1)
    entries = [
        FileEntry("undated.gcode.3mf"),
        FileEntry("dated.gcode.3mf"),
    ]
    sorted_e = sort_files_newest_first(
        entries, name_map={"dated.gcode.3mf": dt}
    )
    assert sorted_e[0].name == "dated.gcode.3mf"
    assert sorted_e[1].name == "undated.gcode.3mf"


def test_sort_name_map_multiple_files_sorted_correctly() -> None:
    """Multiple files with different name_map timestamps sort newest-first."""
    entries = [
        FileEntry("old.gcode.3mf"),
        FileEntry("newest.gcode.3mf"),
        FileEntry("middle.gcode.3mf"),
    ]
    name_map = {
        "old.gcode.3mf": _dt(2024, 1, 1),
        "newest.gcode.3mf": _dt(2026, 12, 1),
        "middle.gcode.3mf": _dt(2025, 6, 1),
    }
    sorted_e = sort_files_newest_first(entries, name_map=name_map)
    assert [e.name for e in sorted_e] == [
        "newest.gcode.3mf",
        "middle.gcode.3mf",
        "old.gcode.3mf",
    ]
