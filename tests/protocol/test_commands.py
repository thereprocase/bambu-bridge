"""Wire-shape + validation lock for the typed P1S command catalog.

These envelopes are what actually reaches the printer; a wrong field name or
category is a silent no-op on real hardware, so pin them exactly.
"""

from __future__ import annotations

import pytest

from bambu_bridge.protocol import commands


def _body(env: dict, category: str) -> dict:
    assert set(env) == {category}, env
    body = env[category]
    assert body["command"]
    assert isinstance(body["sequence_id"], str) and body["sequence_id"]
    return body


def test_print_lifecycle_envelopes() -> None:
    # command name
    assert _body(commands.print_pause(), "print")["command"] == "pause"
    assert _body(commands.print_resume(), "print")["command"] == "resume"
    assert _body(commands.print_stop(), "print")["command"] == "stop"
    # param:"" alignment with OpenBambuAPI (control matrix §1 drift fix)
    assert _body(commands.print_pause(), "print")["param"] == ""
    assert _body(commands.print_resume(), "print")["param"] == ""
    assert _body(commands.print_stop(), "print")["param"] == ""


def test_print_speed_levels() -> None:
    body = _body(commands.print_speed(2), "print")
    assert body["command"] == "print_speed"
    assert body["param"] == "2"
    for bad in (0, 5, -1):
        with pytest.raises(ValueError, match="speed level"):
            commands.print_speed(bad)


def test_gcode_line_appends_newline_and_rejects_empty() -> None:
    body = _body(commands.gcode_line("G28"), "print")
    assert body["command"] == "gcode_line"
    assert body["param"] == "G28\n"
    # already-terminated is left alone
    assert commands.gcode_line("G1 X1\n")["print"]["param"] == "G1 X1\n"
    with pytest.raises(ValueError, match="empty"):
        commands.gcode_line("   ")


def test_temperature_helpers_are_gcode_line() -> None:
    assert commands.set_nozzle_temp(250)["print"]["param"] == "M104 S250\n"
    assert commands.set_bed_temp(70)["print"]["param"] == "M140 S70\n"
    with pytest.raises(ValueError, match="nozzle temp"):
        commands.set_nozzle_temp(400)
    with pytest.raises(ValueError, match="bed temp"):
        commands.set_bed_temp(200)


def test_fan_maps_part_and_scales_percent() -> None:
    # 100% -> 255, chamber is P3
    assert commands.set_fan("chamber", 100)["print"]["param"] == "M106 P3 S255\n"
    assert commands.set_fan("part", 0)["print"]["param"] == "M106 P1 S0\n"
    assert commands.set_fan("aux", 50)["print"]["param"] == "M106 P2 S128\n"
    with pytest.raises(ValueError, match="fan part"):
        commands.set_fan("nope", 50)
    with pytest.raises(ValueError, match="fan percent"):
        commands.set_fan("part", 150)


def test_home_and_move_axis() -> None:
    assert commands.home()["print"]["param"] == "G28\n"
    mv = commands.move_axis("z", 10, feed_mm_min=600)["print"]["param"]
    assert mv == "G91\nG1 Z10 F600\nG90\n"  # relative jog, restores G90
    with pytest.raises(ValueError, match="axis"):
        commands.move_axis("Q", 1)
    with pytest.raises(ValueError, match="feed"):
        commands.move_axis("X", 1, feed_mm_min=0)


def test_chamber_light_steady_on_off() -> None:
    on = _body(commands.chamber_light(True), "system")
    assert on["command"] == "ledctrl"
    assert on["led_node"] == "chamber_light"
    assert on["led_mode"] == "on"
    assert on["loop_times"] == 0
    assert commands.chamber_light(False)["system"]["led_mode"] == "off"


