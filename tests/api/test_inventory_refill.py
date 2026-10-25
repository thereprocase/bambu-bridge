from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from bambu_bridge.service.events import Event
from bambu_bridge.service.refill_reports import RefillReports
from tests.api.test_inventory import AUTH, inventory_app
from tests.test_refill_reports import raw


def fixture(client):
    tracker = RefillReports()
    tracker.observe(raw())
    printer = SimpleNamespace(refill_reports=tracker, connected=True, send_raw=AsyncMock())
    client.app.state.registry.get = lambda _: printer
    return printer, client.app.state.inventory.refill


def test_preview_is_authenticated_exact_and_sends_or_stores_nothing(tmp_path):
    with inventory_app(tmp_path) as (client, writes):
        printer, controller = fixture(client)
        url = "/api/v1/inventory/printers/fixture/refill/preview"
        body = {"wires": [0, 1], "color": "FFFFFFFF"}
        assert client.post(url, json=body).status_code == 401
        result = client.post(url, json=body, headers=AUTH)
        assert result.status_code == 200, result.text
        preview = result.json()
        assert preview["writes_qualified"] is True
        assert preview["plan"]["steps"][0]["before"]["tray_color"] == "FF0000FF"
        assert preview["plan"]["steps"][0]["target"]["tray_color"] == "FFFFFFFF"
        assert controller.store.get("fixture") is None
        assert not writes
        printer.send_raw.assert_not_awaited()
        assert client.post(url, json={**body, "wires": [0, 0]}, headers=AUTH).status_code == 422
        assert client.post(url, json={**body, "wires": [True, 1]}, headers=AUTH).status_code == 422


def test_plan_requires_qualification_and_current_review_and_does_not_send(tmp_path):
    with inventory_app(tmp_path) as (client, writes):
        printer, controller = fixture(client)
        controller.qualified = False
        url = "/api/v1/inventory/printers/fixture/refill"
        body = {"wires": [0, 1], "color": "FFFFFFFF"}
        preview = client.post(url + "/preview", json=body, headers=AUTH).json()
        provider = client.get("/api/v1/inventory", headers=AUTH).json()["provider_id"]
        plan = {
            **body,
            "provider_id": provider,
            "expected_revision": 0,
            "preview_digest": preview["preview_digest"],
        }
        assert client.put(url, json=plan).status_code == 401
        assert client.put(url, json=plan, headers=AUTH).status_code == 409
        assert controller.store.get("fixture") is None
        controller.qualified = True  # only this disposable ownership-store fixture
        printer.refill_reports.observe(raw(serial="replacement"))
        assert client.put(url, json=plan, headers=AUTH).status_code == 409
        plan["preview_digest"] = client.post(url + "/preview", json=body, headers=AUTH).json()[
            "preview_digest"
        ]
        accepted = client.put(url, json=plan, headers=AUTH)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["revision"] == 1
        assert client.put(url, json=plan, headers=AUTH).status_code == 409
        printer.connected = False
        controller.qualified = False
        disabled = client.post(
            url + "/disable", json={"provider_id": provider, "expected_revision": 1}, headers=AUTH
        )
        assert disabled.status_code == 200
        assert disabled.json()["state"] == "restoration_pending"
        assert not disabled.json()["enabled"]
        assert (
            client.post(
                url + "/disable",
                json={"provider_id": provider, "expected_revision": 1},
                headers=AUTH,
            ).status_code
            == 409
        )
        assert not writes
        printer.send_raw.assert_not_awaited()


