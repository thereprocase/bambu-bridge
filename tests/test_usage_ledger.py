"""Spoolman is debited as a print goes: get, put, get, with totals that survive."""

import json
import time
from types import SimpleNamespace

import httpx
import pytest

from bambu_bridge.native_inbox import NativeInbox
from bambu_bridge.service import consumption_delivery, usage_ledger
from tests.test_accounting_attempts import manifest
from tests.test_inventory_service import PRODUCT, Provider, open_service
from tests.test_usage_counting import iso, printer

PLAN = [{"0": 1.0}] * 10  # 10 g over 10 layers; at layer n the count is n - 0.5


def spool(spool_id: int, remaining: float | None = 436.0) -> dict:
    return {
        "id": spool_id,
        "filament": PRODUCT,
        "remaining_weight": remaining,
        "used_weight": 0.0,
        "spool_weight": 150,
    }


class Spoolman(Provider):
    """/use the way Spoolman does it: used_weight moves; remaining follows when known."""

    def __init__(self) -> None:
        super().__init__()
        self.spools = {43: spool(43), 44: spool(44, 900.0), 45: spool(45, None)}
        self.uses: list[tuple[int, float]] = []
        self.fail_put: str | None = None  # "before": nothing sent; "after": applied, reply lost
        self.fail_put_for: set[int] = set()  # spools whose /use is refused before the send
        self.fail_get = False
        self.lose_get_after_put = False
        self.on_put = None  # someone edits the spool right after our write lands

    def _apply(self, spool_id: int, used: float) -> None:
        row = self.spools[spool_id]
        row["used_weight"] = round(row["used_weight"] + used, 2)
        if row["remaining_weight"] is not None:
            row["remaining_weight"] = round(row["remaining_weight"] - used, 2)
        self.uses.append((spool_id, used))

    async def __call__(self, request):
        path = request.url.path
        if "/spool/" in path:
            spool_id = int(path.split("/spool/")[1].split("/")[0])
            if spool_id not in self.spools:
                return httpx.Response(404, json={"message": "no such spool"})
            if path.endswith("/use"):
                if self.fail_put == "before" or spool_id in self.fail_put_for:
                    raise httpx.ConnectError("offline", request=request)
                self._apply(spool_id, json.loads(request.content)["use_weight"])
                if self.on_put:
                    self.on_put()
                if self.lose_get_after_put:
                    self.fail_get = True
                if self.fail_put == "after":
                    raise httpx.ReadTimeout("reply lost", request=request)
                return httpx.Response(200, json=self.spools[spool_id])
            if self.fail_get:
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(200, json=self.spools[spool_id])
        if request.method == "GET" and path.endswith("/spool"):
            first = int(request.url.params.get("offset", "0")) == 0
            return httpx.Response(200, json=list(self.spools.values()) if first else [])
        if "/filament/" in path:
            return httpx.Response(200, json=PRODUCT)
        return await super().__call__(request)


class Clock:
    """Stands in for time.time() in the ledger and the delivery loop; starts now.

    Whole seconds, so a refill stamped from it round-trips through ISO text exactly.
    """

    def __init__(self) -> None:
        self.now = float(int(time.time()))

    def time(self) -> float:
        return self.now


async def running(tmp_path, monkeypatch, *, layer=3, **fields):
    clock = Clock()
    monkeypatch.setattr(usage_ledger, "time", clock)
    monkeypatch.setattr(consumption_delivery, "time", clock)
    provider = Spoolman()
    service = await open_service(tmp_path / "inventory", provider)
    service.consumption_qualified = True
    inbox = NativeInbox(tmp_path / "inbox")
    attempt = inbox.claim_external("fixture", {"print": {"command": "project_file"}}, reserved=True)
    context = manifest()
    context.update({"layer_grams": PLAN, "tool_wires": {"0": 0}, "wire_spools": {"0": 43}} | fields)
    inbox.freeze_accounting(attempt, context)
    at_layer(inbox, attempt, layer, "running")
    return provider, service, inbox, attempt, clock


def at_layer(inbox, attempt, layer, state="running"):
    with inbox.connect() as db:
        db.execute(
            "UPDATE uploads SET start_state=?,layer_reached=? WHERE id=?", (state, layer, attempt)
        )


