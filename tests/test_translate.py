"""Pure snapshot translator tests — contract §6 shape conformance.

Covers the war-council criticals: the §6.0.1 phase rule (the single most
important rule in the doc), AMS dual-emission, cooling_fan native→percent,
print_error → HMS lookup, _raw passthrough, and partial-state tolerance.
"""

from __future__ import annotations

from typing import Any

import pytest

from bambu_bridge.translate import SnapshotContext, _started_at_iso, translate_snapshot


def _ctx(**overrides: Any) -> SnapshotContext:
    defaults: dict[str, Any] = {
        "printer_id": "01P00A3C00000001",
        "serial": "01P00A3C00000001",
        "friendly_name": "Workshop P1S",
        "model": "P1S",
        "connected": True,
        "last_telemetry_at": "2026-05-20T03:14:15Z",
        "last_connect_attempt": "2026-05-20T03:09:01Z",
        "last_failure_phase": None,
        "cert_status": "trusted",
        "expected_fingerprint": "ab" * 32,
    }
    defaults.update(overrides)
    return SnapshotContext(**defaults)


# --------------------------------------------------------------------------- #
# §6.0.1 — the phase rule
# --------------------------------------------------------------------------- #


def test_running_with_layer_zero_is_preparing_not_printing() -> None:
    """The single most important rule in the contract."""
    raw = {"gcode_state": "RUNNING", "layer_num": 0, "mc_percent": 32}
    out = translate_snapshot(raw, _ctx())
    assert out["phase"] == "preparing"
    # The §6.3 trap: mc_percent advances during heat-soak. NEVER expose it.
    assert out["job"]["percent"] is None


def test_running_with_positive_layer_is_printing() -> None:
    raw = {"gcode_state": "RUNNING", "layer_num": 1, "mc_percent": 0}
    out = translate_snapshot(raw, _ctx())
    assert out["phase"] == "printing"
    assert out["job"]["percent"] == 0


def test_printing_exposes_mc_percent() -> None:
    raw = {"gcode_state": "RUNNING", "layer_num": 42, "mc_percent": 17}
    out = translate_snapshot(raw, _ctx())
    assert out["phase"] == "printing"
    assert out["job"]["percent"] == 17


def test_gcode_state_finish_is_completed() -> None:
    raw = {"gcode_state": "FINISH", "layer_num": 200, "total_layer_num": 200}
    out = translate_snapshot(raw, _ctx())
    assert out["phase"] == "completed"


def test_gcode_state_failed_is_failed() -> None:
    raw = {"gcode_state": "FAILED"}
    out = translate_snapshot(raw, _ctx())
    assert out["phase"] == "failed"


def test_gcode_state_pause_is_paused() -> None:
    raw = {"gcode_state": "PAUSE", "layer_num": 30}
    out = translate_snapshot(raw, _ctx())
    assert out["phase"] == "paused"


def test_gcode_state_idle_is_idle() -> None:
    raw = {"gcode_state": "IDLE"}
    out = translate_snapshot(raw, _ctx())
    assert out["phase"] == "idle"


def test_missing_gcode_state_is_unknown() -> None:
    out = translate_snapshot({}, _ctx())
    assert out["phase"] == "unknown"


def test_print_error_during_idle_classifies_as_failed() -> None:
    """A sticky error overrides classification even when gcode_state would say IDLE."""
    raw = {"gcode_state": "IDLE", "mc_print_error_code": "0300_0d00_0003_0001"}
    out = translate_snapshot(raw, _ctx())
    assert out["phase"] == "failed"


# --------------------------------------------------------------------------- #
# Phase reason heuristics
# --------------------------------------------------------------------------- #