def test_ams_and_filament() -> None:
    assert _body(commands.ams_control("resume"), "print")["param"] == "resume"
    with pytest.raises(ValueError, match="ams action"):
        commands.ams_control("eject")
    chg = _body(commands.ams_change_filament(1, cur_temp=240, tar_temp=240), "print")
    assert chg["command"] == "ams_change_filament"
    assert chg["target"] == 1
    assert chg["curr_temp"] == 240
    with pytest.raises(ValueError, match="target tray"):
        commands.ams_change_filament(-1)
    assert _body(commands.unload_filament(), "print")["command"] == "unload_filament"


def test_get_version_is_info_category() -> None:
    body = _body(commands.get_version(), "info")
    assert body["command"] == "get_version"


def test_sequence_ids_are_unique_per_build() -> None:
    a = commands.print_pause()["print"]["sequence_id"]
    b = commands.print_pause()["print"]["sequence_id"]
    assert a != b


# ---------------------------------------------------------------------------
# Wave-1 correctness fixes
# ---------------------------------------------------------------------------


class TestAmsSlotNumbering:
    """Tray/slot indices are 0-based protocol indices end-to-end (the code is
    right; the old contract text said "physical slot" which was wrong)."""

    def test_target_passed_unchanged_to_wire(self) -> None:
        body = _body(commands.ams_change_filament(0), "print")
        assert body["target"] == 0, "0-based index must pass through unchanged"

    def test_tray_1_protocol_index(self) -> None:
        body = _body(commands.ams_change_filament(1), "print")
        assert body["target"] == 1

    def test_tray_3_protocol_index(self) -> None:
        body = _body(commands.ams_change_filament(3), "print")
        assert body["target"] == 3

    def test_negative_tray_rejected(self) -> None:
        with pytest.raises(ValueError, match="target tray"):
            commands.ams_change_filament(-1)


class TestNozzleTempClamp:
    """Nozzle temp: 280 °C stainless default; 300 °C only for hardened_steel."""

    def test_280_accepted_stainless(self) -> None:
        env = commands.set_nozzle_temp(280)
        assert env["print"]["param"] == "M104 S280\n"

    def test_281_rejected_stainless(self) -> None:
        with pytest.raises(ValueError, match="nozzle temp"):
            commands.set_nozzle_temp(281)

    def test_300_accepted_hardened(self) -> None:
        env = commands.set_nozzle_temp(300, hardened=True)
        assert env["print"]["param"] == "M104 S300\n"

    def test_301_rejected_hardened(self) -> None:
        with pytest.raises(ValueError, match="nozzle temp"):
            commands.set_nozzle_temp(301, hardened=True)

    def test_zero_accepted_always(self) -> None:
        assert commands.set_nozzle_temp(0)["print"]["param"] == "M104 S0\n"
        assert commands.set_nozzle_temp(0, hardened=True)["print"]["param"] == "M104 S0\n"

    def test_error_message_names_nozzle_type(self) -> None:
        with pytest.raises(ValueError, match="stainless"):
            commands.set_nozzle_temp(290)
        with pytest.raises(ValueError, match="hardened"):
            commands.set_nozzle_temp(301, hardened=True)


class TestGcodeLineCap:
    """gcode_line payloads must fit within the 4 KB MQTT RX buffer."""

    def test_exactly_at_limit_accepted(self) -> None:
        # Build a line whose encoded form after newline-appending == GCODE_LINE_MAX_BYTES.
        # _gcode appends a "\n" only when the line doesn't already end with one.
        # So we need: len(line_without_newline) + 1 == limit.
        # Prefix "G0 " is 3 bytes; we need total = limit, so X-count = limit - 4.
        limit = commands.GCODE_LINE_MAX_BYTES
        line = "G0 " + "X" * (limit - 4)  # 3 bytes prefix + N bytes + 1 newline = limit
        env = commands.gcode_line(line)
        assert len(env["print"]["param"].encode()) == limit

    def test_one_byte_over_limit_rejected(self) -> None:
        limit = commands.GCODE_LINE_MAX_BYTES
        line = "G0 " + "X" * (limit - 3)  # 3 prefix + N + newline = limit + 1
        with pytest.raises(ValueError, match="4096"):
            commands.gcode_line(line)

    def test_typical_gcode_well_within_limit(self) -> None:
        env = commands.gcode_line("G28")
        assert len(env["print"]["param"].encode()) < commands.GCODE_LINE_MAX_BYTES