async def tick(service, inbox, device=None, failures=None):
    await consumption_delivery.deliver_once(
        service, inbox, lambda _id: device or printer(), 0.0, failures
    )


def row(inbox, attempt, spool_id=43):
    return usage_ledger.rows(inbox, attempt)[spool_id]


async def test_a_running_print_is_debited_as_it_goes_at_most_every_30_s(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    try:
        await tick(service, inbox)
        assert provider.uses == [(43, 2.5)]
        at_layer(inbox, attempt, 5)
        await tick(service, inbox)  # within the write interval: counted, not written
        assert provider.uses == [(43, 2.5)]
        assert row(inbox, attempt)["counted_g"] == 4.5
        clock.now += 31
        await tick(service, inbox)
        assert provider.uses == [(43, 2.5), (43, 2.0)]
        assert row(inbox, attempt)["written_g"] == 4.5
        assert provider.spools[43]["remaining_weight"] == 431.5
    finally:
        await service.close()


async def test_a_timeout_after_the_send_is_witnessed_not_resent(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    provider.fail_put = "after"
    try:
        await tick(service, inbox)
        assert provider.uses == [(43, 2.5)]
        assert row(inbox, attempt)["written_g"] == 2.5
        provider.fail_put = None
        clock.now += 31
        await tick(service, inbox)
        assert provider.uses == [(43, 2.5)]  # nothing owed, nothing sent
    finally:
        await service.close()


async def test_a_failure_before_the_send_is_retried_on_the_next_tick(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    provider.fail_put = "before"
    try:
        await tick(service, inbox)
        assert provider.uses == []
        assert row(inbox, attempt) | {} == row(inbox, attempt)
        assert row(inbox, attempt)["state"] == "open" and row(inbox, attempt)["written_g"] == 0
        provider.fail_put = None
        await tick(service, inbox)  # no interval: the last write never landed
        assert provider.uses == [(43, 2.5)]
    finally:
        await service.close()


async def test_a_concurrent_edit_is_the_truth_and_the_delta_counts_as_written(
    tmp_path, monkeypatch
):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)

    def someone_weighs():
        provider.spools[43]["used_weight"] += 5.0

    provider.on_put = someone_weighs
    try:
        await tick(service, inbox)
        found = row(inbox, attempt)
        assert found["state"] == "open" and found["written_g"] == 2.5
        assert json.loads(found["record"])["used_weight"] == 7.5
        provider.on_put = None
        at_layer(inbox, attempt, 5)
        clock.now += 31
        await tick(service, inbox)
        assert provider.uses == [(43, 2.5), (43, 2.0)]
        assert provider.spools[43]["used_weight"] == 9.5  # their 5 g and our 4.5 g
    finally:
        await service.close()


async def test_a_lost_verifying_read_is_settled_before_anything_else_is_sent(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    provider.lose_get_after_put = True
    try:
        await tick(service, inbox)
        found = row(inbox, attempt)
        assert found["state"] == "sent" and found["written_g"] == 0
        assert (found["sent_before_g"], found["sent_delta_g"]) == (0.0, 2.5)
        # Spoolman still away: nothing more goes out, the row waits.
        at_layer(inbox, attempt, 6)
        clock.now += 31
        await tick(service, inbox)
        assert provider.uses == [(43, 2.5)]
        # Back: the earlier write is witnessed first, then the rest is sent.
        provider.fail_get = provider.lose_get_after_put = False
        await tick(service, inbox)
        assert provider.uses == [(43, 2.5), (43, 3.0)]
        assert row(inbox, attempt)["written_g"] == 5.5
    finally:
        await service.close()


async def test_a_stopped_print_settles_to_its_layer_and_is_delivered(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    try:
        await tick(service, inbox)
        at_layer(inbox, attempt, 6, "interrupted")
        await tick(service, inbox)  # the end ignores the write interval
        assert provider.uses == [(43, 2.5), (43, 3.0)]
        assert row(inbox, attempt)["state"] == "done"
        assert inbox.accounting_event(attempt)["delivery_state"] == "delivered"
        await tick(service, inbox)
        assert provider.uses == [(43, 2.5), (43, 3.0)]
    finally:
        await service.close()


async def test_a_completed_print_is_settled_in_full_once(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch, layer=9)
    try:
        await tick(service, inbox)
        at_layer(inbox, attempt, 10, "completed")
        await tick(service, inbox)
        await tick(service, inbox)
        assert provider.uses == [(43, 8.5), (43, 1.5)]
        assert inbox.accounting_event(attempt)["delivery_state"] == "delivered"
    finally:
        await service.close()


async def test_a_refill_moves_the_running_count_to_the_new_spool(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(
        tmp_path, monkeypatch, wire_spools={"0": 43, "2": 44}
    )
    try:
        await tick(service, inbox)
        assert provider.uses == [(43, 2.5)]
        # AMS tray 1 ran out on layer 4; tray 3 took over.
        clock.now += 31
        switch = {
            "from_slot": 1,
            "to_slot": 3,
            "status": "completed",
            "layer": 4,
            "observed_at": iso(clock.now),  # inside the running print's window
        }
        at_layer(inbox, attempt, 6)
        await tick(service, inbox, printer([switch]))
        # 43: layers 1-3 and half of 4 = 3.5; 44: half of 4, layer 5, half of 6 = 2.0
        assert provider.uses == [(43, 2.5), (43, 1.0), (44, 2.0)]
    finally:
        await service.close()


async def test_a_count_below_what_was_written_takes_nothing_back(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(
        tmp_path, monkeypatch, layer=5, wire_spools={"0": 43, "2": 44}
    )
    try:
        await tick(service, inbox)
        assert provider.uses == [(43, 4.5)]
        # A refill at layer 2 reported late: layers 2-5 belong to the new spool now.
        clock.now += 31
        switch = {
            "from_slot": 1,
            "to_slot": 3,
            "status": "completed",
            "layer": 2,
            "observed_at": iso(clock.now),
        }
        await tick(service, inbox, printer([switch]))
        assert provider.uses == [(43, 4.5), (44, 3.0)]  # 44: half of 2, 3, 4, half of 5
        assert row(inbox, attempt)["counted_g"] == 1.5 and row(inbox, attempt)["written_g"] == 4.5
    finally:
        await service.close()


async def test_a_restart_keeps_the_written_total(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    try:
        await tick(service, inbox)
    finally:
        await service.close()
    service = await open_service(tmp_path / "inventory", provider)
    service.consumption_qualified = True
    inbox = NativeInbox(tmp_path / "inbox")
    try:
        at_layer(inbox, attempt, 5)
        clock.now += 31
        await tick(service, inbox)
        assert provider.uses == [(43, 2.5), (43, 2.0)]
    finally:
        await service.close()


async def test_an_archived_spool_is_held_and_noted_at_the_end(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    provider.spools[43]["archived"] = True
    try:
        await tick(service, inbox)
        assert provider.uses == [] and row(inbox, attempt)["state"] == "held"
        at_layer(inbox, attempt, 6, "interrupted")
        await tick(service, inbox)
        assert provider.uses == []
        # Nothing was taken: the owner is asked where the 5.5 g should go.
        assert inbox.accounting_review(attempt) == "spool_archived"
        assert inbox.accounting_event(attempt)["delivery_state"] == "review"
        assert usage_ledger.shortfalls(inbox, attempt) == [
            {
                "kind": "spool_archived",
                "wire_slot": 0,
                "spool_id": 43,
                "grams": 5.5,
                "material": None,
            }
        ]
    finally:
        await service.close()


async def test_a_spool_that_is_gone_is_held(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(
        tmp_path, monkeypatch, wire_spools={"0": 99}
    )
    try:
        await tick(service, inbox)
        found = row(inbox, attempt, 99)
        assert found["state"] == "held" and found["reason"] == "missing"
    finally:
        await service.close()


async def test_spoolman_unreachable_skips_the_tick(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    provider.fail_get = True
    try:
        await tick(service, inbox)
        assert provider.uses == []
        assert usage_ledger.rows(inbox, attempt) == {} or row(inbox, attempt)["state"] == "open"
        provider.fail_get = False
        await tick(service, inbox)
        assert provider.uses == [(43, 2.5)]
    finally:
        await service.close()


async def test_a_spool_without_a_known_weight_is_still_debited(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(
        tmp_path, monkeypatch, wire_spools={"0": 45}
    )
    try:
        await tick(service, inbox)
        assert provider.uses == [(45, 2.5)]
        assert provider.spools[45]["used_weight"] == 2.5
        assert provider.spools[45]["remaining_weight"] is None
    finally:
        await service.close()


async def test_a_spool_empties_at_zero(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    provider.spools[43]["remaining_weight"] = 1.0
    try:
        await tick(service, inbox)
        assert provider.uses == [(43, 1.0)]
        at_layer(inbox, attempt, 10, "completed")
        await tick(service, inbox)
        assert provider.uses == [(43, 1.0)]
        # The spool ran out 9 g short in Spoolman: the print waits for the owner.
        assert inbox.accounting_event(attempt)["delivery_state"] == "review"
        assert inbox.accounting_review(attempt) == "spool_empty"
        assert row(inbox, attempt)["state"] == "done" and row(inbox, attempt)["written_g"] == 1.0
        assert usage_ledger.shortfalls(inbox, attempt) == [
            {"kind": "spool_empty", "wire_slot": 0, "spool_id": 43, "grams": 9.0, "material": None}
        ]
    finally:
        await service.close()


async def test_live_usage_reports_counted_against_written(tmp_path, monkeypatch):
    from bambu_bridge.api.inventory import live_usage

    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    state = SimpleNamespace(
        native_gateway=SimpleNamespace(inbox=inbox),
        registry=SimpleNamespace(get=lambda _id: printer()),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    try:
        await tick(service, inbox)
        at_layer(inbox, attempt, 5)
        body = await live_usage("fixture", request, service)
        assert body["layer"] == 5 and body["total_layers"] == 10
        assert body["slots"] == [
            {
                "wire_slot": 0,
                "spool_id": 43,
                "used_g": 4.5,
                "planned_g": 10.0,
                "written_g": 2.5,
                "spool_remaining_g": 433.5,
                "state": "open",
            }
        ]
    finally:
        await service.close()


async def test_a_layer_advance_wakes_the_worker_before_its_interval(tmp_path, monkeypatch):
    import asyncio
    import threading

    ticks = asyncio.Event()
    count = 0

    async def counting(*_args):
        nonlocal count
        count += 1
        if count == 2:
            ticks.set()

    monkeypatch.setattr(consumption_delivery, "deliver_once", counting)
    inbox = NativeInbox(tmp_path / "inbox")
    inventory = SimpleNamespace(consumption_qualified=True)
    async with consumption_delivery.delivery_worker(inventory, lambda: inbox, lambda _id: None):
        await asyncio.sleep(0.05)  # the first tick has run and installed the hook
        assert count == 1 and callable(inbox.on_layer)
        threading.Thread(target=inbox.on_layer).start()  # observe() calls from a thread
        await asyncio.wait_for(ticks.wait(), 2)  # well inside the 10 s interval
    assert count == 2


async def test_an_unchanged_count_writes_nothing_to_the_ledger(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    writes = []
    real_set = usage_ledger._set
    monkeypatch.setattr(
        usage_ledger, "_set", lambda *a, **kw: (writes.append(kw), real_set(*a, **kw))
    )
    try:
        await tick(service, inbox)
        assert writes  # the count and the write landed
        writes.clear()
        await tick(service, inbox)  # same layer, inside the interval: nothing to record
        assert writes == []
    finally:
        await service.close()


async def test_a_silent_spoolman_is_retried_for_hours_before_the_print_waits(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    provider.fail_put = "before"
    failures: dict[str, float] = {}
    try:
        at_layer(inbox, attempt, 10, "completed")
        for _ in range(3):
            await tick(service, inbox, failures=failures)
            clock.now += 10
        assert inbox.accounting_event(attempt)["delivery_state"] == "shadow"  # still trying
        clock.now += 6 * 3600
        await tick(service, inbox, failures=failures)
        assert inbox.accounting_event(attempt)["delivery_state"] == "review"
        assert inbox.accounting_review(attempt) == "delivery_error"
        assert usage_ledger.shortfalls(inbox, attempt) == [
            {
                "kind": "delivery_error",
                "wire_slot": 0,
                "spool_id": 43,
                "grams": 10.0,
                "material": None,
            }
        ]
        assert provider.uses == []
    finally:
        await service.close()


async def test_live_usage_says_how_much_is_planned_and_left(tmp_path, monkeypatch):
    from bambu_bridge.api.inventory import live_usage

    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch)
    state = SimpleNamespace(
        native_gateway=SimpleNamespace(inbox=inbox),
        registry=SimpleNamespace(get=lambda _id: printer()),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    try:
        await tick(service, inbox)  # 2.5 g written; Spoolman then has 433.5 g left
        body = await live_usage("fixture", request, service)
        assert body["title"] == ""
        assert body["slots"] == [
            {
                "wire_slot": 0,
                "spool_id": 43,
                "used_g": 2.5,
                "planned_g": 10.0,
                "written_g": 2.5,
                "spool_remaining_g": 433.5,
                "state": "open",
            }
        ]
    finally:
        await service.close()


async def test_a_late_refill_that_empties_a_charged_spool_still_closes_the_print(
    tmp_path, monkeypatch
):
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch, layer=3)
    try:
        await tick(service, inbox)  # layer 3: 43 is charged 2.5 g, its row stays open
        assert row(inbox, attempt)["written_g"] == 2.5
        # A refill reported at layer 0 moves every layer of wire 0 to wire 2 (no spool).
        switch = {
            "from_slot": 1,
            "to_slot": 3,
            "status": "completed",
            "layer": 0,
            "observed_at": iso(clock.now),
        }
        at_layer(inbox, attempt, 10, "completed")
        failures: dict[str, float] = {}
        await tick(service, inbox, printer([switch]), failures)
        # Nothing is owed on 43 any more: its row closes, what it took stays, and
        # the print waits only for the slot that has no spool.
        assert row(inbox, attempt)["state"] == "done"
        assert provider.uses == [(43, 2.5)] and failures == {}
        assert inbox.accounting_event(attempt)["delivery_state"] == "review"
        assert usage_ledger.shortfalls(inbox, attempt) == [
            {
                "kind": "unassigned_slot",
                "wire_slot": 2,
                "spool_id": None,
                "grams": 10.0,
                "material": None,
            }
        ]
    finally:
        await service.close()


async def test_a_spool_dropped_from_the_count_is_noted_as_over_charged(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(
        tmp_path, monkeypatch, layer=3, wire_spools={"0": 43, "2": 44}
    )
    try:
        await tick(service, inbox)  # 43: 2.5 g
        switch = {
            "from_slot": 1,
            "to_slot": 3,
            "status": "completed",
            "layer": 0,
            "observed_at": iso(clock.now),
        }
        at_layer(inbox, attempt, 10, "completed")
        await tick(service, inbox, printer([switch]))
        assert provider.uses == [(43, 2.5), (44, 10.0)]
        assert inbox.accounting_event(attempt)["delivery_state"] == "delivered"
        assert inbox.accounting_review(attempt) == "over_charged_spool_43_3g"
    finally:
        await service.close()


async def test_many_small_ticks_do_not_drift_from_the_count(tmp_path, monkeypatch):
    # 500 layers of 0.137 g, written every tick: Spoolman's total must equal the
    # counted total, since each delta is the counted total less what was written.
    provider, service, inbox, attempt, clock = await running(
        tmp_path, monkeypatch, layer=1, layer_grams=[{"0": 0.137}] * 500
    )
    try:
        for layer in range(1, 501):
            at_layer(inbox, attempt, layer)
            clock.now += 31
            await tick(service, inbox)
        at_layer(inbox, attempt, 500, "completed")
        await tick(service, inbox)
        assert abs(provider.spools[43]["used_weight"] - 68.5) <= 0.01
        assert abs(row(inbox, attempt)["written_g"] - 68.5) <= 0.01
        assert len(provider.uses) >= 400  # written as it went, in hundredths
    finally:
        await service.close()


async def test_a_refill_split_sums_to_the_print_within_a_hundredth(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(
        tmp_path,
        monkeypatch,
        layer=10,
        layer_grams=[{"0": 0.137}] * 500,
        wire_spools={"0": 43, "2": 44},
    )
    switch = {
        "from_slot": 1,
        "to_slot": 3,
        "status": "completed",
        "layer": 201,
        "observed_at": iso(clock.now),
    }
    try:
        at_layer(inbox, attempt, 500, "completed")
        await tick(service, inbox, printer([switch]))
        first, second = provider.spools[43]["used_weight"], provider.spools[44]["used_weight"]
        assert abs(first + second - 68.5) <= 0.01
        assert abs(first - 200.5 * 0.137) <= 0.01 and abs(second - 299.5 * 0.137) <= 0.01
    finally:
        await service.close()


# --- an inbox written by an earlier release --------------------------------------

# usage_ledger as the first ledger release (c285a38) and the fix release (86e787e)
# created it; later releases added remaining_g, first_layer and last_layer.
LEDGER_AT_FIRST_RELEASE = """
CREATE TABLE usage_ledger (
    attempt_id TEXT NOT NULL REFERENCES uploads(id), spool_id INTEGER NOT NULL,
    counted_g REAL NOT NULL DEFAULT 0, written_g REAL NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'open', sent_before_g REAL, sent_delta_g REAL,
    last_write_at REAL, record TEXT, PRIMARY KEY(attempt_id, spool_id)
)
"""
LEDGER_AT_FIX_RELEASE = LEDGER_AT_FIRST_RELEASE.replace(
    "last_write_at REAL, record TEXT,", "last_write_at REAL, reason TEXT, record TEXT,"
)
LEDGER_BEFORE_LAYERS = LEDGER_AT_FIX_RELEASE.replace(
    "reason TEXT, record TEXT,", "reason TEXT, record TEXT, remaining_g REAL,"
)


def ledger_columns(path):
    import sqlite3

    with sqlite3.connect(path / "inbox" / "native-inbox" / "inbox.sqlite3") as db:
        return [row[1] for row in db.execute("PRAGMA table_info(usage_ledger)")]


@pytest.mark.parametrize(
    "older", [LEDGER_AT_FIRST_RELEASE, LEDGER_AT_FIX_RELEASE, LEDGER_BEFORE_LAYERS]
)
async def test_an_inbox_from_an_earlier_release_grows_the_ledger_columns(
    tmp_path, monkeypatch, older
):
    import sqlite3

    from bambu_bridge.api.inventory import accounting_shadow
    from bambu_bridge.api.inventory_usage import Reassign, reassign_print, spool_history
    from tests.test_usage_review import request_for

    # The inbox file as an older release left it: the ledger table without the
    # later columns, and nothing of the newer tables.
    directory = tmp_path / "inbox" / "native-inbox"
    directory.mkdir(parents=True)
    with sqlite3.connect(directory / "inbox.sqlite3") as db:
        db.executescript(older)
    fresh = tmp_path / "fresh"
    NativeInbox(fresh)  # what a new install gets, for comparison
    fresh_columns = [
        row[1]
        for row in sqlite3.connect(fresh / "native-inbox" / "inbox.sqlite3").execute(
            "PRAGMA table_info(usage_ledger)"
        )
    ]
    provider, service, inbox, attempt, clock = await running(tmp_path, monkeypatch, layer=10)
    try:
        assert sorted(ledger_columns(tmp_path)) == sorted(fresh_columns)
        # The whole path on the upgraded file: debit, listing, move, history.
        at_layer(inbox, attempt, 10, "completed")
        await tick(service, inbox)
        assert provider.uses == [(43, 10.0)]
        page = await accounting_shadow(request_for(inbox), service, 50, 0, None)
        assert page["attempts"][0]["charged"] == [
            {"spool_id": 43, "grams": 10.0, "layers": [1, 10]}
        ]
        body = await reassign_print(
            attempt, Reassign(from_spool_id=43, to_spool_id=44), request_for(inbox), service
        )
        assert body["charged"] == [{"spool_id": 44, "grams": 10.0, "layers": [1, 10]}]
        history = await spool_history(44, request_for(inbox), service, 20)
        assert history["prints"][0]["share_pct"] == 100
        NativeInbox(tmp_path / "inbox")  # opening again changes nothing
        assert sorted(ledger_columns(tmp_path)) == sorted(fresh_columns)
    finally:
        await service.close()