def test_preparing_heating_nozzle() -> None:
    raw = {
        "gcode_state": "RUNNING",
        "layer_num": 0,
        "nozzle_temper": 80,
        "nozzle_target_temper": 255,
        "ams": {"tray_now": "1"},
    }
    out = translate_snapshot(raw, _ctx())
    assert out["phase"] == "preparing"
    assert out["phase_reason"] == "heating_nozzle"
    assert "Heating nozzle 80/255" in out["headline"]["subtitle"]


def test_preparing_heating_bed() -> None:
    raw = {
        "gcode_state": "RUNNING",
        "layer_num": 0,
        "nozzle_temper": 250,
        "nozzle_target_temper": 255,
        "bed_temper": 35,
        "bed_target_temper": 70,
        "ams": {"tray_now": "1"},
    }
    out = translate_snapshot(raw, _ctx())
    assert out["phase_reason"] == "heating_bed"


def test_preparing_feed_not_engaged_falls_through_to_heating_or_leveling() -> None:
    """tray_now == 255 + nozzle at temp → leveling (the catch-all)."""
    raw = {
        "gcode_state": "RUNNING",
        "layer_num": 0,
        "nozzle_temper": 255,
        "nozzle_target_temper": 255,
        "bed_temper": 70,
        "bed_target_temper": 70,
        "ams": {"tray_now": "255"},
    }
    out = translate_snapshot(raw, _ctx())
    assert out["phase_reason"] == "leveling"


# --------------------------------------------------------------------------- #
# Headline rendering
# --------------------------------------------------------------------------- #


def test_disconnected_overrides_headline() -> None:
    out = translate_snapshot(
        {"gcode_state": "RUNNING", "layer_num": 50, "mc_percent": 25},
        _ctx(connected=False),
    )
    assert out["headline"]["title"] == "Reconnecting…"
    # Phase is still computed — APK can read it, headline overrides only.
    assert out["phase"] == "printing"


def test_printing_headline_includes_layer_pct_eta() -> None:
    out = translate_snapshot(
        {
            "gcode_state": "RUNNING",
            "layer_num": 42,
            "total_layer_num": 200,
            "mc_percent": 21,
            "mc_remaining_time": 35,
        },
        _ctx(),
    )
    assert out["headline"]["title"] == "Printing"
    assert "Layer 42/200" in out["headline"]["subtitle"]
    assert "21%" in out["headline"]["subtitle"]
    assert "35 min" in out["headline"]["subtitle"]
    assert out["headline"]["indicator"] == "progress"


def test_failed_headline_uses_hms_user_message() -> None:
    out = translate_snapshot(
        {"gcode_state": "FAILED", "mc_print_error_code": "0300_0d00_0003_0001"},
        _ctx(),
    )
    assert out["headline"]["title"] == "Print failed"
    assert "runout" in out["headline"]["subtitle"].lower()
    assert out["headline"]["indicator"] == "red"


def test_idle_headline_is_ready() -> None:
    out = translate_snapshot({"gcode_state": "IDLE"}, _ctx())
    assert out["headline"]["title"] == "Ready"
    assert out["headline"]["indicator"] == "none"


# --------------------------------------------------------------------------- #
# AMS — dual-emission per §6.1
# --------------------------------------------------------------------------- #


def test_ams_engaged_slot_translations() -> None:
    """255 → None, 254 → "external", 0 → 1, 3 → 4."""
    for raw_val, expected in [("255", None), ("254", "external"), ("0", 1), ("3", 4)]:
        out = translate_snapshot({"ams": {"tray_now": raw_val}}, _ctx())
        assert out["ams"]["engaged_slot"] == expected, raw_val