class TestWorkLight:
    """work_light uses system.ledctrl with led_node=work_light.

    Payload spec from control matrix §4 work_light row:
    - on/off: led_on_time=0, led_off_time=0, loop_times=0, interval_time=0
    - flashing: timing fields reflect the requested interval
    """

    def test_on_payload(self) -> None:
        body = _body(commands.work_light("on"), "system")
        assert body["command"] == "ledctrl"
        assert body["led_node"] == "work_light"
        assert body["led_mode"] == "on"
        # Matrix §4: work_light on/off timing fields are 0 (not 500/500)
        assert body["led_on_time"] == 0
        assert body["led_off_time"] == 0
        assert body["loop_times"] == 0
        assert body["interval_time"] == 0

    def test_off_payload(self) -> None:
        body = _body(commands.work_light("off"), "system")
        assert body["led_node"] == "work_light"
        assert body["led_mode"] == "off"
        assert body["led_on_time"] == 0
        assert body["led_off_time"] == 0

    def test_flashing_payload(self) -> None:
        body = _body(commands.work_light("flashing", loop_times=3, interval_time=200), "system")
        assert body["led_mode"] == "flashing"
        assert body["loop_times"] == 3
        assert body["interval_time"] == 200

    def test_flashing_defaults(self) -> None:
        body = _body(commands.work_light("flashing"), "system")
        assert body["led_mode"] == "flashing"
        assert body["loop_times"] == 1       # default
        assert body["interval_time"] == 500  # default

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="led mode"):
            commands.work_light("strobe")

    def test_flashing_bad_loop_times_rejected(self) -> None:
        with pytest.raises(ValueError, match="loop_times"):
            commands.work_light("flashing", loop_times=-1)

    def test_distinct_from_chamber_light(self) -> None:
        assert commands.work_light("on")["system"]["led_node"] == "work_light"
        assert commands.chamber_light(True)["system"]["led_node"] == "chamber_light"


class TestIpcam:
    """ipcam_record_set and ipcam_timelapse use the camera category."""

    def test_record_enable_payload(self) -> None:
        body = _body(commands.ipcam_record_set(True), "camera")
        assert body["command"] == "ipcam_record_set"
        assert body["control"] == "enable"

    def test_record_disable_payload(self) -> None:
        body = _body(commands.ipcam_record_set(False), "camera")
        assert body["control"] == "disable"

    def test_timelapse_enable_payload(self) -> None:
        body = _body(commands.ipcam_timelapse(True), "camera")
        assert body["command"] == "ipcam_timelapse"
        assert body["control"] == "enable"

    def test_timelapse_disable_payload(self) -> None:
        body = _body(commands.ipcam_timelapse(False), "camera")
        assert body["control"] == "disable"

    def test_camera_category_not_print_or_system(self) -> None:
        env = commands.ipcam_record_set(True)
        assert "camera" in env
        assert "print" not in env
        assert "system" not in env


# ---------------------------------------------------------------------------
# Wave-2: xcam builders
# ---------------------------------------------------------------------------


