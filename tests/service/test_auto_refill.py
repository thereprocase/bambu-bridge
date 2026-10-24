from bambu_bridge.service.auto_refill import AutoRefillHistory


def state(slot=0, codes=(), phase="RUNNING", name="part"):
    return {
        "gcode_state": phase,
        "subtask_name": name,
        "ams": {"tray_now": str(slot)},
        "hms": [
            {"attr": 0x07002000 + (source - 1) * 0x100, "code": 0x30000 + stage}
            for source, stage in codes
        ],
    }


def test_successive_refills_keep_history_and_ignore_normal_colour_changes():
    h = AutoRefillHistory()
    a = state()
    b = state(1, [(1, 2)])
    c = state(2, [(1, 2), (2, 2)])
    h.update({}, a)
    h.update(a, b)
    h.update(b, c)
    assert [(r["from_slot"], r["to_slot"], r["status"]) for r in h.transitions] == [
        (1, 2, "completed"),
        (2, 3, "completed"),
    ]
    h.update(c, state(3, [(1, 2), (2, 2)]))
    assert len(h.transitions) == 2 and h.transitions[0]["to_slot"] == 2


def test_switching_is_not_success_and_completion_updates_one_event():
    h = AutoRefillHistory()
    a = state()
    b = state(255, [(1, 1)], "PAUSE")
    h.update(a, b)
    assert h.transitions[0]["status"] == "switching" and h.transitions[0]["to_slot"] is None
    h.update(b, state(1, [(1, 2)]))
    assert len(h.transitions) == 1 and h.transitions[0]["status"] == "completed"
    assert h.transitions[0]["to_slot"] == 2


def test_no_destination_invented_for_external_or_unknown_or_seeded_chain():
    h = AutoRefillHistory()
    h.update({}, state(254, [(1, 2)]))
    assert h.transitions[0]["to_slot"] is None
    h = AutoRefillHistory()
    h.update({}, state(2, [(1, 2), (2, 2)]))
    assert all(r["to_slot"] is None for r in h.transitions)


def test_reconnect_keeps_history_new_print_resets_and_ignores_latched_codes():
    h = AutoRefillHistory()
    a = state()
    b = state(1, [(1, 2)])
    h.update(a, b)
    h.update({}, b)
    assert len(h.transitions) == 1
    finished = state(255, [(1, 2)], "FINISH")
    h.update(b, finished)
    h.update(finished, state(1, [(1, 2)], name="new part"))
    assert h.transitions == []


def test_real_runout_and_feed_errors_never_become_success():
    h = AutoRefillHistory()
    a = state()
    for code in (0x20001, 0x20002, 0x20011):
        b = state(1)
        b["hms"] = [{"attr": 0x07002000, "code": code}]
        h.update(a, b)
    assert h.transitions == []


def test_success_code_before_tray_change_fills_destination_on_later_report():
    h = AutoRefillHistory()
    a = state()
    b = state(0, [(1, 2)])
    h.update(a, b)
    assert h.transitions[0]["to_slot"] is None
    h.update(b, state(1, [(1, 2)]))
    assert h.transitions[0]["to_slot"] == 2


def test_failed_switch_is_not_a_success():
    h = AutoRefillHistory()
    a = state()
    b = state(255, [(1, 1)], "PAUSE")
    h.update(a, b)
    h.update(b, state(255, [(1, 1)], "FAILED"))
    assert h.transitions[0]["status"] == "interrupted"


async def test_service_snapshot_exposes_chain_through_partial_reports():
    from bambu_bridge.protocol.models import ReportMessage
    from bambu_bridge.service.printer import PrinterService
    from tests.conftest import ACCESS_CODE, SERIAL

    svc = PrinterService(SERIAL, "127.0.0.1", ACCESS_CODE, friendly_name="Fixture")
    for report in [state(), state(1, [(1, 2)]), state(2, [(1, 2), (2, 2)])]:
        await svc._handle_report(ReportMessage.parse({"print": report}))
    await svc._handle_report(ReportMessage.parse({"print": {"hms": []}}))
    rows = svc.snapshot()["ams"]["refill_history"]
    assert [(r["from_slot"], r["to_slot"]) for r in rows] == [(1, 2), (2, 3)]
    assert svc.snapshot()["hms"] == []


def test_refill_keeps_the_layer_where_the_old_spool_ran_out():
    h = AutoRefillHistory()
    a = {**state(), "layer_num": 41}
    b = {**state(255, [(1, 1)], "PAUSE"), "layer_num": 42}
    c = {**state(1, [(1, 2)]), "layer_num": 43}
    h.update({}, a)
    h.update(a, b)
    h.update(b, c)
    assert [(r["from_slot"], r["to_slot"], r["layer"]) for r in h.transitions] == [(1, 2, 42)]
    h.update({}, state(5))
    h.update(state(5), state(1, [(1, 2)]))
    assert h.transitions[-1]["layer"] is None