def test_readback_adoption_is_explicit_and_cannot_claim_non_delivery(tmp_path):
    with inventory_app(tmp_path) as (client, _):
        printer, controller = fixture(client)
        before = printer.refill_reports.snapshot(connected=True)
        group = controller.plan("fixture", before, [0, 1], "FFFFFFFF", revision=0)
        group["steps"][0].update(state="uncertain", direction="apply")
        group = controller.store.save("fixture", group, group["revision"])
        provider = client.get("/api/v1/inventory", headers=AUTH).json()["provider_id"]
        review = {
            "provider_id": provider,
            "expected_revision": group["revision"],
            "wire": 0,
            "source_generation": before.slots[0].source_generation,
            "note": "Checked actual target declaration",
        }
        url = "/api/v1/inventory/printers/fixture/refill/reconcile"
        assert client.post(url, json=review).status_code == 401
        assert client.post(url, json=review, headers=AUTH).status_code == 409
        assert (
            client.post(url, json={**review, "definitely_not_sent": True}, headers=AUTH).status_code
            == 422
        )
        from dataclasses import replace

        target = replace(before.slots[0].declaration, tray_color="FFFFFFFF")
        printer.refill_reports.expect(0, before.slots[0].source_generation, target)
        printer.refill_reports.observe(raw("FFFFFFFF"))
        adopted = client.post(url, json=review, headers=AUTH)
        assert adopted.status_code == 200, adopted.text
        assert adopted.json()["steps"][0]["state"] == "owned"
        assert client.post(url, json=review, headers=AUTH).status_code == 409
        printer.send_raw.assert_not_awaited()


def test_release_preserves_an_external_declaration_without_restoring_over_it(tmp_path):
    with inventory_app(tmp_path) as (client, _):
        printer, controller = fixture(client)
        group = controller.plan(
            "fixture",
            printer.refill_reports.snapshot(connected=True),
            [0, 1],
            "FFFFFFFF",
            revision=0,
        )
        group["steps"][0]["state"] = "conflict"
        group = controller.store.save("fixture", group, group["revision"])
        provider = client.get("/api/v1/inventory", headers=AUTH).json()["provider_id"]
        url = "/api/v1/inventory/printers/fixture/refill"
        disabled = client.post(
            url + "/disable",
            json={"provider_id": provider, "expected_revision": group["revision"]},
            headers=AUTH,
        ).json()
        printer.refill_reports.observe(raw("00FF00FF"))
        report = printer.refill_reports.snapshot(connected=True)
        body = {
            "provider_id": provider,
            "expected_revision": disabled["revision"],
            "wire": 0,
            "source_generation": report.slots[0].source_generation,
            "note": "Keep the operator's new declaration",
        }
        assert client.post(url + "/release", json=body).status_code == 401
        assert (
            client.post(
                url + "/release", json={**body, "source_generation": "obsolete"}, headers=AUTH
            ).status_code
            == 409
        )
        accepted = client.post(url + "/release", json=body, headers=AUTH)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["steps"][0]["state"] == "released"
        assert (
            printer.refill_reports.snapshot(connected=True).slots[0].declaration.tray_color
            == "00FF00FF"
        )
        printer.send_raw.assert_not_awaited()


def colors(*values):
    """A raw report with one declared color per AMS A tray."""
    report = raw()
    for tray, value in zip(report["print"]["ams"]["ams"][0]["tray"], values, strict=True):
        tray["tray_color"] = value
    return report


def qualified_fixture(client, monkeypatch, *, readback=True, plan=True):
    from bambu_bridge.api import inventory_refill
    from bambu_bridge.service import refill_adapter
    from bambu_bridge.service.events import Event, EventBus

    monkeypatch.setattr(refill_adapter, "QUALIFIED", True)
    original = refill_adapter.PhysicalRefillAdapter
    monkeypatch.setattr(
        inventory_refill,
        "PhysicalRefillAdapter",
        lambda printer: original(printer, timeout=0.2),
    )
    tracker = RefillReports()
    tracker.observe(colors("FF0000FF", "0000FFFF"))
    printer = SimpleNamespace(
        refill_reports=tracker,
        material_inventory=MagicMock(),
        connected=True,
        raw_bus=EventBus(),
        send_raw=AsyncMock(),
    )
    declared = {0: "FF0000FF", 1: "0000FFFF"}

    async def send(command):
        if readback:
            body = command["print"]
            declared[body["tray_id"]] = body["tray_color"]
            tracker.observe(colors(declared[0], declared[1]))
        printer.raw_bus.publish(Event("snapshot", {}))

    printer.send_raw.side_effect = send
    client.app.state.registry.get = lambda _: printer
    controller = client.app.state.inventory.refill
    controller.qualified = True  # only this disposable ownership-store fixture
    provider = client.get("/api/v1/inventory", headers=AUTH).json()["provider_id"]
    group = (
        controller.plan("fixture", tracker.snapshot(connected=True), [0, 1], "FFFFFFFF", revision=0)
        if plan
        else None
    )
    return printer, controller, provider, group, declared


