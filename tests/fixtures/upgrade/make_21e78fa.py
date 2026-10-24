"""Build the upgrade fixture: every bridge store as release 21e78fa leaves it.

Run by hand, from the repo root, with the test interpreter:

    PYTHONPATH=src .venv/bin/python tests/fixtures/upgrade/make_21e78fa.py

It exports commit 21e78fa (live on CT 105 on 2026-10-23) with `git archive`,
runs that release's own code in a subprocess to create and seed each store the
way the bridge would, checkpoints the SQLite WALs, and writes:

    tests/fixtures/upgrade/21e78fa/state.tar.gz   the state directory
    tests/fixtures/upgrade/21e78fa/schema-*.sql   .schema of each database, for review
    tests/fixtures/upgrade/21e78fa/MANIFEST.md    what was seeded, with the ids

tests/test_upgrade_from_21e78fa.py opens that state with the current code.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

COMMIT = "21e78fa"
REPO = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent / COMMIT

SEED = r"""
import asyncio, json, sys, time
from pathlib import Path

import httpx

state = Path(sys.argv[1])
SERIAL = "00M00A000000000"
NOW = int(time.time())


async def main() -> dict:
    from bambu_bridge.db.jobs import Database, Printer, PrinterRepo, Spool, SpoolRepo
    from bambu_bridge.library import LibraryStore
    from bambu_bridge.native_inbox import NativeInbox
    from bambu_bridge.orca import OrcaStore
    from bambu_bridge.pairing import PairingStore
    from bambu_bridge.service.inventory import InventoryService
    from bambu_bridge.spool_assignments import SpoolAssignment
    from tests.test_accounting_attempts import manifest
    from tests.test_inventory_service import Provider
    from tests.test_library_replay import archive_slice, sliced

    seeded = {"commit": "21e78fa", "serial": SERIAL}

    # jobs.db: the printer, and one cabinet spool the owner re-enters by hand.
    db = Database(str(state / "jobs.db"))
    await db.connect()
    await PrinterRepo(db).add(
        Printer(id=SERIAL, friendly_name="P1S", ip="192.0.2.10", access_code="12345678",
                model="P1S", added_at=NOW)
    )
    cabinet = Spool(
        id="c0ffee00c0ffee00c0ffee00c0ffee00", name="PLA Matte Charcoal", material="PLA",
        color_hex="#1a1a1f", brand="Bambu", total_g=1000, remaining_g=640,
        notes="cabinet row, kept for hand re-entry", added_at=NOW,
    )
    await SpoolRepo(db).add(cabinet)
    seeded["cabinet_spool"] = cabinet.id
    await db.close()

    # Pairing, Orca and the native inbox live together under the pairing directory.
    pairing = PairingStore(state / "pairing")
    OrcaStore(pairing)
    inbox = NativeInbox(pairing.directory)

    def terminal(attempt: str, outcome: str) -> None:
        with inbox.connect() as conn:
            conn.execute("UPDATE uploads SET start_state=? WHERE id=?", (outcome, attempt))

    # A print debited by 21e78fa: single spool, delivered.
    delivered = inbox.claim_external(SERIAL, {"print": {"command": "project_file"}}, reserved=True)
    frozen = manifest()
    frozen["sources"][0].update(physical_source="external", spool_id=5)
    frozen["planned_grams"] = 12.5
    inbox.freeze_accounting(delivered, frozen)
    terminal(delivered, "completed")
    inbox.set_delivery_state(delivered, "delivered")
    seeded["delivered_attempt"] = delivered

    # A print held for review the way 63f16c9b is: no plan, the slicer's figure only.
    held = inbox.claim_external(SERIAL, {"print": {"command": "project_file"}}, reserved=True)
    frozen = manifest()
    frozen["sources"][0].update(physical_source="external", spool_id=5)
    frozen["planned_grams"] = None
    frozen["analysis_issues"] = ["multi_material_review"]
    frozen["sliced_settings"] = {"slicer_mass_g": [83.78, 0.0]}
    inbox.freeze_accounting(held, frozen)
    terminal(held, "completed")
    inbox.review_accounting(held, "analysis_or_source_unverified")
    inbox.set_delivery_state(held, "review")
    seeded["held_attempt"] = held

    # Inventory: provider initialised (checkpoint rows written), one assignment.
    provider = Provider()
    service = await InventoryService.open(
        state / "inventory", "https://inventory.invalid/", transport=httpx.MockTransport(provider)
    )
    await service.initialize_provider()
    snapshot = await service.assignments.snapshot()
    await service.assignments.replace(
        (SpoolAssignment(SERIAL, "external", 5, assigned_at=float(NOW)),),
        provider_id=snapshot.provider_id,
        expected_revision=snapshot.revision,
    )
    seeded["provider_id"] = snapshot.provider_id
    await service.close()

    # Library: one archived slice.
    store = LibraryStore(state / "library")
    seeded["capture"] = archive_slice(store, sliced())
    return seeded


print(json.dumps(asyncio.run(main())))
"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bridge-21e78fa-") as tmp:
        tree = Path(tmp) / "tree"
        tree.mkdir()
        archive = subprocess.run(
            ["git", "-C", str(REPO), "archive", COMMIT, "src", "tests"],
            check=True,
            capture_output=True,
        ).stdout
        subprocess.run(["tar", "-x", "-C", str(tree)], input=archive, check=True)
        (tree / "seed.py").write_text(SEED)
        state = Path(tmp) / "state"
        state.mkdir()
        result = subprocess.run(
            [sys.executable, str(tree / "seed.py"), str(state)],
            check=True,
            capture_output=True,
            text=True,
            env={"PYTHONPATH": f"{tree / 'src'}:{tree}", "PATH": "/usr/bin:/bin"},
        )
        seeded = result.stdout.strip().splitlines()[-1]

        databases = sorted(
            p for p in state.rglob("*") if p.suffix in {".db", ".sqlite", ".sqlite3"}
        )
        for db in databases:
            subprocess.run(["sqlite3", str(db), "PRAGMA wal_checkpoint(TRUNCATE);"], check=True)
            for side in ("-wal", "-shm"):
                (db.parent / (db.name + side)).unlink(missing_ok=True)
            schema = subprocess.run(
                ["sqlite3", str(db), ".schema"], check=True, capture_output=True, text=True
            ).stdout
            (OUT / f"schema-{db.relative_to(state).as_posix().replace('/', '_')}.sql").write_text(
                schema
            )
        for lock in state.rglob("*.lock"):
            lock.unlink()

        tarball = OUT / "state.tar.gz"
        with tarfile.open(tarball, "w:gz") as tar:
            tar.add(
                state, arcname="state", filter=lambda t: t if "node_modules" not in t.name else None
            )
        digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
        files = "\n".join(
            f"- {p.relative_to(state).as_posix()}" for p in sorted(state.rglob("*")) if p.is_file()
        )
        (OUT / "MANIFEST.md").write_text(
            f"# Upgrade fixture from release {COMMIT}\n\n"
            "Generated by tests/fixtures/upgrade/make_21e78fa.py "
            "with that release's own code.\n"
            f"state.tar.gz sha256 {digest}\n\n"
            f"Seeded:\n\n```json\n{seeded}\n```\n\nFiles:\n\n{files}\n"
        )
        print(seeded)
        print(f"wrote {tarball} ({tarball.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
