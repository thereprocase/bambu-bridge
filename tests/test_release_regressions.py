"""Regressions recovered from a deployed-runtime review; all data is synthetic."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import unquote

import pytest
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.testclient import TestClient
from pydantic import ValidationError
from uvicorn.logging import AccessFormatter

from bambu_bridge import log_redaction
from bambu_bridge.api import files, printers
from bambu_bridge.api.auth import require_auth
from bambu_bridge.api.uploads import read_upload
from bambu_bridge.api.viz import _viz_etag
from bambu_bridge.config import Settings
from bambu_bridge.hms import decode_hms_entry, stage_text
from bambu_bridge.protocol.ftps import FtpsTransfer, TransferTooLarge
from bambu_bridge.protocol.mqtt import MqttClient
from bambu_bridge.service.events import EventBus
from bambu_bridge.service.jobs import JobManager
from bambu_bridge.service.viz_cache import VizCache
from bambu_bridge.translate import _job_anomaly, _job_context


def test_uvicorn_credentials_are_redacted_before_handlers() -> None:
    secret = "synthetic-secret-with-a-unique-value"
    log_redaction.install(secret)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    for name in ("uvicorn.error", "uvicorn.access", "uvicorn.protocols.websockets"):
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            logger.info('%s - "WebSocket /status?token=%s&other=visible" [accepted]',
                        "synthetic", secret)
            logger.info("GET /camera?token=unknown-client-token&quality=1")
            logger.info("Authorization: Bearer another-client-key")
            try:
                raise RuntimeError(f"failed using {secret}")
            except RuntimeError:
                logger.exception("request failed")
        finally:
            logger.removeHandler(handler)
    result = stream.getvalue()
    assert secret not in result
    assert "unknown-client-token" not in result
    assert "another-client-key" not in result
    assert "other=visible" in result
    assert "[REDACTED]" in result
    event = log_redaction.redact_event(None, "info", {"access_code": "12345678",
                                                    "nested": {"message": secret}})
    assert secret not in json.dumps(event)
    assert "12345678" not in json.dumps(event)


@pytest.mark.parametrize("status_code", [200, 401])
def test_uvicorn_http_access_formatter_keeps_redacted_arguments(status_code: int) -> None:
    secret = "synthetic-access-log-secret"
    log_redaction.install(secret)
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access", logging.INFO, __file__, 0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:12345", "GET", f"/status?token={secret}&api_key=unknown&view=1",
         "1.1", status_code), None,
    )
    formatter = AccessFormatter(
        '%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False,
    )
    rendered = formatter.format(record)
    plain = logging.Formatter().format(record)
    for output in (rendered, plain):
        assert secret not in output
        assert "unknown" not in output
        assert "token=[REDACTED]&api_key=[REDACTED]&view=1" in output
        assert str(status_code) in output
    assert isinstance(record.args, tuple) and record.args[-1] == status_code


class FakeJobs:
    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}

    async def create(self, job: Any) -> None:
        self.rows[job.id] = job

    async def update(self, id: str, **fields: Any) -> Any:
        self.rows[id] = self.rows[id].model_copy(update=fields)
        return self.rows[id]

    async def get(self, id: str) -> Any:
        return self.rows.get(id)


async def test_terminal_jobs_release_payload_and_manager_entry() -> None:
    repo = FakeJobs()
    service = SimpleNamespace(bus=EventBus())
    manager = JobManager(repo, SimpleNamespace(add=AsyncMock()),
                         SimpleNamespace(get=lambda _: service))  # type: ignore[arg-type]
    runs = []
    for i in range(3):
        job = await manager.submit("synthetic", bytes([i]) * 1024, f"job-{i}.3mf")
        run = manager._runs[job.id]
        runs.append(run)
        await run.request_cancel()
    await asyncio.gather(*(run._task for run in runs))
    await asyncio.sleep(0)
    assert all(j.state.terminal for j in repo.rows.values())
    assert all(run._file_bytes == b"" for run in runs)
    assert manager._runs == {}
    await manager.shutdown()


def archive(x: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/plate_1.gcode",
                   f"G90\nM83\nG1 X0 Y0 Z0.2\nG1 X{x} Y0 E1\n")
    return buf.getvalue()


class MutableFiles:
    def __init__(self) -> None:
        self.data = archive(10)
        self.revision: tuple[int, str] | None = (len(self.data), "20260909120000")
        self.directory = ""
        self.downloads = 0
        self.deleted: list[str] = []

    async def list_dir(self, directory: str) -> list[str]:
        return ["sample.gcode.3mf"] if directory == self.directory else []

    async def file_revision(self, name: str, *, remote_dir: str = "") -> tuple[int, str] | None:
        return self.revision

    async def download_bytes(self, name: str, *, remote_dir: str = "") -> bytes:
        self.downloads += 1
        return self.data

    async def delete(self, path: str) -> None:
        self.deleted.append(path)


async def test_replaced_same_size_toolpath_changes_geometry_and_etag() -> None:
    ftps = MutableFiles()
    cache = VizCache()
    _, before = await cache._run_fill_toolpath("synthetic", ftps, "sample.gcode.3mf")  # type: ignore[arg-type]
    first_etag = _viz_etag("sample.gcode.3mf", len(ftps.data),
                          revision=cache.content_id("synthetic", "sample.gcode.3mf"))
    _, warm = await cache._run_fill_toolpath("synthetic", ftps, "sample.gcode.3mf")  # type: ignore[arg-type]
    assert warm is before and ftps.downloads == 1
    new_data = archive(20)
    assert len(new_data) == len(ftps.data)
    ftps.data = new_data
    ftps.revision = (len(ftps.data), "20260909120100")
    _, after = await cache._run_fill_toolpath("synthetic", ftps, "sample.gcode.3mf")  # type: ignore[arg-type]
    assert after.bbox_max[0] == 20
    assert ftps.downloads == 2
    assert first_etag != _viz_etag("sample.gcode.3mf", len(ftps.data),
                                  revision=cache.content_id("synthetic", "sample.gcode.3mf"))
    ftps.directory = "cache"
    _, relocated = await cache._run_fill_toolpath("synthetic", ftps, "sample.gcode.3mf")  # type: ignore[arg-type]
    assert relocated is not after and ftps.downloads == 3
    cache.invalidate("synthetic")
    assert cache.lookup_toolpath("synthetic", "sample.gcode.3mf") is None


async def test_missing_remote_revision_never_trusts_an_old_name() -> None:
    ftps = MutableFiles()
    ftps.revision = None
    cache = VizCache()
    await cache._run_fill_toolpath("synthetic", ftps, "sample.gcode.3mf")  # type: ignore[arg-type]
    ftps.data = archive(30)
    _, after = await cache._run_fill_toolpath("synthetic", ftps, "sample.gcode.3mf")  # type: ignore[arg-type]
    assert after.bbox_max[0] == 30
    assert ftps.downloads == 2


def test_delete_uses_requested_directory_and_unicode_download_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ftps = MutableFiles()
    app = FastAPI()
    app.include_router(files.router)
    app.dependency_overrides[require_auth] = lambda: None
    app.dependency_overrides[files.get_registry] = lambda: None
    monkeypatch.setattr(files, "_ftps_for", lambda *_: ftps)
    with TestClient(app) as client:
        response = client.delete("/printers/synthetic/files/sample.gcode.3mf?dir=cache")
        assert response.status_code == 200
        assert ftps.deleted == ["/cache/sample.gcode.3mf"]
        response = client.get("/printers/synthetic/files/测试.gcode.3mf")
        assert response.status_code == 200
        header = response.headers["content-disposition"]
        assert "filename*=UTF-8''" in header
        assert "测试.gcode.3mf" in unquote(header)
        assert response.content == ftps.data


@pytest.mark.parametrize("field,value", [
    ("ip", "not-an-ip"), ("access_code", "x"), ("friendly_name", ""),
])
def test_printer_updates_reject_bad_fields(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        printers.UpdatePrinter(**{field: value})


async def test_printer_update_destination_policy_runs_before_persistence() -> None:
    registry = SimpleNamespace(get=lambda _: None, update=AsyncMock())
    app = SimpleNamespace(state=SimpleNamespace(settings=Settings()))
    request = Request({"type": "http", "app": app})
    response = await printers.update_printer(
        "synthetic", printers.UpdatePrinter(ip="127.0.0.1"), request, registry)
    assert response.status_code == 422
    registry.update.assert_not_awaited()


def test_long_outage_backoff_remains_finite() -> None:
    for attempt in (1025, 100_000):
        assert 1 <= MqttClient._backoff(attempt) <= 15


def test_restored_status_distinguishes_early_finish_from_completed_layers() -> None:
    assert _job_context({"gcode_state": "FINISH"}) == "done"
    assert _job_anomaly({"gcode_state": "FINISH", "mc_percent": 20}) is not None
    assert _job_anomaly({"gcode_state": "FINISH", "mc_percent": 97,
                         "layer_num": 581, "total_layer_num": 581}) is None
    assert stage_text(22)
    decoded = decode_hms_entry(0x07002000, 0x00020001, "printing")
    assert decoded["hex"] == "0700_2000_0002_0001"
    assert decoded["context_note"]
    assert decoded["stale"] is False


async def test_upload_memory_limit_rejects_before_transfer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_MAX_TRANSFER_BYTES", "4")
    upload = UploadFile(filename="synthetic.3mf", file=io.BytesIO(b"12345"))
    with pytest.raises(HTTPException) as exc:
        await read_upload(upload)
    assert exc.value.status_code == 413
    assert await read_upload(UploadFile(filename="small.3mf", file=io.BytesIO(b"1234"))) == b"1234"


def test_download_limit_stops_buffer_growth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_MAX_TRANSFER_BYTES", "4")
    transfer = FtpsTransfer("127.0.0.1", "12345678")
    closed = []

    def retrbinary(command: str, receive: Any) -> None:
        receive(b"1234")
        receive(b"5")

    monkeypatch.setattr(transfer, "_connect", lambda: SimpleNamespace(retrbinary=retrbinary))
    monkeypatch.setattr(transfer, "_close", lambda _: closed.append(True))
    with pytest.raises(TransferTooLarge):
        transfer._download("/synthetic.3mf")
    assert closed == [True]