def test_execute_is_unavailable_until_both_gates_are_qualified(tmp_path, monkeypatch):
    from bambu_bridge.service import refill_adapter

    monkeypatch.setattr(refill_adapter, "QUALIFIED", False)
    with inventory_app(tmp_path) as (client, writes):
        printer, controller = fixture(client)
        controller.qualified = False
        provider = client.get("/api/v1/inventory", headers=AUTH).json()["provider_id"]
        url = "/api/v1/inventory/printers/fixture/refill/execute"
        body = {"provider_id": provider, "expected_revision": 0}
        assert client.post(url, json=body).status_code == 401
        assert client.post(url, json=body, headers=AUTH).status_code == 409
        controller.qualified = True  # controller alone is not enough
        assert client.post(url, json=body, headers=AUTH).status_code == 409
        status = client.get("/api/v1/inventory/printers/fixture/refill", headers=AUTH).json()
        assert status["execution_qualified"] is False
        assert controller.store.get("fixture") is None
        assert not writes
        printer.send_raw.assert_not_awaited()


def test_qualified_execute_applies_reads_back_and_restores_owned_aliases(tmp_path, monkeypatch):
    with inventory_app(tmp_path) as (client, _):
        printer, controller, provider, group, declared = qualified_fixture(client, monkeypatch)
        url = "/api/v1/inventory/printers/fixture/refill"
        stale = {"provider_id": provider, "expected_revision": group["revision"] - 1}
        assert client.post(url + "/execute", json=stale, headers=AUTH).status_code == 409
        printer.send_raw.assert_not_awaited()
        body = {"provider_id": provider, "expected_revision": group["revision"]}
        applied = client.post(url + "/execute", json=body, headers=AUTH)
        assert applied.status_code == 200, applied.text
        result = applied.json()
        assert [s["state"] for s in result["steps"]] == ["owned", "owned"]
        assert result["execution_qualified"] is True
        assert declared == {0: "FFFFFFFF", 1: "FFFFFFFF"}
        assert printer.send_raw.await_count == 2
        # Assignment continuity was reserved for each owned color before sending.
        disabled = client.post(
            url + "/disable",
            json={"provider_id": provider, "expected_revision": result["revision"]},
            headers=AUTH,
        ).json()
        restored = client.post(
            url + "/execute",
            json={"provider_id": provider, "expected_revision": disabled["revision"]},
            headers=AUTH,
        ).json()
        assert [s["state"] for s in restored["steps"]] == ["restored", "restored"]
        assert restored["state"] == "disabled"
        assert declared == {0: "FF0000FF", 1: "0000FFFF"}
        assert printer.send_raw.await_count == 4


def test_execute_without_readback_records_uncertainty_and_never_resends(tmp_path, monkeypatch):
    with inventory_app(tmp_path) as (client, _):
        printer, controller, provider, group, declared = qualified_fixture(
            client, monkeypatch, readback=False
        )
        url = "/api/v1/inventory/printers/fixture/refill/execute"
        body = {"provider_id": provider, "expected_revision": group["revision"]}
        first = client.post(url, json=body, headers=AUTH)
        assert first.status_code == 200, first.text
        result = first.json()
        assert result["steps"][0]["state"] == "uncertain"
        assert result["state"] == "needs_attention"
        again = client.post(
            url, json={**body, "expected_revision": result["revision"]}, headers=AUTH
        )
        assert again.status_code == 200
        assert again.json()["steps"][0]["state"] == "uncertain"
        assert printer.send_raw.await_count == 1


BACKUP = "/api/v1/inventory/printers/fixture/refill/backup"


def backup_fixture(client, monkeypatch, *, readback=True):
    """An idle printer: slot 1 red, slot 2 blue, same material; no refill group yet."""
    printer, controller, _, _, declared = qualified_fixture(
        client, monkeypatch, readback=readback, plan=False
    )
    return printer, controller, declared


