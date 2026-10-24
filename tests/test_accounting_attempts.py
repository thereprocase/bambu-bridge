"""Canonical-attempt shadow accounting; no printer or provider involved."""

import copy
import sqlite3

import pytest

from bambu_bridge.gcode_usage import PARSER_VERSION
from bambu_bridge.native_inbox import NativeInbox


def manifest():
    return {
        "artifact_sha256": "a" * 64,
        "plate": 1,
        "parser_version": PARSER_VERSION,
        "provider_id": "fixture-provider",
        "assignment_revision": 12,
        "sliced_settings": {"filament_diameter": [1.75]},
        "planned_grams": 10.5,
        "analysis_issues": [],
        "layer_grams": [{"0": 10.5}],
        "tool_wires": {"0": 0},
        "wire_spools": {"0": 7},
        "tray_materials": {},
        "slot_identities": {},
    }


def reserve(inbox):
    return inbox.claim_external("fixture", {"print": {"command": "project_file"}}, reserved=True)


def terminal(inbox, attempt, outcome):
    with inbox.connect() as db:
        db.execute("UPDATE uploads SET start_state=? WHERE id=?", (outcome, attempt))


def test_context_is_immutable_and_restart_recovery_does_not_duplicate(tmp_path):
    inbox = NativeInbox(tmp_path)
    attempt = reserve(inbox)
    original = manifest()
    inbox.freeze_accounting(attempt, original)
    original["wire_spools"]["0"] = 999
    with pytest.raises(ValueError, match="CONFLICT"):
        inbox.freeze_accounting(attempt, original)
    terminal(inbox, attempt, "completed")
    inbox = NativeInbox(tmp_path)
    inbox.freeze_accounting(attempt, manifest())
    terminal(inbox, attempt, "completed")
    events = inbox.accounting_events()
    assert len(events) == 1
    assert events[0]["attempt_id"] == attempt
    assert events[0]["manifest"]["wire_spools"] == {"0": 7}
    assert events[0]["delivery_state"] == "shadow"


def test_same_artifact_reprint_is_another_attempt_and_abort_is_review_evidence(tmp_path):
    inbox = NativeInbox(tmp_path)
    attempts = []
    for outcome in ("completed", "completed", "interrupted"):
        attempt = reserve(inbox)
        attempts.append(attempt)
        inbox.freeze_accounting(attempt, manifest())
        terminal(inbox, attempt, outcome)
    events = inbox.accounting_events()
    assert {event["attempt_id"] for event in events} == set(attempts)
    assert sorted(event["outcome"] for event in events) == ["completed", "completed", "interrupted"]
    assert all(event["delivery_state"] == "shadow" for event in events)


def test_missing_or_late_manifest_cannot_retroactively_attribute_job(tmp_path):
    inbox = NativeInbox(tmp_path)
    with pytest.raises(ValueError, match="MISSING"):
        inbox.freeze_accounting("missing", manifest())
    attempt = reserve(inbox)
    terminal(inbox, attempt, "completed")
    with pytest.raises(ValueError, match="ALREADY_DISPATCHED"):
        inbox.freeze_accounting(attempt, manifest())
    assert inbox.accounting_events() == []


def test_terminal_and_shadow_event_rollback_together(tmp_path):
    inbox = NativeInbox(tmp_path)
    attempt = reserve(inbox)
    inbox.freeze_accounting(attempt, manifest())
    with inbox.connect() as db:
        db.execute("""CREATE TRIGGER test_disk_failure BEFORE INSERT ON accounting_shadow_events
            BEGIN SELECT RAISE(ABORT,'simulated_disk_failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="simulated_disk_failure"):
        terminal(inbox, attempt, "completed")
    assert inbox.get(attempt)["start_state"] == "reserved"
    assert inbox.accounting_events() == []
    with inbox.connect() as db:
        db.execute("DROP TRIGGER test_disk_failure")
    terminal(inbox, attempt, "completed")
    assert len(inbox.accounting_events()) == 1


@pytest.mark.parametrize(
    "key,value",
    [
        ("artifact_sha256", "bad"),
        ("plate", True),
        ("assignment_revision", -1),
        ("planned_grams", float("nan")),
        ("parser_version", "unknown"),
        ("layer_grams", [{"0": float("inf")}]),
        ("tool_wires", {"0": -1}),
        ("wire_spools", {"a": 7}),
        ("tray_materials", {"0": {"material": 1, "color": None}}),
        ("tray_materials", {"0": {"material": "PETG"}}),
    ],
)
def test_invalid_context_never_reserves(tmp_path, key, value):
    inbox = NativeInbox(tmp_path)
    attempt = reserve(inbox)
    invalid = copy.deepcopy(manifest())
    invalid[key] = value
    with pytest.raises(ValueError):
        inbox.freeze_accounting(attempt, invalid)
    inbox.freeze_accounting(attempt, manifest())


def test_uploaded_artifact_digest_must_match_reserved_context(tmp_path):
    inbox = NativeInbox(tmp_path)
    with inbox.connect() as db:
        db.execute(
            "INSERT INTO uploads (id,printer,logical,remote,state,start_state,created,sha256) "
            "VALUES ('fixture','fixture','/part.3mf','/part.3mf','stored','queued',0,?)",
            ("b" * 64,),
        )
    with pytest.raises(ValueError, match="ARTIFACT_CHANGED"):
        inbox.freeze_accounting("fixture", manifest())
    matching = manifest()
    matching["artifact_sha256"] = "b" * 64
    inbox.freeze_accounting("fixture", matching)
    terminal(inbox, "fixture", "completed")
    assert inbox.accounting_events()[0]["manifest"]["artifact_sha256"] == "b" * 64