def test_ams_slots_physical_and_raw_id_both_present() -> None:
    raw = {
        "ams": {
            "ams": [
                {
                    "tray": [
                        {"id": "0", "tray_type": "ASA", "tray_color": "1E88E5FF", "remain": 41},
                        {"id": "1", "tray_type": "PETG", "tray_color": "212121"},
                        {"id": "2", "tray_type": "", "tray_color": ""},  # empty
                        {"id": "3", "tray_type": "PLA", "tray_color": "FAFAFAFF"},
                    ]
                }
            ]
        }
    }
    out = translate_snapshot(raw, _ctx())
    slots = out["ams"]["slots"]
    assert len(slots) == 4
    # Slot 1 = physical, _raw_id = 0
    assert slots[0]["physical_slot"] == 1
    assert slots[0]["_raw_id"] == 0
    assert slots[0]["type"] == "ASA"
    assert slots[0]["color"] == "#1E88E5"  # alpha stripped, # added, uppercase
    assert slots[0]["state"] == "loaded"
    assert slots[0]["remaining_pct"] == 41  # remain<=100 means it's already a %
    # Slot 3 (raw_id 2) is empty
    assert slots[2]["state"] == "empty"
    assert slots[2]["remaining_g"] is None
    assert slots[2]["remaining_pct"] is None
    # Slot 4 = physical, _raw_id = 3
    assert slots[3]["physical_slot"] == 4
    assert slots[3]["_raw_id"] == 3


def test_ams_color_strips_alpha_and_adds_hash() -> None:
    raw = {"ams": {"ams": [{"tray": [{"id": "0", "tray_type": "PLA", "tray_color": "ff0000ff"}]}]}}
    out = translate_snapshot(raw, _ctx())
    assert out["ams"]["slots"][0]["color"] == "#FF0000"


def test_ams_invalid_color_is_none() -> None:
    raw = {"ams": {"ams": [{"tray": [{"id": "0", "tray_type": "PLA", "tray_color": "garbage"}]}]}}
    out = translate_snapshot(raw, _ctx())
    assert out["ams"]["slots"][0]["color"] is None


def test_ams_missing_block_returns_empty_shape() -> None:
    out = translate_snapshot({"gcode_state": "IDLE"}, _ctx())
    assert out["ams"]["present"] is False
    assert out["ams"]["engaged_slot"] is None
    assert out["ams"]["slots"] == []
    assert out["ams"]["external_spool"] == {
        "in_use": False, "type": None, "color": None, "_raw_id": 254
    }


def test_ams_present_true_with_settled_slots() -> None:
    """ams_exist_bits nonzero + a full tray[] → present, slots populated.

    The clean settled state: 4 slots, hardware attached.
    """
    raw = {
        "ams": {
            "ams_exist_bits": "1",
            "tray_now": "0",
            "ams": [
                {
                    "tray": [
                        {"id": "0", "tray_type": "ASA", "tray_color": "1E88E5FF"},
                        {"id": "1", "tray_type": "PETG", "tray_color": "212121"},
                        {"id": "2", "tray_type": "ASA", "tray_color": "E53935"},
                        {"id": "3", "tray_type": "ASA", "tray_color": "FAFAFAFF"},
                    ]
                }
            ],
        }
    }
    out = translate_snapshot(raw, _ctx())
    assert out["ams"]["present"] is True
    assert len(out["ams"]["slots"]) == 4


def test_ams_present_true_but_slots_empty_is_rescan_transient() -> None:
    """The RFID re-scan window: hardware stays attached (ams_exist_bits "1")
    while ams.ams transiently empties to [].

    `present && slots == []` is the signal clients use to hold the previous
    slot view rather than latch "No AMS detected".
    """
    raw = {"ams": {"ams_exist_bits": "1", "tray_now": "0", "ams": []}}
    out = translate_snapshot(raw, _ctx())
    assert out["ams"]["present"] is True
    assert out["ams"]["slots"] == []


def test_ams_absent_when_exist_bits_missing_or_zero() -> None:
    """No AMS hardware: absent key, empty, or all-zero bitmask → present False."""
    for bits in (None, "", "0", "0x0", "00"):
        ams: dict[str, object] = {"tray_now": "255"}
        if bits is not None:
            ams["ams_exist_bits"] = bits
        out = translate_snapshot({"ams": ams}, _ctx())
        assert out["ams"]["present"] is False, bits


