"""Groups stored by an earlier build, emptied slots, and no dead ends on removal.

The live case (2026-10-24): the 0c416d2 build stored a slot 3 -> slot 4 group
whose steps carry no `writes` and unprefixed generations. Slot 3 was emptied.
After the deploy nothing re-adopted the group, removing it turned slot 4 into a
conflict, and nothing could clear slot 3's step on an empty slot.
"""

from __future__ import annotations

import copy
import itertools
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from bambu_bridge.refill import (
    RUN,
    Declaration,
    RefillController,
    RefillStore,
    backups,
    field_changes,
)
from bambu_bridge.service.refill_reports import RefillReports

FIXTURE = Path(__file__).parent / "fixtures" / "refill" / "legacy-0c416d2-group.json"
UNTAGGED = "0000000000000000"


def decl(idx: str, color: str, low: int, high: int, kind: str = "PETG") -> dict[str, Any]:
    return {
        "tray_info_idx": idx,
        "tray_color": color,
        "nozzle_temp_min": low,
        "nozzle_temp_max": high,
        "tray_type": kind,
    }


class P1S:
    """A P1S as the bridge hears it: a push never names the AMS, the version reply does.

    `trays` maps a wire to its declaration (plus tag_uid); a wire not in it is empty.
    """

    def __init__(self, trays: dict[int, dict[str, Any]]) -> None:
        self.trays = copy.deepcopy(trays)
        self.writes: list[int] = []
        self.start()

    def start(self) -> None:
        """A new bridge run connecting: pushall first, then the version reply."""
        self.reports = RefillReports()
        self.push()
        self.reports.observe({"info": {"module": [{"name": "ams/0", "sn": "ams-a"}]}})
        self.push()

    def push(self) -> None:
        presence = sum(1 << w for w in self.trays)
        self.reports.observe(
            {
                "print": {
                    "gcode_state": "IDLE",
                    "home_flag": 1024,
                    "filam_bak": [],
                    "ams": {
                        "ams_exist_bits": "1",
                        "tray_exist_bits": f"{presence:x}",
                        "ams": [
                            {
                                "id": "0",
                                "tray": [
                                    {"id": str(w), "tag_uid": UNTAGGED, **t}
                                    for w, t in sorted(self.trays.items())
                                ],
                            }
                        ],
                    },
                }
            }
        )

    async def observe(self):
        return self.reports.snapshot(connected=True)

    async def send(self, wire: int, target: Declaration) -> None:
        slot = self.reports.snapshot(connected=True).slots[wire]
        self.reports.expect(wire, slot.source_generation, target)
        self.writes.append(wire)
        self.trays[wire] = {**self.trays[wire], **asdict(target)}
        self.push()

    def shows(self, wire: int) -> dict[str, Any] | None:
        tray = self.trays.get(wire)
        return None if tray is None else {k: tray[k] for k in decl("", "", 0, 0)}


def seed(directory: Path, body: dict[str, Any], printer: str = "p1") -> RefillStore:
    """Store `body` as the database held it, then open the store as a deploy's start does."""
    store = RefillStore(directory)
    with store.connect() as db:
        db.execute(
            "INSERT INTO refill_groups VALUES (?,?,?)",
            (printer, body["revision"], json.dumps(body, sort_keys=True)),
        )
    store.close()
    return RefillStore(directory)


async def remove(controller: RefillController, printer: P1S) -> None:
    """The app's Remove (DELETE …/refill/backup), as remove_backup runs it on an idle printer."""
    group = controller.store.get("p1")
    assert group is not None
    if group["state"] == "disabled":
        return
    report = await printer.observe()
    if group["enabled"]:
        group = controller.disable("p1", revision=group["revision"])
    controller.retry("p1", report, revision=group["revision"])
    await controller.drive("p1", printer.observe, printer.send)
    controller.release_unrestorable(
        "p1", await printer.observe(), note="Backup removed; kept the printer's own setting"
    )


async def worker(controller: RefillController, printer: P1S, passes: int = 3) -> None:
    """Fresh reports reaching note(), and the restoration worker's idle passes."""
    for _ in range(passes):
        controller.note("p1", await printer.observe())
        group = controller.store.get("p1")
        if group and not group["enabled"] and group["state"] == "restoration_pending":
            await controller.drive("p1", printer.observe, printer.send)


# The owner's AMS on 2026-10-24 after the workaround: slot 3 empty, slot 4
# written back by hand to its own spool's declaration.
LIVE_TRAYS = {
    0: decl("GFC99", "FFFFFFFF", 240, 300, "PC"),
    1: {**decl("GFG01", "C9A38180", 230, 260), "tag_uid": "5A3C9F1E00000000"},
    3: decl("GFG99", "1A1A1AFF", 220, 270),
}


