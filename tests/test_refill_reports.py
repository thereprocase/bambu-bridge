import copy
from dataclasses import replace

import pytest

from bambu_bridge.service.refill_reports import RefillReports


def raw(color="FF0000FF", *, serial="fixture-ams", presence="3"):
    return {
        "print": {
            "gcode_state": "IDLE",
            "home_flag": 1024,
            "filam_bak": [3],
            "ams": {
                "ams_exist_bits": "1",
                "tray_exist_bits": presence,
                "ams": [
                    {
                        "id": "0",
                        "sn": serial,
                        "tray": [
                            {
                                "id": str(wire),
                                "tray_info_idx": "GFG99",
                                "tray_type": "PETG",
                                "tray_color": color,
                                "nozzle_temp_min": 220,
                                "nozzle_temp_max": 260,
                                "tag_uid": "0",
                            }
                            for wire in range(2)
                        ],
                    }
                ],
            },
        }
    }


def test_raw_receipts_are_independent_of_temperature_and_merged_snapshots(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("bambu_bridge.service.refill_reports.time.monotonic", lambda: now[0])
    tracker = RefillReports()
    packet = raw()
    before = copy.deepcopy(packet)
    tracker.observe(packet)
    report = tracker.snapshot(connected=True)
    assert report.idle and report.auto_refill is True and report.backup_masks == (3,)
    receipt = report.slots[0].receipt
    now[0] = 120
    tracker.observe({"print": {"nozzle_temper": 200}})
    assert tracker.snapshot(connected=True).slots[0].receipt == receipt
    now[0] = 131
    assert not tracker.snapshot(connected=True).idle
    assert tracker.snapshot(connected=True).slots == {}
    assert tracker.snapshot(connected=True).eligibility_reported_at == 100
    assert packet == before


def test_expected_partial_color_write_preserves_source_but_external_change_does_not():
    tracker = RefillReports()
    tracker.observe(raw())
    original = tracker.snapshot(connected=True).slots[0]
    target = replace(original.declaration, tray_color="FFFFFFFF")
    tracker.expect(0, original.source_generation, target)
    tracker.observe(
        {"print": {"ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_color": "FFFFFFFF"}]}]}}}
    )
    assert 0 not in tracker.snapshot(connected=True).slots
    tracker.observe(raw("FFFFFFFF"))
    confirmed = tracker.snapshot(connected=True).slots[0]
    assert confirmed.source_generation == original.source_generation
    assert confirmed.receipt > original.receipt
    tracker.observe(
        {"print": {"ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_color": "123456FF"}]}]}}}
    )
    tracker.observe(raw("123456FF"))
    assert tracker.snapshot(connected=True).slots[0].source_generation != original.source_generation


def test_removal_reinsertion_and_hardware_change_invalidate_old_ownership():
    tracker = RefillReports()
    tracker.observe(raw())
    original = tracker.snapshot(connected=True).slots[0].source_generation
    tracker.observe({"print": {"ams": {"tray_exist_bits": "2"}}})
    assert 0 not in tracker.snapshot(connected=True).slots
    tracker.observe(raw())
    assert tracker.snapshot(connected=True).slots[0].source_generation != original
    current = tracker.snapshot(connected=True).slots[0].source_generation
    tracker.observe({"info": {"module": [{"name": "ams/0", "sn": "different-ams"}]}})
    assert tracker.snapshot(connected=True).slots == {}
    tracker.observe(raw(serial="different-ams"))
    assert tracker.snapshot(connected=True).slots[0].source_generation != current


