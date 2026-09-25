from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from bambu_bridge.library import Artifact, Capture, LibraryError, LibraryStore


def manifest(data: bytes, cid: str = "a" * 32) -> Capture:
    return Capture(
        id=cid,
        title="Fixture",
        slicer_version="2.5.0-dev",
        plate=1,
        artifacts=(
            Artifact(
                name="part.step",
                role="original",
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            ),
        ),
        originals="complete",
    )


def test_resume_after_process_restart_and_uncommitted_tail(tmp_path: Path) -> None:
    data = b"exact original bytes"
    capture = manifest(data)
    store = LibraryStore(tmp_path)
    store.create("client", capture)
    store.append(capture.id, "part.step", 0, data[:6], "client")
    stage = next((tmp_path / "uploads").iterdir())
    with stage.open("ab") as f:
        f.write(b"uncommitted tail")
    store = LibraryStore(tmp_path)
    assert store.get(capture.id)["uploads"]["part.step"]["offset"] == 6
    store.append(capture.id, "part.step", 6, data[6:], "client")
    stored = store.finalize(capture.id, "client")
    assert stored["state"] == "stored"
    assert stored["project_roundtrip_verified"] is False
    path, digest = store.download(capture.id, "part.step")
    assert path.read_bytes() == data
    assert digest == hashlib.sha256(data).hexdigest()


def test_hash_failure_cannot_finalize_or_download(tmp_path: Path) -> None:
    store = LibraryStore(tmp_path)
    capture = manifest(b"correct")
    store.create("client", capture)
    with pytest.raises(LibraryError, match="checksum"):
        store.append(capture.id, "part.step", 0, b"corrupt", "client")
    assert store.get(capture.id)["uploads"]["part.step"]["offset"] == 0
    with pytest.raises(LibraryError, match="every declared"):
        store.finalize(capture.id, "client")
    with pytest.raises(LibraryError):
        store.download(capture.id, "part.step")
    store.append(capture.id, "part.step", 0, b"correct", "client")
    store.finalize(capture.id, "client")


def test_rename_before_db_commit_recovers(tmp_path: Path) -> None:
    data = b"recovered"
    capture = manifest(data)
    store = LibraryStore(tmp_path)
    store.create("client", capture)
    # Simulate process death after atomic rename but before committing blob row.
    (tmp_path / "blobs" / capture.artifacts[0].sha256).write_bytes(data)
    store.append(capture.id, "part.step", 0, data, "client")
    assert store.finalize(capture.id, "client")["state"] == "stored"


def test_dedup_deletion_and_ids_never_reused(tmp_path: Path) -> None:
    data = b"shared"
    store = LibraryStore(tmp_path)
    for cid in ("a" * 32, "b" * 32):
        store.create("client", manifest(data, cid))
        store.append(cid, "part.step", 0, data, "client")
        store.finalize(cid, "client")
    assert store.usage()["stored_bytes"] == len(data)
    store.delete("a" * 32)
    assert store.collect_unreferenced() == 0
    with pytest.raises(LibraryError, match="different manifest"):
        store.create("client", manifest(data))
    assert store.download("b" * 32, "part.step")[0].read_bytes() == data
    store.delete("b" * 32)
    assert store.collect_unreferenced() == 1
    assert store.usage()["stored_bytes"] == 0


def test_scope_manifest_immutability_offsets_and_quota(tmp_path: Path) -> None:
    store = LibraryStore(tmp_path, quota=5)
    capture = manifest(b"12345")
    store.create("first", capture)
    assert store.create("first", capture)["id"] == capture.id
    with pytest.raises(LibraryError):
        store.get(capture.id, "second")
    with pytest.raises(LibraryError):
        store.append(capture.id, "part.step", 0, b"12345", "second")
    with pytest.raises(LibraryError):
        store.create("second", capture)
    with pytest.raises(LibraryError, match="quota"):
        store.create("first", manifest(b"x", "c" * 32))
    with pytest.raises(LibraryError, match="Offset"):
        store.append(capture.id, "part.step", 1, b"12", "first")
    store.append(capture.id, "part.step", 0, b"12345", "first")
    store.finalize(capture.id, "first")
    with pytest.raises(LibraryError, match="immutable"):
        store.append(capture.id, "part.step", 0, b"12345", "first")


def test_read_and_integrity_scan_detect_bit_rot(tmp_path: Path) -> None:
    store = LibraryStore(tmp_path)
    capture = manifest(b"original")
    store.create("client", capture)
    store.append(capture.id, "part.step", 0, b"original", "client")
    store.finalize(capture.id, "client")
    assert store.verify()["ok"]
    path, _ = store.download(capture.id, "part.step")
    path.write_bytes(b"corrupt!")
    assert not store.verify()["ok"]
    with pytest.raises(LibraryError, match="damaged"):
        store.download(capture.id, "part.step")


def test_verified_reupload_can_repair_damaged_blob(tmp_path: Path) -> None:
    store = LibraryStore(tmp_path)
    data = b"correct"
    for cid in ("a" * 32, "b" * 32):
        store.create("client", manifest(data, cid))
    store.append("a" * 32, "part.step", 0, data, "client")
    store.finalize("a" * 32, "client")
    path, _ = store.download("a" * 32, "part.step")
    path.write_bytes(b"corrupt")
    store.append("b" * 32, "part.step", 0, data, "client")
    store.finalize("b" * 32, "client")
    assert store.verify()["ok"]
    assert store.download("a" * 32, "part.step")[0].read_bytes() == data


def test_pagination_preserves_equal_timestamps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bambu_bridge.library.time.time", lambda: 1234.0)
    store = LibraryStore(tmp_path)
    for char in "abcdef":
        store.create("client", manifest(b"x", char * 32))
    first = store.list(limit=3)
    second = store.list(limit=3, before=first[-1]["created"], before_id=first[-1]["id"])
    assert [row["id"] for row in first + second] == [c * 32 for c in "fedcba"]


@pytest.mark.parametrize("name", ["../secret", "C:\\secret", "..", "a\x00b", "a/b"])
def test_rejects_paths(name: str) -> None:
    with pytest.raises(ValidationError):
        Artifact(name=name, role="original", sha256="f" * 64, size=1)