def test_ams_present_accepts_hex_prefixed_and_multibit() -> None:
    """ams_exist_bits is hex-ish: '0x3' / '4' (multi-unit) are still present."""
    for bits in ("0x1", "0x3", "4", "f"):
        out = translate_snapshot(
            {"ams": {"ams_exist_bits": bits, "tray_now": "255"}}, _ctx()
        )
        assert out["ams"]["present"] is True, bits


def test_ams_tagless_slots_have_no_name_source() -> None:
    """Tagless spools (zeroed tray_uuid, empty tray_id_name): rfid_tray is null;
    the slot is renderable from type + color only."""
    raw = {
        "ams": {
            "ams_exist_bits": "1",
            "ams": [
                {
                    "tray": [
                        {
                            "id": "0",
                            "tray_type": "ASA",
                            "tray_color": "1E88E5FF",
                            "tray_uuid": "00000000000000000000000000000000",
                            "tray_id_name": "",
                        }
                    ]
                }
            ],
        }
    }
    out = translate_snapshot(raw, _ctx())
    slot = out["ams"]["slots"][0]
    assert slot["rfid_tray"] is None
    assert slot["type"] == "ASA"
    assert slot["color"] == "#1E88E5"


def test_ams_external_spool_in_use_when_tray_now_254() -> None:
    raw = {
        "ams": {
            "tray_now": "254",
            "vt_tray": {"tray_type": "PLA", "tray_color": "00FF00"},
        }
    }
    out = translate_snapshot(raw, _ctx())
    assert out["ams"]["external_spool"]["in_use"] is True
    assert out["ams"]["external_spool"]["type"] == "PLA"
    assert out["ams"]["external_spool"]["color"] == "#00FF00"


# --------------------------------------------------------------------------- #
# Cooling — native 0-15 → percent
# --------------------------------------------------------------------------- #


def test_cooling_fan_zero_native_zero_percent() -> None:
    out = translate_snapshot({"cooling_fan_speed": "0"}, _ctx())
    assert out["cooling"]["part_fan"] == {"percent": 0, "_raw": "0"}


def test_cooling_fan_fifteen_native_one_hundred_percent() -> None:
    out = translate_snapshot({"big_fan1_speed": "15"}, _ctx())
    assert out["cooling"]["aux_fan"]["percent"] == 100
    assert out["cooling"]["aux_fan"]["_raw"] == "15"


def test_cooling_fan_eight_native_rounds_to_53() -> None:
    # 8 / 15 * 100 = 53.33… → 53
    out = translate_snapshot({"big_fan2_speed": "8"}, _ctx())
    assert out["cooling"]["chamber_fan"]["percent"] == 53


def test_cooling_fan_invalid_value() -> None:
    out = translate_snapshot({"cooling_fan_speed": "garbage"}, _ctx())
    assert out["cooling"]["part_fan"]["percent"] is None
    assert out["cooling"]["part_fan"]["_raw"] == "garbage"


def test_cooling_fan_missing() -> None:
    out = translate_snapshot({}, _ctx())
    assert out["cooling"]["part_fan"] == {"percent": None, "_raw": None}


# --------------------------------------------------------------------------- #
# print_error → HMS
# --------------------------------------------------------------------------- #


def test_print_error_zero_is_null() -> None:
    out = translate_snapshot({"print_error": 0}, _ctx())
    assert out["print_error"] is None


def test_print_error_known_code_decoded() -> None:
    out = translate_snapshot(
        {"mc_print_error_code": "0300_0d00_0003_0001"}, _ctx()
    )
    assert out["print_error"] is not None
    assert out["print_error"]["severity"] == "warn"
    assert out["print_error"]["category"] == "ams"
    assert "runout" in out["print_error"]["text"].lower()


