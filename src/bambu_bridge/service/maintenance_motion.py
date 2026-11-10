"""Maintenance command targets, never reported/verified physical positions.

A relative maintenance action is translated to another bounded absolute target.
This avoids cumulative overrun even when delivery/completion is unverified.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any, Literal

Axis = Literal["X", "Y", "Z"]
LIMITS = {"X": (0.0, 256.0), "Y": (0.0, 256.0), "Z": (0.0, 250.0)}
STEPS = frozenset({1, 10, 50})
_MOTION = re.compile(
    r"(?im)^\s*(?:N\d+\s+)?(?:G(?:0?[0123]|28|29|38\d*|92)|M(?:17|18|84))(?=[\sXYZEF;]|$)"
)


class MaintenanceMotion:
    def __init__(self) -> None:
        self.targets: dict[str, float | None] = dict.fromkeys(LIMITS)
        self.reason = "No maintenance target requested"
        self.epoch = 0
        self._own_sequences: deque[str] = deque(maxlen=128)

    def snapshot(self) -> dict[str, Any]:
        return {
            "last_requested": dict(self.targets),
            "position_verified": False,
            "reason": self.reason,
        }

    def reset(self, reason: str) -> None:
        self.targets = dict.fromkeys(LIMITS)
        self.reason = reason
        self.epoch += 1

    def register(self, envelope: dict[str, Any]) -> None:
        self._own_sequences.append(str(envelope["print"]["sequence_id"]))

    def requested(self, targets: dict[str, float]) -> None:
        self.targets.update(targets)
        self.reason = "Last requested target; physical completion is unverified"

    def step_target(self, axis: str, direction: int, step_mm: int) -> float:
        if axis not in LIMITS:
            raise ValueError("Maintenance axis must be X, Y or Z")
        if direction not in {-1, 1} or step_mm not in STEPS:
            raise ValueError("Maintenance steps are 1, 10 or 50 mm with direction -1 or 1")
        current = self.targets[axis]
        if current is None:
            raise LookupError("Select an absolute maintenance target first")
        target = current + direction * step_mm
        lo, hi = LIMITS[axis]
        if not lo <= target <= hi:
            raise ValueError(f"{axis} target would exceed {lo:g}..{hi:g} mm")
        return target

    def observe_command(self, envelope: dict[str, Any]) -> bool:
        body = envelope.get("print") or {}
        command = body.get("command")
        if "sequence_id" in body:
            self._own_sequences.append(str(body["sequence_id"]))
        if command in {"project_file", "gcode_file", "axis_control", "home", "calibration"} or (
            command == "gcode_line" and _MOTION.search(str(body.get("param", "")))
        ):
            self.reset("Another movement command invalidated maintenance targets")
            return True
        return False

    def observe_report(self, incoming: dict[str, Any], previous: dict[str, Any]) -> bool:
        flag = incoming.get("home_flag")
        previous_flag = previous.get("home_flag")
        previous_bits = previous_flag & 7 if isinstance(previous_flag, int) else 0
        if "home_flag" in incoming and (
            not isinstance(flag, int) or isinstance(flag, bool) or (flag & 7) != previous_bits
        ):
            self.reset("Reported homing state changed")
            return True
        command = incoming.get("command")
        # Acknowledgements of our own targets are not physical completion.
        # Other command reports are conservatively treated as untracked motion.
        if command in {"gcode_line", "gcode_file", "project_file", "axis_control", "home"}:
            own = str(incoming.get("sequence_id", "")) in self._own_sequences
            failed = "result" in incoming and str(incoming["result"]).lower() != "success"
            if not own or failed:
                self.reset("External command report invalidated maintenance targets")
                return True
        return False
