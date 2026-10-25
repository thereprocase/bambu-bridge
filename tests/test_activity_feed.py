"""Spool activity in the printer's Activity feed: links, refusals, releases and charges."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from bambu_bridge.api import inventory as api
from bambu_bridge.api.events import _format
from bambu_bridge.inventory_journal import InventoryConflict
from bambu_bridge.inventory_sources import source_choices
from bambu_bridge.service import consumption_delivery
from bambu_bridge.service.inventory import InventoryService
from bambu_bridge.service.slot_display import release_reason
from bambu_bridge.spool_assignments import AssignmentConflict, SpoolAssignment
from tests.api.test_inventory import AUTH, PRODUCT
from tests.api.test_spool_routes import _spool, fixture
from tests.test_inventory_sources import printer as sourced_printer


class Feed(list):
    """An EventSink that keeps what it was given."""

    async def __call__(self, **row: Any) -> int:
        self.append(row)
        return len(self)


def _spoolman(spools: dict[int, dict]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.rstrip("/").split("/")
        if parts[-2] == "spool" and parts[-1].isdigit():
            return httpx.Response(200, json=spools[int(parts[-1])])
        if parts[-1] == "health":
            return httpx.Response(200, json={"status": "healthy"})
        if parts[-1] == "info":
            return httpx.Response(200, json={"version": "0.22.1"})
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle)


@pytest.fixture
async def inventory(tmp_path: Path):
    pla = {**PRODUCT, "id": 3, "name": "PLA Cotton White", "material": "PLA"}
    spools = {
        43: {"id": 43, "filament": PRODUCT, "remaining_weight": 400.0},
        44: {"id": 44, "filament": pla, "remaining_weight": 60.0},
    }
    service = await InventoryService.open(
        tmp_path / "inventory", "https://inventory.invalid/", transport=_spoolman(spools)
    )
    feed = Feed()
    service.events = feed
    try:
        yield service, feed
    finally:
        await service.close()


async def test_an_owner_link_and_unlink_are_recorded(inventory) -> None:
    service, feed = inventory
    device = sourced_printer()
    slot = source_choices(device)[0]
    await service.assign(
        spool_id=43,
        printer_id="p1",
        source=slot["source"],
        observed_identity=None,
        choices=lambda: source_choices(device),
    )
    await service.assign(
        spool_id=None,
        printer_id="p1",
        source=slot["source"],
        observed_identity=None,
        choices=lambda: [],
    )
    kinds = [row["event_type"] for row in feed]
    assert kinds == ["spool_assigned", "spool_unassigned"]
    linked, unlinked = (row["payload"] for row in feed)
    assert linked["spool_id"] == 43 and linked["name"] == "ASA Black"
    assert linked["slot_label"] == slot["label"] and linked["material"] == "ASA"
    assert unlinked["spool_id"] == 43 and unlinked["slot_label"] == "AMS A · slot 1"
    assert _format("spool_assigned", linked)[:2] == (
        f"{slot['label']}: #43 ASA Black linked",
        "Linked by hand · ASA",
    )
    assert _format("spool_unassigned", unlinked)[0] == "AMS A · slot 1: #43 unlinked"


async def test_relinking_the_spool_a_slot_holds_records_nothing_new(inventory) -> None:
    service, feed = inventory
    device = sourced_printer()
    slot = source_choices(device)[0]
    for _ in range(2):
        await service.assign(
            spool_id=43,
            printer_id="p1",
            source=slot["source"],
            observed_identity=None,
            choices=lambda: source_choices(device),
        )
    assert [row["event_type"] for row in feed] == ["spool_assigned"]


async def test_a_spool_moved_between_slots_says_where_it_came_from(inventory) -> None:
    service, feed = inventory
    device = sourced_printer()
    first = source_choices(device)[0]
    await service.assign(
        spool_id=43,
        printer_id="p1",
        source=first["source"],
        observed_identity=None,
        choices=lambda: source_choices(device),
    )
    # The same spool on a second ASA slot: one row, naming the slot it left.
    device.material_inventory.observe(
        {
            "ams": {
                "ams": [
                    {
                        "id": "0",
                        "sn": "test-ams",
                        "tray": [
                            {
                                "id": str(i),
                                "tray_type": "ASA" if i < 2 else "",
                                "tray_color": "000000FF",
                            }
                            for i in range(4)
                        ],
                    }
                ],
                "ams_exist_bits": "1",
                "tray_exist_bits": "3",
            }
        }
    )
    second = source_choices(device)[1]
    assert second["assignable"]
    await service.assign(
        spool_id=43,
        printer_id="p1",
        source=second["source"],
        observed_identity=None,
        choices=lambda: source_choices(device),
    )
    assert [row["event_type"] for row in feed] == ["spool_assigned", "spool_assigned"]
    assert _format("spool_assigned", feed[1]["payload"])[1] == (
        "Linked by hand · ASA · moved from AMS A · slot 1"
    )


async def test_clearing_an_empty_slot_records_nothing(inventory) -> None:
    service, feed = inventory
    await service.assign(
        spool_id=None,
        printer_id="p1",
        source="ams:x:tray:0",
        observed_identity=None,
        choices=lambda: [],
    )
    assert feed == []


async def test_a_material_mismatch_is_recorded_as_refused(inventory) -> None:
    service, feed = inventory
    device = sourced_printer()  # slot 1 declares ASA; #44 is PLA
    slot = source_choices(device)[0]
    with pytest.raises(InventoryConflict, match="reports ASA; #44 is PLA"):
        await service.assign(
            spool_id=44,
            printer_id="p1",
            source=slot["source"],
            observed_identity=None,
            choices=lambda: source_choices(device),
        )
    assert [(row["event_type"], row["severity"]) for row in feed] == [
        ("assignment_refused", "warn")
    ]
    title, detail, _ = _format("assignment_refused", feed[0]["payload"])
    assert title == f"{slot['label']} couldn't take #44 PLA Cotton White"
    assert "change the slot on the printer, or pick #44 under Other materials" in detail


async def test_a_revision_race_and_a_tag_seating_are_not_owner_rows(inventory) -> None:
    service, feed = inventory
    device = sourced_printer()
    slot = source_choices(device)[0]
    with pytest.raises(AssignmentConflict):
        await service.assign(
            spool_id=43,
            printer_id="p1",
            source=slot["source"],
            observed_identity=None,
            choices=lambda: source_choices(device),
            expected_revision=99,
        )
    # A tag seating a spool writes its own spool_adopted row.
    await service.assign(
        spool_id=43,
        printer_id="p1",
        source=slot["source"],
        observed_identity=None,
        choices=lambda: source_choices(device),
        origin="rfid_known",
    )
    assert feed == []


async def test_a_failing_feed_never_undoes_the_link(inventory) -> None:
    service, _ = inventory
    service.events = AsyncMock(side_effect=RuntimeError("events table locked"))
    device = sourced_printer()
    slot = source_choices(device)[0]
    await service.assign(
        spool_id=43,
        printer_id="p1",
        source=slot["source"],
        observed_identity=None,
        choices=lambda: source_choices(device),
    )
    snapshot = await service.assignments.snapshot()
    assert [a.spool_id for a in snapshot.assignments] == [43]


def test_a_stale_slot_refusal_reaches_the_activity_feed(tmp_path: Path, monkeypatch) -> None:
    from bambu_bridge import inventory_sources
    from bambu_bridge.db.jobs import EventRepo

    device = sourced_printer()
    now = time.time()
    device.material_inventory.ams_at = now - 60
    monkeypatch.setattr(
        inventory_sources, "time", SimpleNamespace(time=lambda: now, monotonic=time.monotonic)
    )
    with fixture(tmp_path, [_spool(43)]) as (client, _):
        app = client.app
        app.state.inventory.events = EventRepo(app.state.db).add
        monkeypatch.setattr(app.state.registry, "get", lambda _: device)
        monkeypatch.setattr(
            "bambu_bridge.api.inventory.refresh_source_frame", AsyncMock(return_value=None)
        )
        rows = client.get("/api/v1/inventory/printers/p1/sources", headers=AUTH).json()
        body = {"spool_id": 43, "source": rows[0]["source"]}
        refused = client.put("/api/v1/inventory/printers/p1/assignment", json=body, headers=AUTH)
        assert refused.status_code == 409
        feed = client.get("/api/v1/printers/p1/events", headers=AUTH).json()
    # A late slot report is routine, so the refusal is not a warning.
    assert [(row["kind"], row["severity"]) for row in feed] == [("assignment_refused", "info")]
    assert feed[0]["title"] == f"{rows[0]['label']} couldn't take #43"
    assert feed[0]["detail"] == (
        "The printer hasn't sent a slot report for 60 s; try again in a moment."
    )


async def test_a_slot_rewrite_returns_its_feed_row(monkeypatch) -> None:
    from tests.test_slot_setting import REQUEST, Printer, body, service

    device = Printer()
    monkeypatch.setattr(api, "source_choices", lambda _p: [dict(device.choice)])
    row = await api._set_slot_for(service(), device, body(), REQUEST)
    assert row == {
        "spool_id": 5,
        "name": "PET-GF Black",
        "slot_label": "External spool",
        "wire_slot": 254,
        "source": "external",
        "material": "PET-GF",
        "previous_material": "PC",
        "color": "000000",
    }
    assert _format("slot_set", row)[:2] == (
        "External spool set to PET-GF for #5 PET-GF Black",
        "Was PC",
    )
    matching = Printer(material="PET-GF")
    monkeypatch.setattr(api, "source_choices", lambda _p: [dict(matching.choice)])
    assert await api._set_slot_for(service(), matching, body(), REQUEST) is None


def test_release_reasons_name_the_evidence() -> None:
    held = SpoolAssignment("p1", "ams:h:tray:0", 6, assigned_at=1.0, wire_slot=0)
    empty = [{"source": "ams:h:tray:0", "issues": ["source_empty"]}]
    loaded = [{"source": "ams:h:tray:0", "issues": []}]
    assert release_reason(held, empty, {}) == "The AMS reported the slot empty."
    assert release_reason(held, loaded, {0: 2.0}) == "The AMS reported the spool unloaded."
    assert release_reason(held, loaded, {}) == "The AMS reported a different spool in the slot."
    payload = {
        "spool_id": 6,
        "slot_label": "AMS A · slot 1",
        "reason": "The AMS reported the slot empty.",
    }
    assert _format("spool_released", payload)[:2] == (
        "AMS A · slot 1: #6 released",
        "The AMS reported the slot empty.",
    )


class Inbox:
    """The two reads close_summary makes."""

    def __init__(self, written: dict[int, float]) -> None:
        self.attempt = {
            "printer": "p1",
            "title": "",
            "command": json.dumps({"print": {"subtask_name": "large inner diagonal cup"}}),
        }
        self.written = written

    def get(self, _attempt_id: str) -> dict[str, Any]:
        return self.attempt


@pytest.fixture
def ledger(monkeypatch):
    def use(inbox: Inbox) -> None:
        monkeypatch.setattr(
            consumption_delivery.usage_ledger,
            "rows",
            lambda _inbox, _attempt: {s: {"written_g": g} for s, g in inbox.written.items()},
        )

    return use


async def test_a_delivered_print_records_what_it_charged(ledger) -> None:
    inbox = Inbox({10: 49.514, 11: 0.0})
    ledger(inbox)
    summary = await consumption_delivery.close_summary(inbox, "a1", 49.51, [], None)
    assert summary["title"] == "large inner diagonal cup"
    assert summary["charged"] == [{"spool_id": 10, "grams": 49.51}] and summary["total_g"] == 49.51
    feed = Feed()
    await consumption_delivery._record_close(SimpleNamespace(record_event=_recorder(feed)), summary)
    assert [(row["event_type"], row["severity"]) for row in feed] == [("usage_charged", "info")]
    assert _format("usage_charged", feed[0]["payload"])[:2] == (
        "large inner diagonal cup: #10 −49.5 g",
        "Planned 49.5 g",
    )


async def test_a_print_with_shortfalls_records_a_review(ledger) -> None:
    inbox = Inbox({})
    ledger(inbox)
    found = [{"kind": "unassigned_slot", "wire_slot": 0, "grams": 12.0}]
    summary = await consumption_delivery.close_summary(inbox, "a1", 12.0, found, None)
    feed = Feed()
    await consumption_delivery._record_close(SimpleNamespace(record_event=_recorder(feed)), summary)
    assert [(row["event_type"], row["severity"]) for row in feed] == [("usage_review", "warn")]
    title, detail, context = _format("usage_review", feed[0]["payload"])
    assert title == "large inner diagonal cup: charge needs review"
    assert detail == "A slot it used had no spool linked. Charge or skip it on the Filament tab."
    assert context == "Charged so far: nothing charged"


def test_close_notes_are_shown_in_words() -> None:
    payload = {
        "title": "cup",
        "charged": [{"spool_id": 11, "grams": 58.29}],
        "planned_g": 58.3,
        "notes": ["refill_inferred_slot_1_to_3", "tag_changed_slot_1", "over_charged_spool_11_3g"],
    }
    assert _format("usage_charged", payload)[1] == (
        "Planned 58.3 g · Slot 1 ran out and slot 3 finished the print. · "
        "Slot 2 held a different tagged spool by the end. · "
        "Spoolman was charged more than the print used; check the spool's weight."
    )


async def test_a_closed_print_writes_one_row_with_its_notes(tmp_path, monkeypatch) -> None:
    from tests.test_usage_ledger import at_layer, iso, printer, running, tick

    provider, service, inbox, attempt, clock = await running(
        tmp_path, monkeypatch, layer=3, wire_spools={"0": 43, "2": 44}
    )
    feed = Feed()
    service.events = feed
    try:
        await tick(service, inbox)
        switch = {
            "from_slot": 1,
            "to_slot": 3,
            "status": "completed",
            "layer": 0,
            "observed_at": iso(clock.now),
        }
        at_layer(inbox, attempt, 10, "completed")
        # Later passes find the print closed and write nothing more.
        for _ in range(3):
            await tick(service, inbox, printer([switch]))
    finally:
        await service.close()
    assert [row["event_type"] for row in feed] == ["usage_charged"]
    assert feed[0]["payload"]["notes"] == ["over_charged_spool_43_3g"]
    assert inbox.accounting_review(attempt) == "over_charged_spool_43_3g"


async def test_each_released_assignment_writes_one_row(monkeypatch) -> None:
    from bambu_bridge.service import slot_display

    class Stop(Exception):
        pass

    async def one_pass(_seconds: float) -> None:
        raise Stop

    monkeypatch.setattr(slot_display.asyncio, "sleep", one_pass)
    dropped = [
        SpoolAssignment("p1", "ams:h:tray:0", 6, assigned_at=1.0, wire_slot=0),
        SpoolAssignment("p1", "ams:h:tray:1", 7, assigned_at=1.0, wire_slot=1),
    ]
    inventory = SimpleNamespace(release_unseated=AsyncMock(return_value=dropped))
    feed = Feed()
    with pytest.raises(Stop):
        await slot_display.release_unseated(lambda: [sourced_printer()], inventory, events=feed)
    assert [(row["event_type"], row["payload"]["spool_id"]) for row in feed] == [
        ("spool_released", 6),
        ("spool_released", 7),
    ]


def _recorder(feed: Feed):
    async def record(printer_id: str, kind: str, payload: dict, severity: str = "info") -> None:
        await feed(printer_id=printer_id, event_type=kind, payload=payload, severity=severity)

    return record