def live_body() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())["body"]


async def test_the_captured_live_group_clears_on_the_first_fresh_report(tmp_path):
    store = seed(tmp_path, live_body())
    try:
        controller = RefillController(store)
        printer = P1S(LIVE_TRAYS)
        report = await printer.observe()
        assert report.empty == {2}
        # What the deployed build showed: stuck, with slot 3 in conflict.
        assert controller.status("p1", report)["state"] == "needs_attention"
        controller.note("p1", report)
        group = store.get("p1")
        assert group is not None and group["state"] == "disabled"
        assert [(s["wire"], s["state"]) for s in group["steps"]] == [
            (2, "released_empty"),
            (3, "released"),
        ]
        # Slot 3's step never changed anything: the old build's anchor.
        assert group["steps"][0]["writes"] is False and group["steps"][1]["writes"] is True
        status = controller.status("p1", await printer.observe())
        assert status["state"] == "disabled" and backups(status, report) == []
        assert printer.writes == [] and printer.shows(3) == LIVE_TRAYS[3]
        # The owner can set a new backup straight away.
        controller.plan("p1", await printer.observe(), [1, 3], "C9A38180", revision=14)
    finally:
        store.close()


async def test_the_captured_live_group_clears_even_without_word_on_slot_3(tmp_path):
    """The capture lists no slot 3, which a tray caught mid-update would also cause.

    The old build's anchor never changed slot 3, so it settles without evidence.
    """
    store = seed(tmp_path, live_body())
    try:
        controller = RefillController(store)
        printer = P1S(LIVE_TRAYS)
        controller.note("p1", printer.reports.snapshot(connected=False))
        group = store.get("p1")
        assert group is not None and group["state"] == "disabled"
        assert [s["state"] for s in group["steps"]] == ["restored", "released"]
        assert printer.writes == []
    finally:
        store.close()


async def test_the_live_group_as_deployed_restores_slot_4_instead_of_conflicting(tmp_path):
    """Before the workaround: slot 4 still owned and showing the alias, slot 3 empty."""
    body = live_body()
    member = body["steps"][1]
    for key in ("direction", "review_note"):
        member.pop(key)
    member["state"] = "owned"
    body.update(enabled=True, state="needs_attention")
    trays = {**LIVE_TRAYS, 3: dict(member["target"])}
    store = seed(tmp_path, body)
    try:
        controller = RefillController(store)
        printer = P1S(trays)
        controller.note("p1", await printer.observe())  # re-adopts slot 4
        await remove(controller, printer)
        group = store.get("p1")
        assert group is not None and group["state"] == "disabled"
        assert [s["state"] for s in group["steps"]] == ["released_empty", "restored"]
        assert printer.writes == [3] and printer.shows(3) == member["before"]
    finally:
        store.close()


