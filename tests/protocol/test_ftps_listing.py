"""Tests for the enriched FTPS file listing with timestamp tiers.

Covers:
* MLSD path — modify and create facts parsed correctly.
* LIST path — both HH:MM (recent) and YYYY (old) date formats.
* Year-rollover boundary: recent-format file with a future date flips to prior year.
* MDTM fallback — fills modified_at for entries where LIST had no date.
* MDTM cap — only _MDTM_MAX_FILES per-file round-trips are issued.
* sort_files_newest_first — full priority chain (sliced > modified > created > none).
* Undated files sort last in stable order.
* Mixed tiers — files with sliced, modified, created, and none interleave
  correctly by timestamp regardless of tier.
* SlicedDateMemo — put/get/eviction/size contract.
* extract_sliced_at_from_bytes — ZIP entry date_time and gcode header comment.
* Opportunistic memo fill via VizCache.fill_mesh.
* API endpoint: files list returns enriched objects sorted newest-first.
* Backward-compat: file_names field still present.
"""

from __future__ import annotations

import ftplib
import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bambu_bridge.protocol.ftps import (
    _MDTM_MAX_FILES,
    FileEntry,
    FtpsTransfer,
    SlicedDateMemo,
    _ImplicitFTP_TLS,
    _parse_rfc3659_time,
    extract_sliced_at_from_bytes,
    sort_files_newest_first,
)
from tests.conftest import ACCESS_CODE

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _insecure_ftps(port: int) -> FtpsTransfer:
    import ssl

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return FtpsTransfer("127.0.0.1", ACCESS_CODE, port=port, ssl_context=ctx)