class TestXcam:
    """xcam_control uses the xcam category (not print/system/camera).

    The xcam MQTT category is blocked by the raw-command passthrough;
    typed builders are the only safe path.  Matrix §9 confirms which
    module_names are P1S-supported.
    """

    def test_xcam_category(self) -> None:
        env = commands.xcam_control("spaghetti_detector", enabled=True)
        assert "xcam" in env
        assert "print" not in env

    def test_xcam_command_name(self) -> None:
        body = _body(commands.xcam_control("spaghetti_detector", enabled=True), "xcam")
        assert body["command"] == "xcam_control_set"

    def test_xcam_module_name_forwarded(self) -> None:
        body = _body(commands.xcam_control("first_layer_inspector", enabled=False), "xcam")
        assert body["module_name"] == "first_layer_inspector"

    def test_xcam_enabled_true(self) -> None:
        body = _body(commands.xcam_control("spaghetti_detector", enabled=True), "xcam")
        assert body["control"] is True

    def test_xcam_enabled_false(self) -> None:
        body = _body(commands.xcam_control("spaghetti_detector", enabled=False), "xcam")
        assert body["control"] is False

    def test_xcam_print_halt_false_by_default(self) -> None:
        body = _body(commands.xcam_control("spaghetti_detector", enabled=True), "xcam")
        assert body["print_halt"] is False

    def test_xcam_print_halt_true(self) -> None:
        body = _body(
            commands.xcam_control("spaghetti_detector", enabled=True, print_halt=True),
            "xcam",
        )
        assert body["print_halt"] is True

    def test_xcam_all_confirmed_modules_accepted(self) -> None:
        for mod in commands.XCAM_MODULES:
            env = commands.xcam_control(mod, enabled=True)
            assert "xcam" in env

    def test_xcam_unknown_module_rejected(self) -> None:
        with pytest.raises(ValueError, match="xcam module"):
            commands.xcam_control("buildplate_marker_detector", enabled=True)

    def test_xcam_empty_module_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="xcam module"):
            commands.xcam_control("", enabled=True)

    def test_xcam_lidar_module_rejected(self) -> None:
        """buildplate_marker_detector is X1/LIDAR-only; must not be accepted."""
        with pytest.raises(ValueError, match="matrix-confirmed"):
            commands.xcam_control("buildplate_marker_detector", enabled=True)


# ---------------------------------------------------------------------------
# Wave-2: print_option builder
# ---------------------------------------------------------------------------


class TestPrintOption:
    """print_option uses print category; only allowed flags are accepted."""

    def test_single_flag_true(self) -> None:
        body = _body(commands.print_option(air_print_detect=True), "print")
        assert body["command"] == "print_option"
        assert body["air_print_detect"] is True

    def test_single_flag_false(self) -> None:
        body = _body(commands.print_option(auto_recovery=False), "print")
        assert body["auto_recovery"] is False

    def test_multiple_flags_combined(self) -> None:
        body = _body(
            commands.print_option(air_print_detect=True, filament_tangle_detect=False),
            "print",
        )
        assert body["air_print_detect"] is True
        assert body["filament_tangle_detect"] is False

    def test_unknown_flag_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown print_option flags"):
            commands.print_option(magic_detect=True)

    def test_empty_flags_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one flag"):
            commands.print_option()

    def test_all_allowed_flags_accepted(self) -> None:
        for flag in commands.PRINT_OPTION_FLAGS:
            env = commands.print_option(**{flag: True})
            assert "print" in env


# ---------------------------------------------------------------------------
# Wave-2: skip_objects builder
# ---------------------------------------------------------------------------


class TestSkipObjects:
    """skip_objects: non-empty int list; timestamp auto-filled."""

    def test_valid_obj_list(self) -> None:
        body = _body(commands.skip_objects([1, 2, 3]), "print")
        assert body["command"] == "skip_objects"
        assert body["obj_list"] == [1, 2, 3]

    def test_timestamp_is_int(self) -> None:
        body = _body(commands.skip_objects([0]), "print")
        assert isinstance(body["timestamp"], int)
        assert body["timestamp"] > 0

    def test_empty_list_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            commands.skip_objects([])

    def test_non_int_element_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be an int"):
            commands.skip_objects([1, "two"])  # type: ignore[list-item]

    def test_single_object(self) -> None:
        body = _body(commands.skip_objects([42]), "print")
        assert body["obj_list"] == [42]


# ---------------------------------------------------------------------------
# Wave-2: AMS builders
# ---------------------------------------------------------------------------