def observe(printer, *values, state="IDLE"):
    report = colors(*values)
    report["print"]["gcode_state"] = state
    printer.refill_reports.observe(report)


def backups(client):
    status = client.get("/api/v1/inventory/printers/fixture/refill", headers=AUTH)
    return status.json()["backups"]


def test_a_backup_slot_is_set_and_removed_in_one_call_each(tmp_path, monkeypatch):
    with inventory_app(tmp_path) as (client, _):
        printer, controller, declared = backup_fixture(client, monkeypatch)
        body = {"from_wire": 0, "to_wire": 1}
        assert client.put(BACKUP, json=body).status_code == 401
        response = client.put(BACKUP, json=body, headers=AUTH)
        assert response.status_code == 200, response.text
        # Slot 2 takes slot 1's colour; slot 1 already has it, so one write.
        assert declared == {0: "FF0000FF", 1: "FF0000FF"}
        assert printer.send_raw.await_count == 1
        [chain] = response.json()["backups"]
        assert (chain["from_wire"], chain["to_wire"]) == (0, 1)
        assert chain["state"] in ("active", "pending")
        other = client.put(BACKUP, json={"from_wire": 2, "to_wire": 3}, headers=AUTH)
        assert other.status_code == 409
        assert other.json()["message"] == "Remove the slot 1 backup first"
        removed = client.delete(BACKUP + "/0", headers=AUTH)
        assert removed.status_code == 200, removed.text
        assert declared == {0: "FF0000FF", 1: "0000FFFF"} and removed.json()["backups"] == []
        assert client.delete(BACKUP + "/0", headers=AUTH).status_code == 404


def test_a_backup_waits_for_an_idle_printer_and_matching_slots(tmp_path, monkeypatch):
    with inventory_app(tmp_path) as (client, _):
        printer, controller, _ = backup_fixture(client, monkeypatch)
        observe(printer, "FF0000FF", "0000FFFF", state="RUNNING")
        busy = client.put(BACKUP, json={"from_wire": 0, "to_wire": 1}, headers=AUTH)
        assert busy.status_code == 409
        assert busy.json()["message"] == "The printer accepts this change when it's idle"
        observe(printer, "FF0000FF", "0000FFFF")
        cases = {(0, 0): "Choose two different slots", (0, 2): "Slot 3 is empty"}
        for (source, target), message in cases.items():
            refused = client.put(
                BACKUP, json={"from_wire": source, "to_wire": target}, headers=AUTH
            )
            assert refused.status_code == 422 and refused.json()["message"] == message
        report = colors("FF0000FF", "0000FFFF")
        report["print"]["ams"]["ams"][0]["tray"][1].update(tray_type="PLA", tray_info_idx="GFL99")
        printer.refill_reports.observe(report)
        other = client.put(BACKUP, json={"from_wire": 0, "to_wire": 1}, headers=AUTH)
        assert other.json()["message"].startswith(
            "Slot 2 is PLA and slot 1 is PETG; a backup needs the same material."
        )
        printer.send_raw.assert_not_awaited()
        assert controller.store.get("fixture") is None


def unconfirmed(client, monkeypatch):
    """A backup whose write to slot 2 was sent but never read back."""
    printer, controller, declared = backup_fixture(client, monkeypatch, readback=False)
    response = client.put(BACKUP, json={"from_wire": 0, "to_wire": 1}, headers=AUTH)
    [chain] = response.json()["backups"]
    assert chain["state"] == "needs_attention"
    assert chain["message"] == "The printer has not confirmed slot 2 yet"
    assert printer.send_raw.await_count == 1
    return printer, controller


def test_try_again_adopts_a_write_the_printer_now_shows(tmp_path, monkeypatch):
    with inventory_app(tmp_path) as (client, _):
        printer, _ = unconfirmed(client, monkeypatch)
        observe(printer, "FF0000FF", "FF0000FF")  # the write did land
        again = client.put(BACKUP, json={"from_wire": 0, "to_wire": 1}, headers=AUTH)
        assert again.json()["backups"][0]["state"] in ("active", "pending")
        assert printer.send_raw.await_count == 1  # adopted, not resent


