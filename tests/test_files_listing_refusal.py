"""A refused root listing is an error to callers, never an empty card."""

import ftplib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from bambu_bridge.api import files
from bambu_bridge.protocol.ftps import UPLOAD_DIR_CACHE, UPLOAD_DIR_PERSISTENT, FileEntry


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(files, "_LIST_RETRY_DELAY", 0)


async def test_root_refusal_is_503_after_one_retry():
    ftps = SimpleNamespace(
        list_dir_with_timestamps=AsyncMock(side_effect=ftplib.error_perm("550 "))
    )
    with pytest.raises(HTTPException) as info:
        await files._list_entries(ftps, UPLOAD_DIR_PERSISTENT)
    assert info.value.status_code == 503
    assert info.value.headers == {"Retry-After": "5"}
    assert ftps.list_dir_with_timestamps.await_count == 2
    assert ftps.list_dir_with_timestamps.await_args.kwargs == {"strict": True}


async def test_transient_refusal_recovers_on_retry():
    entries = [FileEntry(name="beluga-abc.gcode.3mf")]
    ftps = SimpleNamespace(
        list_dir_with_timestamps=AsyncMock(side_effect=[ftplib.error_perm("550 "), entries])
    )
    assert await files._list_entries(ftps, UPLOAD_DIR_PERSISTENT) == entries


async def test_optional_directories_still_degrade_to_empty():
    ftps = SimpleNamespace(list_dir_with_timestamps=AsyncMock(return_value=[]))
    assert await files._list_entries(ftps, UPLOAD_DIR_CACHE) == []
    assert ftps.list_dir_with_timestamps.await_args.kwargs == {"strict": False}


async def test_other_failures_stay_502():
    ftps = SimpleNamespace(list_dir_with_timestamps=AsyncMock(side_effect=OSError("down")))
    with pytest.raises(HTTPException) as info:
        await files._list_entries(ftps, UPLOAD_DIR_PERSISTENT)
    assert info.value.status_code == 502
