"""Bounded, private, atomic diagnostic history. No raw MQTT or credentials."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from bambu_bridge.service.auto_refill import AutoRefillHistory

log = structlog.get_logger(__name__)
ACTIVE = {"RUNNING", "PAUSE"}
IDENTITY_FIELDS = ("subtask_id", "task_id", "gcode_file", "subtask_name")


def identity(raw: dict[str, Any]) -> dict[str, str]:
    return {key: str(raw[key]) for key in IDENTITY_FIELDS if raw.get(key) not in (None, "", "0", 0)}


def number(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class DiagnosticJournal:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.saved: dict[str, Any] = {}
        self.faults: list[dict[str, Any]] = []
        self.archived_refills: list[dict[str, Any]] = []
        self._restored = False
        self._last_write = 0.0
        self.durable = path is not None
        if path is not None:
            try:
                if path.stat().st_size > 1024 * 1024:
                    raise ValueError("History file too large")
                data = json.loads(path.read_text())
                if data.get("version") == 1:
                    for row in data.get("faults", []):
                        if not isinstance(row, dict) or not {
                            "id",
                            "first_seen",
                            "last_seen",
                            "active",
                            "count",
                            "channel",
                            "code",
                            "text",
                            "severity",
                        }.issubset(row):
                            raise ValueError("Invalid fault history")
                    for row in data.get("refills", []):
                        if (
                            not isinstance(row, dict)
                            or row.get("status") not in {"completed", "switching", "interrupted"}
                            or row.get("from_slot") not in range(1, 5)
                        ):
                            raise ValueError("Invalid refill history")
                    for key, value in data.get("seen", {}).items():
                        if int(key) not in range(1, 5) or value not in {"completed", "switching"}:
                            raise ValueError("Invalid refill baseline")
                    self.saved = data
                    self.faults = data.get("faults", [])[-200:]
                    self.archived_refills = data.get("archived_refills", [])[-20:]
            except FileNotFoundError:
                pass
            except (OSError, ValueError, AttributeError, TypeError):
                self.saved = {}
                self.faults = []
                self.archived_refills = []
                log.warning("diagnostic_history.restore_failed")

    def restore(
        self,
        previous: dict[str, Any],
        current: dict[str, Any],
        history: AutoRefillHistory,
        started_at: str | None,
    ) -> dict[str, Any]:
        if self._restored or "gcode_state" not in current:
            return previous
        if (
            self.saved
            and current.get("gcode_state") in ACTIVE
            and current.get("msg") != 0
            and not any(
                value != self.saved.get("identity", {}).get(key)
                for key, value in identity(current).items()
            )
            and (
                not identity(current)
                or "layer_num" not in current
                or any(key not in current for key in self.saved.get("identity", {}))
            )
        ):
            return previous
        self._restored = True
        saved = self.saved
        known_identity = identity(current)
        matching_start = (
            not (started_at and saved.get("started_at")) or started_at == saved["started_at"]
        )
        same_print = (
            known_identity
            and known_identity == saved.get("identity")
            and matching_start
            and (
                saved.get("state") in ACTIVE
                and current.get("gcode_state") in ACTIVE
                or saved.get("state") == current.get("gcode_state")
                and current.get("gcode_state") in {"FINISH", "FAILED"}
            )
            and number(current.get("layer_num")) >= number(saved.get("layer"))
        )
        if same_print:
            history.transitions[:] = saved.get("refills", [])
            history._seen = {int(key): value for key, value in saved.get("seen", {}).items()}
            return {
                **known_identity,
                "gcode_state": saved["state"],
                "ams": {"tray_now": saved.get("tray_now")},
            }
        return previous

    def update(
        self,
        raw: dict[str, Any],
        snapshot: dict[str, Any],
        history: AutoRefillHistory,
        started_at: str | None,
        reported: dict[str, Any] | None = None,
    ) -> None:
        if self.saved and not self._restored:
            return
        now = datetime.now(UTC).isoformat()
        reported = raw if reported is None else reported
        channels = set()
        if "hms" in reported:
            channels.add("hms")
        if "print_error" in reported or "mc_print_error_code" in reported:
            channels.add("print_error")
        active: dict[str, dict[str, Any]] = {}
        for row in snapshot.get("hms", []):
            if "hms" in channels and row["severity"] != "info":
                active["hms:" + row["hex"]] = row
        error = snapshot.get("print_error")
        if "print_error" in channels and error and error.get("severity") != "info":
            active["print_error:" + str(error.get("code"))] = error
        changed = False
        for key, row in active.items():
            existing = next((r for r in self.faults if r["id"] == key), None)
            if existing is None:
                existing = {"id": key, "first_seen": now, "count": 0, "active": False}
                self.faults.append(existing)
            if not existing["active"]:
                existing["count"] += 1
                changed = True
            existing.update(
                code=row.get("hex") or str(row.get("code")),
                text=row["text"],
                severity=row["severity"],
                last_seen=now,
                active=True,
                stale=bool(row.get("stale")),
                channel=key.split(":", 1)[0],
            )
        for row in self.faults:
            if row["channel"] in channels and row["active"] and row["id"] not in active:
                row.update(active=False, cleared_at=now)
                changed = True
        if len(self.faults) > 200:
            self.faults = sorted(self.faults, key=lambda row: (row["active"], row["last_seen"]))[
                -200:
            ]
        current_identity = identity(raw)
        old = self.saved
        if old.get("refills") and (
            old.get("identity") != current_identity or not history.transitions
        ):
            self.archived_refills.append({"print": old.get("identity"), "refills": old["refills"]})
            self.archived_refills = self.archived_refills[-20:]
        refills = [dict(row) for row in history.transitions]
        changed = changed or old.get("refills") != refills
        self.saved = {
            "version": 1,
            "identity": current_identity,
            "started_at": started_at,
            "state": raw.get("gcode_state"),
            "layer": raw.get("layer_num"),
            "tray_now": (raw.get("ams") or {}).get("tray_now"),
            "refills": refills,
            "seen": dict(history._seen),
            "faults": self.faults,
            "archived_refills": self.archived_refills,
        }
        if self.path is not None and (changed or time.monotonic() - self._last_write >= 10):
            try:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".tmp")
                with temporary.open("w") as stream:
                    os.chmod(temporary, 0o600)
                    json.dump(self.saved, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(self.path)
                self._last_write = time.monotonic()
                self.durable = True
            except OSError:
                self.durable = False
                log.warning("diagnostic_history.write_failed")

    def view(self) -> dict[str, Any]:
        return {
            "durable": self.durable,
            "faults": sorted(
                (dict(row) for row in self.faults), key=lambda row: row["last_seen"], reverse=True
            ),
            "archived_refills": list(reversed(self.archived_refills)),
        }