def test_try_again_resends_once_when_the_printer_still_shows_the_original(tmp_path, monkeypatch):
    with inventory_app(tmp_path) as (client, _):
        printer, _ = unconfirmed(client, monkeypatch)
        declared = {0: "FF0000FF", 1: "0000FFFF"}

        async def lands(command):
            body = command["print"]
            declared[body["tray_id"]] = body["tray_color"]
            observe(printer, declared[0], declared[1])
            printer.raw_bus.publish(Event("snapshot", {}))

        printer.send_raw.side_effect = lands
        observe(printer, "FF0000FF", "0000FFFF")
        again = client.put(BACKUP, json={"from_wire": 0, "to_wire": 1}, headers=AUTH)
        assert again.json()["backups"][0]["state"] in ("active", "pending")
        assert printer.send_raw.await_count == 2 and declared[1] == "FF0000FF"


def test_try_again_leaves_a_slot_showing_something_else_for_the_owner(tmp_path, monkeypatch):
    with inventory_app(tmp_path) as (client, _):
        printer, _ = unconfirmed(client, monkeypatch)
        observe(printer, "FF0000FF", "00FF00FF")  # neither what was there nor what was sent
        [chain] = client.put(BACKUP, json={"from_wire": 0, "to_wire": 1}, headers=AUTH).json()[
            "backups"
        ]
        assert chain["state"] == "needs_attention"
        assert chain["message"] == "The printer reports a different colour on slot 2"
        assert printer.send_raw.await_count == 1


async def test_the_automatic_restore_never_resends_an_unconfirmed_write(tmp_path, monkeypatch):
    import asyncio

    from bambu_bridge.service.refill_adapter import restore_when_idle

    with inventory_app(tmp_path) as (client, _):
        printer, _, declared = backup_fixture(client, monkeypatch)
        client.put(BACKUP, json={"from_wire": 0, "to_wire": 1}, headers=AUTH)
        printer.send_raw.side_effect = None  # from here on nothing reads back
        client.delete(BACKUP + "/0", headers=AUTH)
        sent = printer.send_raw.await_count
        assert backups(client)[0]["state"] == "needs_attention"
        controller = client.app.state.inventory.refill
        task = asyncio.create_task(restore_when_idle(lambda: [printer], controller, interval=0.02))
        await asyncio.sleep(0.3)
        task.cancel()
        assert printer.send_raw.await_count == sent


def gone_quiet(printer, declared):
    """The printer, after a minute of saying nothing, as an idle P1S does.

    Asked for a full report, it reports what its slots declare now.
    """
    import time

    tracker = printer.refill_reports
    tracker.slots = {
        wire: replace(slot, reported_at=slot.reported_at - 60)
        for wire, slot in tracker.slots.items()
    }
    tracker.hardware = (tracker.hardware[0], tracker.hardware[1] - 60)
    for name in ("presence_at", "unit_at", "state_at", "auto_at", "backups_at"):
        setattr(tracker, name, getattr(tracker, name) - 60)
    printer.material_inventory.snapshot.return_value = {"ams_at": time.time() - 60}

    async def full_report():
        observe(printer, declared[0], declared[1])
        printer.material_inventory.snapshot.return_value = {"ams_at": time.time()}
        printer.raw_bus.publish(Event("snapshot", {}))

    printer.request_full_report = AsyncMock(side_effect=full_report)


