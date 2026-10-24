"""Per-print AMS handoffs from the firmware's explicit purge/success notices.

Source: BambuStudio resources/hms/hms_en_094.json (0700/2x00/0003/0001–2).
A tray change alone is never an auto-refill: multicolour jobs also change trays.
One exception, kept conservative: while an active refill group is known (`group`),
the printer is printing (stg_cur 0) and tray_now moves from the group's primary to
its backup and stays there while layers keep advancing and the printer reports
auto-refill on (home_flag bit 10), the switch is recorded as
inferred (`source: "tray_now"`, `review: True`) in case the firmware notice never
came. The usage delivery flags such a print for the owner.
History lives with the printer service, independently of mobile clients.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

_CODE = re.compile(r"^0700_2([0-3])00_0003_000([12])$")


def notices(raw: dict[str, Any]) -> dict[int, str]:
    result: dict[int, str] = {}
    for item in raw.get("hms", []) if isinstance(raw.get("hms"), list) else []:
        if not isinstance(item, dict):
            continue
        try:
            attr, code = int(item["attr"]), int(item["code"])
        except (KeyError, ValueError, TypeError):
            continue
        key = f"{attr >> 16:04x}_{attr & 65535:04x}_{code >> 16:04x}_{code & 65535:04x}"
        match = _CODE.fullmatch(key)
        if match:
            source = int(match[1]) + 1
            status = "completed" if match[2] == "2" else "switching"
            if result.get(source) != "completed":
                result[source] = status
    return result


def slot(raw: dict[str, Any]) -> int | None:
    try:
        tray = (raw.get("ams") or {}).get("tray_now")
        if tray is None:
            return None
        value = int(tray)
        return value + 1 if 0 <= value < 4 else None
    except (ValueError, TypeError):
        return None


# Fresh reports in a row on the backup, with layers advancing, before a switch is inferred.
INFER_FRAMES = 3
# stg_cur while printing (VERIFIED live on the P1S 2026-10-24: layer 2 RUNNING reported
# stg_cur 0, mc_print_stage "2"); load, unload, purge and pause use other stages.
PRINTING_STAGE = 0


def _stage(raw: dict[str, Any]) -> int | None:
    try:
        return int(raw.get("stg_cur"))
    except (ValueError, TypeError):
        return None


AUTO_REFILL_BIT = 1 << 10  # home_flag: the printer's AMS auto-refill setting (see RefillReports)


def _auto_refill_on(raw: dict[str, Any]) -> bool:
    """True only when the printer's own frames say auto-refill is on; unknown is not on."""
    flag = raw.get("home_flag")
    return type(flag) is int and flag >= 0 and bool(flag & AUTO_REFILL_BIT)


def layer(raw: dict[str, Any]) -> int | None:
    try:
        value = int(raw.get("layer_num"))
    except (ValueError, TypeError):
        return None
    return value if value > 0 else None


