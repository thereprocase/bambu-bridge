from __future__ import annotations

import json
from pathlib import Path

import pytest

from bambu_bridge.library import LibraryError, LibraryStore
from bambu_bridge.library_backup import backup, restore
from tests.test_library import manifest


def test_backup_restore_keeps_committed_blobs_and_resumes_partial_uploads(tmp_path: Path) -> None:
    store = LibraryStore(tmp_path / "active")
    data = b"original bytes from a temporary folder"
    for cid in ("a" * 32, "b" * 32):
        store.create("client", manifest(data, cid))
    store.append("a" * 32, "part.step", 0, data, "client")
    store.finalize("a" * 32, "client")
    # A different blob is left partially committed with an uncommitted tail.
    pending = manifest(b"partial data", "c" * 32)
    store.create("client", pending)
    store.append(pending.id, "part.step", 0, b"partial", "client")
    stage = next((store.root / "uploads").iterdir())
    with stage.open("ab") as f:
        f.write(b" crash tail")
    report = backup(store, tmp_path / "backup")
    assert report["files"] == 3  # database, one shared blob, one staged prefix
    restored = restore(tmp_path / "backup", tmp_path / "restored")
    assert restored.download("a" * 32, "part.step")[0].read_bytes() == data
    assert restored.finalize("b" * 32, "client")["state"] == "stored"
    assert restored.get(pending.id)["uploads"]["part.step"]["offset"] == 7
    restored.append(pending.id, "part.step", 7, b" data", "client")
    restored.finalize(pending.id, "client")
    assert restored.download(pending.id, "part.step")[0].read_bytes() == b"partial data"
    assert restored.verify()["ok"]
    # Backing up/restoring did not mutate the active pending capture.
    assert store.get(pending.id)["state"] == "pending"


def test_backup_does_not_replace_existing_directories(tmp_path: Path) -> None:
    store = LibraryStore(tmp_path / "active")
    with pytest.raises(LibraryError, match="outside"):
        backup(store, store.root / "nested")
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "keep"
    marker.write_text("untouched")
    with pytest.raises(LibraryError, match="new directory"):
        backup(store, existing)
    assert marker.read_text() == "untouched"


def test_corrupted_or_escaping_backups_cannot_be_restored(tmp_path: Path) -> None:
    store = LibraryStore(tmp_path / "active")
    destination = tmp_path / "backup"
    backup(store, destination)
    (destination / "library.sqlite3").write_bytes(b"corrupted")
    with pytest.raises(LibraryError, match="shorter|checksum"):
        restore(destination, tmp_path / "bad")
    assert (tmp_path / "bad" / "INCOMPLETE").exists()
    with pytest.raises(LibraryError, match="incomplete"):
        LibraryStore(tmp_path / "bad")
    report = json.loads((destination / "snapshot.json").read_text())
    report["files"]["../escape"] = {"size": 1, "sha256": "a" * 64}
    (destination / "snapshot.json").write_text(json.dumps(report))
    with pytest.raises(LibraryError, match="Invalid backup"):
        restore(destination, tmp_path / "escaped")
    assert not (tmp_path / "escaped").exists()
