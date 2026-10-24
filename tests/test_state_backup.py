import hashlib
import os
import sqlite3
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "deploy" / "state-backup.sh"


def database(path: Path, value: str) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    db.execute("INSERT INTO t VALUES (?)", (value,))
    db.commit()
    return db


def run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={"PATH": os.environ["PATH"], "TMPDIR": env["TMPDIR"], **env},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def state(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "state"
    inventory = tmp_path / "inventory"
    spoolman = tmp_path / "spoolman" / "spoolman.db"
    (tmp_path / "tmp").mkdir()
    return {"root": root, "inventory": inventory, "spoolman": spoolman, "tmp": tmp_path / "tmp"}


def test_snapshots_all_bridge_and_inventory_state_privately(state: dict[str, Path]) -> None:
    root, inventory = state["root"], state["inventory"]
    # Connections stay open in WAL mode, as in a running bridge.
    open_dbs = [
        database(root / "jobs.db", "job"),
        database(inventory / "inventory.sqlite", "assignment"),
        database(inventory / "refill" / "refill.sqlite3", "owned-alias"),
        database(root / "pairing" / "devices.sqlite3", "device"),
        database(root / "pairing" / "native-inbox" / "inbox.sqlite3", "attempt"),
        database(state["spoolman"], "spool-43"),
    ]
    (inventory / "writer.lock").write_text("")
    (inventory / "refill" / "refill.lock").write_text("")
    (root / "pairing" / "ca.pem").write_text("certificate")
    (root / "pairing" / "native.key").write_bytes(b"\x00\x01binary key material")
    (root / "diagnostic-history").mkdir()
    (root / "diagnostic-history" / "p1s.json").write_text('{"archived_refills": []}')
    (root / "diagnostic-history" / "p1s.tmp").write_text("partial")
    (root / "pairing" / "native-inbox" / "abc.payload").write_bytes(b"artifact")
    (root / "pairing" / "large.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    result = run(
        {
            "BRIDGE_DB_PATH": str(root / "jobs.db"),
            "BRIDGE_INVENTORY_DIR": str(inventory),
            "SPOOLMAN_DB_PATH": str(state["spoolman"]),
            "TMPDIR": str(state["tmp"]),
        }
    )
    assert result.returncode == 0, result.stderr
    assert "null byte" not in result.stderr
    archives = list((root / "backups").glob("bridge-state-*.tar.gz"))
    assert len(archives) == 1
    assert stat.S_IMODE(archives[0].stat().st_mode) == 0o600
    out = state["tmp"] / "out"
    with tarfile.open(archives[0]) as tar:
        tar.extractall(out, filter="data")
    names = {str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()} - {
        "MANIFEST.sha256",
        "BACKUP-INFO",
    }
    assert names == {
        "jobs/jobs.db",
        "inventory/inventory.sqlite",
        "inventory/refill/refill.sqlite3",
        "pairing/devices.sqlite3",
        "pairing/native-inbox/inbox.sqlite3",
        "pairing/ca.pem",
        "pairing/native.key",
        "diagnostics/p1s.json",
        "spoolman/spoolman.db",
    }
    for line in (out / "MANIFEST.sha256").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    restored = sqlite3.connect(out / "inventory/refill/refill.sqlite3")
    assert restored.execute("SELECT v FROM t").fetchall() == [("owned-alias",)]
    info = (out / "BACKUP-INFO").read_text()
    assert "inventory_included=yes" in info and "spoolman_included=yes" in info
    for db in open_dbs:
        db.close()


def test_inventory_dir_is_optional_but_must_exist_when_set(state: dict[str, Path]) -> None:
    root = state["root"]
    database(root / "jobs.db", "job").close()
    base = {"BRIDGE_DB_PATH": str(root / "jobs.db"), "TMPDIR": str(state["tmp"])}
    assert run(base).returncode == 0
    missing = run({**base, "BRIDGE_INVENTORY_DIR": str(state["inventory"])})
    assert missing.returncode != 0
    assert "does not exist" in missing.stderr


def test_rejects_a_non_sqlite_spoolman_path(state: dict[str, Path]) -> None:
    root = state["root"]
    database(root / "jobs.db", "job").close()
    state["spoolman"].parent.mkdir(parents=True)
    state["spoolman"].write_text("not a database")
    result = run(
        {
            "BRIDGE_DB_PATH": str(root / "jobs.db"),
            "SPOOLMAN_DB_PATH": str(state["spoolman"]),
            "TMPDIR": str(state["tmp"]),
        }
    )
    assert result.returncode != 0
    assert not list((root / "backups").glob("*.tar.gz"))


RESTORE = Path(__file__).parents[1] / "deploy" / "state-restore.sh"


def restore(archive: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(RESTORE), str(archive)],
        env={
            "PATH": os.environ["PATH"],
            "BRIDGE_HEALTH_URL": "http://127.0.0.1:9/unused",
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def values(path: Path) -> list[tuple[str]]:
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT v FROM t").fetchall()
    finally:
        db.close()


def test_restore_round_trip_keeps_replaced_files_and_leaves_spoolman(
    state: dict[str, Path],
) -> None:
    root, inventory = state["root"], state["inventory"]
    for path, value in [
        (root / "jobs.db", "job"),
        (inventory / "inventory.sqlite", "assignment-v1"),
        (inventory / "refill" / "refill.sqlite3", "owned"),
        (root / "pairing" / "devices.sqlite3", "device"),
        (state["spoolman"], "spool-43"),
    ]:
        database(path, value).close()
    history = root / "diagnostic-history" / "p1s.json"
    history.parent.mkdir()
    history.write_text("refills-v1")
    env = {
        "BRIDGE_DB_PATH": str(root / "jobs.db"),
        "BRIDGE_INVENTORY_DIR": str(inventory),
        "SPOOLMAN_DB_PATH": str(state["spoolman"]),
        "TMPDIR": str(state["tmp"]),
    }
    assert run(env).returncode == 0
    archive = next((root / "backups").glob("bridge-state-*.tar.gz"))
    # Later changes that the restore must replace but keep aside.
    database(inventory / "inventory.sqlite", "assignment-v2").close()
    database(state["spoolman"], "spool-43-debited").close()
    history.write_text("refills-v2")
    result = restore(archive, env)
    assert result.returncode == 0, result.stderr
    assert "Spoolman snapshot" in result.stdout
    assert values(inventory / "inventory.sqlite") == [("assignment-v1",)]
    aside = next(inventory.glob("pre-restore-*"))
    assert values(aside / "inventory.sqlite") == [("assignment-v1",), ("assignment-v2",)]
    assert values(state["spoolman"]) == [("spool-43",), ("spool-43-debited",)]
    assert history.read_text() == "refills-v1"
    assert (next(history.parent.glob("pre-restore-*")) / "p1s.json").read_text() == "refills-v2"


def test_restore_refuses_while_the_bridge_answers(state: dict[str, Path]) -> None:
    import http.server
    import threading

    root = state["root"]
    database(root / "jobs.db", "job").close()
    env = {"BRIDGE_DB_PATH": str(root / "jobs.db"), "TMPDIR": str(state["tmp"])}
    assert run(env).returncode == 0
    archive = next((root / "backups").glob("bridge-state-*.tar.gz"))
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        result = restore(archive, {**env, "BRIDGE_HEALTH_URL": url})
    finally:
        server.shutdown()
    assert result.returncode != 0
    assert "stop the service first" in result.stderr
    assert not list(root.glob("pre-restore-*"))


def test_restore_rejects_a_tampered_archive(state: dict[str, Path]) -> None:
    root = state["root"]
    database(root / "jobs.db", "job").close()
    env = {"BRIDGE_DB_PATH": str(root / "jobs.db"), "TMPDIR": str(state["tmp"])}
    assert run(env).returncode == 0
    archive = next((root / "backups").glob("bridge-state-*.tar.gz"))
    unpacked = state["tmp"] / "tamper"
    with tarfile.open(archive) as tar:
        tar.extractall(unpacked, filter="data")
    database(unpacked / "jobs" / "jobs.db", "forged").close()
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(unpacked, arcname=".")
    result = restore(archive, env)
    assert result.returncode != 0
    assert "manifest verification failed" in result.stderr
    assert values(root / "jobs.db") == [("job",)]