class AutoRefillHistory:
    def __init__(self) -> None:
        self.transitions: list[dict[str, Any]] = []
        self._seen: dict[int, str] = {}
        # (primary, backup) slots, 1-based, of the active refill group; set by whoever
        # reads the group (the refill status route). None disables the tray_now fallback.
        self.group: tuple[int, int] | None = None
        self._on: int | None = None  # last slot tray_now named
        self._candidate: dict[str, int] | None = None

    def update(self, previous: dict[str, Any], current: dict[str, Any]) -> None:
        state, old_state = current.get("gcode_state"), previous.get("gcode_state")
        new_run = previous and (
            state == "RUNNING"
            and old_state in {"IDLE", "FINISH", "FAILED", "PREPARE"}
            or current.get("subtask_name") != previous.get("subtask_name")
            and current.get("subtask_name") is not None
            and previous.get("subtask_name") is not None
        )
        active = notices(current)
        if new_run:
            self.transitions.clear()
            self._candidate = None
            self._on = None
            # Codes carried from the previous print are historical, not new handoffs.
            self._seen = {
                source: status
                for source, status in active.items()
                if notices(previous).get(source) == status
            }
        if state != "RUNNING":
            self._candidate = None
        if state not in {"RUNNING", "PAUSE"}:
            for row in self.transitions:
                if row["status"] == "switching":
                    row["status"] = "interrupted"
                    row["interrupted_at"] = datetime.now(UTC).isoformat()
            return
        for source, status in active.items():
            if self._seen.get(source) == status:
                # The success HMS and tray telemetry may arrive in separate reports.
                latest = next(
                    (row for row in reversed(self.transitions) if row["from_slot"] == source), None
                )
                destination = slot(current)
                if (
                    latest is not None
                    and latest["status"] == "completed"
                    and latest["to_slot"] is None
                    and slot(previous) == source
                    and destination is not None
                    and destination != source
                ):
                    latest["to_slot"] = destination
                continue
            pending = next(
                (
                    row
                    for row in reversed(self.transitions)
                    if row["from_slot"] == source and row["status"] == "switching"
                ),
                None,
            )
            # The notice came after all: it replaces the inferred row, never adds a second.
            inferred = next(
                (
                    row
                    for row in reversed(self.transitions)
                    if row["from_slot"] == source and row.get("source") == "tray_now"
                ),
                None,
            )
            if inferred is not None:
                self.transitions.remove(inferred)
            destination = slot(current)
            # If seeding with several old successes, their order/destinations are unknown.
            known = (
                destination is not None
                and destination != source
                and (slot(previous) == source or len(active) == 1)
            )
            now = datetime.now(UTC).isoformat()
            row = {
                "from_slot": source,
                "to_slot": destination if status == "completed" and known else None,
                "status": status,
                "observed_at": now,
                "started_at": now if status == "switching" else None,
                "completed_at": now if status == "completed" else None,
                # Printing layer when the old spool ran out; splits the print's usage.
                "layer": layer(current),
            }
            if inferred is not None and inferred["layer"] is not None:
                # The tray moved after the runout, so its layer is the later one.
                row["layer"] = min(row["layer"] or inferred["layer"], inferred["layer"])
            if pending is not None and status == "completed":
                row["started_at"] = pending.get("started_at")
                row["layer"] = pending.get("layer") or row["layer"]
                pending.update(row)
            else:
                self.transitions.append(row)
        self._seen = active
        self._infer(current)

    def _infer(self, current: dict[str, Any]) -> None:
        tray, group = slot(current), self.group
        # The slot that was feeding the last time the printer was plainly printing; a
        # change made during a load/unload/purge stage is judged when printing resumes.
        on = self._on
        if tray is not None and _stage(current) == PRINTING_STAGE:
            self._on = tray
        if group is None or current.get("gcode_state") != "RUNNING":
            self._candidate = None
            return
        primary, backup = group
        reached = layer(current)
        if (
            tray != backup
            or reached is None
            or _stage(current) != PRINTING_STAGE
            or not _auto_refill_on(current)  # with it off, a tray move is the owner's doing
            or any(row["from_slot"] == primary for row in self.transitions)
        ):
            # Off the backup, in a load/purge stage, or the notice already spoke.
            self._candidate = None
            return
        if self._candidate is None:
            if on == primary:
                self._candidate = {"layer": reached, "frames": 1}
            return
        self._candidate["frames"] += 1
        if self._candidate["frames"] >= INFER_FRAMES and reached > self._candidate["layer"]:
            now = datetime.now(UTC).isoformat()
            self.transitions.append(
                {
                    "from_slot": primary,
                    "to_slot": backup,
                    "status": "completed",
                    "observed_at": now,
                    "started_at": None,
                    "completed_at": now,
                    "layer": self._candidate["layer"],
                    "source": "tray_now",
                    "review": True,
                }
            )
            self._candidate = None