def test_presence_and_eligibility_require_their_own_fresh_reports(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("bambu_bridge.service.refill_reports.time.monotonic", lambda: now[0])
    tracker = RefillReports()
    tracker.observe(raw())
    now[0] = 131
    packet = raw()
    del packet["print"]["ams"]["tray_exist_bits"]
    del packet["print"]["home_flag"]
    tracker.observe(packet)
    report = tracker.snapshot(connected=True)
    assert report.slots[0].reported_at == 100  # Fresh declaration cannot refresh presence.
    assert report.eligibility_reported_at == 100  # Fresh backup masks cannot refresh the toggle.


def test_real_or_unknown_tag_is_not_editable_and_disconnect_changes_generation():
    tracker = RefillReports()
    packet = raw()
    packet["print"]["ams"]["ams"][0]["tray"][0]["tag_uid"] = "real-tag"
    tracker.observe(packet)
    slot = tracker.snapshot(connected=True).slots[0]
    assert not slot.editable
    with pytest.raises(ValueError):
        tracker.expect(0, slot.source_generation, slot.declaration)
    tracker.clear()
    tracker.observe(raw())
    assert tracker.snapshot(connected=True).slots[0].source_generation != slot.source_generation


def test_numeric_wire_temperatures_are_canonical_and_missing_hardware_reply_invalidates():
    tracker = RefillReports()
    packet = raw()
    tray = packet["print"]["ams"]["ams"][0]["tray"][0]
    tray["nozzle_temp_min"] = "220"
    tray["nozzle_temp_max"] = "260"
    tray["tray_color"] = "ff0000ff"
    tracker.observe(packet)
    slot = tracker.snapshot(connected=True).slots[0]
    assert slot.declaration.nozzle_temp_min == 220
    assert slot.declaration.tray_color == "FF0000FF"
    tracker.observe({"info": {"module": []}})
    assert tracker.snapshot(connected=True).slots == {}


async def test_printer_observer_clears_on_disconnect_and_knows_the_same_tray_again():
    from bambu_bridge.protocol.models import ReportMessage
    from tests.service.test_printer import _service

    service = _service(1883)
    await service._handle_report(ReportMessage.model_validate(raw()))
    report = service.refill_reports.snapshot(connected=True)
    generation = report.slots[0].source_generation
    receipt = report.slots[0].receipt
    await service._handle_report(ReportMessage.model_validate({"print": {"nozzle_temper": 230}}))
    assert service.refill_reports.snapshot(connected=True).slots[0].receipt == receipt
    await service._handle_lost()
    assert service.refill_reports.snapshot(connected=True).slots == {}
    await service._handle_report(ReportMessage.model_validate(raw()))
    # The same tray after a reconnect is the same source; another colour is not.
    assert service.refill_reports.snapshot(connected=True).slots[0].source_generation == generation
    await service._handle_lost()
    await service._handle_report(ReportMessage.model_validate(raw("00FF00FF")))
    assert service.refill_reports.snapshot(connected=True).slots[0].source_generation != generation


def test_explicit_tray_identity_change_cannot_hide_behind_matching_color():
    tracker = RefillReports()
    first = raw()
    first["print"]["ams"]["ams"][0]["tray"][0]["tray_uuid"] = "first-physical-spool"
    tracker.observe(first)
    before = tracker.snapshot(connected=True).slots[0]
    tracker.expect(0, before.source_generation, replace(before.declaration, tray_color="FFFFFFFF"))
    second = raw("FFFFFFFF")
    second["print"]["ams"]["ams"][0]["tray"][0]["tray_uuid"] = "replacement-spool"
    tracker.observe(second)
    assert tracker.snapshot(connected=True).slots[0].source_generation != before.source_generation


def test_new_identity_baseline_survives_an_unexpected_declaration_change():
    tracker = RefillReports()
    packet = raw()
    packet["print"]["ams"]["ams"][0]["tray"][0]["tray_uuid"] = "original"
    tracker.observe(packet)
    packet["print"]["ams"]["ams"][0]["tray"][0]["tray_color"] = "FFFFFFFF"
    tracker.observe(packet)
    rebound = tracker.snapshot(connected=True).slots[0].source_generation
    packet["print"]["ams"]["ams"][0]["tray"][0]["tray_uuid"] = "replacement"
    tracker.observe(packet)
    assert tracker.snapshot(connected=True).slots[0].source_generation != rebound


def test_an_identical_declaration_after_a_receipt_gap_is_the_same_source(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("bambu_bridge.service.refill_reports.time.monotonic", lambda: now[0])
    tracker = RefillReports()
    tracker.observe(raw())
    original = tracker.snapshot(connected=True).slots[0]
    now[0] = 120
    tracker.observe(raw())
    assert tracker.snapshot(connected=True).slots[0].source_generation == original.source_generation
    now[0] = 151
    tracker.observe(raw())
    assert tracker.snapshot(connected=True).slots[0].source_generation == original.source_generation
    tracker.observe(raw("00FF00FF"))
    assert tracker.snapshot(connected=True).slots[0].source_generation != original.source_generation


def _full(**changes):
    packet = raw()
    packet["print"]["ams"]["ams"][0]["tray"][0].update(changes)
    return packet


def test_an_expected_whole_declaration_keeps_the_source_but_not_another_material():
    tracker = RefillReports()
    tracker.observe(raw())
    original = tracker.snapshot(connected=True).slots[0]
    target = replace(
        original.declaration,
        tray_info_idx="GFG02",
        nozzle_temp_min=230,
        tray_color="FFFFFFFF",
    )
    with pytest.raises(ValueError, match="material"):
        tracker.expect(0, original.source_generation, replace(target, tray_type="PLA"))
    tracker.expect(0, original.source_generation, target)
    tracker.observe(_full(tray_info_idx="GFG02", nozzle_temp_min=230, tray_color="FFFFFFFF"))
    confirmed = tracker.snapshot(connected=True).slots[0]
    assert confirmed.declaration == target
    assert confirmed.source_generation == original.source_generation
    # The same change unannounced (the printer screen) is another source generation.
    tracker.observe(_full(tray_info_idx="GFG99", nozzle_temp_min=220, tray_color="FF0000FF"))
    assert tracker.snapshot(connected=True).slots[0].source_generation != (
        original.source_generation
    )


def test_a_real_tag_and_a_read_in_progress_are_reported():
    tracker = RefillReports()
    packet = _full(tag_uid="1234567890ABCDEF")
    packet["print"]["ams"]["tray_reading_bits"] = "2"
    tracker.observe(packet)
    report = tracker.snapshot(connected=True)
    assert (report.slots[0].tagged, report.slots[0].editable) == (True, False)
    assert (report.slots[1].tagged, report.slots[1].editable) == (False, True)
    assert report.reading == frozenset({1})
    tracker.observe({"print": {"ams": {"tray_reading_bits": "0"}}})
    assert tracker.snapshot(connected=True).reading == frozenset()
    tracker.observe(_full(tag_uid=""))  # blank: neither untagged nor a tag
    blank = tracker.snapshot(connected=True).slots[0]
    assert (blank.tagged, blank.editable) == (False, False)


def test_a_reading_bit_ends_with_its_tray_or_its_unit():
    tracker = RefillReports()
    reading = _full(tag_uid="1234567890ABCDEF")
    reading["print"]["ams"]["tray_reading_bits"] = "1"
    tracker.observe(reading)  # the tag's identity changes in the same frame
    assert tracker.snapshot(connected=True).reading == frozenset({0})
    tracker.observe({"print": {"ams": {"tray_exist_bits": "2"}}})  # tray 1 pulled
    tracker.observe(raw())  # reloaded; no reading bits in either frame
    assert tracker.snapshot(connected=True).reading == frozenset()
    tracker.observe(reading)
    tracker.observe({"print": {"ams": {"ams_exist_bits": "0"}}})  # the AMS is gone
    tracker.observe(raw())
    assert tracker.snapshot(connected=True).reading == frozenset()


def test_a_p1s_naming_its_ams_after_the_first_pushall_keeps_the_first_generation():
    """A P1S names its AMS only in the version reply, which lands after pushall."""
    tracker = RefillReports()
    push = raw()
    del push["print"]["ams"]["ams"][0]["sn"]
    tracker.observe(push)
    tracker.observe({"info": {"module": [{"name": "ams/0", "sn": "fixture-ams"}]}})
    tracker.observe(copy.deepcopy(push))
    slots = tracker.snapshot(connected=True).slots
    assert slots[0].first_of_run and slots[1].first_of_run
    # Another AMS named later is a change of hardware.
    tracker.observe({"info": {"module": [{"name": "ams/0", "sn": "different-ams"}]}})
    tracker.observe(copy.deepcopy(push))
    assert not tracker.snapshot(connected=True).slots[0].first_of_run