def test_only_a_fresh_presence_report_says_a_slot_is_empty(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("bambu_bridge.service.refill_reports.time.monotonic", lambda: now[0])
    printer = P1S({0: decl("GFG99", "FFFFFFFF", 230, 260)})
    assert printer.reports.snapshot(connected=True).empty == {1, 2, 3}
    assert printer.reports.snapshot(connected=False).empty is None
    now[0] = 131
    assert printer.reports.snapshot(connected=True).empty is None
    printer.reports.clear()  # a lost session
    assert printer.reports.snapshot(connected=True).empty is None


async def test_an_enabled_group_is_left_alone_when_its_primary_runs_out(tmp_path):
    printer = P1S({0: decl("GFG99", "FFFFFFFF", 230, 260), 1: decl("GFG01", "8B5A2BFF", 220, 250)})
    store = RefillStore(tmp_path)
    try:
        controller = RefillController(store)
        controller.plan("p1", await printer.observe(), [0, 1], "FFFFFFFF", revision=0)
        await controller.drive("p1", printer.observe, printer.send)
        del printer.trays[0]  # the backup takes over: that is its job
        printer.push()
        before = store.get("p1")
        controller.note("p1", await printer.observe())
        assert store.get("p1") == before
    finally:
        store.close()


# --- No dead ends -----------------------------------------------------------

ANCHOR = decl("GFG99", "FFFFFFFF", 230, 260)
BEFORE = decl("GFG01", "8B5A2BFF", 220, 250)
OTHER = decl("GFG02", "00FF00FF", 210, 240)
TARGET = ANCHOR  # the member is declared as the anchor's filament

# What a slot can show for a step in each state while it still holds the
# step's spool (or, for "other", once the printer's own setting replaced it).
# A state, or state:direction for a step a send left in doubt.
SHOWN = {
    "pending": ["before"],
    "owned": ["target"],
    "uncertain:apply": ["before", "target", "other"],
    "uncertain:restore": ["before", "target", "other"],
    "conflict": ["before", "target", "other"],
    "restored": ["before"],
    "released": ["other"],
    "released_empty": [],
    "applying:apply": ["before", "target"],
    "restoring:restore": ["before", "target"],
}
# An enabled group's steps; a removed group's, except a send still applying.
ENABLED_STATES = {"pending", "owned", "uncertain:apply", "conflict", "applying:apply"}
ANCHOR_STATES = ["pending", "owned", "conflict", "restored"]


def step(wire: int, state: str, before: dict, generation: str, *, legacy: bool) -> dict:
    state, _, direction = state.partition(":")
    row = {
        "wire": wire,
        "source_generation": generation,
        "before": dict(before),
        "target": dict(TARGET),
        "state": state,
    }
    if direction:
        row["direction"] = direction
    if not legacy:
        changes = field_changes(before, TARGET)
        row.update(
            anchor=wire == 0, tagged=False, tag_key="", writes=bool(changes), changes=changes
        )
    return row


def cases():
    member = [(s, shown) for s, shows in SHOWN.items() for shown in [*shows, "empty"]]
    anchor = [(s, shown) for s in ANCHOR_STATES for shown in ("same", "empty")]
    for kind, enabled, (a_state, a_shown), (m_state, m_shown) in itertools.product(
        ("same_run", "earlier_run", "legacy"), (True, False), anchor, member
    ):
        if enabled and not {a_state, m_state} <= ENABLED_STATES:
            continue
        if not enabled and m_state == "applying:apply":
            continue
        # Interrupted sends exist at rest only in a database a restart opens.
        if kind == "same_run" and m_state in {"applying:apply", "restoring:restore"}:
            continue
        if m_state == "released_empty" and m_shown != "empty":
            continue
        yield kind, enabled, a_state, a_shown, m_state, m_shown


async def test_every_group_reaches_disabled_with_no_alias_left_on_a_present_slot(tmp_path):
    """From any stored state: fresh reports, the app's Remove and the worker end it.

    Over steps from this run, an earlier run and the 0c416d2 build, each slot
    holding the step's spool or emptied.
    """
    failures = []
    for n, (kind, enabled, a_state, a_shown, m_state, m_shown) in enumerate(cases()):
        trays = {}
        if a_shown != "empty":
            trays[0] = dict(ANCHOR)
        if m_shown != "empty":
            trays[1] = dict({"before": BEFORE, "target": TARGET, "other": OTHER}[m_shown])
        printer = P1S(trays)
        slots = printer.reports.snapshot(connected=True).slots
        current = {w: s.source_generation for w, s in slots.items()}

        def generation(
            wire: int, still_ours: bool, kind: str = kind, current: dict = current
        ) -> str:
            if kind == "legacy":
                return f"{wire:032x}"
            if kind == "earlier_run":
                return f"0123456789ab-{wire:032x}"
            # This run: the slot's generation while it holds the step's spool.
            return current[wire] if still_ours and wire in current else f"{RUN}-{wire:032x}"

        legacy = kind == "legacy"
        ours = m_shown in {"before", "target"}
        steps = [
            step(0, a_state, ANCHOR, generation(0, True), legacy=legacy),
            step(1, m_state, BEFORE, generation(1, ours), legacy=legacy),
        ]
        doubt = {s["state"] for s in steps} & {"uncertain", "conflict"}
        state = "needs_attention" if doubt else ("pending" if enabled else "restoration_pending")
        body = {"enabled": enabled, "state": state, "steps": steps, "revision": 5}
        store = seed(tmp_path / str(n), body)
        case = (kind, enabled, a_state, a_shown, m_state, m_shown)
        try:
            controller = RefillController(store)
            await worker(controller, printer, passes=1)
            await remove(controller, printer)
            await worker(controller, printer)
            group = store.get("p1")
            assert group is not None
            status = controller.status("p1", await printer.observe())
            if (
                group["state"] != "disabled"
                or status["state"] != "disabled"
                or printer.shows(1) == TARGET
            ):
                states = [s["state"] for s in group["steps"]]
                failures.append((*case, group["state"], states, printer.shows(1)))
        except Exception as exc:  # one broken case must not hide the others
            failures.append((*case, repr(exc)))
        finally:
            store.close()
    assert n > 300
    assert not failures, "\n".join(map(str, failures))