def test_print_error_unmapped_code_falls_through() -> None:
    out = translate_snapshot({"mc_print_error_code": "dead_beef_dead_beef"}, _ctx())
    assert out["print_error"]["severity"] == "unknown"
    assert out["print_error"]["category"] == "unmapped"


# --------------------------------------------------------------------------- #
# Session + cert keys preserved
# --------------------------------------------------------------------------- #


def test_session_block_carries_all_health_fields() -> None:
    out = translate_snapshot(
        {"gcode_state": "IDLE"},
        _ctx(
            last_telemetry_at="2026-05-20T03:14:15Z",
            last_connect_attempt="2026-05-20T03:09:01Z",
            last_failure_phase="mqtt_connack",
            connected=False,
        ),
    )
    assert out["session"] == {
        "connected": False,
        "last_telemetry_at": "2026-05-20T03:14:15Z",
        "last_connect_attempt": "2026-05-20T03:09:01Z",
        "last_failure_phase": "mqtt_connack",
    }


def test_cert_keys_round_trip_through_translator() -> None:
    """cert_status survives the snapshot rewrite; expected_fingerprint is NOT emitted.

    PR A.2 stores expected_fingerprint on PrinterService for TOFU decisions
    (cert_gate, trust endpoint). It is intentionally kept off the public
    snapshot so the fingerprint value never leaks to API clients — only the
    derived cert_status enum does.
    """
    out = translate_snapshot(
        {"gcode_state": "IDLE"},
        _ctx(cert_status="changed", expected_fingerprint="cafe" * 16),
    )
    assert out["cert_status"] == "changed"
    # The raw fingerprint is NOT a snapshot output key — only cert_status is.
    assert "expected_fingerprint" not in out


# --------------------------------------------------------------------------- #
# _raw passthrough
# --------------------------------------------------------------------------- #


def test_raw_block_preserves_critical_debug_keys() -> None:
    raw = {
        "gcode_state": "RUNNING",
        "layer_num": 0,
        "mc_percent": 32,  # the §6.3 lie
        "print_error": 0,
        "info": {"firmware_ver": "01.07.00.00"},
        "mc_print": {"command": "extrude"},
    }
    out = translate_snapshot(raw, _ctx())
    assert out["_raw"]["gcode_state"] == "RUNNING"
    assert out["_raw"]["mc_percent"] == 32
    assert out["_raw"]["info"]["firmware_ver"] == "01.07.00.00"


# --------------------------------------------------------------------------- #
# Partial state tolerance
# --------------------------------------------------------------------------- #


def test_empty_raw_produces_full_shape() -> None:
    """A pre-pushall snapshot — every block exists with nulls/defaults."""
    out = translate_snapshot({}, _ctx())
    assert set(out.keys()) >= {
        "printer_id", "serial", "friendly_name", "model",
        "session", "cert_status",
        "phase", "phase_reason", "headline", "job",
        "temps", "cooling", "lights", "print_params", "motion",
        "ams", "print_error", "_raw",
    }
    # expected_fingerprint is a service-internal TOFU field — never in output.
    assert "expected_fingerprint" not in out
    assert out["phase"] == "unknown"
    assert out["job"]["percent"] is None


def test_lights_chamber_on() -> None:
    out = translate_snapshot(
        {"lights_report": [{"node": "chamber_light", "mode": "on"}]}, _ctx()
    )
    assert out["lights"]["chamber_on"] is True


def test_lights_chamber_off() -> None:
    out = translate_snapshot(
        {"lights_report": [{"node": "chamber_light", "mode": "off"}]}, _ctx()
    )
    assert out["lights"]["chamber_on"] is False


def test_lights_missing_array() -> None:
    out = translate_snapshot({}, _ctx())
    assert out["lights"]["chamber_on"] is None


def test_temps_partial() -> None:
    out = translate_snapshot({"nozzle_temper": "210.5"}, _ctx())
    assert out["temps"]["nozzle"]["current_c"] == pytest.approx(210.5)
    assert out["temps"]["nozzle"]["target_c"] is None
    assert out["temps"]["bed"]["current_c"] is None


