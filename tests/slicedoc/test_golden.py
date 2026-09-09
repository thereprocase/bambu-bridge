"""Golden tests against the real artifact.

`probes/3DBenchy_PETG_slot2.gcode.3mf` is the exact container that was
*accepted* by the printer, heated to 255 °C, ran motion to layer ~38, and
extruded nothing for 17 minutes (REPORT §6.3). The headline assertion here:
**slicedoc's gate flags that file before it would ever be uploaded.** Then we
synthesize a corrected single-tray container from the same static members and
gcode and prove it validates clean.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bambu_bridge.slicedoc import (
    AmsFeed,
    Filament,
    PlateInfo,
    SliceConsistencyError,
    StaticMembers,
    build_project_file_command,
    gcode_md5,
    read_member,
    scan_gcode,
    sd_filename,
    synthesize,
    validate,
)
from bambu_bridge.slicedoc.container import MD5_MEMBER

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


def test_donor_gcode_is_rejected_without_normalize_but_clean_with() -> None:
    """The probe's own gcode is internally inconsistent (M620 S1A vs
    M621 S0A — the 2026-05-19 air-print). Wrapping it as-is must be refused;
    only selector-normalization makes it printable."""
    raw = PROBE.read_bytes()
    static = StaticMembers.from_zip(raw)
    gcode = read_member(raw, "Metadata/plate_1.gcode")
    feed = AmsFeed.single(
        Filament("GFG96", "PETG", "#161616", 3.71, 11.33), tray=1
    )
    plate = PlateInfo(
        printer_model_id="C12",
        total_layers=200,
        prediction_s=3382,
        weight_g=11.33,
        first_layer_time_s=470.344147,
        object_id=83,
        object_name="3DBenchy.drc",
    )
    # As-is: the tightened invariant catches the M621 S0A finish.
    with pytest.raises(SliceConsistencyError, match="finishes|not in"):
        synthesize(gcode=gcode, feed=feed, plate=plate, static=static)

    # Normalized: every selector rewritten to tray 1 -> coherent + valid.
    good = synthesize(
        gcode=gcode,
        feed=feed,
        plate=plate,
        static=static,
        normalize_ams=True,
    )
    report = validate(good, expected_ams_mapping=[1])
    assert report.ok, report.issues
    inner = read_member(good, "Metadata/plate_1.gcode")
    assert scan_gcode(inner).bound_trays == frozenset({1})  # no stray S0A
    stored = read_member(good, MD5_MEMBER)
    assert stored == gcode_md5(inner).encode("ascii")  # md5 of normalized gc
    assert stored.decode() == stored.decode().upper()
    assert b"\n" not in stored and len(stored) == 32


def test_6_3_reproduction_is_unconstructable() -> None:
    raw = PROBE.read_bytes()
    static = StaticMembers.from_zip(raw)
    gcode = read_member(raw, "Metadata/plate_1.gcode")
    plate = PlateInfo(
        printer_model_id="C12",
        total_layers=200,
        prediction_s=3382,
        weight_g=11.33,
        first_layer_time_s=470.344147,
        object_id=83,
        object_name="3DBenchy.drc",
    )
    # gcode loads tray 1; bind tray 0 -> exactly the §6.3 inconsistency.
    bad = AmsFeed.single(
        Filament("GFG96", "PETG", "#161616", 3.71, 11.33), tray=0
    )
    with pytest.raises(SliceConsistencyError, match="§6.3|ams_mapping"):
        synthesize(gcode=gcode, feed=bad, plate=plate, static=static)


def test_project_file_command_uses_confirmed_scheme() -> None:
    feed = AmsFeed.single(
        Filament("GFG96", "PETG", "#161616", 3.71, 11.33), tray=1
    )
    cmd = build_project_file_command(feed, "3DBenchy")
    assert cmd["url"] == "file:///sdcard/3DBenchy.gcode.3mf"  # not ftp://
    assert cmd["param"] == "Metadata/plate_1.gcode"
    assert cmd["use_ams"] is True
    assert cmd["ams_mapping"] == [1]
    assert cmd["bed_type"] == "textured_plate"
    # idempotent on an already-suffixed name
    assert sd_filename("3DBenchy.gcode.3mf") == "3DBenchy.gcode.3mf"