class TestAmsFilamentSetting:
    """ams_filament_setting: all validation cases."""

    def test_valid_payload(self) -> None:
        body = _body(
            commands.ams_filament_setting(
                ams_id=0,
                tray_id=1,
                tray_info_idx="GFB61",
                tray_color="FFFFFFFF",
                nozzle_temp_min=190,
                nozzle_temp_max=230,
                tray_type="PLA",
            ),
            "print",
        )
        assert body["command"] == "ams_filament_setting"
        assert body["ams_id"] == 0
        assert body["tray_id"] == 1
        assert body["tray_info_idx"] == "GFB61"
        assert body["tray_color"] == "FFFFFFFF"
        assert body["nozzle_temp_min"] == 190
        assert body["nozzle_temp_max"] == 230
        assert body["tray_type"] == "PLA"

    def test_negative_ams_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="ams_id"):
            commands.ams_filament_setting(
                ams_id=-1, tray_id=0, tray_info_idx="",
                tray_color="FFFFFFFF", nozzle_temp_min=190,
                nozzle_temp_max=230, tray_type="PLA",
            )

    def test_ams_id_above_max_rejected(self) -> None:
        with pytest.raises(ValueError, match="ams_id"):
            commands.ams_filament_setting(
                ams_id=commands.AMS_ID_MAX + 1, tray_id=0, tray_info_idx="",
                tray_color="FFFFFFFF", nozzle_temp_min=190,
                nozzle_temp_max=230, tray_type="PLA",
            )

    def test_invalid_tray_color_length_rejected(self) -> None:
        with pytest.raises(ValueError, match="tray_color"):
            commands.ams_filament_setting(
                ams_id=0, tray_id=0, tray_info_idx="",
                tray_color="FFF",  # too short
                nozzle_temp_min=190, nozzle_temp_max=230, tray_type="PLA",
            )

    def test_invalid_tray_color_nonhex_rejected(self) -> None:
        with pytest.raises(ValueError, match="tray_color"):
            commands.ams_filament_setting(
                ams_id=0, tray_id=0, tray_info_idx="",
                tray_color="GGGGGGGG",  # not hex
                nozzle_temp_min=190, nozzle_temp_max=230, tray_type="PLA",
            )

    def test_temp_min_ge_max_rejected(self) -> None:
        with pytest.raises(ValueError, match="nozzle_temp_min"):
            commands.ams_filament_setting(
                ams_id=0, tray_id=0, tray_info_idx="",
                tray_color="FFFFFFFF",
                nozzle_temp_min=230, nozzle_temp_max=230,  # equal
                tray_type="PLA",
            )

    def test_temp_max_over_ceiling_rejected(self) -> None:
        with pytest.raises(ValueError, match="absolute ceiling"):
            commands.ams_filament_setting(
                ams_id=0, tray_id=0, tray_info_idx="",
                tray_color="FFFFFFFF",
                nozzle_temp_min=250, nozzle_temp_max=310,  # > 300
                tray_type="ABS",
            )

    def test_unknown_tray_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="tray_type"):
            commands.ams_filament_setting(
                ams_id=0, tray_id=0, tray_info_idx="",
                tray_color="FFFFFFFF",
                nozzle_temp_min=190, nozzle_temp_max=230,
                tray_type="UNOBTANIUM",
            )

    def test_lowercase_hex_color_accepted(self) -> None:
        """Builder should accept both uppercase and lowercase hex."""
        body = _body(
            commands.ams_filament_setting(
                ams_id=0, tray_id=0, tray_info_idx="",
                tray_color="ffffffff",
                nozzle_temp_min=190, nozzle_temp_max=230, tray_type="PLA",
            ),
            "print",
        )
        assert body["tray_color"] == "ffffffff"


class TestAmsGetRfid:
    """ams_get_rfid: slot identity passed through; negative indices rejected."""

    def test_valid_payload(self) -> None:
        body = _body(commands.ams_get_rfid(ams_id=0, slot_id=2), "print")
        assert body["command"] == "ams_get_rfid"
        assert body["ams_id"] == 0
        assert body["slot_id"] == 2

    def test_negative_ams_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="ams_id"):
            commands.ams_get_rfid(ams_id=-1, slot_id=0)

    def test_ams_id_above_max_rejected(self) -> None:
        with pytest.raises(ValueError, match="ams_id"):
            commands.ams_get_rfid(ams_id=commands.AMS_ID_MAX + 1, slot_id=0)

    def test_negative_slot_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="slot_id"):
            commands.ams_get_rfid(ams_id=0, slot_id=-1)


