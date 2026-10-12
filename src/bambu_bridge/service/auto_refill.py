"""Per-print AMS handoffs from the firmware's explicit purge/success notices.

Source: BambuStudio resources/hms/hms_en_094.json (0700/2x00/0003/0001–2).
A tray change alone is never an auto-refill: multicolour jobs also change trays.
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


class AutoRefillHistory:
    def __init__(self) -> None:
        self.transitions: list[dict[str, Any]] = []
        self._seen: dict[int, str] = {}

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
            # Codes carried from the previous print are historical, not new handoffs.
            self._seen = {
                source: status
                for source, status in active.items()
                if notices(previous).get(source) == status
            }
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
            }
            if pending is not None and status == "completed":
                row["started_at"] = pending.get("started_at")
                pending.update(row)
            else:
                self.transitions.append(row)
        self._seen = active
