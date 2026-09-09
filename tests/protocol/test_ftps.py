"""M1: implicit-TLS FTPS round-trip against the fake P1S FTPS server."""

from __future__ import annotations

import ftplib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bambu_bridge.protocol.ftps import FtpsTransfer, _ImplicitFTP_TLS
from tests.conftest import ACCESS_CODE


def _insecure_ftps(port: int) -> FtpsTransfer:
    import ssl

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return FtpsTransfer("127.0.0.1", ACCESS_CODE, port=port, ssl_context=ctx)


@pytest.mark.asyncio
async def test_upload_list_delete_roundtrip(
    ftps_server: tuple[int, Path],
) -> None:
    """Round-trip to the FTPS root ("") — the P1S-confirmed default directory."""
    port, storage = ftps_server
    ftps = _insecure_ftps(port)

    # Default upload target is root (""): remote path becomes "/<name>"
    remote = await ftps.upload_bytes(b"3MF-CONTENT", "benchy.3mf")
    assert remote == "/benchy.3mf"
    assert (storage / "benchy.3mf").read_bytes() == b"3MF-CONTENT"

    listing = await ftps.list_dir()
    assert "benchy.3mf" in listing

    await ftps.delete("/benchy.3mf")
    assert not (storage / "benchy.3mf").exists()


@pytest.mark.asyncio
async def test_upload_to_cache_dir(ftps_server: tuple[int, Path]) -> None:
    port, storage = ftps_server
    ftps = _insecure_ftps(port)
    remote = await ftps.upload_bytes(b"x", "transient.3mf", remote_dir="cache")
    assert remote == "/cache/transient.3mf"
    assert (storage / "cache" / "transient.3mf").exists()


@pytest.mark.asyncio
async def test_download_bytes_roundtrip(ftps_server: tuple[int, Path]) -> None:
    port, storage = ftps_server
    ftps = _insecure_ftps(port)
    blob = b"GCODE-3MF-" + bytes(range(256)) * 8  # non-trivial, binary-safe
    # Root download: file lives at storage root (FTPS "/")
    (storage / "back.3mf").write_bytes(blob)
    got = await ftps.download_bytes("back.3mf")
    assert got == blob
    # Cache dir is a real P1S sub-directory and still supported
    await ftps.upload_bytes(b"RT", "rt.3mf", remote_dir="cache")
    assert await ftps.download_bytes("rt.3mf", remote_dir="cache") == b"RT"


def test_ftps_tls_context_is_pinned_to_tls_1_2() -> None:
    """Same family as mqtt.py gotcha #8: the P1S is TLS-1.2-only."""
    import ssl

    from bambu_bridge.protocol.tls import insecure_tls_context

    ctx = insecure_tls_context()
    assert ctx.minimum_version is ssl.TLSVersion.TLSv1_2
    assert ctx.maximum_version is ssl.TLSVersion.TLSv1_2
    assert ctx.verify_mode is ssl.CERT_NONE


@pytest.mark.asyncio
async def test_list_dir_550_degrades_to_empty_list(capsys: pytest.CaptureFixture[str]) -> None:
    """550 from LIST (directory not found on P1S) must return empty list, not raise.

    The /model directory does NOT exist on P1S firmware — empirically confirmed
    on hardware 2026-05-19. Listing it returns 550, which the bridge must degrade
    to an empty list so the UI shows no files rather than an error banner. The
    same degrade must apply to any optional dir that a firmware variant hasn't
    pre-created (e.g. /timelapse on base firmware).

    structlog in test config routes through PrintLoggerFactory (stdout), so we
    capture stdout rather than caplog to verify the warning is emitted.
    """
    ftps = FtpsTransfer("127.0.0.1", "badcode", port=990)

    # Build a mock FTP object: MLSD raises error_perm (P1S doesn't support it),
    # LIST also raises error_perm 550 (directory not found) — the exact error
    # that was silently swallowed in production and broke the files UI.
    mock_ftp: MagicMock = MagicMock(spec=_ImplicitFTP_TLS)
    mock_ftp.mlsd.side_effect = ftplib.error_perm("500 MLSD not supported")
    mock_ftp.retrlines.side_effect = ftplib.error_perm("550 /timelapse: No such file or directory")

    with patch.object(ftps, "_connect", return_value=mock_ftp):
        result = await ftps.list_dir("timelapse")

    assert result == [], "550 from LIST must degrade to empty list"
    # structlog warns to stdout — verify the event key is present.
    out = capsys.readouterr().out
    assert "ftps.list_empty_on_perm_error" in out, (
        "Expected a warning log entry when 550 is returned for LIST"
    )


@pytest.mark.asyncio
async def test_list_dir_happy_path_root(
    ftps_server: tuple[int, Path],
) -> None:
    """Happy path: list_dir() with no argument returns files at FTPS root.

    The P1S stores .gcode.3mf files directly in "/" (confirmed on hardware
    2026-05-19). The default UPLOAD_DIR_PERSISTENT="" resolves to FTPS root.
    """
    port, storage = ftps_server
    ftps = _insecure_ftps(port)

    # Seed the storage root with two files (mirrors real P1S layout).
    (storage / "part_a.3mf").write_bytes(b"A")
    (storage / "part_b.3mf").write_bytes(b"B")

    # list_dir with no argument defaults to root.
    listing = await ftps.list_dir()

    assert "part_a.3mf" in listing
    assert "part_b.3mf" in listing
    # Dot entries must not appear.
    assert "." not in listing
    assert ".." not in listing
