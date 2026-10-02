"""OrcaSlicer's busy rules for motion and filament moves (StatusPanel.cpp)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from bambu_bridge.api.control import BUSY_MESSAGE, PAUSED_FILAMENT_MESSAGE, require_not_busy


def _svc(state: str | None) -> SimpleNamespace:
    return SimpleNamespace(_state={"gcode_state": state} if state else {})


@pytest.mark.parametrize("state", [None, "IDLE", "FINISH", "FAILED", "UNKNOWN"])
@pytest.mark.parametrize("tray", [None, 0, 3, 254])
def test_not_printing_allows_everything(state: str | None, tray: int | None) -> None:
    require_not_busy(_svc(state), tray=tray)  # type: ignore[arg-type]


@pytest.mark.parametrize("state", ["PREPARE", "RUNNING", "SLICING"])
@pytest.mark.parametrize("tray", [None, 0, 254])
def test_printing_refuses_motion_and_filament(state: str, tray: int | None) -> None:
    with pytest.raises(HTTPException) as exc:
        require_not_busy(_svc(state), tray=tray)  # type: ignore[arg-type]
    assert exc.value.status_code == 409
    assert exc.value.detail["message"] == BUSY_MESSAGE


def test_paused_allows_motion_and_external_spool_only() -> None:
    paused = _svc("PAUSE")
    require_not_busy(paused)  # type: ignore[arg-type]
    require_not_busy(paused, tray=254)  # type: ignore[arg-type]
    for tray in (0, 1, 2, 3, -1):
        with pytest.raises(HTTPException) as exc:
            require_not_busy(paused, tray=tray)  # type: ignore[arg-type]
        assert exc.value.status_code == 409
        assert exc.value.detail["message"] == PAUSED_FILAMENT_MESSAGE