class TestAmsFilamentDrying:
    """ams_filament_drying: parameter validation."""

    def test_valid_payload(self) -> None:
        body = _body(
            commands.ams_filament_drying(
                ams_id=0, temp=55, cooling_temp=35, duration=240, humidity=15
            ),
            "print",
        )
        assert body["command"] == "ams_filament_drying"
        assert body["temp"] == 55
        assert body["duration"] == 240

    def test_zero_temp_rejected(self) -> None:
        with pytest.raises(ValueError, match="temp"):
            commands.ams_filament_drying(
                ams_id=0, temp=0, cooling_temp=30, duration=120, humidity=20
            )

    def test_zero_duration_rejected(self) -> None:
        with pytest.raises(ValueError, match="duration"):
            commands.ams_filament_drying(
                ams_id=0, temp=55, cooling_temp=30, duration=0, humidity=20
            )

    def test_humidity_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="humidity"):
            commands.ams_filament_drying(
                ams_id=0, temp=55, cooling_temp=30, duration=120, humidity=101
            )

    def test_negative_cooling_temp_rejected(self) -> None:
        with pytest.raises(ValueError, match="cooling_temp"):
            commands.ams_filament_drying(
                ams_id=0, temp=55, cooling_temp=-1, duration=120, humidity=20
            )

    def test_rotate_tray_default_false(self) -> None:
        body = _body(
            commands.ams_filament_drying(
                ams_id=0, temp=55, cooling_temp=30, duration=120, humidity=20
            ),
            "print",
        )
        assert body["rotate_tray"] is False

    def test_temp_at_ceiling_accepted(self) -> None:
        """Drying temp at the AMS_DRYING_MAX_TEMP_C ceiling is accepted."""
        body = _body(
            commands.ams_filament_drying(
                ams_id=0, temp=commands.AMS_DRYING_MAX_TEMP_C,
                cooling_temp=30, duration=120, humidity=20
            ),
            "print",
        )
        assert body["temp"] == commands.AMS_DRYING_MAX_TEMP_C

    def test_temp_above_ceiling_rejected(self) -> None:
        """Drying temp above the ceiling must be rejected."""
        with pytest.raises(ValueError, match="ceiling"):
            commands.ams_filament_drying(
                ams_id=0, temp=commands.AMS_DRYING_MAX_TEMP_C + 1,
                cooling_temp=30, duration=120, humidity=20
            )

    def test_ams_id_at_max_accepted(self) -> None:
        """ams_id at AMS_ID_MAX (3) is accepted."""
        body = _body(
            commands.ams_filament_drying(
                ams_id=commands.AMS_ID_MAX, temp=55, cooling_temp=30, duration=120, humidity=20
            ),
            "print",
        )
        assert body["ams_id"] == commands.AMS_ID_MAX

    def test_ams_id_above_max_rejected(self) -> None:
        """ams_id above AMS_ID_MAX is rejected."""
        with pytest.raises(ValueError, match="ams_id"):
            commands.ams_filament_drying(
                ams_id=commands.AMS_ID_MAX + 1, temp=55,
                cooling_temp=30, duration=120, humidity=20
            )


class TestAmsUserSetting:
    """ams_user_setting: booleans forwarded correctly."""

    def test_valid_payload(self) -> None:
        body = _body(
            commands.ams_user_setting(
                ams_id=0,
                startup_read_option=True,
                tray_read_option=False,
            ),
            "print",
        )
        assert body["command"] == "ams_user_setting"
        assert body["startup_read_option"] is True
        assert body["tray_read_option"] is False

    def test_negative_ams_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="ams_id"):
            commands.ams_user_setting(ams_id=-1, startup_read_option=True, tray_read_option=True)

    def test_ams_id_above_max_rejected(self) -> None:
        with pytest.raises(ValueError, match="ams_id"):
            commands.ams_user_setting(
                ams_id=commands.AMS_ID_MAX + 1,
                startup_read_option=True,
                tray_read_option=True,
            )


