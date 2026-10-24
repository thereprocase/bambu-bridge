"""An RFID tag puts its spool into the slot: known tags come back, new tags get a spool."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from bambu_bridge.inventory_sources import source_choices
from bambu_bridge.service.tagged_spools import TagAdopter, density_for
from bambu_bridge.spool_assignments import SpoolAssignment
from tests.test_inventory_service import open_service
from tests.test_inventory_sources import printer, recolor

TAG = {
    "tag_uid": "1234567890ABCDEF",
    "tray_sub_brands": "PETG Translucent",
    "tray_type": "PETG",
    "tray_weight": "1000",
    "remain": 80,
    "tray_info_idx": "GFG02",
}


class Spoolman:
    """An in-memory Spoolman: vendors, filaments and spools, searched by name."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[int, dict[str, Any]]] = {
            "vendor": {},
            "filament": {},
            "spool": {},
        }
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.lose_spool_reply = False

    def _create(self, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        table = self.tables[kind]
        record = {"id": max(table, default=0) + 1, **body}
        if kind == "filament":
            record["vendor"] = self.tables["vendor"].get(body.get("vendor_id"))
        if kind == "spool":
            record["filament"] = self.tables["filament"][body["filament_id"]]
            record.setdefault("used_weight", 0)
            record.setdefault("archived", False)
        table[record["id"]] = record
        return record

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")  # api, v1, kind[, id]
        kind = parts[2]
        if request.method == "GET":
            if kind == "health":
                return httpx.Response(200, json={"status": "healthy"})
            if kind == "info":
                return httpx.Response(200, json={"version": "0.22.1"})
            if len(parts) > 3:
                record = self.tables[kind].get(int(parts[3]))
                return httpx.Response(200 if record else 404, json=record or {"message": "no"})
            params = request.url.params
            rows = list(self.tables[kind].values())
            if params.get("name"):
                rows = [r for r in rows if params["name"].lower() in (r.get("name") or "").lower()]
            if params.get("filament.name"):
                rows = [
                    r
                    for r in rows
                    if params["filament.name"].lower() in (r["filament"].get("name") or "").lower()
                ]
            if kind == "spool" and params.get("allow_archived") != "true":
                rows = [r for r in rows if not r.get("archived")]
            offset, limit = int(params.get("offset", 0)), int(params.get("limit", 50))
            return httpx.Response(200, json=rows[offset : offset + limit])
        body = json.loads(request.content)
        self.posts.append((kind, body))
        if request.method == "POST":
            record = self._create(kind, body)
            if kind == "spool" and self.lose_spool_reply:
                self.lose_spool_reply = False
                raise httpx.ReadTimeout("reply lost after Spoolman accepted it", request=request)
            return httpx.Response(200, json=record)
        record = self.tables[kind][int(parts[3])]
        record.update({k: v for k, v in body.items() if k in {"remaining_weight", "archived"}})
        return httpx.Response(200, json=record)


def tagged_printer(**changes: Any):
    service = printer()
    recolor(service, "8B5A2BFF", **{**TAG, **changes})
    return service


async def settle(adopter: TagAdopter, prn, rows=None) -> list[dict[str, Any]]:
    """Two seating passes: the first sees the tag, the second acts on it."""
    first = await adopter.adopt(prn, rows)
    return first + await adopter.adopt(prn, rows)


def events_sink(events: list[dict[str, Any]]):
    async def record(**event: Any) -> int:
        events.append(event)
        return 1

    return record


async def test_a_new_tag_gets_a_spool_in_spoolman_and_its_slot(tmp_path: Path) -> None:
    spoolman, events = Spoolman(), []
    service = await open_service(tmp_path, spoolman)
    try:
        adopter = TagAdopter(service, events_sink(events))
        prn = tagged_printer()
        assert await adopter.adopt(prn) == []  # first sight: the tray may still be settling
        done = await adopter.adopt(prn)
        assert [d["spool_id"] for d in done] == [1]
        vendor = next(iter(spoolman.tables["vendor"].values()))
        product = next(iter(spoolman.tables["filament"].values()))
        spool = next(iter(spoolman.tables["spool"].values()))
        assert vendor["name"] == "Bambu Lab"
        assert product["name"] == "PETG Translucent" and product["material"] == "PETG"
        assert product["color_hex"] == "8B5A2B" and product["density"] == density_for("PETG")
        assert product["weight"] == 1000
        assert spool["initial_weight"] == 1000 and spool["remaining_weight"] == 800
        snapshot = await service.assignments.snapshot()
        row = source_choices(prn)[0]
        assert [(a.spool_id, a.observed_identity, a.origin) for a in snapshot.assignments] == [
            (1, row["observed_identity"], "rfid_new")
        ]
        assert events[0]["event_type"] == "spool_adopted"
        assert events[0]["payload"]["text"] == (
            "New spool #1 · Bambu PETG Translucent · AMS A · slot 1 (from its RFID tag)"
        )
        # The same tag on a later report changes nothing.
        assert await adopter.adopt(prn) == []
        assert len(spoolman.tables["spool"]) == 1
    finally:
        await service.close()


async def test_a_known_tag_gets_its_spool_back_after_a_restart(tmp_path: Path) -> None:
    spoolman, events = Spoolman(), []
    service = await open_service(tmp_path, spoolman)
    try:
        prn = tagged_printer()
        await settle(TagAdopter(service), prn)
        # The owner takes the spool out and the slot is cleared, as the seating worker does.
        await service.assign(
            spool_id=None,
            printer_id="p1",
            source=source_choices(prn)[0]["source"],
            observed_identity=None,
            choices=lambda: [],
        )
        assert (await service.assignments.snapshot()).assignments == ()
        done = await settle(TagAdopter(service, events_sink(events)), prn)  # a fresh bridge
        assert [d["spool_id"] for d in done] == [1]
        assert len(spoolman.tables["spool"]) == 1
        assert events[0]["payload"]["text"] == (
            "Spool #1 · Bambu PETG Translucent · AMS A · slot 1 (matched by its RFID tag)"
        )
        assert [a.origin for a in (await service.assignments.snapshot()).assignments] == [
            "rfid_known"
        ]
    finally:
        await service.close()


async def test_a_tag_that_moved_to_another_slot_brings_its_spool_along(tmp_path: Path) -> None:
    spoolman = Spoolman()
    service = await open_service(tmp_path, spoolman)
    try:
        prn = tagged_printer()
        await settle(TagAdopter(service), prn)
        first = source_choices(prn)[0]["source"]
        # The tagged spool goes from slot 1 to slot 2; slot 1 is empty now.
        frame = prn.material_inventory.snapshot()["ams"]
        frame["ams"][0]["tray"][0] = {"id": "0", "tray_type": "", "tray_color": "000000FF"}
        frame["ams"][0]["tray"][1].update(tray_color="8B5A2BFF", **TAG)
        frame["tray_exist_bits"] = "2"  # slot 2 loaded, slot 1 empty
        prn.material_inventory.observe({"ams": frame})
        await service.release_unseated(
            "p1", source_choices(prn), prn.material_inventory.take_unloaded()
        )
        done = await settle(TagAdopter(service), prn)
        assert [d["spool_id"] for d in done] == [1]
        snapshot = await service.assignments.snapshot()
        assert [(a.source == first, a.spool_id) for a in snapshot.assignments] == [(False, 1)]
        assert len(spoolman.tables["spool"]) == 1
    finally:
        await service.close()


async def test_a_lost_create_reply_never_makes_a_second_spool(tmp_path: Path) -> None:
    spoolman = Spoolman()
    spoolman.lose_spool_reply = True
    service = await open_service(tmp_path, spoolman)
    try:
        adopter = TagAdopter(service)
        prn = tagged_printer()
        assert await settle(adopter, prn) == []  # the reply was lost: nothing assigned yet
        assert (await service.assignments.snapshot()).assignments == ()
        done = await adopter.adopt(prn)  # the next report: the spool is found, not made again
        assert [d["spool_id"] for d in done] == [1]
        assert len(spoolman.tables["spool"]) == 1
        assert sum(1 for kind, _ in spoolman.posts if kind == "spool") == 1
    finally:
        await service.close()


async def test_adoption_runs_on_the_seating_pass_rows_and_never_on_a_stale_frame(
    tmp_path: Path,
) -> None:
    spoolman = Spoolman()
    service = await open_service(tmp_path, spoolman)
    try:
        prn = tagged_printer()
        rows = source_choices(prn)
        stale = [{**row, "observed_at": row["observed_at"] - 3600} for row in rows]
        assert await settle(TagAdopter(service), prn, stale) == []
        assert not spoolman.posts
        done = await settle(TagAdopter(service), prn, rows)
        assert [d["spool_id"] for d in done] == [1]
    finally:
        await service.close()


async def test_untagged_trays_are_left_alone(tmp_path: Path) -> None:
    spoolman = Spoolman()
    service = await open_service(tmp_path, spoolman)
    try:
        assert await settle(TagAdopter(service), printer()) == []
        assert not spoolman.posts
        assert (await service.assignments.snapshot()).assignments == ()
    finally:
        await service.close()


async def test_the_same_filament_on_another_tag_is_another_spool(tmp_path: Path) -> None:
    spoolman = Spoolman()
    service = await open_service(tmp_path, spoolman)
    try:
        prn = tagged_printer()
        adopter = TagAdopter(service)
        await settle(adopter, prn)
        recolor(prn, "8B5A2BFF", tag_uid="FEDCBA0987654321")  # same material, colour and name
        done = await settle(adopter, prn)
        assert [d["spool_id"] for d in done] == [2]
        assert len(spoolman.tables["filament"]) == 1  # the filament record is reused
        assert len(spoolman.tables["spool"]) == 2
        snapshot = await service.assignments.snapshot()
        assert [a.spool_id for a in snapshot.assignments] == [2]
    finally:
        await service.close()


async def test_an_archived_spool_is_not_brought_back(tmp_path: Path) -> None:
    spoolman = Spoolman()
    service = await open_service(tmp_path, spoolman)
    try:
        prn = tagged_printer()
        await settle(TagAdopter(service), prn)
        spoolman.tables["spool"][1]["archived"] = True
        snapshot = await service.assignments.snapshot()
        await service.assignments.replace(
            (), provider_id=snapshot.provider_id, expected_revision=snapshot.revision
        )
        done = await settle(TagAdopter(service), prn)
        assert [d["spool_id"] for d in done] == [2]
        assert [a.spool_id for a in (await service.assignments.snapshot()).assignments] == [2]
    finally:
        await service.close()


async def test_a_tray_reported_in_pieces_makes_one_spool_with_its_full_name(
    tmp_path: Path,
) -> None:
    spoolman = Spoolman()
    service = await open_service(tmp_path, spoolman)
    try:
        adopter = TagAdopter(service)
        prn = printer()
        # First report: the tag and material only; the name, colour and weight come later.
        recolor(prn, "000000FF", tag_uid=TAG["tag_uid"], tray_type="PETG")
        assert await adopter.adopt(prn) == []
        recolor(prn, "8B5A2BFF", **TAG)  # the finished tray, a different identity
        assert await adopter.adopt(prn) == []  # seen once: wait for the next pass
        done = await adopter.adopt(prn)
        assert [d["spool_id"] for d in done] == [1]
        assert len(spoolman.tables["spool"]) == 1
        assert len(spoolman.tables["filament"]) == 1
        product = next(iter(spoolman.tables["filament"].values()))
        assert product["name"] == "PETG Translucent" and product["color_hex"] == "8B5A2B"
        assert sum(1 for kind, _ in spoolman.posts if kind == "spool") == 1
    finally:
        await service.close()


async def test_a_tag_still_being_read_is_not_acted_on(tmp_path: Path) -> None:
    spoolman = Spoolman()
    service = await open_service(tmp_path, spoolman)
    try:
        adopter = TagAdopter(service)
        prn = tagged_printer()
        frame = prn.material_inventory.snapshot()["ams"]
        # The AMS is reading slot 1's tag: the row may still change.
        prn.material_inventory.observe({"ams": {**frame, "tray_reading_bits": "1"}})
        assert source_choices(prn)[0]["tag_reading"] is True
        assert await settle(adopter, prn) == []
        assert not spoolman.posts
        # Read done, and the tray is stable across two passes: exactly one spool.
        prn.material_inventory.observe(
            {"ams": {**frame, "tray_reading_bits": "0", "tray_read_done_bits": "1"}}
        )
        assert source_choices(prn)[0]["tag_reading"] is False
        assert [d["spool_id"] for d in await settle(adopter, prn)] == [1]
        assert len(spoolman.tables["spool"]) == 1
        # A frame whose read-done bit is clear for this tray also waits, quietly.
        prn.material_inventory.observe({"ams": {**frame, "tray_read_done_bits": "0"}})
        row = source_choices(prn)[0]
        assert row["tag_reading"] is False and row["tag_read_done"] is False
        recolor(prn, "8B5A2BFF", tag_uid="FEDCBA0987654321")  # a new tag in that frame
        assert await settle(adopter, prn) == []
        assert len(spoolman.tables["spool"]) == 1
    finally:
        await service.close()


async def test_a_tray_being_read_says_so_even_before_its_tag_shows(tmp_path: Path) -> None:
    spoolman = Spoolman()
    service = await open_service(tmp_path, spoolman)
    try:
        prn = printer()  # slot 1 holds an untagged ASA tray
        frame = prn.material_inventory.snapshot()["ams"]
        prn.material_inventory.observe({"ams": {**frame, "tray_reading_bits": "1"}})
        row = source_choices(prn)[0]
        assert row["tagged"] is False and row["tag_reading"] is True
        assert await settle(TagAdopter(service), prn) == []
        assert not spoolman.posts
    finally:
        await service.close()


def test_the_live_p1s_frame_lets_the_tagged_slot_through() -> None:
    # Read from the owner's P1S on 2026-10-23 19:34Z, mid-print: three trays present
    # (wires 0, 1, 3), none being read, read-done set for every present tray, the
    # tagged spool #4 in wire 1. The hex strings carry more than one bit.
    prn = printer()
    frame = prn.material_inventory.snapshot()["ams"]
    frame["ams"][0]["tray"][1].update(tray_color="8B5A2BFF", **TAG)
    frame["ams"][0]["tray"][3].update(tray_type="PLA", tray_color="FFFFFFFF")
    prn.material_inventory.observe(
        {
            "ams": {
                **frame,
                "tray_exist_bits": "b",
                "tray_reading_bits": "0",
                "tray_read_done_bits": "b",
                "tray_is_bbl_bits": "b",
            }
        }
    )
    rows = {row["wire_slot"]: row for row in source_choices(prn)}
    assert rows[1]["tagged"] is True and rows[1]["tag_reading"] is False
    assert rows[1]["tag_read_done"] is True
    assert rows[0]["tag_reading"] is False and rows[3]["tag_reading"] is False
    assert rows[2]["assignable"] is False  # wire 2 is not present


def test_source_rows_say_what_the_tag_says() -> None:
    row = source_choices(tagged_printer())[0]
    assert row["tagged"] is True
    assert row["tag"] == {
        "brand": "PETG Translucent",
        "material": "PETG",
        "color": "8B5A2B",
        "weight_g": 1000,
        "remain_percent": 80,
    }
    assert row["tag_reading"] is False and row["tag_read_done"] is None  # no bit fields
    assert source_choices(printer())[0]["tag"] is None
    assert isinstance(SpoolAssignment("p1", "external", 1), SpoolAssignment)


async def test_a_relabelled_tagged_slot_is_not_adopted_again(tmp_path: Path) -> None:
    # A declaration written to a tagged slot changes its declared identity but
    # not its tag: the slot still holds the same spool, and no second spool is made.
    spoolman = Spoolman()
    service = await open_service(tmp_path, spoolman)
    try:
        adopter = TagAdopter(service, events_sink([]))
        prn = tagged_printer()
        assert [d["spool_id"] for d in await settle(adopter, prn)] == [1]
        recolor(prn, "FFFFFFFF", tray_info_idx="GFG99", nozzle_temp_min="230")
        assert await settle(adopter, prn) == []
        assert len(spoolman.tables["spool"]) == 1
        assert [a.spool_id for a in (await service.assignments.snapshot()).assignments] == [1]
    finally:
        await service.close()
