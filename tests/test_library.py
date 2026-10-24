from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from bambu_bridge.library import Artifact, Attempt, Capture, LibraryError, LibraryStore


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
    assert store.read(capture.id, "part.step") == b"original"
    path, _ = store.download(capture.id, "part.step")
    path.write_bytes(b"corrupt!")
    assert not store.verify()["ok"]
    for read in (store.download, store.read):
        with pytest.raises(LibraryError, match="damaged"):
            read(capture.id, "part.step")
    path.write_bytes(b"short")
    with pytest.raises(LibraryError, match="missing or damaged"):
        store.read(capture.id, "part.step")


def test_read_refuses_an_unfinalized_capture(tmp_path: Path) -> None:
    store = LibraryStore(tmp_path)
    capture = manifest(b"original")
    store.create("client", capture)
    store.append(capture.id, "part.step", 0, b"original", "client")
    with pytest.raises(LibraryError, match="not found"):
        store.read(capture.id, "part.step")


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


def test_prune_drops_what_was_printed_longest_ago_first(tmp_path):
    import hashlib
    import time

    from bambu_bridge.library import Artifact, Capture, LibraryStore

    store = LibraryStore(tmp_path / "lib", quota=10**6)

    def add(ident, data, created):
        digest = hashlib.sha256(data).hexdigest()
        capture = Capture(
            id=ident,
            title=ident,
            slicer_version="x",
            plate=1,
            artifacts=(Artifact(name="p.gcode", role="slice", sha256=digest, size=len(data)),),
        )
        store.create("owner", capture)
        store.append(ident, "p.gcode", 0, data, "owner")
        store.finalize(ident, "owner")
        with store.connect() as db:
            db.execute("UPDATE captures SET created=? WHERE id=?", (created, ident))

    now = time.time()
    add("a" * 32, b"x" * 400, now - 300)
    add("b" * 32, b"y" * 400, now - 200)
    add("c" * 32, b"z" * 400, now - 100)
    assert store.prune(keep_bytes=800) == ["a" * 32]
    assert store.prune(keep_bytes=10**6, max_age_s=150) == ["b" * 32]
    assert [c["id"] for c in store.list()] == ["c" * 32]


def test_prune_reads_the_library_once_when_a_reprint_kept_an_old_capture(tmp_path):
    import time
    from contextlib import contextmanager

    store = LibraryStore(tmp_path / "lib", quota=10**6)
    data = b"x" * 400
    store.create("owner", manifest(data))
    store.append("a" * 32, "part.step", 0, data, "owner")
    store.finalize("a" * 32, "owner")
    now = time.time()
    with store.connect() as db:
        db.execute("UPDATE captures SET created=?", (now - 300,))
        db.execute("INSERT INTO attempts VALUES ('t', ?, 'native', 't', '{}', ?)", ("a" * 32, now))
    statements: list[str] = []
    connect = store.connect

    @contextmanager
    def traced():
        with connect() as db:
            db.set_trace_callback(statements.append)
            yield db

    store.connect = traced  # type: ignore[method-assign]
    # Printed again just now, so within the age limit even though it was captured long ago.
    assert store.prune(keep_bytes=10**6, max_age_s=150) == []
    assert len([s for s in statements if s.startswith("SELECT")]) == 1


PNG_SMALL = b"\x89PNG\r\n\x1a\nsmall"
PNG_LARGE = b"\x89PNG\r\n\x1a\nlarge"


def sliced_bundle(members: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as bundle:
        bundle.writestr("Metadata/plate_1.gcode", b"G1 X1\n")
        for name, value in members.items():
            bundle.writestr(f"Metadata/{name}", value)
    return data.getvalue()


def stored_slice(store: LibraryStore, data: bytes, plate: int = 1, cid: str = "c" * 32) -> str:
    from bambu_bridge.service.print_capture import store_copy

    store_copy(store, cid, "part", plate, "part.gcode.3mf", data)
    return cid


def test_a_plate_thumbnail_prefers_the_small_render(tmp_path: Path) -> None:
    store = LibraryStore(tmp_path)
    both = {"plate_1_small.png": PNG_SMALL, "plate_1.png": PNG_LARGE, "plate_2.png": PNG_LARGE}
    cid = stored_slice(store, sliced_bundle(both))
    data, etag = store.thumbnail(cid)
    assert data == PNG_SMALL and etag.endswith('-plate_1_small.png"')
    assert store.thumbnail(cid, 2)[0] == PNG_LARGE
    only_large = stored_slice(store, sliced_bundle({"plate_1.png": PNG_LARGE}), cid="d" * 32)
    assert store.thumbnail(only_large)[0] == PNG_LARGE


@pytest.mark.parametrize(
    "data",
    [
        sliced_bundle({}),
        sliced_bundle({"plate_1.png": b"not a picture"}),
        b"; plain G-code\nG1 X1\n",
    ],
)
def test_a_capture_without_a_plate_picture_has_no_thumbnail(tmp_path: Path, data: bytes) -> None:
    store = LibraryStore(tmp_path)
    cid = stored_slice(store, data)
    with pytest.raises(LibraryError, match="no plate picture") as raised:
        store.thumbnail(cid)
    assert raised.value.status == 404


def test_captures_for_maps_recorded_inbox_rows_to_their_captures(tmp_path: Path) -> None:
    import time

    store = LibraryStore(tmp_path)
    cid = stored_slice(store, sliced_bundle({}))
    digest = store.get(cid)["artifacts"][0]["sha256"]
    for source, source_id in (("native", "a" * 32), ("bridge", "b" * 32)):
        store.record_attempt(
            Attempt(
                id=hashlib.sha256(source_id.encode()).hexdigest()[:32],
                capture_id=cid,
                slice_sha256=digest,
                printer_id="P1S",
                source=source,
                source_id=source_id,
                state="active",
                source_state="running",
                created_at=time.time(),
            )
        )
    # Only inbox rows (native or replay) map; a bridge job's id is another namespace.
    assert store.captures_for(["a" * 32, "b" * 32, "e" * 32]) == {"a" * 32: cid}
    assert store.captures_for([]) == {}


def test_an_encrypted_plate_picture_is_no_picture(tmp_path: Path) -> None:
    raw = bytearray(sliced_bundle({"plate_1.png": PNG_SMALL}))
    for signature, flag in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = 0
        while (at := raw.find(signature, start)) >= 0:
            raw[at + flag] |= 1  # general-purpose bit 0: encrypted member
            start = at + 1
    store = LibraryStore(tmp_path)
    cid = stored_slice(store, bytes(raw))
    with pytest.raises(LibraryError, match="no plate picture"):
        store.thumbnail(cid)
