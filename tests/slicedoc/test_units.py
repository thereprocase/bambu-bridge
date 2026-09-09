"""slicedoc pure units: the §6.3 invariant, slice_info, gcode scan/envelope."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from bambu_bridge.slicedoc import (
    AmsFeed,
    AmsFilament,
    ExternalSpoolFeed,
    Filament,
    PlateInfo,
    SliceConsistencyError,
    TemperatureEnvelopeError,
    normalize_ams_selectors,
    render_slice_info,
    scan_gcode,
)
from bambu_bridge.slicedoc.gcode import assert_temperature_envelope

PETG = Filament("GFG96", "PETG", "#161616", 3.71, 11.33)
PLA = Filament("GFA00", "PLA", "#FFFFFF", 1.0, 2.0)


# --------------------------------------------------------------------------- #
# Filament / FeedPlan invariant
# --------------------------------------------------------------------------- #


def test_filament_validation() -> None:
    with pytest.raises(SliceConsistencyError):
        Filament("", "PETG", "#161616", 1, 1)
    with pytest.raises(SliceConsistencyError):
        Filament("GFG96", "", "#161616", 1, 1)
    with pytest.raises(SliceConsistencyError):
        Filament("GFG96", "PETG", "161616", 1, 1)  # no '#', wrong length


def test_ams_single_derivations() -> None:
    feed = AmsFeed.single(PETG, tray=1)
    assert feed.use_ams is True
    assert feed.ams_mapping == [1]
    assert feed.filament_maps_value() == "1"
    assert feed.limit_filament_maps_value() == "0"
    assert feed.slice_filaments() == [(1, PETG)]


def test_ams_multi_derivations() -> None:
    feed = AmsFeed((AmsFilament(PETG, 0), AmsFilament(PLA, 2)))
    assert feed.ams_mapping == [0, 2]
    assert feed.filament_maps_value() == "1 1"
    assert [i for i, _ in feed.slice_filaments()] == [1, 2]  # ids 1-based


def test_ams_rejects_inconsistency_at_construction() -> None:
    with pytest.raises(SliceConsistencyError):
        AmsFeed(())  # empty
    with pytest.raises(SliceConsistencyError):
        AmsFeed((AmsFilament(PETG, 1), AmsFilament(PLA, 1)))  # dup tray
    with pytest.raises(SliceConsistencyError):
        AmsFeed.single(PETG, tray=4)  # out of 0..3
    with pytest.raises(SliceConsistencyError):
        AmsFeed.single(PETG, tray=-1)


def test_ams_gcode_consistency_is_the_6_3_gate() -> None:
    feed = AmsFeed.single(PETG, tray=1)
    # load≡finish≡tool all = 1, plus the standard end-gcode S255 unload: ok
    feed.assert_gcode_consistent(
        scan_gcode(b"M620 S1A\nT1\nM621 S1A\nM620 S255\nT255\nM621 S255\n")
    )
    # THE 2026-05-19 bug: load tray 1 but finish tray 0 — must be rejected
    with pytest.raises(SliceConsistencyError, match="M620.*M621|not in"):
        feed.assert_gcode_consistent(scan_gcode(b"M620 S1A\nM621 S0A\n"))
    # a stray tool-select also trips it (the gate now covers T<n>)
    with pytest.raises(SliceConsistencyError):
        feed.assert_gcode_consistent(scan_gcode(b"M620 S1A\nT2\nM621 S1A\n"))
    with pytest.raises(SliceConsistencyError):
        feed.assert_gcode_consistent(scan_gcode(b"G1 X0 Y0\n"))  # no load
    with pytest.raises(SliceConsistencyError, match="ExternalSpoolFeed"):
        feed.assert_gcode_consistent(scan_gcode(b"M620 S255\nT255\n"))


def test_external_spool_contract() -> None:
    feed = ExternalSpoolFeed(PETG)
    assert feed.use_ams is False
    assert feed.ams_mapping == []
    feed.assert_gcode_consistent(scan_gcode(b"M620 S255\nT255\nM621 S255\n"))
    with pytest.raises(SliceConsistencyError):
        feed.assert_gcode_consistent(scan_gcode(b"M620 S1A\n"))  # binds real


def test_normalize_ams_selectors_makes_handshake_coherent() -> None:
    """The exact 2026-05-19 shape: M620 S1A load, M621 S0A finish, T0."""
    bad = (
        b"M620 M\n"
        b"M620 S1A   ; switch material if AMS exist\n"
        b"T0\n"
        b"M621 S0A\n"
        b"M620.1 E F199.559 T260\n"  # calibration: must NOT be touched
        b"T1000\n"  # flush pseudo-tool: must NOT be touched
        b"M104 S255\n"  # a temperature, not a tray: must NOT be touched
        b"M620 S255\nT255\nM621 S255\n"  # end-gcode unload: preserved
    )
    out, changes = normalize_ams_selectors(bad, tray=1)
    after = scan_gcode(out)
    assert after.bound_trays == frozenset({1})  # all real selectors -> 1
    assert b"M621 S1A" in out and b"\nT1\n" in out
    assert b"M620.1 E F199.559 T260" in out  # calibration intact
    assert b"\nT1000\n" in out and b"M104 S255" in out  # untouched
    assert b"M620 S255" in out and b"T255" in out  # sentinels preserved
    # forensic audit names what changed (M621 S0A->S1A and T0->T1 at least)
    changed = {(o.strip(), n.strip()) for _, o, n in changes}
    assert ("M621 S0A", "M621 S1A") in changed
    assert ("T0", "T1") in changed
    # already-correct M620 S1A is not logged as a change
    assert not any(o.strip() == "M620 S1A" for _, o, _ in changes)
    # idempotent
    out2, changes2 = normalize_ams_selectors(out, tray=1)
    assert out2 == out and changes2 == []


# --------------------------------------------------------------------------- #
# slice_info.config rendering
# --------------------------------------------------------------------------- #


def _plate(total_layers: int = 200) -> PlateInfo:
    return PlateInfo(
        printer_model_id="C12",
        total_layers=total_layers,
        prediction_s=3382,
        weight_g=11.33,
        first_layer_time_s=470.344147,
        object_id=83,
        object_name="3DBenchy.drc",
    )


def test_render_single_is_well_formed_and_consistent() -> None:
    xml = render_slice_info(_plate(), AmsFeed.single(PETG, tray=1))
    root = ET.fromstring(xml)  # must parse
    maps = [
        m.get("value")
        for m in root.iter("metadata")
        if m.get("key") == "filament_maps"
    ]
    assert maps == ["1"]
    fils = list(root.iter("filament"))
    assert len(fils) == 1
    assert fils[0].get("id") == "1"
    assert fils[0].get("tray_info_idx") == "GFG96"
    assert fils[0].get("type") == "PETG"
    lfl = list(root.iter("layer_filament_list"))
    assert lfl[0].get("filament_list") == "0"
    assert lfl[0].get("layer_ranges") == "0 199"


def test_render_multi_requires_explicit_layer_lists() -> None:
    feed = AmsFeed((AmsFilament(PETG, 0), AmsFilament(PLA, 1)))
    with pytest.raises(SliceConsistencyError):
        render_slice_info(_plate(), feed)
    xml = render_slice_info(
        _plate(), feed, layer_lists=[(0, "0 99"), (1, "100 199")]
    )
    assert 'filament_list="1"' in xml


def test_plateinfo_rejects_zero_layers() -> None:
    with pytest.raises(SliceConsistencyError):
        _plate(total_layers=0)


# --------------------------------------------------------------------------- #
# gcode scan + temperature envelope
# --------------------------------------------------------------------------- #

_GCODE = (
    b"M104 S75 ;preheat\n"
    b"M140 S70 ;bed\n"
    b"M190 S70\n"
    b"M620 M\n"
    b"M620 S1A   ; switch material if AMS exist\n"
    b"M621 S0A\n"
    b"M620.1 E F199.559 T260\n"
    b"T1000\n"
    b"M109 S255\n"
    b"M104 S0 ; off\n"
    b"M620 S255\n"
    b"T255\n"
    b"M621 S255\n"
)


def test_scan_separates_trays_from_temps() -> None:
    scan = scan_gcode(_GCODE)
    # 255 sentinels go to has_external, not the real-tray sets; M620.1 and
    # the flush T1000 are excluded entirely.
    assert scan.load_trays == frozenset({1})
    assert scan.finish_trays == frozenset({0})
    assert scan.tool_trays == frozenset()  # T1000 (flush) + T255 excluded
    assert scan.has_external is True  # M620 S255 / T255 / M621 S255 seen
    assert scan.bound_trays == frozenset({0, 1})  # the §6.3 incoherence
    assert scan.max_nozzle_c == 255  # M104 S255-style is a temp, not a tray
    assert scan.max_bed_c == 70
    assert_temperature_envelope(scan)  # within envelope


def test_temperature_envelope_rejects_unsafe() -> None:
    with pytest.raises(TemperatureEnvelopeError, match="nozzle"):
        assert_temperature_envelope(scan_gcode(b"M104 S300\n"))
    with pytest.raises(TemperatureEnvelopeError, match="bed"):
        assert_temperature_envelope(scan_gcode(b"M190 S130\n"))
