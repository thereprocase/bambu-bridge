"""Evidence that the printer has discarded its resumable job."""

from typing import Any


def empty_idle_report(incoming: dict[str, Any], current: dict[str, Any]) -> bool:
    """Require an explicit empty identity, not missing fields or silence.

    P1 reports may split state and identity across deltas. A fresh state or
    identity edge can complete the evidence; unrelated temperature packets
    cannot. PAUSE and named IDLE jobs are deliberately preserved.
    """
    return (
        any(key in incoming for key in ("gcode_state", "gcode_file", "subtask_name"))
        and current.get("gcode_state") == "IDLE"
        and current.get("gcode_file") == ""
        and current.get("subtask_name") == ""
    )
