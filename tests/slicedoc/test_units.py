"""slicedoc pure units: the gcode scan and the project_file command."""

from __future__ import annotations

from bambu_bridge.slicedoc import project_file_command, scan_gcode, sd_filename

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


def test_project_file_command_uses_confirmed_scheme() -> None:
    cmd = project_file_command("3DBenchy", use_ams=True, ams_mapping=[1])
    assert cmd["url"] == "file:///sdcard/3DBenchy.gcode.3mf"  # not ftp://
    assert cmd["param"] == "Metadata/plate_1.gcode"
    assert cmd["use_ams"] is True
    assert cmd["ams_mapping"] == [1]
    assert cmd["bed_type"] == "textured_plate"
    # idempotent on an already-suffixed name
    assert sd_filename("3DBenchy.gcode.3mf") == "3DBenchy.gcode.3mf"
