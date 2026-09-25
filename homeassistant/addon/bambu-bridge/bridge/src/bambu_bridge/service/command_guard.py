"""Fail-closed start boundary shared by HTTP and native MQTT passthrough.

This is a start-bypass guard, not a substitute for typed motion/thermal guards.
Only the existing typed G-code grammar is accepted; SD commands/macros and
unknown firmware instructions must not become an unreserved print path.
"""

import re
from typing import Any

from bambu_bridge.db.starts import StartConflict

_PRINT_COMMANDS = frozenset(
    {
        "project_file",
        "pause",
        "resume",
        "stop",
        "print_speed",
        "gcode_line",
        "ams_control",
        "ams_change_filament",
        "unload_filament",
        "print_option",
        "skip_objects",
        "ams_filament_setting",
        "ams_get_rfid",
        "ams_filament_drying",
        "ams_user_setting",
        "calibration",
        "push_status",
    }
)
_GCODE = frozenset({"G0", "G1", "G28", "G90", "G91", "M83", "M84", "M104", "M106", "M140"})
_PARAMETER = re.compile(r"[XYZEFSP](?:[-+]?(?:\d+(?:\.\d*)?|\.\d+))?", re.ASCII)


def guard_passthrough(envelope: dict[str, Any]) -> None:
    body = envelope.get("print")
    if body is None:
        return
    if not isinstance(body, dict) or body.get("command") not in _PRINT_COMMANDS:
        raise StartConflict("Unsupported raw print command; use managed starts or typed controls")
    if body.get("command") == "project_file" and set(envelope) != {"print"}:
        raise StartConflict("A managed start cannot contain additional command categories")
    if body.get("command") != "gcode_line":
        return
    source = body.get("param")
    if not isinstance(source, str) or len(source.encode()) > 1024:
        raise StartConflict("Invalid raw G-code payload")
    for line in source.splitlines():
        words = line.split(";", 1)[0].strip().upper().split()
        if not words:
            continue
        if words[0] not in _GCODE or any(not _PARAMETER.fullmatch(word) for word in words[1:]):
            raise StartConflict("Raw SD starts, macros, and unreviewed G-code are disabled")
