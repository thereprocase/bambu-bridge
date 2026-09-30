from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bambu_bridge import preview_source

GCODE = b"M83\nG1 X100 Y100 Z1\nG1 X110 E1\n"
SNAPSHOT = {"printer_id": "fixture", "_raw": {"gcode_file": "ordinary.gcode"}}


@pytest.mark.asyncio
async def test_source_shares_local_load_and_retains_content(tmp_path):
    calls = []

    def local(snapshot):
        calls.append(snapshot)
        return GCODE

    sources = preview_source.PreviewSources(tmp_path, lambda: None, local, lambda: 990)
    first, second = await asyncio.gather(sources.load(SNAPSHOT), sources.load(SNAPSHOT))
    assert first is second
    assert first is not None
    assert len(calls) == 1
    assert len([p for p in sources.directory.iterdir() if len(p.name) == 64]) == 1
    assert sources.inflight == {}


@pytest.mark.asyncio
async def test_ordinary_print_uses_printer_storage_and_recovers_failed_fetch(tmp_path, monkeypatch):
    calls = []

    class Transfer:
        def __init__(self, ip, code, *, port):
            assert (ip, code, port) == ("fixture-ip", "fixture-code", 990)

        async def list_dir(self, directory):
            return ["ordinary.gcode"]

        async def download_bytes(self, filename, *, remote_dir):
            calls.append(filename)
            if len(calls) == 1:
                raise OSError("synthetic interruption")
            return GCODE

    monkeypatch.setattr(preview_source, "FtpsTransfer", Transfer)
    service = SimpleNamespace(serial="fixture", ip="fixture-ip", access_code="fixture-code")
    sources = preview_source.PreviewSources(
        tmp_path, lambda: service, lambda snapshot: None, lambda: 990
    )
    with pytest.raises(OSError):
        await sources.load(SNAPSHOT)
    assert sources.inflight == {}
    result = await sources.load(SNAPSHOT)
    assert result is not None
    assert calls == ["ordinary.gcode", "ordinary.gcode"]


@pytest.mark.asyncio
async def test_old_printer_snapshot_cannot_fetch_from_replacement(tmp_path):
    service = SimpleNamespace(serial="replacement")
    sources = preview_source.PreviewSources(
        tmp_path, lambda: service, lambda snapshot: None, lambda: 990
    )
    with pytest.raises(ValueError, match="printer changed"):
        await sources.load(SNAPSHOT)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({"param": "Metadata/plate_3.gcode"}, 3),
        ({"plate_idx": "2"}, 2),
        ({"plate_idx": "invalid"}, 1),
        ({}, 1),
    ],
)
def test_selected_plate(raw, expected):
    assert preview_source.selected_plate(raw) == expected


def test_inner_member_uses_task_archive_name():
    raw = {"gcode_file": "Metadata/plate_2.gcode", "subtask_name": "ordinary"}
    assert preview_source.source_name(raw) == "ordinary"
    assert preview_source.selected_plate(raw) == 2


@pytest.mark.asyncio
async def test_restart_reuses_source_for_same_job_but_reprint_reacquires(tmp_path):
    current = {
        **SNAPSHOT,
        "_raw": {**SNAPSHOT["_raw"], "gcode_state": "RUNNING"},
        "job": {"started_at": "2026-01-01T00:00:00Z"},
    }
    first = preview_source.PreviewSources(
        tmp_path, lambda: None, lambda snapshot: GCODE, lambda: 990
    )
    assert await first.load(current) is not None

    def missing(snapshot):
        return None

    service = SimpleNamespace(serial="replacement")
    restarted = preview_source.PreviewSources(tmp_path, lambda: service, missing, lambda: 990)
    assert await restarted.load(current) is not None
    reprint = {**current, "job": {"started_at": "2026-01-02T00:00:00Z"}}
    with pytest.raises(ValueError, match="printer changed"):
        await restarted.load(reprint)
