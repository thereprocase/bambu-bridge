"""Client-only inventory identity, delta coherence and command containment."""

import copy
from dataclasses import replace

import pytest

from bambu_bridge.native_spools import (
    NativeSpoolOverlay,
    VirtualSpool,
    VirtualSpoolStateUnavailable,
    physical_identity,
)


def snapshot():
    return {
        "print": {
            "command": "push_status",
            "gcode_state": "IDLE",
            "ams": {
                "ams_exist_bits": "1",
                "tray_exist_bits": "7",
                "tray_read_done_bits": "7",
                "tray_reading_bits": "0",
                "calibrate_remain_flag": False,
                "ams": [
                    {
                        "id": "0",
                        "humidity": "3",
                        "tray": [
                            {
                                "id": "0",
                                "tray_type": "ASA",
                                "tray_info_idx": "GFB98",
                                "tray_color": "000000FF",
                                "tag_uid": "0",
                                "remain": -1,
                            },
                            {
                                "id": "1",
                                "tray_type": "ASA",
                                "tray_info_idx": "GFB61",
                                "tray_color": "FF8000FF",
                                "tag_uid": "1234567890ABCDEF",
                                "remain": 62,
                            },
                            {
                                "id": "2",
                                "tray_type": "PETG",
                                "tray_info_idx": "GFG99",
                                "tray_color": "00FF00FF",
                                "tag_uid": "0",
                                "remain": -1,
                            },
                        ],
                    }
                ],
            },
            "filam_bak": [5],
            "tray_now": "0",
        }
    }


def trays(message):
    return message["print"]["ams"]["ams"][0]["tray"]


def spool(raw=None, **changes):
    raw = raw or snapshot()
    return replace(
        VirtualSpool(
            provider="provider-a",
            spool_id=43,
            wire_slot=0,
            material_id="GFB61",
            material="ASA",
            color="000000FF",
            nominal_g=1000,
            remaining_g=436,
            observed_identity=physical_identity(trays(raw)[0]),
        ),
        **changes,
    )


def overlay(raw=None):
    result = NativeSpoolOverlay("bridge-a", (spool(raw),))
    result.enabled = True
    return result


def test_projection_keeps_raw_printer_state_and_real_rfid_unchanged():
    raw = snapshot()
    before = copy.deepcopy(raw)
    result = overlay(raw).project(raw, raw)
    first = trays(result)[0]
    assert first["tray_info_idx"] == "GFB61"
    assert first["remain"] == 44
    assert first["tag_uid"] != "0"
    assert result["print"]["ams"]["calibrate_remain_flag"] is True
    assert trays(result)[1] == trays(raw)[1] | {"tray_uuid": "0" * 32}
    assert trays(result)[2]["tag_uid"] == "0"
    assert trays(result)[3]["tray_type"] == ""
    assert result["print"]["filam_bak"] == [5]
    assert result["print"]["ams"]["tray_exist_bits"] == "7"
    assert result["print"]["ams"]["tray_read_done_bits"] == "7"
    assert raw == before
    first["tray_color"] = "changed"
    assert raw == before


@pytest.mark.parametrize(
    "weight,expected", [(0, 0), (400, 40), (436, 44), (1000, 100), (1030, 100), (None, -1)]
)
def test_display_mass_uses_nominal_capacity_without_losing_quantity(weight, expected):
    binding = spool(remaining_g=weight)
    assert binding.remaining_percent == expected
    assert binding.remaining_g == weight


@pytest.mark.parametrize(
    "field,value",
    [
        ("remaining_g", -1),
        ("remaining_g", float("nan")),
        ("remaining_g", True),
        ("nominal_g", 0),
        ("nominal_g", float("inf")),
        ("spool_id", True),
        ("wire_slot", 16),
        ("color", "#000000"),
        ("material_id", "contains spaces"),
    ],
)
def test_invalid_inventory_never_enters_wire_projection(field, value):
    with pytest.raises(ValueError):
        spool(**{field: value})


def test_virtual_identity_follows_spool_not_slot_and_changes_with_provider():
    binding = spool()
    uid = binding.tag_uid("bridge-a")
    assert len(uid) == 16 and uid[12:14] == "01"
    assert replace(binding, wire_slot=3).tag_uid("bridge-a") == uid
    assert replace(binding, provider="provider-b").tag_uid("bridge-a") != uid
    assert binding.tag_uid("bridge-b") != uid
    with pytest.raises(ValueError):
        NativeSpoolOverlay("bridge-a", (binding, replace(binding, wire_slot=3)))