# ---------------------------------------------------------------------------
# Wave-2: set_accessories_nozzle
# ---------------------------------------------------------------------------


class TestSetAccessoriesNozzle:
    """set_accessories_nozzle: system category; validated enum values."""

    def test_stainless_steel_payload(self) -> None:
        body = _body(
            commands.set_accessories_nozzle(
                nozzle_type="stainless_steel", nozzle_diameter=0.4
            ),
            "system",
        )
        assert body["command"] == "set_accessories"
        assert body["accessory_type"] == "nozzle"
        assert body["nozzle_type"] == "stainless_steel"
        assert body["nozzle_diameter"] == 0.4

    def test_hardened_steel_payload(self) -> None:
        body = _body(
            commands.set_accessories_nozzle(
                nozzle_type="hardened_steel", nozzle_diameter=0.6
            ),
            "system",
        )
        assert body["nozzle_type"] == "hardened_steel"

    def test_invalid_nozzle_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="nozzle_type"):
            commands.set_accessories_nozzle(nozzle_type="titanium", nozzle_diameter=0.4)

    def test_invalid_diameter_rejected(self) -> None:
        with pytest.raises(ValueError, match="nozzle_diameter"):
            commands.set_accessories_nozzle(nozzle_type="stainless_steel", nozzle_diameter=0.3)

    def test_all_valid_diameters_accepted(self) -> None:
        for d in commands.NOZZLE_DIAMETERS:
            env = commands.set_accessories_nozzle(
                nozzle_type="stainless_steel", nozzle_diameter=d
            )
            assert "system" in env


# ---------------------------------------------------------------------------
# Wave-2: calibration builder
# ---------------------------------------------------------------------------


class TestCalibration:
    """calibration: any non-zero combination of bits 0-2 accepted (matrix §8).

    Valid: 1 (vibration), 2 (bed), 3 (vibration+bed), 4 (flow),
           5 (vibration+flow), 6 (bed+flow), 7 (all three).
    Invalid: 0 (no-op), 8+ (bits 3+, X1-only LIDAR).
    """

    def test_vibration_only(self) -> None:
        body = _body(commands.calibration(1), "print")
        assert body["command"] == "calibration"
        assert body["option"] == 1

    def test_bed_level_only(self) -> None:
        body = _body(commands.calibration(2), "print")
        assert body["option"] == 2

    def test_vibration_and_bed(self) -> None:
        """Combination 3 = vibration + bed leveling — previously rejected, now accepted."""
        body = _body(commands.calibration(3), "print")
        assert body["option"] == 3

    def test_flow_calibration_only(self) -> None:
        body = _body(commands.calibration(4), "print")
        assert body["option"] == 4

    def test_vibration_and_flow(self) -> None:
        """Combination 5 = vibration + flow calibration — previously rejected, now accepted."""
        body = _body(commands.calibration(5), "print")
        assert body["option"] == 5

    def test_bed_and_flow(self) -> None:
        """Combination 6 = bed leveling + flow calibration — previously rejected, now accepted."""
        body = _body(commands.calibration(6), "print")
        assert body["option"] == 6

    def test_all_calibrations(self) -> None:
        body = _body(commands.calibration(7), "print")
        assert body["option"] == 7

    def test_bed_type_default_is_1(self) -> None:
        body = _body(commands.calibration(2), "print")
        assert body["bed_type"] == 1

    def test_custom_bed_type(self) -> None:
        body = _body(commands.calibration(2, bed_type=2), "print")
        assert body["bed_type"] == 2

    @pytest.mark.parametrize("bad_option", [0, 8, 16, 255])
    def test_non_p1s_bits_rejected(self, bad_option: int) -> None:
        with pytest.raises(ValueError, match="P1S-CONTROL-MATRIX"):
            commands.calibration(bad_option)

    def test_error_message_names_the_matrix(self) -> None:
        with pytest.raises(ValueError, match="P1S-CONTROL-MATRIX"):
            commands.calibration(8)

    def test_zero_rejected(self) -> None:
        """0 is a no-op and must be rejected explicitly."""
        with pytest.raises(ValueError, match="P1S-CONTROL-MATRIX"):
            commands.calibration(0)


