"""Translate-layer tests for filament memory (G3).

Pure function tests — no I/O, no MQTT. Tests the `filament_memory` field
on SnapshotContext and how it threads through to each slot dict.
"""

from __future__ import annotations

from typing import Any

from bambu_bridge.db.jobs import FilamentMemory
from bambu_bridge.translate import SnapshotContext, translate_snapshot

_AMS_RAW = {
    "ams": [
        {
            "id": "0",
            "tray": [
                {"id": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF", "remain": 80},
                {"id": "1", "tray_type": "PETG", "tray_color": "000000FF", "remain": 50},
                {"id": "2", "tray_type": "",     "tray_color": "FF0000FF", "remain": 0},
                {"id": "3", "tray_type": "ABS",  "tray_color": "0000FFFF", "remain": 10},
            ],
        }
    ],
    "tray_now": "255",
    "ams_exist_bits": "1",
}


def _ctx(**overrides: Any) -> SnapshotContext:
    defaults: dict[str, Any] = {
        "printer_id": "TEST01",
        "serial": "TEST01",
        "friendly_name": "Test P1S",
        "model": "P1S",
        "connected": True,
        "last_telemetry_at": None,
        "last_connect_attempt": None,
        "last_failure_phase": None,
        "cert_status": "trusted",
        "expected_fingerprint": None,
    }
    defaults.update(overrides)
    return SnapshotContext(**defaults)


def _slots(snap: dict[str, Any]) -> list[dict[str, Any]]:
    return snap["ams"]["slots"]


# --------------------------------------------------------------------------- #
# memory field is always present in slot dict
# --------------------------------------------------------------------------- #


def test_slot_has_memory_key_always() -> None:
    """Every slot dict must carry a 'memory' key (None or dict) regardless of context."""
    ctx = _ctx()  # filament_memory=None (default)
    snap = translate_snapshot({"ams": _AMS_RAW, "gcode_state": "IDLE"}, ctx)
    for slot in _slots(snap):
        assert "memory" in slot, f"slot {slot['physical_slot']} missing memory key"
    # expected_fingerprint is a service-internal TOFU field — never in snapshot output.
    assert "expected_fingerprint" not in snap


def test_memory_is_null_when_ctx_memory_is_none() -> None:
    """filament_memory=None on ctx → all slots get memory=null."""
    ctx = _ctx(filament_memory=None)
    snap = translate_snapshot({"ams": _AMS_RAW, "gcode_state": "IDLE"}, ctx)
    for slot in _slots(snap):
        assert slot["memory"] is None, (
            f"slot {slot['physical_slot']} should be null, got {slot['memory']!r}"
        )


def test_memory_is_null_when_ctx_memory_is_empty_dict() -> None:
    """filament_memory={} → cache wired but no labels → all slots get null."""
    ctx = _ctx(filament_memory={})
    snap = translate_snapshot({"ams": _AMS_RAW, "gcode_state": "IDLE"}, ctx)
    for slot in _slots(snap):
        assert slot["memory"] is None


def test_memory_present_for_labelled_slot() -> None:
    """A slot with a matching entry in filament_memory gets memory merged in."""
    mem = FilamentMemory(
        slot=1, make="Bambu", model="PLA Matte", profile="0.20 Standard",
        tray_type_seen="PLA", updated_at=0,
    )
    ctx = _ctx(filament_memory={1: mem})
    snap = translate_snapshot({"ams": _AMS_RAW, "gcode_state": "IDLE"}, ctx)
    slot1 = next(s for s in _slots(snap) if s["physical_slot"] == 1)
    assert slot1["memory"] is not None
    assert slot1["memory"]["make"] == "Bambu"
    assert slot1["memory"]["model"] == "PLA Matte"
    assert slot1["memory"]["profile"] == "0.20 Standard"


def test_memory_null_for_unlabelled_slot() -> None:
    """When cache is wired but a slot has no entry, memory is null for that slot."""
    mem = FilamentMemory(
        slot=1, make="Bambu", model=None, profile=None,
        tray_type_seen="PLA", updated_at=0,
    )
    ctx = _ctx(filament_memory={1: mem})
    snap = translate_snapshot({"ams": _AMS_RAW, "gcode_state": "IDLE"}, ctx)
    for slot in _slots(snap):
        if slot["physical_slot"] == 1:
            assert slot["memory"] is not None
        else:
            assert slot["memory"] is None, (
                f"unlabelled slot {slot['physical_slot']} should have null memory"
            )


def test_memory_all_four_slots() -> None:
    """All four slots can carry labels independently."""
    filament_memory = {
        1: FilamentMemory(slot=1, make="A", model=None, profile=None,
                          tray_type_seen="PLA", updated_at=0),
        2: FilamentMemory(slot=2, make="B", model=None, profile=None,
                          tray_type_seen="PETG", updated_at=0),
        3: FilamentMemory(slot=3, make="C", model=None, profile=None,
                          tray_type_seen="", updated_at=0),
        4: FilamentMemory(slot=4, make="D", model=None, profile=None,
                          tray_type_seen="ABS", updated_at=0),
    }
    ctx = _ctx(filament_memory=filament_memory)
    snap = translate_snapshot({"ams": _AMS_RAW, "gcode_state": "IDLE"}, ctx)
    for slot in _slots(snap):
        ph = slot["physical_slot"]
        assert slot["memory"] is not None, f"slot {ph} missing memory"
        assert slot["memory"]["make"] == chr(ord("A") + ph - 1)


def test_memory_partial_fields() -> None:
    """Memory with only some fields set — None values pass through."""
    mem = FilamentMemory(
        slot=2, make=None, model="PETG CF", profile=None,
        tray_type_seen="PETG", updated_at=0,
    )
    ctx = _ctx(filament_memory={2: mem})
    snap = translate_snapshot({"ams": _AMS_RAW, "gcode_state": "IDLE"}, ctx)
    slot2 = next(s for s in _slots(snap) if s["physical_slot"] == 2)
    assert slot2["memory"]["make"] is None
    assert slot2["memory"]["model"] == "PETG CF"
    assert slot2["memory"]["profile"] is None


def test_memory_not_affected_by_no_ams() -> None:
    """When AMS is absent, slots is [] — no crash from memory threading."""
    ctx = _ctx(filament_memory={1: FilamentMemory(slot=1, make="X", model=None,
                                                   profile=None, tray_type_seen="PLA",
                                                   updated_at=0)})
    snap = translate_snapshot({"gcode_state": "IDLE"}, ctx)
    assert snap["ams"]["slots"] == []
    assert snap["ams"]["present"] is False


def test_memory_with_dict_style_entry() -> None:
    """filament_memory values may be plain dicts (e.g. from tests) — handled via .get()."""
    mem_dict = {"make": "DictMake", "model": "DictModel", "profile": "DictProfile"}
    ctx = _ctx(filament_memory={3: mem_dict})
    snap = translate_snapshot({"ams": _AMS_RAW, "gcode_state": "IDLE"}, ctx)
    slot3 = next(s for s in _slots(snap) if s["physical_slot"] == 3)
    assert slot3["memory"]["make"] == "DictMake"
    assert slot3["memory"]["model"] == "DictModel"
    assert slot3["memory"]["profile"] == "DictProfile"