def test_motion_block_returns_nulls_when_unsurfaced() -> None:
    out = translate_snapshot({"gcode_state": "RUNNING", "layer_num": 5}, _ctx())
    assert out["motion"] == {"x": None, "y": None, "z": None, "e": None}


# --------------------------------------------------------------------------- #
# started_at — epoch seconds → ISO-8601 UTC  (contract §6 job.started_at)
# --------------------------------------------------------------------------- #

_EPOCH_1684328294_ISO = "2023-05-17T12:58:14Z"


def test_started_at_digit_string() -> None:
    """gcode_start_time as a digit string → ISO-8601 UTC."""
    assert _started_at_iso({"gcode_start_time": "1684328294"}) == _EPOCH_1684328294_ISO


def test_started_at_int() -> None:
    """gcode_start_time as a native int → same ISO-8601."""
    assert _started_at_iso({"gcode_start_time": 1684328294}) == _EPOCH_1684328294_ISO


def test_started_at_zero_string_is_none() -> None:
    """'0' means idle — must not emit a 1970 timestamp."""
    assert _started_at_iso({"gcode_start_time": "0"}) is None


def test_started_at_both_keys_absent_is_none() -> None:
    """No start time in payload → None, not a KeyError."""
    assert _started_at_iso({}) is None


def test_started_at_fallback_to_start_time_key() -> None:
    """start_time fallback: only start_time present → ISO-8601."""
    assert _started_at_iso({"start_time": "1684328294"}) == _EPOCH_1684328294_ISO


def test_started_at_already_iso_passes_through() -> None:
    """A future firmware/bridge emitting ISO 8601 directly must not be re-encoded."""
    iso = "2026-05-20T03:14:01Z"
    assert _started_at_iso({"gcode_start_time": iso}) == iso


def test_started_at_surfaced_in_job_block() -> None:
    """translate_snapshot wires gcode_start_time through to job.started_at."""
    raw = {
        "gcode_state": "RUNNING",
        "layer_num": 10,
        "gcode_start_time": "1684328294",
    }
    out = translate_snapshot(raw, _ctx())
    assert out["job"]["started_at"] == _EPOCH_1684328294_ISO


def test_started_at_null_when_key_absent_in_snapshot() -> None:
    """No start time in push_status → job.started_at is null, not 'start_time' string."""
    raw = {"gcode_state": "RUNNING", "layer_num": 10}
    out = translate_snapshot(raw, _ctx())
    assert out["job"]["started_at"] is None


def test_started_at_falls_back_to_ctx_when_raw_absent() -> None:
    """No raw start key (the P1S reality) → job.started_at takes the bridge-
    synthesized ctx.print_started_at value."""
    raw = {"gcode_state": "RUNNING", "layer_num": 10}
    out = translate_snapshot(raw, _ctx(print_started_at="2026-05-20T03:14:01Z"))
    assert out["job"]["started_at"] == "2026-05-20T03:14:01Z"


def test_started_at_raw_wins_over_ctx() -> None:
    """A future firmware that DOES ship gcode_start_time overrides the
    bridge-synthesized ctx value — raw is the source of truth when present."""
    raw = {
        "gcode_state": "RUNNING",
        "layer_num": 10,
        "gcode_start_time": "1684328294",
    }
    out = translate_snapshot(raw, _ctx(print_started_at="2026-05-20T03:14:01Z"))
    assert out["job"]["started_at"] == _EPOCH_1684328294_ISO


def test_started_at_null_when_neither_raw_nor_ctx() -> None:
    """No raw key and no synthesized ctx value → honest null."""
    raw = {"gcode_state": "RUNNING", "layer_num": 10}
    out = translate_snapshot(raw, _ctx(print_started_at=None))
    assert out["job"]["started_at"] is None
