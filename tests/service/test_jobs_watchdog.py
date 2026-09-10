"""FED_NO_PROGRESS watchdog signal — the 2026-05-19 hard lesson.

The air-print recurrence reached layer 43 at mc_percent 32 while
``ams.tray_now`` was *never set*. The watchdog must key on AMS engagement,
**not** mc_percent/layer_num (which advance during a dry run). This locks
that in.
"""

from __future__ import annotations

from typing import Any

import pytest

from bambu_bridge.service.jobs import _ams_engaged


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"ams": {"tray_now": "1"}}, True),  # AMS tray engaged
        ({"ams": {"tray_now": "0"}}, True),
        ({"ams": {"tray_now": "254"}}, True),  # external/VT spool loaded
        ({"ams": {"tray_now": "255"}}, False),  # 255 = nothing selected
        ({"ams": {"tray_now": ""}}, False),
        ({"ams": {"tray_now": None}}, False),
        ({"ams": {}}, False),  # ams present, no tray_now (the §6.3 shape)
        ({}, False),
        ({"ams": "not-a-dict"}, False),
        ({"state": {"ams": {"tray_now": "2"}}}, True),  # nested under state
        # THE regression: mc_percent/layer_num high but AMS never engaged ->
        # NOT progress (this exact shape produced the false PRINTING_CONFIRMED).
        ({"mc_percent": 32, "layer_num": 43}, False),
        ({"mc_percent": 99, "gcode_state": "RUNNING"}, False),
    ],
)
def test_ams_engaged_discriminates_print_from_air(
    data: dict[str, Any], expected: bool
) -> None:
    assert _ams_engaged(data) is expected