def test_removing_a_backup_from_a_quiet_idle_printer_asks_for_a_fresh_report(tmp_path, monkeypatch):
    # The restore had landed, yet the slot stayed uncertain until a sources
    # request happened to ask the printer for a report.
    with inventory_app(tmp_path) as (client, _):
        printer, controller, declared = backup_fixture(client, monkeypatch)
        client.put(BACKUP, json={"from_wire": 0, "to_wire": 1}, headers=AUTH)
        assert declared == {0: "FF0000FF", 1: "FF0000FF"}

        async def lands_without_readback(command):
            body = command["print"]
            declared[body["tray_id"]] = body["tray_color"]

        printer.send_raw.side_effect = lands_without_readback
        [chain] = client.delete(BACKUP + "/0", headers=AUTH).json()["backups"]
        assert chain["state"] == "needs_attention"
        assert declared == {0: "FF0000FF", 1: "0000FFFF"}
        sent = printer.send_raw.await_count

        gone_quiet(printer, declared)
        removed = client.delete(BACKUP + "/0", headers=AUTH)
        assert removed.status_code == 200, removed.text
        assert removed.json()["backups"] == []
        assert [s["state"] for s in controller.store.get("fixture")["steps"]] == [
            "restored",
            "restored",
        ]
        assert printer.send_raw.await_count == sent  # adopted from the readback, not resent
        printer.request_full_report.assert_awaited_once()


def test_try_again_on_a_quiet_idle_printer_asks_for_a_fresh_report(tmp_path, monkeypatch):
    with inventory_app(tmp_path) as (client, _):
        printer, _ = unconfirmed(client, monkeypatch)
        declared = {0: "FF0000FF", 1: "FF0000FF"}  # the write did land
        gone_quiet(printer, declared)
        again = client.put(BACKUP, json={"from_wire": 0, "to_wire": 1}, headers=AUTH)
        assert again.status_code == 200, again.text
        assert again.json()["backups"][0]["state"] in ("active", "pending")
        assert printer.send_raw.await_count == 1  # adopted, not resent
        printer.request_full_report.assert_awaited_once()


def declaration_fixture(client, monkeypatch):
    """An idle printer whose slots differ in product and temperatures too.

    Its fake printer applies every field a write sends, as the P1S does.
    """
    printer, controller, provider, _, _ = qualified_fixture(client, monkeypatch, plan=False)
    trays = {
        0: {"tray_info_idx": "GFG99", "tray_color": "FFFFFFFF", "nozzle_temp_min": 230},
        1: {"tray_info_idx": "GFG02", "tray_color": "8B5A2BFF", "nozzle_temp_min": 220},
    }

    def report():
        body = raw()
        for wire, fields in trays.items():
            body["print"]["ams"]["ams"][0]["tray"][wire].update(fields)
        return body

    async def send(command):
        body = command["print"]
        trays[body["tray_id"]] = {
            k: body[k] for k in ("tray_info_idx", "tray_color", "nozzle_temp_min")
        }
        printer.refill_reports.observe(report())
        printer.raw_bus.publish(Event("snapshot", {}))

    printer.send_raw.side_effect = send
    printer.refill_reports.observe(report())
    return printer, controller, trays, report


def test_a_backup_preview_lists_each_field_and_set_applies_exactly_that(tmp_path, monkeypatch):
    with inventory_app(tmp_path) as (client, _):
        printer, controller, trays, report = declaration_fixture(client, monkeypatch)
        body = {"from_wire": 0, "to_wire": 1}
        preview = client.post(BACKUP + "/preview", json=body, headers=AUTH)
        assert preview.status_code == 200, preview.text
        anchor, member = preview.json()["steps"]
        assert (anchor["wire"], anchor["anchor"], anchor["writes"]) == (0, True, False)
        assert member["changes"] == [
            {"field": "tray_info_idx", "before": "GFG02", "after": "GFG99"},
            {"field": "nozzle_temp_min", "before": 220, "after": 230},
            {"field": "tray_color", "before": "8B5A2BFF", "after": "FFFFFFFF"},
        ]
        assert preview.json()["restored_on_disable"] is True
        printer.send_raw.assert_not_awaited()
        assert controller.store.get("fixture") is None
        digest = preview.json()["preview_digest"]
        # The slot changed after the preview: nothing is written.
        trays[1]["nozzle_temp_min"] = 225
        printer.refill_reports.observe(report())
        stale = client.put(BACKUP, json={**body, "preview_digest": digest}, headers=AUTH)
        assert stale.status_code == 409
        printer.send_raw.assert_not_awaited()
        digest = client.post(BACKUP + "/preview", json=body, headers=AUTH).json()["preview_digest"]
        applied = client.put(BACKUP, json={**body, "preview_digest": digest}, headers=AUTH)
        assert applied.status_code == 200, applied.text
        assert trays[1] == trays[0]
        assert printer.send_raw.await_count == 1
        [chain] = applied.json()["backups"]
        assert (chain["state"], chain["stale"]) == ("active", False)
        assert chain["confirmed_at"] is not None and chain["last_confirmed_age_seconds"] >= 0
        removed = client.delete(BACKUP + "/0", headers=AUTH)
        assert removed.status_code == 200, removed.text
        assert trays[1] == {
            "tray_info_idx": "GFG02",
            "tray_color": "8B5A2BFF",
            "nozzle_temp_min": 225,
        }


