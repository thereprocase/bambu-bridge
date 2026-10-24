import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "deploy" / "release-prune.py"
spec = importlib.util.spec_from_file_location("release_prune", SCRIPT)
assert spec is not None and spec.loader is not None
prune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune)


def release(root: Path, name: str, age_days: float) -> Path:
    path = root / name
    (path / "src").mkdir(parents=True)
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "releases"
    root.mkdir()
    # Live: its .venv is a symlink into an older release, whose venv was made
    # from yet another release's interpreter (the CT105 pattern).
    interpreter = release(root, "main-ba8c21e", 40)
    (interpreter / "python" / "bin").mkdir(parents=True)
    venv_owner = release(root, "refill-fbd64b4", 10)
    venv = venv_owner / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = {interpreter}/python/bin\nversion = 3.12.13\n")
    (venv / "bin" / "uvicorn").write_text(f"#!{venv_owner}/.venv/bin/python\n")
    shebang = release(root, "pr12-ee89952", 50)
    (venv / "bin" / "tool").write_text(f"#!{shebang}/.venv/bin/python3 -I\n")
    site = venv / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    pth_target = release(root, "0.2.0", 60)
    (site / "_editable.pth").write_text(f"{pth_target}/src\n")
    live = release(root, "p1s-300c-20261017", 5)
    (live / ".venv").symlink_to(venv)
    media = release(root, "adaptive-candidate", 30)
    for days, name in enumerate(["old-a", "old-b", "old-c", "old-d"], start=70):
        release(root, name, days)
    unit = tmp_path / "unit.txt"
    unit.write_text(
        f"WorkingDirectory={live}\n"
        f"ExecStart={{ path={live}/.venv/bin/python ; argv[]={live}/.venv/bin/python "
        "-m bambu_bridge.main }\n"
        f"Environment=PYTHONPATH={live}/src BRIDGE_MEDIAMTX_PATH={media}/mediamtx\n"
    )
    return {"root": root, "unit": unit, "tmp": tmp_path}


def run(tree: dict[str, Path], capsys, *extra: str) -> dict:
    code = prune.main(
        [
            "--root",
            str(tree["root"]),
            "--unit-text",
            str(tree["unit"]),
            "--backups-dir",
            str(tree["tmp"] / "backups"),
            *extra,
        ]
    )
    assert code == 0
    return json.loads(capsys.readouterr().out)


def test_report_keeps_the_whole_reference_chain_and_deletes_nothing(tree, capsys) -> None:
    report = run(tree, capsys, "--keep", "1")
    kept = report["kept_releases"]
    assert set(kept) == {
        "p1s-300c-20261017",
        "refill-fbd64b4",
        "main-ba8c21e",
        "pr12-ee89952",
        "0.2.0",
        "adaptive-candidate",
    }
    assert kept["main-ba8c21e"] == "referenced by refill-fbd64b4"
    assert kept["adaptive-candidate"] == "live unit"
    assert set(report["remove_releases"]) == {"old-a", "old-b", "old-c", "old-d"}
    assert all((tree["root"] / name).is_dir() for name in report["remove_releases"])


def test_apply_removes_only_unreferenced_releases(tree, capsys) -> None:
    report = run(tree, capsys, "--keep", "1", "--apply")
    remaining = {p.name for p in tree["root"].iterdir()}
    assert remaining == set(report["kept_releases"])
    # The live interpreter chain still resolves after pruning.
    venv = tree["root"] / "p1s-300c-20261017" / ".venv"
    home = (venv / "pyvenv.cfg").read_text().split("=", 1)[1].split()[0]
    assert Path(home).is_dir()


def test_recent_dropins_keep_their_releases_for_rollback(tree, capsys) -> None:
    dropin_dir = tree["tmp"] / "dropins"
    dropin_dir.mkdir()
    older = dropin_dir / "99z-old-a.conf"
    older.write_text(f"[Service]\nWorkingDirectory={tree['root'] / 'old-a'}\n")
    previous = dropin_dir / "99zz-old-b.conf"
    previous.write_text(f"[Service]\nWorkingDirectory={tree['root'] / 'old-b'}\n")
    common = ["--keep", "1", "--dropin", str(older), "--dropin", str(previous)]
    report = run(tree, capsys, *common)
    assert report["kept_releases"]["old-b"] == "drop-in 99zz-old-b.conf"
    assert "old-a" in report["remove_releases"]


def test_deploy_snapshots_keep_newest_and_skip_own_retention(tree, capsys) -> None:
    backups = tree["tmp"] / "backups"
    backups.mkdir()
    for index in range(5):
        path = backups / f"release-0.{index}.0-20261001"
        path.mkdir()
        stamp = time.time() - (10 - index) * 86400
        os.utime(path, (stamp, stamp))
    for name in ["jobs-20261001T000000Z.db.gz", "bridge-state-20261001T000000Z.tar.gz"]:
        (backups / name).write_bytes(b"x")
        os.utime(backups / name, (0, 0))
    report = run(tree, capsys, "--backups-keep", "2", "--apply")
    assert sorted(report["remove_backups"]) == [
        "release-0.0.0-20261001",
        "release-0.1.0-20261001",
        "release-0.2.0-20261001",
    ]
    assert sorted(p.name for p in backups.iterdir()) == [
        "bridge-state-20261001T000000Z.tar.gz",
        "jobs-20261001T000000Z.db.gz",
        "release-0.3.0-20261001",
        "release-0.4.0-20261001",
    ]


def test_refuses_when_the_unit_points_nowhere_under_root(tree, capsys) -> None:
    tree["unit"].write_text("WorkingDirectory=/srv/elsewhere\n")
    code = prune.main(["--root", str(tree["root"]), "--unit-text", str(tree["unit"]), "--apply"])
    assert code == 2
    assert len(list(tree["root"].iterdir())) == 10
