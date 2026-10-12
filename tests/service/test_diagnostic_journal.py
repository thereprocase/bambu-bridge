from bambu_bridge.service.auto_refill import AutoRefillHistory
from bambu_bridge.service.diagnostic_journal import DiagnosticJournal
from tests.service.test_auto_refill import state


def raw(slot=1, layer=100, name="part"):
    return {**state(slot, [(1, 2)], name=name), "layer_num": layer, "gcode_file": "part.gcode"}


def sample_faults(present=True):
    return {
        "hms": [{"hex": "0300_0100_0001_0001", "text": "Heater fault", "severity": "error"}]
        if present
        else []
    }


def save(path):
    history = AutoRefillHistory()
    current = raw()
    history.update({}, current)
    journal = DiagnosticJournal(path)
    journal.restore({}, current, history, "2026-10-12T10:00:00+00:00")
    journal.update(current, sample_faults(), history, "2026-10-12T10:00:00+00:00")
    return history, journal


def test_restart_recovers_chain_and_captures_the_next_handoff(tmp_path):
    path = tmp_path / "history.json"
    _, journal = save(path)
    restarted = DiagnosticJournal(path)
    history = AutoRefillHistory()
    next_raw = {**raw(2, 101), "hms": state(2, [(2, 2)])["hms"]}
    previous = restarted.restore({}, next_raw, history, "2026-10-12T10:00:00+00:00")
    history.update(previous, next_raw)
    assert [(r["from_slot"], r["to_slot"]) for r in history.transitions] == [(1, 2), (2, 3)]
    assert restarted.faults[0]["first_seen"] == journal.faults[0]["first_seen"]
    assert path.stat().st_mode & 0o777 == 0o600


def test_first_partial_report_does_not_erase_saved_history(tmp_path):
    path = tmp_path / "history.json"
    save(path)
    initial = path.read_bytes()
    journal = DiagnosticJournal(path)
    history = AutoRefillHistory()
    journal.restore({}, {"gcode_state": "RUNNING"}, history, None)
    journal.update({"gcode_state": "RUNNING"}, {}, history, None)
    assert path.read_bytes() == initial
    journal.restore({}, raw(layer=101), history, "2026-10-12T10:00:00+00:00")
    assert len(history.transitions) == 1


def test_new_identity_regressed_layer_or_changed_start_never_inherits_a_chain(tmp_path):
    path = tmp_path / "history.json"
    save(path)
    for current, started in [
        (raw(name="new"), None),
        (raw(layer=1), None),
        (raw(), "2026-10-12T11:00:00+00:00"),
    ]:
        journal = DiagnosticJournal(path)
        history = AutoRefillHistory()
        journal.restore({}, current, history, started)
        assert history.transitions == []


def test_recurrence_counts_episodes_not_telemetry_and_missing_fields_do_not_clear(tmp_path):
    history, journal = save(tmp_path / "history.json")
    first = journal.faults[0]["first_seen"]
    journal.update(raw(), sample_faults(), history, None)
    assert journal.faults[0]["count"] == 1
    journal.update(raw(), sample_faults(False), history, None, {"nozzle_temper": 260})
    assert journal.faults[0]["active"]
    journal.update(raw(), sample_faults(False), history, None, {"hms": []})
    assert not journal.faults[0]["active"] and journal.faults[0]["cleared_at"]
    journal.update(raw(), sample_faults(), history, None)
    assert journal.faults[0]["count"] == 2 and journal.faults[0]["first_seen"] == first


def test_refill_keeps_both_observed_start_and_completion_times():
    history = AutoRefillHistory()
    pending = state(255, [(1, 1)], phase="PAUSE")
    history.update(state(), pending)
    start = history.transitions[0]["started_at"]
    history.update(pending, state(1, [(1, 2)]))
    row = history.transitions[0]
    assert row["started_at"] == start and row["completed_at"] is not None


def test_corrupt_file_and_write_failure_do_not_crash_telemetry(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("broken json")
    journal = DiagnosticJournal(path)
    assert journal.faults == []
    bad = tmp_path / "file"
    bad.write_text("not a directory")
    journal = DiagnosticJournal(bad / "journal.json")
    history = AutoRefillHistory()
    journal.update(raw(), sample_faults(), history, None)
    assert not journal.durable


def test_new_print_archives_the_previous_refill_details(tmp_path):
    path = tmp_path / "history.json"
    history, journal = save(path)
    previous = raw()
    current = raw(name="next part", layer=1)
    history.update(previous, current)
    journal.update(current, {"hms": []}, history, None)
    assert history.transitions == []
    assert journal.archived_refills[0]["refills"][0]["from_slot"] == 1
    assert journal.archived_refills[0]["print"]["subtask_name"] == "part"


def test_partial_identity_waits_for_the_rest_of_the_startup_report(tmp_path):
    path = tmp_path / "history.json"
    save(path)
    journal = DiagnosticJournal(path)
    history = AutoRefillHistory()
    partial = {"gcode_state": "RUNNING", "subtask_name": "part", "layer_num": 101}
    journal.restore({}, partial, history, None)
    journal.update(partial, {}, history, None)
    assert not journal._restored
    journal.restore({}, raw(layer=101), history, "2026-10-12T10:00:00+00:00")
    assert len(history.transitions) == 1