def test_removing_a_backup_while_the_printer_is_away_records_it(tmp_path, monkeypatch):
    with inventory_app(tmp_path) as (client, _):
        printer, controller, trays, report = declaration_fixture(client, monkeypatch)
        set_ = client.put(BACKUP, json={"from_wire": 0, "to_wire": 1}, headers=AUTH)
        assert set_.status_code == 200, set_.text
        sent = printer.send_raw.await_count
        printer.connected = False
        removed = client.delete(BACKUP + "/0", headers=AUTH)
        assert removed.status_code == 200, removed.text
        group = controller.store.get("fixture")
        assert (group["enabled"], group["state"]) == (False, "restoration_pending")
        assert printer.send_raw.await_count == sent  # nothing sent while away
        # Back and idle: the restoration worker's drive puts every field back.
        printer.connected = True
        printer.refill_reports.observe(report())
        again = client.delete(BACKUP + "/0", headers=AUTH)
        assert again.status_code == 200, again.text
        assert trays[1] == {
            "tray_info_idx": "GFG02",
            "tray_color": "8B5A2BFF",
            "nozzle_temp_min": 220,
        }


async def test_a_stored_source_v3_assignment_is_stamped_before_a_refill_relabels_it(tmp_path):
    import pytest
    from fastapi import HTTPException

    from bambu_bridge.api.inventory_refill import seat_first
    from tests.test_inventory_seating import _legacy, assigned
    from tests.test_inventory_sources import printer as sourced_printer

    prn = sourced_printer()
    inventory = await assigned(tmp_path, prn)
    try:
        await _legacy(inventory)
        await seat_first(inventory, prn, prn.serial, [0, 1])
        (assignment,) = (await inventory.assignments.snapshot()).assignments
        assert assignment.spool_key == "untagged"
        await _legacy(inventory)
        prn.material_inventory.ams_at -= 60  # nothing fresh to stamp from
        with pytest.raises(HTTPException) as refused:
            await seat_first(inventory, prn, prn.serial, [0, 1])
        assert refused.value.status_code == 409
        await seat_first(inventory, prn, prn.serial, [2, 3])  # other slots: no need
    finally:
        await inventory.close()


def test_an_untagged_slot_backed_up_by_a_tag_poses_as_the_tag(tmp_path, monkeypatch):
    # The owner's case: slot 1 (untagged white PETG) backed up by slot 2's Bambu tag.
    with inventory_app(tmp_path) as (client, _):
        printer, controller, trays, report = declaration_fixture(client, monkeypatch)
        trays[1]["tag_uid"] = "1234567890ABCDEF"
        printer.refill_reports.observe(report())
        body = {"from_wire": 0, "to_wire": 1}
        preview = client.post(BACKUP + "/preview", json=body, headers=AUTH).json()
        member, anchor = preview["steps"]
        assert (anchor["anchor"], anchor["tagged"], anchor["writes"]) == (True, True, False)
        assert member["target"]["tray_color"] == "8B5A2BFF"
        applied = client.put(
            BACKUP, json={**body, "preview_digest": preview["preview_digest"]}, headers=AUTH
        )
        assert applied.status_code == 200, applied.text
        sent = [call.args[0]["print"]["tray_id"] for call in printer.send_raw.await_args_list]
        assert sent == [0]  # the tag's slot is never written
        assert trays[0] == {
            "tray_info_idx": "GFG02",
            "tray_color": "8B5A2BFF",
            "nozzle_temp_min": 220,
        }
        assert client.delete(BACKUP + "/0", headers=AUTH).status_code == 200
        sent = [call.args[0]["print"]["tray_id"] for call in printer.send_raw.await_args_list]
        assert sent == [0, 0]
        assert trays[0] == {
            "tray_info_idx": "GFG99",
            "tray_color": "FFFFFFFF",
            "nozzle_temp_min": 230,
        }


