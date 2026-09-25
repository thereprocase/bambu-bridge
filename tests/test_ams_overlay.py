import io
import time
from datetime import UTC, datetime

import pytest
from PIL import Image

from bambu_bridge.ams_overlay import ams_panel, measured
from bambu_bridge.camera_overlay import render_frame


def sample():
    return {
        "session": {"connected": True, "last_telemetry_at": datetime.now(UTC).isoformat()},
        "_raw": {
            "ams": {
                "tray_now": "3",
                "ams": [
                    {
                        "id": "0",
                        "humidity_raw": "7",
                        "temp": "32.8",
                        "tray": [
                            {"id": str(i), "tray_type": "ASA", "tray_color": c}
                            for i, c in enumerate(["2C2C2CFF", "898989FF", "FFFFFFFF", "161616FF"])
                        ],
                    }
                ],
            }
        },
    }


def test_live_selection_and_reported_values():
    panel = ams_panel(sample(), time.time())
    assert panel["title"] == "AMS · 7% RH · 32.8°C"
    assert panel["subtitle"] == "Slot 4 · ASA"
    assert panel["active"] == 3 and panel["colors"][2] == "#FFFFFF"
    assert not panel["stale"] and panel["fault"] is None


@pytest.mark.parametrize("value", [None, "", "nan", "inf", -1, 101, True])
def test_invalid_humidity_is_not_a_measurement(value):
    assert measured(value, 0, 100) is None


def test_missing_fields_external_and_stale_faults():
    s = sample()
    s["_raw"]["ams"]["ams"][0].pop("humidity_raw")
    s["_raw"]["ams"]["tray_now"] = "254"
    s["session"]["connected"] = False
    s["hms"] = [{"category": "ams", "stale": True, "severity": "error", "text": "Old fault"}]
    p = ams_panel(s, time.time())
    assert "RH" not in p["title"] and p["subtitle"] == "External spool"
    assert p["active"] is None and p["stale"] and p["fault"] is None
    s["session"]["connected"] = True
    s["hms"][0]["stale"] = False
    assert ams_panel(s, time.time())["fault"] == "Old fault"
    s["session"]["last_telemetry_at"] = "2020-01-01T00:00:00+00:00"
    assert ams_panel(s, time.time())["stale"]
    assert ams_panel({}, time.time()) is None


def test_multiple_units_selects_active_unit():
    s = sample()
    unit = dict(s["_raw"]["ams"]["ams"][0], id="1", humidity_raw="12")
    s["_raw"]["ams"]["ams"].append(unit)
    s["_raw"]["ams"]["tray_now"] = "5"
    p = ams_panel(s, time.time())
    assert p["title"].startswith("AMS 2 · 12%") and p["active"] == 1


@pytest.mark.parametrize("dimensions", [(320, 240), (1280, 720), (1920, 1080)])
def test_panel_changes_upper_right_preserves_center_and_existing_hud(dimensions):
    out = io.BytesIO()
    Image.new("RGB", dimensions, "#354960").save(out, format="JPEG")
    base = Image.open(
        io.BytesIO(render_frame(out.getvalue(), ["PRINTING", "Finishes ~8:30 AM"], False))
    )
    result = Image.open(
        io.BytesIO(
            render_frame(
                out.getvalue(),
                ["PRINTING", "Finishes ~8:30 AM"],
                False,
                ams_panel(sample(), time.time()),
            )
        )
    )
    w, h = dimensions
    assert result.size == dimensions
    assert result.crop((0, h // 2, w, h)).tobytes() == base.crop((0, h // 2, w, h)).tobytes()
    assert (
        result.crop((w // 2, 0, w, h // 3)).tobytes() != base.crop((w // 2, 0, w, h // 3)).tobytes()
    )
