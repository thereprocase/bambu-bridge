"""Durable color-alias ownership. No production activation or implicit refill toggle.

The adapter must supply fresh physical reports and a source-generation token
that changes on removal/replacement, but not on our owned color write. Client
projection and Spoolman's actual colors are never inputs to this controller.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import fcntl
import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bambu_bridge.protocol import commands

# Source generations carry this run's id (RefillReports), so a group stored by
# an earlier bridge run can be told from one whose slot changed during this run.
RUN = uuid.uuid4().hex[:12]


class RefillConflict(ValueError):
    """Rejected before any physical send; the reviewed group is missing or stale."""


class NotSent(ValueError):
    """A sender refused before anything reached the printer; the step can run again."""


@dataclass(frozen=True)
class Declaration:
    tray_info_idx: str
    tray_color: str
    nozzle_temp_min: int
    nozzle_temp_max: int
    tray_type: str

    def validate(self) -> None:
        if (
            type(self.nozzle_temp_min) is not int
            or type(self.nozzle_temp_max) is not int
            or self.nozzle_temp_min <= 0
            or not isinstance(self.tray_info_idx, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{0,64}", self.tray_info_idx)
            or not isinstance(self.tray_color, str)
            or not isinstance(self.tray_type, str)
        ):
            raise ValueError("invalid physical filament declaration")
        commands.ams_filament_setting(ams_id=0, tray_id=0, **asdict(self))


@dataclass(frozen=True)
class SlotReport:
    declaration: Declaration
    source_generation: str
    receipt: int
    reported_at: float
    editable: bool  # Only confirmed untagged slots are ever written.
    # A real RFID tag was read: the slot may anchor a group, never be written.
    # Neither editable nor tagged: the tag is blank or unreadable.
    tagged: bool = False
    tag_key: str = ""  # inventory_sources.spool_key of the tag; "" when untagged
    # The generation is the first this bridge run issued for the slot: nothing
    # was seen to change there since the run began (see RefillController.note).
    first_of_run: bool = False


@dataclass(frozen=True)
class RefillReport:
    connected: bool
    idle: bool
    slots: dict[int, SlotReport]
    auto_refill: bool | None
    backup_masks: tuple[int, ...] | None
    eligibility_reported_at: float | None
    # Wires whose RFID the AMS is reading now: their declaration may change.
    reading: frozenset[int] = frozenset()
    # Wires a fresh presence report shows without a spool; None when presence
    # is not freshly known. A wire missing from `slots` alone is no evidence.
    empty: frozenset[int] | None = None


class RefillStore:
    """One writer; revisioned intents and history survive each physical write."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = (directory / "refill.lock").open("a")
        # Told after every committed save, so a listener never sees an unsaved group.
        self.on_save: Callable[[str, dict[str, Any]], None] | None = None
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.path = directory / "refill.sqlite3"
            with self.connect() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS refill_groups (
                        printer TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS refill_events (
                        id INTEGER PRIMARY KEY, printer TEXT NOT NULL,
                        revision INTEGER NOT NULL, body TEXT NOT NULL, at REAL NOT NULL
                    );
                """)
                rows = db.execute("SELECT printer,body FROM refill_groups").fetchall()
            self.path.chmod(0o600)
            for printer, encoded in rows:
                group = json.loads(encoded)
                interrupted = False
                for step in group["steps"]:
                    if step["state"] in {"applying", "restoring"}:
                        step["state"] = "uncertain"
                        interrupted = True
                if interrupted:
                    group["state"] = "needs_attention"
                    self.save(printer, group, group["revision"])
        except BaseException:
            self.lock.close()
            raise

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def close(self) -> None:
        self.lock.close()

    def get(self, printer: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT body FROM refill_groups WHERE printer=?", (printer,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, printer: str, group: dict[str, Any], revision: int) -> dict[str, Any]:
        result = copy.deepcopy(group)
        result["revision"] = revision + 1
        encoded = json.dumps(result, sort_keys=True, allow_nan=False)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT revision FROM refill_groups WHERE printer=?", (printer,)
            ).fetchone()
            if (row[0] if row else 0) != revision:
                raise ValueError("refill configuration changed; refresh before editing")
            db.execute(
                "INSERT INTO refill_groups VALUES (?,?,?) ON CONFLICT(printer) DO UPDATE "
                "SET revision=excluded.revision,body=excluded.body",
                (printer, revision + 1, encoded),
            )
            db.execute(
                "INSERT INTO refill_events(printer,revision,body,at) VALUES (?,?,?,?)",
                (printer, revision + 1, encoded, time.time()),
            )
        if self.on_save is not None:
            self.on_save(printer, result)
        return result


# A disabled group's step in one of these needs nothing more; when every step is,
# the group is disabled. released_empty: the slot held no spool, so there was
# nothing to restore.
SETTLED = frozenset({"restored", "released", "released_empty"})


# The order a step's changes are listed in: what the filament is, then how it looks.
FIELD_ORDER = ("tray_type", "tray_info_idx", "nozzle_temp_min", "nozzle_temp_max", "tray_color")
FIELD_NAMES = {
    "tray_type": "material",
    "tray_info_idx": "product",
    "nozzle_temp_min": "temperature range",
    "nozzle_temp_max": "temperature range",
    "tray_color": "colour",
}


def field_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Every declaration field that differs, as {field, before, after}."""
    return [
        {"field": key, "before": before[key], "after": after[key]}
        for key in FIELD_ORDER
        if before[key] != after[key]
    ]


class RefillController:
    def __init__(self, store: RefillStore):
        self.store = store
        self.qualified = True
        self._lock = asyncio.Lock()
        # printer -> (group fingerprint, wall time) of the last fresh report that
        # showed the group fully active. In memory: after a restart the next
        # fresh report confirms it again.
        self._confirmed: dict[str, tuple[str, float]] = {}
        # status() also runs in worker threads (the status route).
        self._confirmed_lock = threading.Lock()
        # Print histories to tell which pair is lying; see attach().
        self._histories: dict[str, Any] = {}
        store.on_save = self._publish

    def forget_confirmation(self, printer: str) -> None:
        """A new, removed or retried group starts unconfirmed, even for the same slots."""
        with self._confirmed_lock:
            self._confirmed.pop(printer, None)

    @staticmethod
    def fallback_group(group: dict[str, Any] | None) -> tuple[int, int] | None:
        """(primary, backup) slots, 1-based, once every step's alias is written and owned.

        This is what the print history needs to tell a refill from a colour change when the
        firmware notice is missing. Only the stored group counts, so it holds across restarts
        and needs no app to ask; a group still being written, restored or in doubt is None.
        """
        if not group or not group.get("enabled") or len(group.get("steps") or []) < 2:
            return None
        if any(step["state"] != "owned" for step in group["steps"]):
            return None
        first, second = group["steps"][:2]
        return first["wire"] + 1, second["wire"] + 1

    def attach(self, printer: str, history: Any) -> None:
        """Keep `history.group` (an AutoRefillHistory) at this printer's stored group.

        Applied at once from the store, so a restarted bridge needs no plan or poll first.
        """
        self._histories[printer] = history
        history.group = self.fallback_group(self.store.get(printer))

    def _publish(self, printer: str, group: dict[str, Any]) -> None:
        history = self._histories.get(printer)
        if history is not None:
            history.group = self.fallback_group(group)

    @staticmethod
    def _fresh(report: RefillReport, slot: SlotReport) -> bool:
        age = time.monotonic() - slot.reported_at
        return report.connected and 0 <= age <= 30 and slot.receipt > 0

    def plan(
        self, printer: str, report: RefillReport, wires: list[int], color: str, *, revision: int
    ) -> dict[str, Any]:
        if self._lock.locked():
            raise ValueError("wait for the current alias readback before editing")
        previous = self.store.get(printer)
        if previous and previous["state"] != "disabled":
            raise ValueError("restore or reconcile the current refill group before replacing it")
        saved = self.store.save(printer, self.preview(report, wires, color), revision)
        self.forget_confirmation(printer)
        return saved

    def preview(self, report: RefillReport, wires: list[int], color: str) -> dict[str, Any]:
        """Compute the exact physical declarations without storing or sending.

        One slot is the anchor and every other member is declared as its whole
        filament (product, temperatures and colour) so the printer groups them.
        An RFID slot may be a member only as the anchor: its tag declares the
        filament and it is never written, so the group takes the tag's colour.
        Otherwise the first slot anchors and `color` is the group's colour. Only
        chemistry must already match. Each step lists every field it changes,
        and restore puts each step's `before` back whole.
        """
        if (
            len(set(wires)) != len(wires)
            or not 2 <= len(wires) <= 4
            or any(type(w) is not int or w not in range(4) for w in wires)
        ):
            raise ValueError("select two to four distinct slots on the qualified AMS")
        slots = {}
        for wire in wires:
            slot = report.slots.get(wire)
            if slot is None or not self._fresh(report, slot) or not slot.source_generation:
                raise ValueError(
                    f"Slot {wire + 1} has not reported recently; try again in a moment"
                )
            if wire in report.reading:
                raise ValueError(
                    f"Slot {wire + 1} is still reading its tag; try again when it's done"
                )
            if not (slot.editable or slot.tagged):
                raise ValueError(
                    f"Slot {wire + 1}'s tag could not be read; "
                    "reload the spool or read its tag again"
                )
            slot.declaration.validate()
            slots[wire] = slot
        tagged = [wire for wire in wires if slots[wire].tagged]
        if len(tagged) > 1:
            raise ValueError(
                f"Slots {tagged[0] + 1} and {tagged[1] + 1} both hold RFID spools; a backup can "
                "follow one tag, and a tag's slot is never rewritten"
            )
        # What the P1S compares to group slots, from this repository's evidence
        # (P1S refill captures): colour VERIFIED, tray_type and
        # tray_info_idx LIKELY, temperature range and tag-with-untagged grouping
        # UNKNOWN. Declaring the whole filament covers all of them; until the
        # printer reports the mask the group stays pending.
        anchor_wire = tagged[0] if tagged else wires[0]
        anchor = asdict(slots[anchor_wire].declaration)
        if tagged and color.upper() != anchor["tray_color"].upper():
            raise ValueError(
                f"Slot {anchor_wire + 1} holds an RFID spool; the backup takes its tag's colour"
            )
        target = {**anchor, "tray_color": color.upper()} if not tagged else dict(anchor)
        Declaration(**target).validate()
        steps = []
        for wire in wires:
            before = asdict(slots[wire].declaration)
            if before["tray_type"].strip().upper() != anchor["tray_type"].strip().upper():
                raise ValueError(
                    f"Slot {wire + 1} is {before['tray_type'] or 'unset'} and slot "
                    f"{anchor_wire + 1} is {anchor['tray_type'] or 'unset'}; a backup needs "
                    "the same material. Load a matching spool or set the slot's material first."
                )
            changes = field_changes(before, target)
            steps.append(
                {
                    "wire": wire,
                    "source_generation": slots[wire].source_generation,
                    "before": before,
                    "target": dict(target),
                    "anchor": wire == anchor_wire,
                    "tagged": slots[wire].tagged,
                    "tag_key": slots[wire].tag_key,
                    "writes": bool(changes),
                    "changes": changes,
                    "state": "pending",
                }
            )
        return {"enabled": True, "state": "pending", "steps": steps}

    @staticmethod
    def _same_source(step: dict[str, Any], slot: SlotReport) -> bool:
        """Whether `slot` still holds the step's source.

        A tag anchor is never written, so it is the same while it shows the same
        tag; its generation also turns over on every tag re-read. Every other
        member is the same while its source generation stands.
        """
        if step.get("anchor") and step.get("tagged"):
            return bool(slot.tagged and step.get("tag_key") and slot.tag_key == step["tag_key"])
        return slot.editable and slot.source_generation == step["source_generation"]

    @staticmethod
    def _writes(step: dict[str, Any]) -> bool:
        """Whether the step changes its slot at all.

        Rows stored before `writes` was recorded (the 0c416d2 build) carry
        none; their before and target say it, the same way preview decides.
        """
        writes = step.get("writes")
        return step["before"] != step["target"] if writes is None else bool(writes)

    def _nothing_to_restore(self, step: dict[str, Any], report: RefillReport) -> str | None:
        """How a disabled group's step settles without a send, or None if it can't.

        A slot the printer reports empty holds no spool to put back, whatever
        the step's state. A step never sent (pending, an anchor, a slot that
        already matched) has nothing to undo and needs no slot evidence, so an
        emptied tag slot never blocks removing the backup.
        """
        if report.connected and report.empty is not None and step["wire"] in report.empty:
            return "released_empty"
        if step["state"] == "pending" or not self._writes(step):
            return "restored"
        return None

    @staticmethod
    def _restoring_state(group: dict[str, Any]) -> str:
        """A disabled group's stored state from its steps.

        Uncertain waits for the owner's Try again. A conflict is left for the
        next drive to re-read (a fresh same-source readback may settle it) and
        the drive moves it to needs_attention if it still stands.
        """
        states = {s["state"] for s in group["steps"]}
        if states <= SETTLED:
            return "disabled"
        if states & {"uncertain", "applying", "restoring"}:
            return "needs_attention"
        return "restoration_pending"

    def disable(self, printer: str, *, revision: int) -> dict[str, Any]:
        if self._lock.locked():
            raise ValueError("wait for the current alias readback before disabling")
        group = self.store.get(printer)
        if group is None:
            raise ValueError("refill group not found")
        group.update(enabled=False, state="restoration_pending")
        saved = self.store.save(printer, group, revision)
        self.forget_confirmation(printer)
        return saved

    def reconcile(
        self,
        printer: str,
        wire: int,
        report: RefillReport,
        *,
        revision: int,
        applied: bool,
        note: str,
        definitely_not_sent: bool = False,
    ) -> dict[str, Any]:
        """Settle an uncertain step from a fresh readback of its slot.

        A slot showing what was sent adopts the step. A slot still showing what
        was there before may be sent again only when the caller says so: the
        write is an absolute set, so a resend is harmless, but only the owner's
        explicit Try again (retry) decides it. Automatic paths never resend.
        """
        if self._lock.locked() or not note.strip() or len(note) > 1000:
            raise ValueError("a review note and completed readback are required")
        group = self.store.get(printer)
        if group is None:
            raise ValueError("refill group not found")
        step = next((s for s in group["steps"] if s["wire"] == wire), None)
        if step is None or step["state"] != "uncertain":
            raise ValueError("this slot has no uncertain alias write")
        slot = report.slots.get(wire)
        if (
            slot is None
            or not self._fresh(report, slot)
            or not slot.editable
            or slot.source_generation != step["source_generation"]
        ):
            raise ValueError("fresh original-source readback is required")
        restoring = step.get("direction") == "restore"
        desired = step["before"] if restoring else step["target"]
        previous = step["target"] if restoring else step["before"]
        if not applied and not definitely_not_sent:
            raise ValueError("an old declaration alone does not prove a write was not delivered")
        if asdict(slot.declaration) != (desired if applied else previous):
            raise ValueError("physical declaration does not match the reviewed outcome")
        step["state"] = (
            ("restored" if restoring else "owned")
            if applied
            else ("owned" if restoring else "pending")
        )
        step["review_note"] = note.strip()
        group["state"] = "pending" if group["enabled"] else "restoration_pending"
        return self.store.save(printer, group, revision)

    def retry(self, printer: str, report: RefillReport, *, revision: int) -> dict[str, Any]:
        """The owner's Try again: re-read each uncertain slot, then adopt or re-arm it.

        A step whose slot shows the target is adopted; one still showing the
        original is armed for one more send by the next drive; a slot showing
        anything else stays uncertain for the owner to see.
        """
        group = self.store.get(printer)
        if group is None or group["revision"] != revision:
            raise RefillConflict("refill configuration changed; review it before retrying")
        self.forget_confirmation(printer)
        for wire in [s["wire"] for s in group["steps"] if s["state"] == "uncertain"]:
            for applied in (True, False):
                with contextlib.suppress(ValueError):
                    group = self.reconcile(
                        printer,
                        wire,
                        report,
                        revision=group["revision"],
                        applied=applied,
                        definitely_not_sent=True,
                        note="Owner chose Try again",
                    )
                    break
        return group

    def release_external_change(
        self, printer: str, wire: int, report: RefillReport, *, revision: int, note: str
    ) -> dict[str, Any]:
        """Explicitly keep a later operator declaration; never restore over it.

        An uncertain step qualifies too once Try again could not settle it: its
        slot shows something else, and without release the group could never
        be disabled.
        """
        if self._lock.locked() or not note.strip() or len(note) > 1000:
            raise ValueError("a review note and completed readback are required")
        group = self.store.get(printer)
        if group is None or group["enabled"]:
            raise ValueError("disable the group before releasing an externally changed slot")
        step = next((s for s in group["steps"] if s["wire"] == wire), None)
        slot = report.slots.get(wire)
        if (
            step is None
            or step["state"] not in {"conflict", "uncertain"}
            or slot is None
            or not self._fresh(report, slot)
        ):
            raise ValueError("a fresh conflicted-slot readback is required")
        step["state"] = "released"
        step["review_note"] = note.strip()
        group["state"] = self._restoring_state(group)
        return self.store.save(printer, group, revision)

    def release_unrestorable(
        self, printer: str, report: RefillReport, *, note: str
    ) -> dict[str, Any] | None:
        """Removal's last step, after its drive: keep what can no longer be ours.

        A conflict left after the drive re-read it shows the printer's own
        setting, as does an uncertain step whose slot has since become another
        source. An uncertain one on the same source may still hold our alias,
        so it waits for Try again; a slot that isn't reporting stays for the
        app to show until it does.
        """
        group = self.store.get(printer)
        if group is None or group["enabled"]:
            return group
        for step in list(group["steps"]):
            slot = report.slots.get(step["wire"])
            if slot is None or not self._fresh(report, slot):
                continue
            if step["state"] == "conflict" or (
                step["state"] == "uncertain" and not self._same_source(step, slot)
            ):
                group = self.release_external_change(
                    printer, step["wire"], report, revision=group["revision"], note=note
                )
        return group

    def release_empty(
        self, printer: str, wire: int, report: RefillReport, *, revision: int, note: str
    ) -> dict[str, Any]:
        """The owner confirms a slot has no spool: its step has nothing to keep or restore.

        Refused only on contrary evidence, a fresh report of a spool there. With
        the printer silent the owner's word stands, so a stale group can always
        be cleared; the note records it.
        """
        if self._lock.locked() or not note.strip() or len(note) > 1000:
            raise ValueError("a review note is required")
        group = self.store.get(printer)
        if group is None:
            raise ValueError("refill group not found")
        number = wire + 1
        if group["enabled"]:
            raise ValueError(f"Remove the backup before clearing slot {number}")
        step = next((s for s in group["steps"] if s["wire"] == wire), None)
        if step is None or step["state"] in SETTLED:
            raise ValueError(f"Slot {number} has nothing left to clear")
        slot = report.slots.get(wire)
        loaded = (slot is not None and self._fresh(report, slot)) or (
            report.connected and report.empty is not None and wire not in report.empty
        )
        if loaded:
            raise ValueError(f"The printer reports a spool in slot {number}, so it isn't empty")
        step["state"] = "released_empty"
        step["review_note"] = note.strip()
        group["state"] = self._restoring_state(group)
        return self.store.save(printer, group, revision)

    def _activity(self, group: dict[str, Any], report: RefillReport) -> tuple[str, bool, bool, str]:
        """(state, stale, contradicted, fingerprint) for an enabled, settled group.

        Read-only. Active is current evidence only: a fresh report showing every
        step owned, the printer's backup mask for exactly these slots and Auto
        Refill on. stale: freshness (or a malformed mask or flag) is the only
        thing missing. contradicted: fresh evidence against it (another mask,
        Auto Refill off, a slot changed).
        """
        fingerprint = json.dumps(
            [
                (s["wire"], s.get("tag_key") or s["source_generation"], s["target"])
                for s in group["steps"]
            ],
            sort_keys=True,
        )
        if any(s["state"] != "owned" for s in group["steps"]):
            return "pending", False, True, fingerprint
        silent = contradicted = False
        for step in group["steps"]:
            slot = report.slots.get(step["wire"])
            if slot is None or not self._fresh(report, slot):
                silent = True
            elif not (self._same_source(step, slot) and asdict(slot.declaration) == step["target"]):
                contradicted = True
        # A mask or flag the printer sent malformed (None) is no evidence either way.
        if (
            report.connected
            and report.eligibility_reported_at is not None
            and 0 <= time.monotonic() - report.eligibility_reported_at <= 30
            and report.backup_masks is not None
            and report.auto_refill is not None
        ):
            mask = sum(1 << s["wire"] for s in group["steps"])
            eligible = any(type(bits) is int and bits == mask for bits in report.backup_masks)
            contradicted = contradicted or not eligible or report.auto_refill is False
        else:
            silent = True
        if contradicted:
            return "pending", False, True, fingerprint
        return (
            ("pending", True, False, fingerprint)
            if silent
            else (
                "active",
                False,
                False,
                fingerprint,
            )
        )

    def _readopt(self, group: dict[str, Any], report: RefillReport) -> bool:
        """Carry a group stored by an earlier bridge run over to this run's generations.

        A restart retires every source generation. A step whose generation is
        from another run is re-adopted when its slot's current generation is
        the first this run issued there (nothing was seen to change since) and
        the slot shows exactly what the step left it as: its target once owned,
        its before while pending, either while uncertain or in conflict. That
        is the evidence an untagged spool's assignment stands on. Tag
        anchors are judged by their tag and need nothing here.

        Rows from before run-prefixed generations (the 0c416d2 build) carry no
        `writes`; it is filled in from before and target, as preview decides
        it. Mutates `group`; True when anything changed.
        """
        changed = False
        for step in group["steps"]:
            if "writes" not in step:
                step["writes"] = self._writes(step)
                step["changes"] = field_changes(step["before"], step["target"])
                changed = True
            if str(step["source_generation"]).startswith(f"{RUN}-"):
                continue
            if step.get("anchor") and step.get("tagged"):
                continue
            slot = report.slots.get(step["wire"])
            if (
                slot is None
                or not slot.first_of_run
                or not slot.editable
                or not self._fresh(report, slot)
            ):
                continue
            shown = asdict(slot.declaration)
            wanted = {
                "owned": [step["target"]],
                "pending": [step["before"]],
                "uncertain": [step["before"], step["target"]],
                # A conflict records what an earlier readback showed. A slot
                # unchanged since this run began that shows exactly the alias
                # or the original is ours again, so removal can restore it
                # instead of releasing it with the alias left on.
                "conflict": [step["before"], step["target"]],
            }.get(step["state"], [])
            if shown in wanted:
                step["source_generation"] = slot.source_generation
                changed = True
        return changed

    def _settle(self, group: dict[str, Any], report: RefillReport) -> bool:
        """Settle every step of a disabled group that needs no send; True if any did."""
        if group["enabled"]:
            return False
        changed = False
        for step in group["steps"]:
            if step["state"] in SETTLED or step["state"] in {"applying", "restoring"}:
                continue
            if settled := self._nothing_to_restore(step, report):
                step["state"] = settled
                changed = True
        return changed

    def note(self, printer: str, report: RefillReport) -> None:
        """Take in a fresh report: re-adopt a stored group, settle what a removed
        backup no longer needs, then remember or clear its confirmation. Called
        for every report the printer sends.

        Settling here, not only in a drive, is what clears a disabled group left
        in needs_attention by a slot that has since gone empty: the restoration
        worker drives only restoration_pending groups.
        """
        if self._lock.locked():
            return  # a drive owns the group's revisions until it finishes
        group = self.store.get(printer)
        if group is None:
            return
        changed = self._readopt(group, report)
        if not group["enabled"] and (self._settle(group, report) or changed):
            group["state"] = self._restoring_state(group)
            changed = True
        if changed:
            group = self.store.save(printer, group, group["revision"])
        self._remember(printer, group, report)

    def _remember(self, printer: str, group: dict[str, Any], report: RefillReport) -> None:
        if not group["enabled"] or not self.qualified:
            return
        state, _stale, contradicted, fingerprint = self._activity(group, report)
        with self._confirmed_lock:
            confirmed = self._confirmed.get(printer)
            if contradicted or (confirmed is not None and confirmed[0] != fingerprint):
                self._confirmed.pop(printer, None)
            if state == "active":
                self._confirmed[printer] = (fingerprint, time.time())

    def status(self, printer: str, report: RefillReport) -> dict[str, Any]:
        """Never expose a persisted Active state as current physical evidence.

        Read-only. state stays strict: active only on a fresh report. When
        freshness is the only thing missing, the group is pending with stale
        true, and confirmed_at / last_confirmed_age_seconds say when a fresh
        report last showed this incarnation of the group active (note()); the
        app shows that as "last confirmed N ago". A new, removed or retried
        group, or fresh contrary evidence, has none.
        """
        stale = contradicted = False
        fingerprint = None
        group = self.store.get(printer)
        if group is None:
            group = {"enabled": False, "state": "disabled", "steps": [], "revision": 0}
        elif not group["enabled"] and all(s["state"] in SETTLED for s in group["steps"]):
            group["state"] = "disabled"
        elif not self.qualified:
            group["state"] = "unsupported"
        elif any(
            s["state"] in {"uncertain", "conflict", "applying", "restoring"} for s in group["steps"]
        ):
            group["state"] = "needs_attention"
        elif not group["enabled"]:
            group["state"] = "restoration_pending"
        else:
            group["state"], stale, contradicted, fingerprint = self._activity(group, report)
        with self._confirmed_lock:
            confirmed = self._confirmed.get(printer)
        if (
            confirmed is None
            or contradicted
            or fingerprint is None
            or confirmed[0] != fingerprint
            or group["state"] not in {"active", "pending"}
        ):
            confirmed = None
        return {
            **group,
            "stale": stale,
            "confirmed_at": confirmed[1] if confirmed else None,
            "last_confirmed_age_seconds": max(0.0, time.time() - confirmed[1])
            if confirmed
            else None,
            "writes_qualified": self.qualified,
            "printer_auto_refill": report.auto_refill,
            "reported_backup_masks": report.backup_masks,
        }

    async def drive(
        self,
        printer: str,
        observe: Callable[[], Awaitable[RefillReport]],
        send: Callable[[int, Declaration], Awaitable[None]],
        *,
        revision: int | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            group = self.store.get(printer)
            if group is None:
                raise RefillConflict("refill group not found")
            if revision is not None and group["revision"] != revision:
                raise RefillConflict("refill configuration changed; review it before executing")
            if not self.qualified:
                group["state"] = "unsupported"
                return self.store.save(printer, group, group["revision"])
            for index in range(len(group["steps"])):
                group = self.store.get(printer)
                assert group is not None
                step = group["steps"][index]
                applying = group["enabled"]
                if not applying and step["state"] in SETTLED:
                    continue
                if not applying and (settled := self._nothing_to_restore(step, await observe())):
                    step["state"] = settled
                    group = self.store.save(printer, group, group["revision"])
                    continue
                # A restore re-reads a conflicted slot below: a fresh same-source
                # readback of the alias or the original settles it.
                if step["state"] in {"uncertain", "applying", "restoring"} or (
                    applying and step["state"] == "conflict"
                ):
                    if step["state"] in {"applying", "restoring"}:
                        step["state"] = "uncertain"
                    group["state"] = "needs_attention"
                    group = self.store.save(printer, group, group["revision"])
                    if applying:
                        return group
                    continue
                report = await observe()
                slot = report.slots.get(step["wire"])
                if not report.idle and not applying:
                    group["state"] = "restoration_pending"
                    return self.store.save(printer, group, group["revision"])
                if (
                    slot is None
                    or not self._fresh(report, slot)
                    or not self._same_source(step, slot)
                ):
                    step["state"] = "conflict"
                    group["state"] = "needs_attention"
                    group = self.store.save(printer, group, group["revision"])
                    # One slot's conflict never holds back restoring the others.
                    if applying:
                        return group
                    continue
                current = asdict(slot.declaration)
                expected = (
                    step["before"] if applying and step["state"] == "pending" else step["target"]
                )
                desired = step["target"] if applying else step["before"]
                if current == desired and (
                    not applying or step["state"] == "owned" or expected == desired
                ):
                    step["state"] = "owned" if applying else "restored"
                    group = self.store.save(printer, group, group["revision"])
                    continue
                # A tag's slot is never written, whatever it shows.
                if current != expected or not slot.editable:
                    step["state"] = "conflict"
                    group["state"] = "needs_attention"
                    group = self.store.save(printer, group, group["revision"])
                    if applying:
                        return group
                    continue
                if not report.idle:
                    group["state"] = "pending"
                    return self.store.save(printer, group, group["revision"])
                step["direction"] = "apply" if applying else "restore"
                step["state"] = "applying" if applying else "restoring"
                group = self.store.save(printer, group, group["revision"])
                step = group["steps"][index]
                try:
                    await send(step["wire"], Declaration(**desired))
                    after = await observe()
                    observed = after.slots.get(step["wire"])
                    confirmed = (
                        observed is not None
                        and self._fresh(after, observed)
                        and (
                            observed.receipt > slot.receipt
                            and observed.source_generation == step["source_generation"]
                            and asdict(observed.declaration) == desired
                        )
                    )
                except NotSent:
                    step["state"] = "pending" if applying else "owned"
                    group["state"] = "pending" if applying else "restoration_pending"
                    self.store.save(printer, group, group["revision"])
                    raise
                except BaseException:
                    step["state"] = "uncertain"
                    group["state"] = "needs_attention"
                    self.store.save(printer, group, group["revision"])
                    raise
                step["state"] = ("owned" if applying else "restored") if confirmed else "uncertain"
                group["state"] = "pending" if confirmed else "needs_attention"
                group = self.store.save(printer, group, group["revision"])
                if not confirmed:
                    return group
            report = await observe()
            group = self.store.get(printer)
            assert group is not None
            group["state"] = self.status(printer, report)["state"]
            self._remember(printer, group, report)  # note() skips reports during a drive
            return self.store.save(printer, group, group["revision"])


# A group as the owner sees it: "when slot A runs out, continue from slot B".
BACKUP_STATES = {
    "active": "active",
    "pending": "pending",
    "needs_attention": "needs_attention",
    "restoration_pending": "restoring",
}


def backups(status: dict[str, Any], report: RefillReport) -> list[dict[str, Any]]:
    """The printer's backup chain from its group status, with a plain message.

    One group per printer, so at most one chain: its first slot and the next.
    """
    state = BACKUP_STATES.get(status["state"])
    steps = status["steps"]
    if state is None or len(steps) < 2:
        return []
    message = None
    if state == "needs_attention":
        step = next(
            s for s in steps if s["state"] in {"uncertain", "conflict", "applying", "restoring"}
        )
        slot, number = report.slots.get(step["wire"]), step["wire"] + 1
        if slot is None and report.empty is not None and step["wire"] in report.empty:
            message = f"Slot {number} is empty"
        elif slot is None:
            message = f"The printer does not report slot {number}"
        elif asdict(slot.declaration) not in (step["before"], step["target"]):
            changed = field_changes(step["target"], asdict(slot.declaration))
            what = FIELD_NAMES[changed[0]["field"]] if changed else "setting"
            message = f"The printer reports a different {what} on slot {number}"
        elif slot.source_generation != step["source_generation"]:
            message = f"The spool in slot {number} was changed on the printer"
        else:
            message = f"The printer has not confirmed slot {number} yet"
    elif state == "pending" and report.auto_refill is False:
        message = "Turn on AMS auto-refill on the printer"
    return [
        {
            "from_wire": steps[0]["wire"],
            "to_wire": steps[1]["wire"],
            "state": state,
            "message": message,
            # The last time the printer confirmed this backup, and whether only
            # a recent report is missing (an idle printer goes quiet).
            "stale": bool(status.get("stale")),
            "confirmed_at": status.get("confirmed_at"),
            "last_confirmed_age_seconds": status.get("last_confirmed_age_seconds"),
        }
    ]