# ---------------------------------------------------------------------------
# Wave-3: extrude/retract builder
# ---------------------------------------------------------------------------


class TestExtrude:
    """extrude: bounds, feedrate whitelist, sign convention."""

    def test_positive_extrudes(self) -> None:
        body = _body(commands.extrude(10.0), "print")
        assert "M83" in body["param"]
        assert "G1 E10.0" in body["param"]
        assert "M82" in body["param"]

    def test_negative_retracts(self) -> None:
        body = _body(commands.extrude(-5.0), "print")
        assert "G1 E-5.0" in body["param"]

    def test_zero_distance_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-zero"):
            commands.extrude(0.0)

    def test_over_max_rejected(self) -> None:
        with pytest.raises(ValueError, match="100"):
            commands.extrude(101.0)

    def test_at_max_accepted(self) -> None:
        env = commands.extrude(100.0)
        assert "G1 E100.0" in env["print"]["param"]

    def test_negative_at_max_accepted(self) -> None:
        env = commands.extrude(-100.0)
        assert "G1 E-100.0" in env["print"]["param"]

    def test_over_max_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="100"):
            commands.extrude(-100.1)

    def test_default_feedrate_is_300(self) -> None:
        body = _body(commands.extrude(5.0), "print")
        assert "F300" in body["param"]

    def test_custom_feedrate_in_whitelist(self) -> None:
        for fr in commands.EXTRUDE_FEEDRATE_WHITELIST:
            body = _body(commands.extrude(5.0, feedrate=fr), "print")
            assert f"F{fr}" in body["param"]

    def test_feedrate_not_in_whitelist_rejected(self) -> None:
        with pytest.raises(ValueError, match="feedrate"):
            commands.extrude(5.0, feedrate=500)

    def test_gcode_uses_relative_then_absolute_mode(self) -> None:
        """M83 (relative E) before, M82 (absolute E) after."""
        body = _body(commands.extrude(5.0), "print")
        param = body["param"]
        m83_pos = param.index("M83")
        g1_pos = param.index("G1 E")
        m82_pos = param.index("M82")
        assert m83_pos < g1_pos < m82_pos


# ---------------------------------------------------------------------------
# Wave-3: steppers_off builder
# ---------------------------------------------------------------------------


class TestSteppersOff:
    """steppers_off: single M84 gcode_line in print category."""

    def test_payload(self) -> None:
        body = _body(commands.steppers_off(), "print")
        assert body["command"] == "gcode_line"
        assert "M84" in body["param"]

    def test_is_gcode_line(self) -> None:
        env = commands.steppers_off()
        assert env["print"]["command"] == "gcode_line"


# ---------------------------------------------------------------------------
# Wave-3: raw_gcode_enabled gate
# ---------------------------------------------------------------------------


class TestRawGcodeGate:
    """raw_gcode_enabled(): checks BRIDGE_ENABLE_RAW_GCODE env var."""

    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRIDGE_ENABLE_RAW_GCODE", raising=False)
        assert not commands.raw_gcode_enabled()

    def test_enabled_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_ENABLE_RAW_GCODE", "1")
        assert commands.raw_gcode_enabled()

    def test_empty_string_is_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_ENABLE_RAW_GCODE", "")
        assert not commands.raw_gcode_enabled()

    def test_whitespace_only_is_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRIDGE_ENABLE_RAW_GCODE", "   ")
        assert not commands.raw_gcode_enabled()
