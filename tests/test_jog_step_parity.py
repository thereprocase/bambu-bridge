"""The web and app jog steps must equal the bridge's ALLOWED_STEPS.

All three follow OrcaSlicer (1 and 10 mm). A step a UI offers but the bridge
refuses is a button that can never work, so keep the copies equal by test.
"""

from __future__ import annotations

import re
from pathlib import Path

from bambu_bridge.api.control import ALLOWED_STEPS

ROOT = Path(__file__).resolve().parents[1]


def _numbers(path: Path, pattern: str) -> list[float]:
    match = re.search(pattern, path.read_text(encoding="utf-8"))
    assert match, f"jog step list not found in {path}"
    return sorted(float(n) for n in re.findall(r"\d+(?:\.\d+)?", match.group(1)))


def test_bridge_steps_are_orcas() -> None:
    assert sorted(ALLOWED_STEPS) == [1.0, 10.0]


def test_web_steps_match_bridge() -> None:
    web = ROOT / "src/bambu_bridge/static/app/controls.js"
    assert _numbers(web, r"export const JOG_STEPS = \[([^\]]*)\]") == sorted(ALLOWED_STEPS)


def test_app_steps_match_bridge() -> None:
    app = ROOT / "mobile/app/(tabs)/controls.tsx"
    assert _numbers(app, r"const STEP_MM = \[([^\]]*)\]") == sorted(ALLOWED_STEPS)
