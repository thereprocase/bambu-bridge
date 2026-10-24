"""Printer reports reach every observer, and a real P1S's AMS stays known between versions."""

from __future__ import annotations

from bambu_bridge.inventory_sources import source_choices
from bambu_bridge.protocol.models import ReportMessage
from bambu_bridge.service.refill_reports import RefillReports
from tests.service.test_printer import _service
from tests.test_refill_reports import raw


def p1s_push(colors=("FF0000FF", "0000FFFF")) -> dict:
    """A push as a P1S sends it: its AMS unit carries no serial."""
    report = raw()
    unit = report["print"]["ams"]["ams"][0]
    unit.pop("sn", None)
    for tray, color in zip(unit["tray"], colors, strict=False):
        tray["tray_color"] = color
    return report


VERSION = {"info": {"command": "get_version", "module": [{"name": "ams/0", "sn": "fixture-ams"}]}}


async def test_a_raising_observer_never_starves_the_material_frame(monkeypatch):
    service = _service(1883)

    def broken(_raw):
        raise KeyError("unexpected report shape")

    monkeypatch.setattr(service.refill_reports, "observe", broken)
    report = raw()
    report["print"]["ams"]["ams"][0]["tray"] += [{"id": "2"}, {"id": "3"}]  # a complete unit
    await service._handle_report(ReportMessage.model_validate(report))
    assert service.material_inventory.snapshot().get("ams_at") is not None
    assert source_choices(service)


def test_a_p1s_ams_named_once_by_its_version_stays_known(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("bambu_bridge.service.refill_reports.time.monotonic", lambda: now[0])
    tracker = RefillReports()
    tracker.observe(VERSION)
    tracker.observe(p1s_push())
    assert sorted(tracker.snapshot(connected=True).slots) == [0, 1]
    now[0] = 200.0  # long after the version reply; pushes keep coming
    tracker.observe(p1s_push())
    assert sorted(tracker.snapshot(connected=True).slots) == [0, 1]


def test_a_full_report_that_cannot_fill_the_ams_frame_is_logged(monkeypatch):
    from unittest.mock import MagicMock

    from bambu_bridge.service import material_inventory

    log = MagicMock()
    monkeypatch.setattr(material_inventory, "log", log)
    inventory = material_inventory.MaterialInventory()
    inventory.observe({"msg": 0, "ams": {"ams": []}})
    inventory.observe({**raw()["print"], "msg": 0})  # complete: nothing to report
    inventory.observe({"ams": {"ams": []}})  # a delta is not a full report
    [call] = log.info.call_args_list
    assert call.args == ("material.full_report_without_ams",)
    assert call.kwargs == {"ams_keys": ["ams"], "vt_tray": False}


def complete_report() -> dict:
    report = raw()
    report["print"]["ams"]["ams"][0]["tray"] += [{"id": "2"}, {"id": "3"}]
    return report


async def test_a_partial_ams_report_keeps_every_slot_and_applies_its_fields():
    service = _service(1883)
    await service._handle_report(ReportMessage.model_validate(complete_report()))
    before = {row["wire_slot"]: row for row in source_choices(service)}
    tray = {"id": "1", "tray_color": "00FF00FF"}
    one_tray = {"print": {"ams": {"ams": [{"id": "0", "tray": [tray]}]}}}
    await service._handle_report(ReportMessage.model_validate(one_tray))
    humidity = {"print": {"ams": {"ams": [{"id": "0", "humidity": "3"}]}}}
    await service._handle_report(ReportMessage.model_validate(humidity))
    after = {row["wire_slot"]: row for row in source_choices(service)}
    assert sorted(after) == sorted(before) and {0, 1, 2, 3} <= set(after)
    assert after[1]["color"] == "00FF00FF" and after[0]["color"] == before[0]["color"]


async def test_a_partial_ams_report_still_records_an_unload():
    service = _service(1883)
    await service._handle_report(ReportMessage.model_validate(complete_report()))
    service.material_inventory.take_unloaded()
    unload = {"print": {"ams": {"tray_exist_bits": "1"}}}  # slot 2 emptied
    await service._handle_report(ReportMessage.model_validate(unload))
    assert set(service.material_inventory.take_unloaded()) == {1}
    rows = {row["wire_slot"]: row for row in source_choices(service)}
    assert "source_empty" in rows[1]["issues"] and {0, 1, 2, 3} <= set(rows)
