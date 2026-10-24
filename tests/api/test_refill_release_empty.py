"""An emptied slot never strands a refill group: release and Remove both clear it."""

from __future__ import annotations

from bambu_bridge.service.events import Event
from tests.api.test_inventory import AUTH, inventory_app
from tests.api.test_inventory_refill import BACKUP, backup_fixture, colors, fixture
from tests.test_refill_reports import raw

URL = "/api/v1/inventory/printers/fixture/refill"


def emptied(*values):
    """A raw report with slot 1 empty and the given colours on the rest."""
    report = colors(*values)
    report["print"]["ams"]["tray_exist_bits"] = "2"
    report["print"]["ams"]["ams"][0]["tray"][0] = {"id": "0"}
    return report


def disabled_with_conflict(client):
    """A removed group whose slot 2 step is in conflict, as the old build left it."""
    printer, controller = fixture(client)
    group = controller.plan(
        "fixture", printer.refill_reports.snapshot(connected=True), [0, 1], "FFFFFFFF", revision=0
    )
    group["steps"][1]["state"] = "conflict"
    group = controller.store.save("fixture", group, group["revision"])
    provider = client.get("/api/v1/inventory", headers=AUTH).json()["provider_id"]
    group = client.post(
        URL + "/disable",
        json={"provider_id": provider, "expected_revision": group["revision"]},
        headers=AUTH,
    ).json()
    return printer, provider, group


def test_release_clears_an_emptied_slot_on_the_owners_confirmation(tmp_path):
    with inventory_app(tmp_path) as (client, _):
        printer, provider, group = disabled_with_conflict(client)
        printer.refill_reports.observe(raw(presence="1"))  # slot 2 emptied
        body = {
            "provider_id": provider,
            "expected_revision": group["revision"],
            "wire": 1,
            "note": "Spool taken out",
        }
        unconfirmed = client.post(
            URL + "/release", json={**body, "source_generation": "old"}, headers=AUTH
        )
        assert unconfirmed.status_code == 409
        assert unconfirmed.json()["message"] == (
            "Slot 2 is empty; confirm that to clear its backup step"
        )
        assert client.post(URL + "/release", json=body, headers=AUTH).status_code == 422
        loaded = client.post(
            URL + "/release", json={**body, "wire": 0, "slot_empty": True}, headers=AUTH
        )
        assert loaded.status_code == 409
        assert (
            loaded.json()["message"] == "The printer reports a spool in slot 1, so it isn't empty"
        )
        released = client.post(URL + "/release", json={**body, "slot_empty": True}, headers=AUTH)
        assert released.status_code == 200, released.text
        assert released.json()["steps"][1]["state"] == "released_empty"
        printer.send_raw.assert_not_awaited()


def test_release_takes_the_owners_word_while_the_printer_is_silent(tmp_path):
    with inventory_app(tmp_path) as (client, _):
        printer, provider, group = disabled_with_conflict(client)
        printer.connected = False
        released = client.post(
            URL + "/release",
            json={
                "provider_id": provider,
                "expected_revision": group["revision"],
                "wire": 1,
                "note": "Spool taken out",
                "slot_empty": True,
            },
            headers=AUTH,
        )
        assert released.status_code == 200, released.text
        assert released.json()["steps"][1]["state"] == "released_empty"


def test_release_of_an_empty_slot_waits_for_the_backup_to_be_removed(tmp_path):
    with inventory_app(tmp_path) as (client, _):
        printer, controller = fixture(client)
        group = controller.plan(
            "fixture",
            printer.refill_reports.snapshot(connected=True),
            [0, 1],
            "FFFFFFFF",
            revision=0,
        )
        provider = client.get("/api/v1/inventory", headers=AUTH).json()["provider_id"]
        refused = client.post(
            URL + "/release",
            json={
                "provider_id": provider,
                "expected_revision": group["revision"],
                "wire": 1,
                "note": "Spool taken out",
                "slot_empty": True,
            },
            headers=AUTH,
        )
        assert refused.status_code == 409
        assert refused.json()["message"] == "Remove the backup before clearing slot 2"


def test_removing_a_backup_whose_primary_was_emptied_restores_the_backup_slot(
    tmp_path, monkeypatch
):
    """The journal's DELETE 409: slot 1's step in conflict and its slot empty."""
    with inventory_app(tmp_path) as (client, _):
        printer, controller, declared = backup_fixture(client, monkeypatch)
        client.put(BACKUP, json={"from_wire": 0, "to_wire": 1}, headers=AUTH)
        assert declared == {0: "FF0000FF", 1: "FF0000FF"}
        group = controller.store.get("fixture")
        group["steps"][0]["state"] = "conflict"
        controller.store.save("fixture", group, group["revision"])

        async def send(command):
            body = command["print"]
            declared[body["tray_id"]] = body["tray_color"]
            printer.refill_reports.observe(emptied(declared[0], declared[1]))
            printer.raw_bus.publish(Event("snapshot", {}))

        printer.send_raw.side_effect = send
        printer.refill_reports.observe(emptied(declared[0], declared[1]))
        removed = client.delete(BACKUP + "/0", headers=AUTH)
        assert removed.status_code == 200, removed.text
        result = removed.json()
        assert result["state"] == "disabled" and result["backups"] == []
        assert [s["state"] for s in result["steps"]] == ["released_empty", "restored"]
        assert declared[1] == "0000FFFF"
