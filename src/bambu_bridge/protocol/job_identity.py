"""Evidence that the printer has discarded its resumable job."""

from typing import Any


def job_lost(current: dict[str, Any]) -> bool:
    """IDLE with an explicitly empty job identity: nothing left to resume.

    PAUSE and named IDLE jobs are deliberately preserved (resumable).
    """
    return (
        current.get("gcode_state") == "IDLE"
        and current.get("gcode_file") == ""
        and current.get("subtask_name") == ""
    )


def empty_idle_report(incoming: dict[str, Any], current: dict[str, Any]) -> bool:
    """Require an explicit empty identity, not missing fields or silence.

    P1 reports may split state and identity across deltas. A fresh state or
    identity edge can complete the evidence; unrelated temperature packets
    cannot.
    """
    return any(key in incoming for key in ("gcode_state", "gcode_file", "subtask_name")) and (
        job_lost(current)
    )