def test_two_tags_cannot_back_each_other_up(tmp_path, monkeypatch):
    with inventory_app(tmp_path) as (client, _):
        printer, controller, trays, report = declaration_fixture(client, monkeypatch)
        trays[0]["tag_uid"] = "1111111111111111"
        trays[1]["tag_uid"] = "2222222222222222"
        printer.refill_reports.observe(report())
        refused = client.put(BACKUP, json={"from_wire": 0, "to_wire": 1}, headers=AUTH)
        assert refused.status_code == 422
        assert "both hold RFID spools" in refused.json()["message"]
        printer.send_raw.assert_not_awaited()


class Feed(list):
    """The printer's Activity feed, as the inventory service writes it."""

    async def __call__(self, **row):
        self.append(row)


def feed_of(client):
    feed = Feed()
    client.app.state.inventory.events = feed
    return feed


def test_setting_and_removing_a_backup_each_write_one_activity_row(tmp_path, monkeypatch):
    from bambu_bridge.api.events import _format

    with inventory_app(tmp_path) as (client, _):
        backup_fixture(client, monkeypatch)
        feed = feed_of(client)
        set_ = client.put(BACKUP, json={"from_wire": 0, "to_wire": 1}, headers=AUTH)
        assert set_.status_code == 200, set_.text
        removed = client.delete(BACKUP + "/0", headers=AUTH)
        assert removed.status_code == 200, removed.text
    assert [(row["event_type"], row["severity"]) for row in feed] == [
        ("backup_set", "info"),
        ("backup_removed", "info"),
    ]
    added, gone = (row["payload"] for row in feed)
    assert (added["from_wire"], added["to_wire"]) == (0, 1)
    title, detail, _ = _format("backup_set", added)
    assert title == "AMS A · slot 1 backed up by slot 2"
    assert detail in (
        "When AMS A · slot 1 runs out, the printer continues from slot 2.",
        "Saved; the printer hasn't confirmed it yet.",
    )
    assert gone["state"] == "disabled"
    assert _format("backup_removed", gone)[:2] == (
        "AMS A · slot 1 backup (slot 2) removed",
        "Both slots are back as they were.",
    )


def test_try_again_on_a_backup_writes_no_second_row(tmp_path, monkeypatch):
    from bambu_bridge.api.events import _format

    with inventory_app(tmp_path) as (client, _):
        feed = feed_of(client)
        printer, _ = unconfirmed(client, monkeypatch)
        observe(printer, "FF0000FF", "FF0000FF")
        client.put(BACKUP, json={"from_wire": 0, "to_wire": 1}, headers=AUTH)
    [row] = feed
    assert (row["event_type"], row["severity"]) == ("backup_set", "warn")
    assert _format("backup_set", row["payload"])[1] == (
        "The printer didn't confirm the change. "
        "Open the slot's backup spool on the Filament tab and tap Try again."
    )


def test_a_backup_removed_while_the_printer_is_away_says_it_waits(tmp_path, monkeypatch):
    from bambu_bridge.api.events import _format

    with inventory_app(tmp_path) as (client, _):
        printer, controller, trays, report = declaration_fixture(client, monkeypatch)
        client.put(BACKUP, json={"from_wire": 0, "to_wire": 1}, headers=AUTH)
        feed = feed_of(client)
        printer.connected = False
        client.delete(BACKUP + "/0", headers=AUTH)
        # Back and idle: the second call is Try again and writes no row.
        printer.connected = True
        printer.refill_reports.observe(report())
        client.delete(BACKUP + "/0", headers=AUTH)
    [row] = feed
    assert row["event_type"] == "backup_removed"
    assert _format("backup_removed", row["payload"])[1] == (
        "The bridge puts both slots back when the printer is idle and online."
    )
