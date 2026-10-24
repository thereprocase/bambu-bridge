import hashlib
import os
import time
from pathlib import Path

from bambu_bridge.retention import prune_native_backups, prune_preview_caches, prune_state
from bambu_bridge.source_assets import SourceAssets

DAY = 86400


def age(path: Path, days: float) -> None:
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))


def test_unread_sources_are_removed_with_their_references(tmp_path: Path) -> None:
    assets = SourceAssets(tmp_path)
    old = assets.put("job:old", b"old print")
    fresh = assets.put("job:fresh", b"fresh print")
    age(assets.directory / old, 31)
    result = prune_preview_caches(tmp_path, 30 * DAY)
    assert result.files == 1 and result.bytes == len(b"old print")
    assert not (assets.directory / old).exists()
    assert assets.read("job:old") is None
    assert assets.read("job:fresh") == b"fresh print"
    assert (assets.directory / fresh).exists()


def test_reading_a_source_keeps_it(tmp_path: Path) -> None:
    # The active job's preview is read while it prints, which refreshes mtime.
    assets = SourceAssets(tmp_path)
    digest = assets.put("job:running", b"running print")
    age(assets.directory / digest, 45)
    assert assets.read("job:running") == b"running print"
    assert prune_preview_caches(tmp_path, 30 * DAY).files == 0
    assert assets.read("job:running") == b"running print"


def test_geometry_and_frames_age_out_and_unrelated_files_stay(tmp_path: Path) -> None:
    geometry = tmp_path / "preview-geometry"
    frames = tmp_path / "preview-frames"
    geometry.mkdir()
    frames.mkdir()
    name = hashlib.sha256(b"x").hexdigest()
    paths = [geometry / f"{name}.gz", frames / f"{name}.zip"]
    keep = [geometry / "notes.txt", frames / f"{name}.zip.tmp"]
    for path in paths + keep:
        path.write_bytes(b"data")
        age(path, 60)
    assert prune_preview_caches(tmp_path, 30 * DAY).files == 2
    assert not any(p.exists() for p in paths)
    assert all(p.exists() for p in keep)


def test_zero_age_disables_preview_pruning(tmp_path: Path) -> None:
    assets = SourceAssets(tmp_path)
    digest = assets.put("job:old", b"old")
    age(assets.directory / digest, 400)
    assert prune_preview_caches(tmp_path, 0).files == 0
    assert (assets.directory / digest).exists()


def test_native_backups_keep_the_newest(tmp_path: Path) -> None:
    root = tmp_path / "native-backups"
    root.mkdir()
    names = [f"{i:032x}.zip" for i in range(12)]
    for days, name in enumerate(reversed(names)):
        (root / name).write_bytes(b"zip")
        age(root / name, days)
    (root / ".abc.partial").write_bytes(b"in progress")
    (root / "rollback-x").mkdir()
    result = prune_native_backups(tmp_path, 10)
    assert result.files == 2
    remaining = sorted(p.name for p in root.glob("*.zip"))
    assert remaining == sorted(names[2:])
    assert (root / ".abc.partial").exists() and (root / "rollback-x").is_dir()
    assert prune_native_backups(tmp_path, 0).files == 0


def test_prune_state_tolerates_a_fresh_directory(tmp_path: Path) -> None:
    result = prune_state(tmp_path, preview_max_age_days=30, native_backups_keep=10)
    assert result.files == 0
