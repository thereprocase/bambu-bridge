import asyncio
import json
from pathlib import Path

import pytest

from bambu_bridge.spool_assignments import AssignmentConflict, SpoolAssignment, SpoolAssignmentStore


async def test_restart_and_atomic_move(tmp_path: Path) -> None:
    path = tmp_path / "inventory.sqlite"
    store = await SpoolAssignmentStore.open(path)
    initial = await store.snapshot()
    first = await store.replace(
        (SpoolAssignment("p1", "ams:hardware-a:0", 43),),
        provider_id=initial.provider_id,
        expected_revision=0,
    )
    await store.close()
    store = await SpoolAssignmentStore.open(path)
    try:
        assert await store.snapshot() == first
        moved = await store.replace(
            (SpoolAssignment("p2", "external", 43),),
            provider_id=first.provider_id,
            expected_revision=1,
        )
        assert moved.revision == 2
        assert moved.assignments == (SpoolAssignment("p2", "external", 43),)
        with pytest.raises(AssignmentConflict):
            await store.replace((), provider_id=first.provider_id, expected_revision=1)
        with pytest.raises(AssignmentConflict):
            await store.replace((), provider_id="unrelated-provider", expected_revision=2)
        assert await store.snapshot() == moved
    finally:
        await store.close()


async def test_two_connections_cannot_overwrite_each_other(tmp_path: Path) -> None:
    path = tmp_path / "inventory.sqlite"
    one = await SpoolAssignmentStore.open(path)
    two = await SpoolAssignmentStore.open(path)
    try:
        initial = await one.snapshot()
        await two.replace((), provider_id=initial.provider_id, expected_revision=0)
        with pytest.raises(AssignmentConflict):
            await one.replace((), provider_id=initial.provider_id, expected_revision=0)
        assert (await one.snapshot()).revision == 1
    finally:
        await one.close()
        await two.close()


@pytest.mark.parametrize(
    "assignments",
    [
        (SpoolAssignment("p1", "external", 1), SpoolAssignment("p2", "external", 1)),
        (SpoolAssignment("p1", "external", 1), SpoolAssignment("p1", "external", 2)),
    ],
)
async def test_duplicate_rejected_without_mutation(tmp_path: Path, assignments: tuple) -> None:
    store = await SpoolAssignmentStore.open(tmp_path / "inventory.sqlite")
    try:
        initial = await store.snapshot()
        with pytest.raises(ValueError):
            await store.replace(assignments, provider_id=initial.provider_id, expected_revision=0)
        assert await store.snapshot() == initial
    finally:
        await store.close()


@pytest.mark.parametrize("revision", [True, -1, 0.5, "0"])
async def test_revision_must_be_integer(tmp_path: Path, revision: object) -> None:
    store = await SpoolAssignmentStore.open(tmp_path / "inventory.sqlite")
    try:
        initial = await store.snapshot()
        with pytest.raises(ValueError):
            await store.replace((), provider_id=initial.provider_id, expected_revision=revision)
        assert await store.snapshot() == initial
    finally:
        await store.close()


# The fields of SpoolAssignment at 0c416d2, whose loader is SpoolAssignment(**v).
OLD_FIELDS = {
    "printer_id",
    "source",
    "spool_id",
    "assigned_at",
    "observed_identity",
    "wire_slot",
    "origin",
}


async def test_seats_live_outside_the_payload_an_older_bridge_loads(tmp_path: Path) -> None:
    path = tmp_path / "inventory.sqlite"
    store = await SpoolAssignmentStore.open(path)
    try:
        initial = await store.snapshot()
        seated = SpoolAssignment(
            "p1", "ams:a:tray:0", 43, assigned_at=5.25, spool_key="untagged", tray_type="PETG"
        )
        await store.replace((seated,), provider_id=initial.provider_id, expected_revision=0)
        async with store._conn.execute("SELECT payload FROM spool_assignments") as cursor:
            payload = json.loads((await cursor.fetchone())[0])
        assert set(payload[0]) <= OLD_FIELDS
        assert (await store.snapshot()).assignments == (seated,)
    finally:
        await store.close()
    store = await SpoolAssignmentStore.open(path)  # a restart keeps the seat
    try:
        assert (await store.snapshot()).assignments[0].spool_key == "untagged"
    finally:
        await store.close()


async def test_a_seat_from_an_earlier_assignment_is_not_applied(tmp_path: Path) -> None:
    # An older bridge (after a downgrade) reassigned the slot and left the seat.
    store = await SpoolAssignmentStore.open(tmp_path / "inventory.sqlite")
    try:
        initial = await store.snapshot()
        old = SpoolAssignment(
            "p1", "ams:a:tray:0", 43, assigned_at=1, spool_key="untagged", tray_type="PETG"
        )
        await store.replace((old,), provider_id=initial.provider_id, expected_revision=0)
        later = json.dumps(
            [
                {
                    "printer_id": "p1",
                    "source": "ams:a:tray:0",
                    "spool_id": 44,
                    "assigned_at": 2,
                    "observed_identity": None,
                    "wire_slot": 0,
                    "origin": "owner",
                    "added_by_a_newer_bridge": True,
                }
            ]
        )
        await store._conn.execute("UPDATE spool_assignments SET payload=?", (later,))
        await store._conn.commit()
        (assignment,) = (await store.snapshot()).assignments
        assert (assignment.spool_id, assignment.spool_key) == (44, None)
    finally:
        await store.close()


async def test_seats_and_payload_roll_back_together(tmp_path: Path, monkeypatch) -> None:
    store = await SpoolAssignmentStore.open(tmp_path / "inventory.sqlite")
    try:
        initial = await store.snapshot()
        kept = SpoolAssignment(
            "p1", "ams:a:tray:0", 43, assigned_at=1, spool_key="untagged", tray_type="PETG"
        )
        before = await store.replace((kept,), provider_id=initial.provider_id, expected_revision=0)
        execute = store._conn.execute

        async def failing(sql, *args):
            if sql.startswith("INSERT INTO assignment_history"):
                raise OSError("disk went away")
            return await execute(sql, *args)

        monkeypatch.setattr(store._conn, "execute", failing)
        moved = SpoolAssignment(
            "p1",
            "ams:a:tray:1",
            43,
            assigned_at=2,
            observed_identity="b" * 64,
            spool_key="k" * 64,
            tray_type="PLA",
        )
        with pytest.raises(OSError):
            await store.replace((moved,), provider_id=initial.provider_id, expected_revision=1)
        monkeypatch.setattr(store._conn, "execute", execute)
        assert await store.snapshot() == before
    finally:
        await store.close()


async def test_a_snapshot_never_sees_a_replace_half_done(tmp_path: Path) -> None:
    store = await SpoolAssignmentStore.open(tmp_path / "inventory.sqlite")
    try:
        initial = await store.snapshot()
        states = [
            (
                SpoolAssignment(
                    "p1",
                    f"ams:a:tray:{i % 4}",
                    43,
                    assigned_at=float(i + 1),
                    spool_key="untagged",
                    tray_type="PETG",
                ),
            )
            for i in range(20)
        ]

        async def write() -> None:
            for revision, state in enumerate(states):
                await store.replace(
                    state, provider_id=initial.provider_id, expected_revision=revision
                )
                await asyncio.sleep(0)

        async def read() -> list:
            seen = []
            for _ in range(60):
                seen.append(await store.snapshot())
                await asyncio.sleep(0)
            return seen

        _, seen = await asyncio.gather(write(), read())
        assert all(a.spool_key == "untagged" for s in seen for a in s.assignments)
    finally:
        await store.close()