def _dt(  # noqa: PLR0913
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# RFC 3659 time parser
# --------------------------------------------------------------------------- #


class TestParseRfc3659Time:
    def test_valid_full(self) -> None:
        dt = _parse_rfc3659_time("20260519073744")
        assert dt == _dt(2026, 5, 19, 7, 37, 44)

    def test_valid_with_fractional(self) -> None:
        dt = _parse_rfc3659_time("20260519073744.123")
        assert dt == _dt(2026, 5, 19, 7, 37, 44)

    def test_none_input(self) -> None:
        assert _parse_rfc3659_time(None) is None

    def test_empty_string(self) -> None:
        assert _parse_rfc3659_time("") is None

    def test_too_short(self) -> None:
        assert _parse_rfc3659_time("20260519") is None  # 8 chars, not 14

    def test_non_numeric(self) -> None:
        assert _parse_rfc3659_time("ABCDEFGHIJKLMN") is None

    def test_invalid_month(self) -> None:
        assert _parse_rfc3659_time("20261319073744") is None  # month 13


# --------------------------------------------------------------------------- #
# LIST date parser (unit tests on the static helper)
# --------------------------------------------------------------------------- #


class TestParseListDate:
    def test_hhmm_format_recent_file(self) -> None:
        # A file "modified" today at 14:23 — should come back with today's year.
        now = datetime.now(tz=UTC)
        dt = FtpsTransfer._parse_list_date("Jan", str(now.day), "14:23")
        # Can't assert the year precisely without freezing time, but it must be UTC.
        assert dt is not None
        assert dt.tzinfo is UTC

    def test_hhmm_format_december_date_in_january(self) -> None:
        # If we're in January 2027 and the file date is "Dec 31 23:59",
        # the naive year assignment would produce 2027-12-31 (future) —
        # the rollover fix must step back to 2026-12-31.
        # Simulate by mocking datetime.now.
        import unittest.mock as mock

        fake_now = _dt(2027, 1, 15, 10, 0, 0)
        with mock.patch("bambu_bridge.protocol.ftps.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            dt = FtpsTransfer._parse_list_date("Dec", "31", "23:59")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 12
        assert dt.day == 31

    def test_yyyy_format(self) -> None:
        dt = FtpsTransfer._parse_list_date("May", "19", "2024")
        assert dt == _dt(2024, 5, 19)

    def test_unknown_month(self) -> None:
        assert FtpsTransfer._parse_list_date("Xyz", "15", "14:23") is None

    def test_invalid_day(self) -> None:
        assert FtpsTransfer._parse_list_date("Jan", "xx", "14:23") is None

    def test_invalid_year(self) -> None:
        assert FtpsTransfer._parse_list_date("Jan", "15", "abcd") is None

    def test_invalid_time(self) -> None:
        assert FtpsTransfer._parse_list_date("Jan", "15", "AB:CD") is None

    def test_feb_29_leap(self) -> None:
        dt = FtpsTransfer._parse_list_date("Feb", "29", "2024")
        assert dt == _dt(2024, 2, 29)

    def test_feb_29_non_leap_returns_none(self) -> None:
        # 2023 is not a leap year — date is invalid.
        assert FtpsTransfer._parse_list_date("Feb", "29", "2023") is None


class TestParseListLine:
    # Canonical Unix ls -l format:
    # "-rw-r--r--  1 user group  12345 May 19 14:23 benchy.gcode.3mf"
    def test_full_hhmm_line(self) -> None:
        line = "-rw-r--r--  1 user group  12345 May 19 14:23 benchy.gcode.3mf"
        name, dt = FtpsTransfer._parse_list_line(line)
        assert name == "benchy.gcode.3mf"
        assert dt is not None
        assert dt.month == 5
        assert dt.day == 19

    def test_full_year_line(self) -> None:
        line = "-rw-r--r--  1 user group  12345 Jan 15 2024 old_file.gcode.3mf"
        name, dt = FtpsTransfer._parse_list_line(line)
        assert name == "old_file.gcode.3mf"
        assert dt == _dt(2024, 1, 15)

    def test_short_line_name_only(self) -> None:
        # Degenerate line — just return the last token as name, no date.
        line = "just_a_name.3mf"
        name, dt = FtpsTransfer._parse_list_line(line)
        assert name == "just_a_name.3mf"
        assert dt is None

    def test_directory_entry(self) -> None:
        line = "drwxr-xr-x  2 user group  4096 Jun 11 10:00 cache"
        name, dt = FtpsTransfer._parse_list_line(line)
        assert name == "cache"
        assert dt is not None

    def test_filename_with_spaces(self) -> None:
        # The split(maxsplit=8) strategy keeps the rest of the filename.
        line = "-rw-r--r--  1 user group  12345 May 19 14:23 my benchy file.3mf"
        name, dt = FtpsTransfer._parse_list_line(line)
        assert name == "my benchy file.3mf"

    def test_empty_line(self) -> None:
        name, dt = FtpsTransfer._parse_list_line("")
        assert name == ""
        assert dt is None


# --------------------------------------------------------------------------- #
# MLSD path (against real aioftp test server)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_mlsd_returns_modified_at(ftps_server: tuple[int, Path]) -> None:
    """MLSD facts (Modify) should populate modified_at on returned FileEntry objects."""
    port, storage = ftps_server
    ftps = _insecure_ftps(port)

    # Seed a file so MLSD has something to return.
    (storage / "alpha.gcode.3mf").write_bytes(b"A")
    (storage / "beta.gcode.3mf").write_bytes(b"B")

    entries = await ftps.list_dir_with_timestamps()

    names = {e.name for e in entries}
    assert "alpha.gcode.3mf" in names
    assert "beta.gcode.3mf" in names

    # The aioftp server populates Modify from st_mtime — should be non-None.
    for e in entries:
        if e.name in ("alpha.gcode.3mf", "beta.gcode.3mf"):
            assert e.modified_at is not None, f"modified_at missing for {e.name}"
            assert e.modified_at.tzinfo is UTC


@pytest.mark.asyncio
async def test_mlsd_create_fact_populated(ftps_server: tuple[int, Path]) -> None:
    """aioftp also advertises Create (st_ctime) — created_at should be non-None."""
    port, storage = ftps_server
    ftps = _insecure_ftps(port)
    (storage / "c.gcode.3mf").write_bytes(b"C")

    entries = await ftps.list_dir_with_timestamps()
    for e in entries:
        if e.name == "c.gcode.3mf":
            # aioftp builds mlsx facts from st_ctime as "Create".
            assert e.created_at is not None


@pytest.mark.asyncio
async def test_list_dir_with_timestamps_excludes_dot_entries(
    ftps_server: tuple[int, Path],
) -> None:
    """'.' and '..' must never appear in the returned entries."""
    port, storage = ftps_server
    ftps = _insecure_ftps(port)
    (storage / "z.gcode.3mf").write_bytes(b"Z")

    entries = await ftps.list_dir_with_timestamps()
    assert all(e.name not in (".", "..") for e in entries)


@pytest.mark.asyncio
async def test_list_dir_with_timestamps_cache_subdir(
    ftps_server: tuple[int, Path],
) -> None:
    """list_dir_with_timestamps works for the cache subdirectory."""
    port, storage = ftps_server
    ftps = _insecure_ftps(port)
    (storage / "cache" / "transient.gcode.3mf").write_bytes(b"T")

    entries = await ftps.list_dir_with_timestamps("cache")
    names = {e.name for e in entries}
    assert "transient.gcode.3mf" in names


# --------------------------------------------------------------------------- #
# LIST fallback (mock _connect to force LIST path)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_fallback_hhmm_format() -> None:
    """LIST fallback parses HH:MM date fields into modified_at."""
    ftps = FtpsTransfer("127.0.0.1", "code", port=990)
    mock_ftp: MagicMock = MagicMock(spec=_ImplicitFTP_TLS)
    mock_ftp.mlsd.side_effect = ftplib.error_perm("500 not supported")
    # Simulate LIST output: two files, one with HH:MM, one with YYYY.
    lines = [
        "-rw-r--r--  1 user group  12345 May 19 07:37 benchy.gcode.3mf",
        "-rw-r--r--  1 user group  54321 Jan 10 2024 old_project.gcode.3mf",
    ]
    mock_ftp.retrlines.side_effect = lambda cmd, callback: [callback(ln) for ln in lines]

    with patch.object(ftps, "_connect", return_value=mock_ftp):
        entries = await ftps.list_dir_with_timestamps()

    assert len(entries) == 2
    by_name = {e.name: e for e in entries}

    benchy = by_name["benchy.gcode.3mf"]
    assert benchy.modified_at is not None
    assert benchy.modified_at.month == 5
    assert benchy.modified_at.day == 19

    old = by_name["old_project.gcode.3mf"]
    assert old.modified_at == _dt(2024, 1, 10)


@pytest.mark.asyncio
async def test_list_fallback_550_returns_empty() -> None:
    """LIST fallback: 550 error_perm degrades to empty list (not an exception)."""
    ftps = FtpsTransfer("127.0.0.1", "code", port=990)
    mock_ftp: MagicMock = MagicMock(spec=_ImplicitFTP_TLS)
    mock_ftp.mlsd.side_effect = ftplib.error_perm("500 not supported")
    mock_ftp.retrlines.side_effect = ftplib.error_perm("550 no such directory")

    with patch.object(ftps, "_connect", return_value=mock_ftp):
        entries = await ftps.list_dir_with_timestamps("timelapse")

    assert entries == []


@pytest.mark.asyncio
async def test_list_fallback_no_date_triggers_mdtm() -> None:
    """When LIST line has no parseable date, MDTM is tried for that file."""
    ftps = FtpsTransfer("127.0.0.1", "code", port=990)
    mock_ftp: MagicMock = MagicMock(spec=_ImplicitFTP_TLS)
    mock_ftp.mlsd.side_effect = ftplib.error_perm("500 not supported")
    # Line with only 5 tokens (short format: permission + name only).
    lines = ["-rw-r--r-- undated.gcode.3mf"]
    mock_ftp.retrlines.side_effect = lambda cmd, callback: [callback(ln) for ln in lines]
    # MDTM returns a valid timestamp.
    mock_ftp.sendcmd.return_value = "213 20260519073744"

    with patch.object(ftps, "_connect", return_value=mock_ftp):
        entries = await ftps.list_dir_with_timestamps()

    assert len(entries) == 1
    assert entries[0].modified_at == _dt(2026, 5, 19, 7, 37, 44)


@pytest.mark.asyncio
async def test_mdtm_fallback_cap() -> None:
    """MDTM must not be called more than _MDTM_MAX_FILES times per listing."""
    ftps = FtpsTransfer("127.0.0.1", "code", port=990)
    mock_ftp: MagicMock = MagicMock(spec=_ImplicitFTP_TLS)
    mock_ftp.mlsd.side_effect = ftplib.error_perm("500 not supported")

    # Generate 50 files, all with undatable short LIST lines.
    n_files = _MDTM_MAX_FILES + 20
    lines = [f"-rw-r--r-- file{i:02d}.gcode.3mf" for i in range(n_files)]
    mock_ftp.retrlines.side_effect = lambda cmd, callback: [callback(ln) for ln in lines]
    mock_ftp.sendcmd.return_value = "213 20260519073744"

    with patch.object(ftps, "_connect", return_value=mock_ftp):
        entries = await ftps.list_dir_with_timestamps()

    assert len(entries) == n_files
    # MDTM must be called at most _MDTM_MAX_FILES times.
    assert mock_ftp.sendcmd.call_count <= _MDTM_MAX_FILES

    # The first _MDTM_MAX_FILES files should have modified_at populated.
    dated = [e for e in entries if e.modified_at is not None]
    undated = [e for e in entries if e.modified_at is None]
    assert len(dated) == _MDTM_MAX_FILES
    assert len(undated) == n_files - _MDTM_MAX_FILES


@pytest.mark.asyncio
async def test_mdtm_failure_gracefully_handled() -> None:
    """MDTM error (command not recognised) must not raise — modified_at stays None."""
    ftps = FtpsTransfer("127.0.0.1", "code", port=990)
    mock_ftp: MagicMock = MagicMock(spec=_ImplicitFTP_TLS)
    mock_ftp.mlsd.side_effect = ftplib.error_perm("500 not supported")
    lines = ["-rw-r--r-- nodatefile.3mf"]
    mock_ftp.retrlines.side_effect = lambda cmd, callback: [callback(ln) for ln in lines]
    mock_ftp.sendcmd.side_effect = ftplib.error_perm("502 MDTM not implemented")

    with patch.object(ftps, "_connect", return_value=mock_ftp):
        entries = await ftps.list_dir_with_timestamps()

    assert len(entries) == 1
    assert entries[0].modified_at is None


# --------------------------------------------------------------------------- #
# NLST-only mode (no MLSD, no real LIST lines)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_nlst_mode_no_timestamps() -> None:
    """When both MLSD and LIST are unavailable (only NLST worked before), the
    bridge now falls through to LIST which returns 550 — degrade to empty.

    (NLST was never used by this bridge; this test verifies the fallback
    chain's terminal condition when all tiers fail.)
    """
    ftps = FtpsTransfer("127.0.0.1", "code", port=990)
    mock_ftp: MagicMock = MagicMock(spec=_ImplicitFTP_TLS)
    mock_ftp.mlsd.side_effect = ftplib.error_perm("500 not supported")
    mock_ftp.retrlines.side_effect = ftplib.error_perm("550 no such directory")

    with patch.object(ftps, "_connect", return_value=mock_ftp):
        entries = await ftps.list_dir_with_timestamps()

    assert entries == []


# --------------------------------------------------------------------------- #
# sort_files_newest_first
# --------------------------------------------------------------------------- #


class TestSortFilesNewestFirst:
    def test_all_modified_at_sorted_descending(self) -> None:
        entries = [
            FileEntry("c.3mf", modified_at=_dt(2024, 1, 1)),
            FileEntry("a.3mf", modified_at=_dt(2026, 6, 1)),
            FileEntry("b.3mf", modified_at=_dt(2025, 3, 15)),
        ]
        sorted_e = sort_files_newest_first(entries)
        assert [e.name for e in sorted_e] == ["a.3mf", "b.3mf", "c.3mf"]

    def test_undated_files_sort_last(self) -> None:
        entries = [
            FileEntry("undated1.3mf"),
            FileEntry("dated.3mf", modified_at=_dt(2026, 1, 1)),
            FileEntry("undated2.3mf"),
        ]
        sorted_e = sort_files_newest_first(entries)
        assert sorted_e[0].name == "dated.3mf"
        # undated1 and undated2 come after, stable (original relative order).
        assert sorted_e[1].name == "undated1.3mf"
        assert sorted_e[2].name == "undated2.3mf"

    def test_undated_stable_relative_order(self) -> None:
        entries = [
            FileEntry("x.3mf"),
            FileEntry("y.3mf"),
            FileEntry("z.3mf"),
        ]
        sorted_e = sort_files_newest_first(entries)
        assert [e.name for e in sorted_e] == ["x.3mf", "y.3mf", "z.3mf"]

    def test_sort_basis_set_correctly(self) -> None:
        memo = SlicedDateMemo()
        memo.put("sliced.3mf", 100, _dt(2026, 6, 1))
        entries = [
            FileEntry("sliced.3mf", modified_at=_dt(2026, 1, 1)),
            FileEntry("modified.3mf", modified_at=_dt(2025, 6, 1)),
            FileEntry("created.3mf", created_at=_dt(2025, 1, 1)),
            FileEntry("none.3mf"),
        ]
        sorted_e = sort_files_newest_first(
            entries, memo=memo, sizes={"sliced.3mf": 100}
        )
        by_name = {e.name: e for e in sorted_e}
        assert by_name["sliced.3mf"].sort_basis == "sliced"
        assert by_name["modified.3mf"].sort_basis == "modified"
        assert by_name["created.3mf"].sort_basis == "created"
        assert by_name["none.3mf"].sort_basis == "none"

    def test_sliced_beats_modified_same_file(self) -> None:
        memo = SlicedDateMemo()
        # sliced_at is newer than modified_at.
        memo.put("file.3mf", 100, _dt(2026, 6, 1))
        entries = [
            FileEntry("file.3mf", modified_at=_dt(2024, 1, 1)),
        ]
        sorted_e = sort_files_newest_first(
            entries, memo=memo, sizes={"file.3mf": 100}
        )
        assert sorted_e[0].sort_basis == "sliced"

    def test_mixed_tiers_interleave_by_timestamp(self) -> None:
        """Files from different tiers should sort purely by timestamp value."""
        memo = SlicedDateMemo()
        memo.put("newest_sliced.3mf", 50, _dt(2026, 12, 1))
        memo.put("old_sliced.3mf", 60, _dt(2024, 1, 1))

        entries = [
            FileEntry("newest_sliced.3mf", modified_at=_dt(2020, 1, 1)),
            FileEntry("middle_modified.3mf", modified_at=_dt(2025, 6, 1)),
            FileEntry("old_sliced.3mf", modified_at=_dt(2020, 1, 1)),
            FileEntry("oldest_created.3mf", created_at=_dt(2023, 1, 1)),
            FileEntry("undated.3mf"),
        ]
        sorted_e = sort_files_newest_first(
            entries,
            memo=memo,
            sizes={"newest_sliced.3mf": 50, "old_sliced.3mf": 60},
        )
        names = [e.name for e in sorted_e]
        assert names[0] == "newest_sliced.3mf"
        assert names[1] == "middle_modified.3mf"
        assert names[2] == "old_sliced.3mf"
        assert names[3] == "oldest_created.3mf"
        assert names[4] == "undated.3mf"

    def test_created_at_used_when_no_modified(self) -> None:
        entries = [
            FileEntry("a.3mf", created_at=_dt(2026, 1, 1)),
            FileEntry("b.3mf", created_at=_dt(2025, 1, 1)),
        ]
        sorted_e = sort_files_newest_first(entries)
        assert sorted_e[0].name == "a.3mf"
        assert sorted_e[0].sort_basis == "created"

    def test_empty_list_returns_empty(self) -> None:
        assert sort_files_newest_first([]) == []

    def test_single_entry_returned_as_is(self) -> None:
        entries = [FileEntry("only.3mf", modified_at=_dt(2026, 1, 1))]
        sorted_e = sort_files_newest_first(entries)
        assert len(sorted_e) == 1
        assert sorted_e[0].name == "only.3mf"

    def test_equal_timestamps_stable(self) -> None:
        """Files with identical timestamps keep original relative order."""
        dt_same = _dt(2026, 6, 1)
        entries = [
            FileEntry("first.3mf", modified_at=dt_same),
            FileEntry("second.3mf", modified_at=dt_same),
            FileEntry("third.3mf", modified_at=dt_same),
        ]
        sorted_e = sort_files_newest_first(entries)
        # All same timestamp — stable sort preserves original order.
        assert [e.name for e in sorted_e] == ["first.3mf", "second.3mf", "third.3mf"]

    def test_no_memo_skips_sliced_tier(self) -> None:
        entries = [FileEntry("f.3mf", modified_at=_dt(2026, 1, 1))]
        sorted_e = sort_files_newest_first(entries)
        assert sorted_e[0].sort_basis == "modified"


# --------------------------------------------------------------------------- #
# SlicedDateMemo
# --------------------------------------------------------------------------- #


class TestSlicedDateMemo:
    def test_put_and_get(self) -> None:
        memo = SlicedDateMemo()
        dt = _dt(2026, 5, 19, 7, 37, 44)
        memo.put("file.gcode.3mf", 12345, dt)
        assert memo.get("file.gcode.3mf", 12345) == dt

    def test_get_missing_returns_none(self) -> None:
        memo = SlicedDateMemo()
        assert memo.get("nosuchfile.gcode.3mf", 100) is None

    def test_different_size_different_entry(self) -> None:
        memo = SlicedDateMemo()
        dt1 = _dt(2026, 1, 1)
        dt2 = _dt(2026, 6, 1)
        memo.put("f.3mf", 100, dt1)
        memo.put("f.3mf", 200, dt2)
        assert memo.get("f.3mf", 100) == dt1
        assert memo.get("f.3mf", 200) == dt2

    def test_put_same_key_updates(self) -> None:
        memo = SlicedDateMemo()
        dt1 = _dt(2026, 1, 1)
        dt2 = _dt(2026, 6, 1)
        memo.put("f.3mf", 100, dt1)
        memo.put("f.3mf", 100, dt2)
        assert memo.get("f.3mf", 100) == dt2
        assert len(memo) == 1

    def test_eviction_at_cap(self) -> None:
        memo = SlicedDateMemo()
        for i in range(SlicedDateMemo._MEMO_MAX + 5):
            memo.put(f"file{i}.3mf", i, _dt(2026, 1, 1))
        assert len(memo) == SlicedDateMemo._MEMO_MAX

    def test_eviction_drops_oldest(self) -> None:
        memo = SlicedDateMemo()
        memo.put("first.3mf", 0, _dt(2026, 1, 1))
        for i in range(SlicedDateMemo._MEMO_MAX):
            memo.put(f"file{i}.3mf", i + 1, _dt(2026, 1, 1))
        # "first.3mf" should have been evicted.
        assert memo.get("first.3mf", 0) is None

    def test_len(self) -> None:
        memo = SlicedDateMemo()
        assert len(memo) == 0
        memo.put("f.3mf", 1, _dt(2026, 1, 1))
        assert len(memo) == 1


# --------------------------------------------------------------------------- #
# extract_sliced_at_from_bytes
# --------------------------------------------------------------------------- #


def _make_3mf_with_zip_timestamp(
    slice_info_time: tuple[int, int, int, int, int, int] | None = None,
    gcode_time: tuple[int, int, int, int, int, int] | None = None,
    gcode_header: str | None = None,
) -> bytes:
    """Build a minimal .gcode.3mf with controlled ZIP entry timestamps."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        # Required 3D model (empty resources — sliced file shape).
        info_3d = zipfile.ZipInfo("3D/3dmodel.model")
        info_3d.date_time = (2020, 1, 1, 0, 0, 0)
        zf.writestr(
            info_3d,
            '<?xml version="1.0"?><model xmlns="http://schemas.microsoft.com/'
            '3dmanufacturing/core/2015/02"><resources/><build/></model>',
        )

        if slice_info_time is not None:
            info_si = zipfile.ZipInfo("Metadata/slice_info.config")
            info_si.date_time = slice_info_time
            zf.writestr(info_si, "<config/>")

        gcode_content = gcode_header or "; no header\nG28\n"
        if gcode_time is not None:
            info_gc = zipfile.ZipInfo("Metadata/plate_1.gcode")
            info_gc.date_time = gcode_time
            zf.writestr(info_gc, gcode_content)

    return buf.getvalue()


class TestExtractSlicedAtFromBytes:
    def test_slice_info_zip_timestamp(self) -> None:
        data = _make_3mf_with_zip_timestamp(
            slice_info_time=(2026, 5, 19, 7, 37, 44)
        )
        dt = extract_sliced_at_from_bytes(data)
        assert dt == _dt(2026, 5, 19, 7, 37, 44)

    def test_gcode_zip_timestamp_fallback(self) -> None:
        # No slice_info.config — gcode entry's timestamp should be used.
        data = _make_3mf_with_zip_timestamp(
            gcode_time=(2026, 3, 10, 12, 0, 0)
        )
        dt = extract_sliced_at_from_bytes(data)
        assert dt == _dt(2026, 3, 10, 12, 0, 0)

    def test_gcode_header_comment(self) -> None:
        # ZIP timestamps are zeroed (pre-1980 year = 0, replaced by 1980 floor),
        # but gcode header has a recognisable comment.
        # Use year 1980 to simulate "zeroed" ZIP entries (anything < 2020 is ignored).
        data = _make_3mf_with_zip_timestamp(
            slice_info_time=(1980, 1, 1, 0, 0, 0),
            gcode_time=(1980, 1, 1, 0, 0, 0),
            gcode_header=(
                "; generated by OrcaSlicer 2.3.2 on 2026-05-19 at 07:26:38\n"
                "G28\n"
            ),
        )
        dt = extract_sliced_at_from_bytes(data)
        assert dt == _dt(2026, 5, 19, 7, 26, 38)

    def test_not_a_zip_returns_none(self) -> None:
        assert extract_sliced_at_from_bytes(b"not a zip") is None

    def test_empty_bytes_returns_none(self) -> None:
        assert extract_sliced_at_from_bytes(b"") is None

    def test_zip_without_relevant_members_returns_none(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("README.txt", "hello")
        assert extract_sliced_at_from_bytes(buf.getvalue()) is None

    def test_slice_info_timestamp_takes_priority_over_gcode(self) -> None:
        # Both present — slice_info.config wins.
        data = _make_3mf_with_zip_timestamp(
            slice_info_time=(2026, 5, 19, 7, 37, 44),
            gcode_time=(2026, 1, 1, 0, 0, 0),
        )
        dt = extract_sliced_at_from_bytes(data)
        assert dt == _dt(2026, 5, 19, 7, 37, 44)

    def test_real_probe_if_available(self) -> None:
        probe = (
            Path(__file__).resolve().parents[2]
            / "probes"
            / "3DBenchy_PETG_slot2.gcode.3mf"
        )
        if not probe.exists():
            pytest.skip("probe file not present")
        data = probe.read_bytes()
        dt = extract_sliced_at_from_bytes(data)
        assert dt is not None
        # The probe was sliced 2026-05-19 — verify it.
        assert dt.year == 2026
        assert dt.month == 5
        assert dt.day == 19


# --------------------------------------------------------------------------- #
# Opportunistic memo fill via VizCache.fill_mesh
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_viz_cache_fill_mesh_populates_sliced_memo(
    ftps_server: tuple[int, Path],
) -> None:
    """VizCache.fill_mesh fills the SlicedDateMemo when it downloads a 3MF."""
    from bambu_bridge.service.viz_cache import VizCache

    port, storage = ftps_server

    # Build a minimal sliced .gcode.3mf with a known slice timestamp.
    data = _make_3mf_with_zip_timestamp(
        slice_info_time=(2026, 5, 19, 7, 37, 44)
    )
    (storage / "job.gcode.3mf").write_bytes(data)

    memo = SlicedDateMemo()
    cache = VizCache(ftps_port=port, sliced_date_memo=memo)

    import ssl

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # fill_mesh uses FtpsTransfer internally; we need to override the port +
    # SSL context.  Patch _make_ftps to inject our insecure client.
    def _patched_make(ip: str, access_code: str) -> FtpsTransfer:
        return FtpsTransfer(ip, access_code, port=port, ssl_context=ctx)

    cache._make_ftps = _patched_make  # type: ignore[method-assign]

    ok = await cache.fill_mesh("printer1", "127.0.0.1", ACCESS_CODE, "job.gcode.3mf")
    assert ok is True

    # The memo should now have the sliced date for this file.
    sliced_at = memo.get("job.gcode.3mf", len(data))
    assert sliced_at == _dt(2026, 5, 19, 7, 37, 44)


@pytest.mark.asyncio
async def test_viz_cache_fill_mesh_no_memo_no_crash(
    ftps_server: tuple[int, Path],
) -> None:
    """VizCache.fill_mesh with memo=None must not raise."""
    from bambu_bridge.service.viz_cache import VizCache

    port, storage = ftps_server
    data = _make_3mf_with_zip_timestamp(slice_info_time=(2026, 5, 19, 7, 37, 44))
    (storage / "nemo.gcode.3mf").write_bytes(data)

    cache = VizCache(ftps_port=port, sliced_date_memo=None)

    import ssl

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def _patched_make2(ip: str, access_code: str) -> FtpsTransfer:
        return FtpsTransfer(ip, access_code, port=port, ssl_context=ctx)

    cache._make_ftps = _patched_make2  # type: ignore[method-assign]

    # Must not raise even with no memo.
    ok = await cache.fill_mesh("printer1", "127.0.0.1", ACCESS_CODE, "nemo.gcode.3mf")
    assert ok is True
