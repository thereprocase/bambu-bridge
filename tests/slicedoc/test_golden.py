"""Golden tests against the real artifact.

`probes/3DBenchy_PETG_slot2.gcode.3mf` is the exact container that was
*accepted* by the printer, heated to 255 °C, ran motion to layer ~38, and
extruded nothing for 17 minutes (REPORT §6.3). The headline assertion here:
**slicedoc's gate flags that file before it would ever be uploaded.**
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bambu_bridge.slicedoc import validate

PROBE = (
    Path(__file__).resolve().parents[2]
    / "probes"
    / "3DBenchy_PETG_slot2.gcode.3mf"
)

pytestmark = pytest.mark.skipif(
    not PROBE.exists(), reason="probe .gcode.3mf fixture not present"
)


def test_gate_catches_the_real_6_3_failure() -> None:
    """The config that wasted 17 min of real hardware must not pass."""
    report = validate(PROBE.read_bytes(), expected_ams_mapping=[1])
    assert report.ok is False
    blob = " ".join(report.issues)
    assert "G5" in blob  # AMS-consistency gate fired
    # 5-slot filament_maps vs a single <filament> record — inconsistency A.
    assert "arity" in blob
