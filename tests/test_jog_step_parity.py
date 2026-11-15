"""Maintenance step sizes must agree across the Android app, the web app and the bridge.

The bridge accepts a fixed set of step_mm values on /move/maintenance-step
and /move/maintenance-xy-step. A step a UI offers but the bridge refuses is a
button that can never work, so the three copies are kept equal by test.

The classic jog endpoint (ALLOWED_STEPS, 1 and 10 mm, OrcaSlicer's) is a
separate list and is pinned against the web JOG_STEPS below.
"""

from __future__ import annotations

import re
from pathlib import Path

from bambu_bridge.api.control import ALLOWED_STEPS

ROOT = Path(__file__).resolve().parents[1]
CONTROL_PY = ROOT / "src/bambu_bridge/api/control.py"
WEB = ROOT / "src/bambu_bridge/static/app/controls.js"
APP = ROOT / "mobile/src/lib/maintenance.ts"


def _numbers(text: str) -> list[float]:
    return sorted(float(n) for n in re.findall(r"\d+(?:\.\d+)?", text))


def _find(path: Path, *patterns: str) -> list[float]:
    """Numbers from the first pattern that matches in path (group 1)."""
    source = path.read_text(encoding="utf-8")
    for pattern in patterns:
        match = re.search(pattern, source)
        if match:
            return _numbers(match.group(1))
    raise AssertionError(f"step list not found in {path}")


def _bridge_maintenance_steps() -> list[float]:
    """Every maintenance step model must declare the same Literal."""
    source = CONTROL_PY.read_text(encoding="utf-8")
    lists = {tuple(_numbers(m)) for m in re.findall(r"step_mm: Literal\[([^\]]*)\]", source)}
    assert len(lists) == 1, f"maintenance step models disagree in {CONTROL_PY}: {lists}"
    return list(lists.pop())


def _web_maintenance_steps() -> list[float]:
    return _find(
        WEB,
        r"MAINTENANCE_STEPS = \[([^\]]*)\]",
        r"\[([\d,\s]+)\]\.map\(\(step\)",
    )


def _app_maintenance_steps() -> list[float]:
    return _find(APP, r"export const MAINTENANCE_STEPS = \[([^\]]*)\]")


def test_bridge_maintenance_steps() -> None:
    assert _bridge_maintenance_steps() == [1.0, 10.0, 50.0]


def test_web_maintenance_steps_match_bridge() -> None:
    assert _web_maintenance_steps() == _bridge_maintenance_steps()


def test_app_maintenance_steps_match_bridge() -> None:
    assert _app_maintenance_steps() == _bridge_maintenance_steps()


def test_app_and_web_maintenance_steps_agree() -> None:
    assert _app_maintenance_steps() == _web_maintenance_steps()


def test_classic_jog_steps_are_orcas() -> None:
    assert sorted(ALLOWED_STEPS) == [1.0, 10.0]
    assert _find(WEB, r"export const JOG_STEPS = \[([^\]]*)\]") == sorted(ALLOWED_STEPS)


def test_client_jog_feeds_within_bridge_ceilings() -> None:
    """Both clients' relative-jog feed rates are accepted by the bridge's caps."""
    import re
    from pathlib import Path

    from bambu_bridge.protocol import commands

    root = Path(__file__).resolve().parents[1]
    sources = {
        "web": root / "src/bambu_bridge/static/app/controls.js",
        "android": root / "mobile/src/api/control.ts",
    }
    for name, path in sources.items():
        match = re.search(
            r"JOG_FEED\w*\s*=\s*\{\s*X:\s*(\d+),\s*Y:\s*(\d+),\s*Z:\s*(\d+)\s*\}", path.read_text()
        )
        assert match, f"jog feed table not found in {path}"
        x, y, z = (int(v) for v in match.groups())
        assert max(x, y) <= commands.JOG_FEED_MAX_XY, name
        assert z <= commands.JOG_FEED_MAX_Z, name
