"""Start routes accept the states a P1S can be restarted from, and only those."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from bambu_bridge.api.capabilities import require_start_ready


def _service(state: str, *, age_s: float = 0, connected: bool = True) -> SimpleNamespace:
    stamp = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()
    snapshot = {
        "_raw": {"gcode_state": state},
        "session": {"connected": connected, "last_telemetry_at": stamp},
    }
    return SimpleNamespace(snapshot=lambda: snapshot)


@pytest.mark.parametrize("state", ["IDLE", "FINISH", "FAILED"])
def test_restartable_states_pass(state: str) -> None:
    # FAILED persists on a P1S after stop + screen dismiss until the next job.
    require_start_ready(_service(state))


@pytest.mark.parametrize("state", ["RUNNING", "PAUSE", "PREPARE", "SLICING", "UNKNOWN"])
def test_busy_or_unknown_states_refused(state: str) -> None:
    with pytest.raises(HTTPException) as exc:
        require_start_ready(_service(state), "a stored file")
    assert exc.value.status_code == 409
    assert exc.value.detail["message"] == "Printer must be idle before starting a stored file"


@pytest.mark.parametrize("kwargs", [{"age_s": 60}, {"connected": False}])
def test_failed_still_needs_fresh_telemetry(kwargs: dict) -> None:
    with pytest.raises(HTTPException) as exc:
        require_start_ready(_service("FAILED", **kwargs))
    assert exc.value.detail["message"] == "Fresh printer status required"