@pytest.mark.parametrize(
    "partial",
    [
        {"nozzle_temper": 23},
        {"ams": {"ams": [{"id": "0", "tray": [{"id": "0", "remain": 20}]}]}},
        {"ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "ASA"}]}]}},
        {},
    ],
)
def test_partial_status_and_inventory_only_updates_keep_coherent_identity(partial):
    raw = snapshot()
    projection = overlay(raw)
    projection.project(raw, raw)
    # Native snapshot merges nested dicts but tray arrays replace. The projection
    # may fill identified partial rows from its own physical-only cache.
    if "ams" in partial:
        raw["print"]["ams"]["ams"] = partial["ams"]["ams"]
    result = projection.project({"print": {"command": "push_status", **partial}}, raw)
    first = trays(result)[0]
    assert first["tray_type"] == "ASA" and first["tray_info_idx"] == "GFB61"
    assert first["remain"] == 44 and first["tag_uid"] != "0"
    assert trays(result)[1]["tag_uid"] == "1234567890ABCDEF"


def test_disabling_and_unassigning_emit_explicit_raw_restoration():
    raw = snapshot()
    projection = overlay(raw)
    projection.project(raw, raw)
    projection.enabled = False
    result = projection.project({"print": {"command": "push_status"}}, raw)
    first = trays(result)[0]
    assert first["tag_uid"] == "0" and first["tray_uuid"] == "0" * 32
    assert first["tray_info_idx"] == "GFB98" and first["remain"] == -1
    assert result["print"]["ams"]["calibrate_remain_flag"] is False
    projection.enabled = True
    projection.replace_bindings(())
    assert trays(projection.project(raw, raw)) == trays(result)


def test_physical_removal_then_reinsertion_cannot_resurrect_old_assignment():
    raw = snapshot()
    projection = overlay(raw)
    projection.project(raw, raw)
    raw["print"]["ams"]["tray_exist_bits"] = "6"
    result = projection.project(raw, raw)
    assert trays(result)[0]["tag_uid"] == "0"
    assert trays(result)[0]["tray_type"] == ""
    raw["print"]["ams"]["tray_exist_bits"] = "7"
    raw["print"]["ams"]["ams"][0]["tray"][0] = {"id": "0"}
    assert trays(projection.project(raw, raw))[0]["tag_uid"] == "0"


@pytest.mark.parametrize(
    "change",
    [
        {"tag_uid": "1111111111111111"},
        {"tray_type": "PETG"},
        {"tray_color": "FF0000FF"},
        {"tray_info_idx": "different"},
    ],
)
def test_real_tag_or_changed_physical_declaration_suppresses_virtual_identity(change):
    raw = snapshot()
    projection = overlay(raw)
    projection.project(raw, raw)
    trays(raw)[0].update(change)
    result = projection.project(raw, raw)
    assert trays(result)[0]["tray_info_idx"] == trays(raw)[0]["tray_info_idx"]
    assert trays(result)[0]["tag_uid"] == trays(raw)[0]["tag_uid"]


def test_bad_layout_fails_closed_and_command_acks_are_not_status():
    raw = snapshot()
    projection = overlay(raw)
    raw["print"]["ams"]["tray_exist_bits"] = "not hex"
    assert projection.project(raw, raw) == raw
    ack = {"print": {"command": "project_file", "result": "success"}}
    assert projection.project(ack, snapshot()) == ack


@pytest.mark.parametrize(
    "command",
    [
        {"command": "ams_filament_setting", "ams_id": 0, "slot_id": 0},
        {"command": "ams_filament_setting", "ams_id": -1, "slot_id": 0},
        {"command": "ams_get_rfid"},
        {"command": "ams_user_setting"},
        {"command": "ams_calibrate"},
        {"command": "print_option", "calibrate_remain_flag": True},
        # Observed from stock Orca 2.4.2: Confirm and Reset in the virtual
        # spool's AMS Materials Setting dialog.
        {"command": "extrusion_cali_set", "tray_id": 0, "k_value": 0.0, "n_coef": 1.4},
        {"command": "extrusion_cali_set", "tray_id": 0, "k_value": 0.02, "n_coef": 1.4},
        {"command": "extrusion_cali_sel", "tray_id": 0, "cali_idx": 3},
        {"command": "extrusion_cali_sel", "ams_id": 0, "slot_id": 0, "cali_idx": 3},
        {"command": "extrusion_cali_set", "k_value": 0.02},
        {"command": "extrusion_cali_set", "tray_id": 99, "k_value": 0.02},
    ],
)
def test_material_and_rfid_setting_requests_receive_honest_local_rejection(command):
    payload = {"print": command | {"sequence_id": "17"}}
    response = overlay().command_response(payload, snapshot())
    assert response["print"]["result"] == "FAIL"
    assert response["print"]["sequence_id"] == "17"
    assert response["print"]["reason"] == "BBVIRTUAL_SPOOL_READ_ONLY"


@pytest.mark.parametrize(
    "command",
    [
        {"command": "print_option", "auto_switch_filament": True},
        {"command": "project_file", "ams_mapping": [0]},
        {"command": "pushall"},
        {"command": "ams_filament_setting", "ams_id": 0, "slot_id": 2},
        {"command": "extrusion_cali_set", "tray_id": 2, "k_value": 0.02},
        {"command": "extrusion_cali_sel", "ams_id": 0, "slot_id": 2, "cali_idx": 1},
        {"command": "extrusion_cali_set", "tray_id": 254, "k_value": 0.02},
        {"command": "extrusion_cali_set", "tray_id": "5", "k_value": 0.02},
    ],
)
def test_real_controls_and_refill_toggle_remain_unmodified(command):
    assert overlay().command_response({"print": command}, snapshot()) is None


def test_disabled_overlay_does_not_block_material_commands():
    projection = overlay()
    projection.enabled = False
    assert (
        projection.command_response({"print": {"command": "ams_user_setting"}}, snapshot()) is None
    )


def test_complete_same_material_reinsertion_requires_explicit_confirmation():
    raw = snapshot()
    projection = overlay(raw)
    original = copy.deepcopy(raw)
    # Central observer runs even with no subscribed client and sees rapid edges.
    projection.observe_physical(raw)
    raw["print"]["ams"]["tray_exist_bits"] = "6"
    projection.observe_physical(raw)
    projection.observe_physical(original)
    assert trays(projection.project(original, original))[0]["tag_uid"] == "0"
    projection.replace_bindings((spool(original, remaining_g=400),))
    assert trays(projection.project(original, original))[0]["tag_uid"] == "0"
    projection.replace_bindings((spool(original),), confirm=True)
    assert trays(projection.project(original, original))[0]["remain"] == 44


@pytest.mark.parametrize("state", ["disabled", "removed", "malformed"])
def test_delayed_synthetic_writes_stay_blocked_until_client_reconnect(state):
    raw = snapshot()
    projection = overlay(raw)
    client_slots = set()
    projection.project(raw, raw, projected_slots=client_slots)
    if state == "disabled":
        projection.enabled = False
    elif state == "removed":
        raw["print"]["ams"]["tray_exist_bits"] = "6"
    else:
        raw = {}
    command = {"print": {"command": "ams_filament_setting", "ams_id": 0, "slot_id": 0}}
    assert projection.command_response(command, raw, protected_slots=client_slots) is not None
    if state == "disabled":
        # A new native connection has never seen the virtual identity.
        assert projection.command_response(command, raw, protected_slots=set()) is None


def test_disconnected_or_invalid_source_closes_a_previously_projected_client():
    raw = snapshot()
    projection = overlay(raw)
    projection.project(raw, raw)
    with pytest.raises(VirtualSpoolStateUnavailable, match="DISCONNECTED"):
        projection.project(raw, raw, connected=False)
    assert trays(projection.project(raw, raw))[0]["tag_uid"] == "0"
    with pytest.raises(VirtualSpoolStateUnavailable, match="RESYNC"):
        projection.project({"print": {"command": "push_status"}}, {})


def test_absent_original_estimation_flag_is_explicitly_restored_to_default():
    raw = snapshot()
    del raw["print"]["ams"]["calibrate_remain_flag"]
    projection = overlay(raw)
    assert projection.project(raw, raw)["print"]["ams"]["calibrate_remain_flag"] is True
    projection.enabled = False
    assert projection.project(raw, raw)["print"]["ams"]["calibrate_remain_flag"] is False


def test_targeted_rfid_reread_of_genuine_slot_is_not_blocked():
    command = {"print": {"command": "ams_get_rfid", "ams_id": 0, "slot_id": 1}}
    assert overlay().command_response(command, snapshot()) is None
