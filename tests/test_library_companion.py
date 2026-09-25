from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from bambu_bridge.library import Capture, LibraryStore

ROOT = Path(__file__).resolve().parents[1]
_paths = sys.path[:]
sys.path[:0] = [str(ROOT / "companion"), str(ROOT / "plugins" / "bridge_library")]
try:
    spec = importlib.util.spec_from_file_location("companion_test", ROOT / "companion/companion.py")
    assert spec and spec.loader
    companion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(companion)
    import receiver
    import store as custody_module
finally:
    sys.path[:] = _paths

KEY = "bcs_" + "x" * 44
ORIGIN = "https://desktop.example.ts.net"


def slice_bytes(plate: int = 1) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(zipfile.ZipInfo(f"Metadata/plate_{plate}.gcode"), "G1 X1 Y1 E0.1\n")
    return output.getvalue()


def project_file(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("3D/3dmodel.model", "<model/>")
        archive.writestr("Metadata/project_settings.config", "{}")
    return path


class ArchiveConnection:
    def __init__(self, root: Path):
        self.store = LibraryStore(root)
        self.lose_response = True

    def request(self, method: str, resource: str, body: object = None) -> dict:
        url = urlsplit(resource)
        parts = url.path.split("/")
        if resource == "/captures" and method == "POST":
            return self.store.create("companion", Capture.model_validate(body))
        if parts[-1] == "finalize":
            result = self.store.finalize(parts[2], "companion")
            if self.lose_response:
                self.lose_response = False
                raise ConnectionError("Saved response was lost")
            return result
        if method == "PUT":
            return self.store.append(
                parts[2],
                unquote(parts[-1]),
                int(parse_qs(url.query)["offset"][0]),
                body,
                "companion",
            )
        raise AssertionError(f"Unexpected action {method} {resource}")


def test_stock_orca_upload_shape_and_archive_retry_survive_restart(tmp_path: Path) -> None:
    custody = companion.Custody(tmp_path / "desktop")
    source = tmp_path / "original.step"
    source.write_bytes(b"exact original CAD bytes")
    original = custody.remember(source, "original")
    project = custody.remember(project_file(tmp_path / "saved.3mf"), "project")
    source.unlink()
    app = companion.create_app(custody, ORIGIN, KEY)
    with TestClient(app, base_url=ORIGIN, headers={"X-Api-Key": KEY}) as client:
        assert client.get("/api/version").json()["text"].startswith("OctoPrint")
        response = client.post(
            "/api/files/local",
            data={"print": "false", "path": "", "plateindex": "3"},
            files={"file": ("part.gcode.3mf", slice_bytes(3))},
        )
    assert response.status_code == 201, response.text
    assert response.json()["effectivePrint"] is False
    row = custody.rows("inbox")[0]
    assert row["plate"] == 3
    assert row["association"] == "unassigned"
    assert row["sha256"] == hashlib.sha256(slice_bytes(3)).hexdigest()
    choices = [original["id"], project["id"]]
    with pytest.raises(ValueError, match="Confirm"):
        custody.freeze(row["id"], "Part", "2.4.2", choices)
    cid = custody.freeze(
        row["id"], "Part", "2.4.2", choices, association_confirmed=True, originals_complete=True
    )
    bridge = ArchiveConnection(tmp_path / "bridge")
    with pytest.raises(ConnectionError):
        custody.deliver(cid, bridge)
    custody = companion.Custody(tmp_path / "desktop")
    assert custody.outbox.pending() == [cid]
    status = custody.deliver(cid, bridge)
    assert status["state"] == "stored"
    assert status["project_roundtrip_verified"] is False
    assert custody.outbox.pending() == []
    assert (
        bridge.store.download(cid, "original.step")[0].read_bytes() == b"exact original CAD bytes"
    )
    assert bridge.store.download(cid, "part.gcode.3mf")[0].read_bytes() == slice_bytes(3)
    assert (
        custody.freeze(
            row["id"], "Part", "2.4.2", choices, association_confirmed=True, originals_complete=True
        )
        == cid
    )
    assert bridge.store.usage()["captures"] == 1
    with pytest.raises(ValueError, match="different choices"):
        custody.freeze(row["id"], "Part", "2.4.2", choices, association_confirmed=True)


def test_duplicate_sends_have_distinct_receipts_and_no_automatic_association(
    tmp_path: Path,
) -> None:
    custody = companion.Custody(tmp_path)
    rows = [custody.receive("same.gcode.3mf", 1, io.BytesIO(slice_bytes())) for _ in range(2)]
    assert rows[0]["id"] != rows[1]["id"]
    assert rows[0]["sha256"] == rows[1]["sha256"]
    assert all(row["association"] == "unassigned" for row in rows)
    assert not custody.outbox.pending()


def test_upload_and_print_is_not_silently_downgraded(tmp_path: Path) -> None:
    custody = companion.Custody(tmp_path)
    with TestClient(
        companion.create_app(custody, ORIGIN, KEY), base_url=ORIGIN, headers={"X-Api-Key": KEY}
    ) as client:
        response = client.post(
            "/api/files/local",
            data={"print": "true"},
            files={"file": ("part.gcode.3mf", slice_bytes())},
        )
    assert response.status_code == 409
    assert "No print was started" in response.text
    assert not custody.rows("inbox")


@pytest.mark.parametrize(
    "bad_name",
    [
        "../part.gcode.3mf",
        "C:\\part.gcode.3mf",
        "con.gcode.3mf",
        "part.gcode.3mf.",
        "part:other.gcode.3mf",
    ],
)
def test_unsafe_upload_names_leave_no_files(tmp_path: Path, bad_name: str) -> None:
    custody = companion.Custody(tmp_path)
    with pytest.raises(ValueError):
        custody.receive(bad_name, 1, io.BytesIO(slice_bytes()))
    assert not list((tmp_path / "inbox").iterdir())


def test_wrong_plate_and_size_limit_preserve_existing_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custody = companion.Custody(tmp_path)
    good = custody.receive("part.gcode.3mf", 1, io.BytesIO(slice_bytes()))
    with pytest.raises(ValueError, match="absent"):
        custody.receive("part.gcode.3mf", 2, io.BytesIO(slice_bytes()))
    monkeypatch.setattr(custody_module, "FILE_LIMIT", 8)
    with pytest.raises(ValueError, match="limit"):
        custody.receive("part.gcode.3mf", 1, io.BytesIO(slice_bytes()))
    assert [row["id"] for row in custody.rows("inbox")] == [good["id"]]


def test_auth_https_and_chunked_body_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = companion.create_app(companion.Custody(tmp_path), ORIGIN, KEY)
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/version").status_code == 401
        assert client.get("/api/version?key=" + KEY, headers={"X-Api-Key": KEY}).status_code == 401
        assert (
            client.get(
                "/api/version", headers={"X-Api-Key": KEY, "Origin": "https://other.ts.net"}
            ).status_code
            == 403
        )
        assert (
            client.get(
                "http://desktop.example.ts.net/api/version", headers={"X-Api-Key": KEY}
            ).status_code
            == 403
        )
        monkeypatch.setattr(receiver, "BODY_LIMIT", 20)
        response = client.post(
            "/api/files/local",
            headers={"X-Api-Key": KEY, "Content-Type": "multipart/form-data; boundary=aaa"},
            content=iter([b"--aaa\r\n", b"X" * 100]),
        )
        assert response.status_code == 413, response.text


def test_saved_project_and_local_snapshot_integrity(tmp_path: Path) -> None:
    custody = companion.Custody(tmp_path / "desktop")
    bad = tmp_path / "fake.3mf"
    bad.write_bytes(slice_bytes())
    with pytest.raises(ValueError, match="geometry"):
        custody.remember(bad, "project")
    # Metadata-like input names never overwrite internal receipts.
    source = tmp_path / "record.json"
    source.write_bytes(b"selected original")
    original = custody.remember(source, "original")
    assert custody.record("inputs", original["id"])["sha256"] == original["sha256"]
    row = custody.receive("part.gcode.3mf", 1, io.BytesIO(slice_bytes()))
    (custody.root / "inputs" / original["id"] / "files" / source.name).write_bytes(b"changed")
    with pytest.raises(ValueError, match="damaged"):
        custody.freeze(row["id"], "Part", "2.4.2", [original["id"]], association_confirmed=True)


def test_acknowledgement_crash_does_not_duplicate_a_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custody = companion.Custody(tmp_path)
    row = custody.receive("part.gcode.3mf", 1, io.BytesIO(slice_bytes()))
    original_write = custody_module.atomic_json

    def crash(path: Path, value: dict) -> None:
        if path.name == "record.json" and value.get("capture_id"):
            raise OSError("Simulated interruption after outbox commit")
        original_write(path, value)

    monkeypatch.setattr(custody_module, "atomic_json", crash)
    with pytest.raises(OSError):
        custody.freeze(row["id"], "Part", "2.4.2", [])
    monkeypatch.setattr(custody_module, "atomic_json", original_write)
    custody = companion.Custody(tmp_path)
    cid = custody.freeze(row["id"], "Part", "2.4.2", [])
    assert custody.outbox.pending() == [cid]
    assert json.loads((custody.outbox.root / cid / "manifest.json").read_text())["id"] == cid


def test_single_instance_protects_custody_and_quota(tmp_path: Path) -> None:
    lock = companion.InstanceLock(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="already"):
            companion.InstanceLock(tmp_path)
    finally:
        lock.close()
    companion.InstanceLock(tmp_path).close()
