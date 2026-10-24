"""A print runs dry on AMS slot 3 and the firmware hands it to slot 4: replayed from frames.

The other usage tests start from a finished transition row. This one feeds the
printer's own telemetry frames (tray_now, layer_num, the 0700_2x00_0003 notices)
through AutoRefillHistory and delivers the result to Spoolman, so the seam
between the two is covered as well.
"""

import pytest

from bambu_bridge.service import auto_refill
from bambu_bridge.service.auto_refill import AutoRefillHistory
from tests.test_usage_counting import printer
from tests.test_usage_ledger import at_layer, running, tick

# Uneven grams per layer, so a split by layer count cannot pass by luck.
LAYERS = [{"0": round(0.2 + (n % 7) * 0.05, 3)} for n in range(100)]
PLANNED = round(sum(layer["0"] for layer in LAYERS), 2)
SWITCH_LAYER = 41


def frame(tray, layer, codes=(), phase="RUNNING"):
    return {
        "gcode_state": phase,
        "subtask_name": "part",
        "layer_num": layer,
        "ams": {"tray_now": str(tray)},
        "hms": [
            {"attr": 0x07002000 + (source - 1) * 0x100, "code": 0x30000 + stage}
            for source, stage in codes
        ],
    }


class FakeDatetime:
    """Stamps refill rows from the test clock so they fall inside the print window."""

    clock = None

    @classmethod
    def now(cls, tz=None):
        from datetime import datetime

        return datetime.fromtimestamp(cls.clock.now, tz)


def replay_runout(history, *, at=SWITCH_LAYER):
    """Slot 3 (tray_now 2) feeds up to `at`, runs out, and slot 4 (tray_now 3) takes over."""
    steady = frame(2, at - 1)
    history.update({}, steady)
    switching = frame(255, at, [(3, 1)], "PAUSE")  # tray unloaded while the notice is up
    history.update(steady, switching)
    done = frame(3, at, [(3, 2)])
    history.update(switching, done)
    return done


async def run_replay(tmp_path, monkeypatch, *, final_layer, outcome):
    provider, service, inbox, attempt, clock = await running(
        tmp_path,
        monkeypatch,
        layer=final_layer,
        layer_grams=LAYERS,
        tool_wires={"0": 2},
        wire_spools={"2": 43, "3": 44},
    )
    FakeDatetime.clock = clock
    monkeypatch.setattr(auto_refill, "datetime", FakeDatetime)
    history = AutoRefillHistory()
    replay_runout(history)
    at_layer(inbox, attempt, final_layer, outcome)
    device = printer()
    device.auto_refill = history
    await tick(service, inbox, device)
    return provider, service, history


async def test_a_completed_print_charges_each_slot_for_its_own_layers(tmp_path, monkeypatch):
    provider, service, history = await run_replay(
        tmp_path, monkeypatch, final_layer=100, outcome="completed"
    )
    try:
        row = history.transitions[0]
        assert (row["from_slot"], row["to_slot"], row["layer"]) == (3, 4, SWITCH_LAYER)
        before = sum(layer["0"] for layer in LAYERS[: SWITCH_LAYER - 1])
        shared = LAYERS[SWITCH_LAYER - 1]["0"]  # the layer the old spool ran out on, split evenly
        after = sum(layer["0"] for layer in LAYERS[SWITCH_LAYER:])
        slot3, slot4 = provider.spools[43]["used_weight"], provider.spools[44]["used_weight"]
        assert slot3 == pytest.approx(before + shared / 2, abs=0.01)
        assert slot4 == pytest.approx(after + shared / 2, abs=0.01)
        assert slot3 + slot4 == pytest.approx(PLANNED, abs=0.01)
        assert {spool for spool, _ in provider.uses} == {43, 44}
    finally:
        await service.close()


async def test_a_print_stopped_after_the_switch_charges_both_slots_up_to_the_layer(
    tmp_path, monkeypatch
):
    provider, service, _ = await run_replay(
        tmp_path, monkeypatch, final_layer=60, outcome="interrupted"
    )
    try:
        # layers 1-59 whole, layer 60 by half
        expected = sum(layer["0"] for layer in LAYERS[:59]) + LAYERS[59]["0"] / 2
        slot3, slot4 = provider.spools[43]["used_weight"], provider.spools[44]["used_weight"]
        assert slot3 + slot4 == pytest.approx(expected, abs=0.01)
        assert slot3 == pytest.approx(
            sum(layer["0"] for layer in LAYERS[:40]) + LAYERS[40]["0"] / 2, abs=0.01
        )
    finally:
        await service.close()


async def test_a_print_stopped_before_the_switch_charges_only_slot_3(tmp_path, monkeypatch):
    provider, service, _ = await run_replay(
        tmp_path, monkeypatch, final_layer=30, outcome="interrupted"
    )
    try:
        assert provider.spools[44]["used_weight"] == 0.0
        expected = sum(layer["0"] for layer in LAYERS[:29]) + LAYERS[29]["0"] / 2
        assert provider.spools[43]["used_weight"] == pytest.approx(expected, abs=0.01)
    finally:
        await service.close()


async def test_debits_already_written_before_the_switch_are_not_charged_twice(
    tmp_path, monkeypatch
):
    # The ledger writes as the print goes. Slot 3 is debited through layer 38 before
    # the runout is seen; the rest must land on slot 4 with the totals unchanged.
    provider, service, inbox, attempt, clock = await running(
        tmp_path,
        monkeypatch,
        layer=38,
        layer_grams=LAYERS,
        tool_wires={"0": 2},
        wire_spools={"2": 43, "3": 44},
    )
    FakeDatetime.clock = clock
    monkeypatch.setattr(auto_refill, "datetime", FakeDatetime)
    try:
        await tick(service, inbox)
        early = provider.spools[43]["used_weight"]
        assert early == pytest.approx(sum(layer["0"] for layer in LAYERS[:37]) + 0.1, abs=0.5)
        history = AutoRefillHistory()
        replay_runout(history)
        at_layer(inbox, attempt, 100, "completed")
        device = printer()
        device.auto_refill = history
        await tick(service, inbox, device)
        slot3, slot4 = provider.spools[43]["used_weight"], provider.spools[44]["used_weight"]
        assert slot3 + slot4 == pytest.approx(PLANNED, abs=0.01)
        assert slot3 == pytest.approx(
            sum(layer["0"] for layer in LAYERS[:40]) + LAYERS[40]["0"] / 2, abs=0.01
        )
    finally:
        await service.close()


def test_a_tray_change_without_the_firmware_notice_is_not_a_refill():
    # Pins today's rule (auto_refill.py docstring): tray_now alone also moves for
    # multicolour jobs, so without the 0700_2x00_0003 notice no switch is recorded
    # and the whole print stays on slot 3. If a printer ever refills silently,
    # its grams would be charged to the empty spool.
    history = AutoRefillHistory()
    before = frame(2, 40)
    history.update({}, before)
    history.update(before, frame(3, 41))
    assert history.transitions == []


# --- The tray_now fallback when the firmware notice never comes ----------------


def run_frames(history, frames):
    previous = {}
    for current in frames:
        history.update(previous, current)
        previous = current


AUTO_REFILL_ON = 0x7 | (1 << 10)  # axes homed, plus the auto-refill bit


def printing(tray, layer, stage=0, home_flag=AUTO_REFILL_ON):
    # VERIFIED live on the P1S (2026-10-24): printing layer 2 reports stg_cur 0 with
    # gcode_state RUNNING, so stage 0 is what the fallback may trust as "printing".
    return {**frame(tray, layer), "stg_cur": stage, "home_flag": home_flag}


def grouped():
    history = AutoRefillHistory()
    history.group = (3, 4)
    return history


def test_the_fallback_fires_once_and_is_flagged_for_review():
    history = grouped()
    run_frames(
        history,
        [printing(2, 40), printing(2, 41)]
        + [printing(3, 42)] * 3  # on the backup, but layers not yet advancing
        + [printing(3, 43), printing(3, 44), printing(3, 45)],
    )
    assert len(history.transitions) == 1
    row = history.transitions[0]
    assert (row["from_slot"], row["to_slot"], row["status"]) == (3, 4, "completed")
    assert row["review"] is True and row["source"] == "tray_now"
    assert row["layer"] == 42  # the layer the backup first fed


def test_the_fallback_needs_frames_and_advancing_layers():
    history = grouped()
    run_frames(history, [printing(2, 40), printing(3, 41), printing(3, 41)])
    assert history.transitions == []  # two frames on the backup
    history = grouped()
    run_frames(history, [printing(2, 40), printing(3, 41), printing(3, 42)])
    assert history.transitions == []  # layers advance, but only two frames
    history = grouped()
    run_frames(history, [printing(2, 40)] + [printing(3, 41)] * 10)
    assert history.transitions == []  # layer never moved


def test_the_fallback_stays_quiet_without_an_active_group_or_a_move_from_the_primary():
    history = AutoRefillHistory()  # no group
    run_frames(history, [printing(2, 40)] + [printing(3, n) for n in range(41, 46)])
    assert history.transitions == []
    history = grouped()  # tray 1 to the backup: not this group's primary
    run_frames(history, [printing(0, 40)] + [printing(3, n) for n in range(41, 46)])
    assert history.transitions == []


def test_flapping_during_a_load_does_not_fire():
    history = grouped()
    run_frames(
        history,
        [printing(2, 40)]
        + [printing(3, 40, stage=4)] * 3  # loading: tray_now touches the backup
        + [printing(2, 40), printing(3, 40, stage=4), printing(2, 40)]
        + [printing(2, n) for n in range(41, 46)],
    )
    assert history.transitions == []


def test_a_candidate_dies_when_the_stage_leaves_printing():
    history = grouped()
    run_frames(
        history,
        [printing(2, 40), printing(3, 41), printing(3, 42, stage=7)]
        + [printing(3, n) for n in range(43, 47)],
    )
    assert history.transitions == []


def test_a_paused_print_does_not_fire():
    history = grouped()
    paused = lambda tray, n: {**printing(tray, n), "gcode_state": "PAUSE"}  # noqa: E731
    run_frames(history, [printing(2, 40)] + [paused(3, n) for n in range(41, 47)])
    assert history.transitions == []


def test_a_switch_that_resumes_printing_after_a_change_stage_still_fires():
    history = grouped()
    run_frames(
        history,
        [printing(2, 40), printing(3, 41, stage=4), printing(3, 41, stage=4)]
        + [printing(3, n) for n in range(41, 46)],
    )
    assert [(r["from_slot"], r["to_slot"]) for r in history.transitions] == [(3, 4)]


def test_the_firmware_notice_first_leaves_the_fallback_silent():
    history = grouped()
    run_frames(
        history,
        [printing(2, 40), {**frame(255, 41, [(3, 1)], "PAUSE"), "stg_cur": 6}]
        + [{**frame(3, 41, [(3, 2)]), "stg_cur": 0}]
        + [printing(3, n) for n in range(42, 48)],
    )
    assert len(history.transitions) == 1
    assert "review" not in history.transitions[0] and "source" not in history.transitions[0]


def test_a_late_notice_replaces_the_inferred_row_instead_of_doubling_it():
    history = grouped()
    steady = [printing(2, 40)] + [printing(3, n) for n in range(41, 46)]
    run_frames(history, steady)
    assert history.transitions[0]["source"] == "tray_now"
    notice = {**frame(3, 46, [(3, 2)]), "stg_cur": 0}
    history.update(steady[-1], notice)
    assert len(history.transitions) == 1
    row = history.transitions[0]
    assert (row["from_slot"], row["to_slot"], row["status"]) == (3, 4, "completed")
    assert "review" not in row and row["layer"] == 41  # the earlier, inferred layer


async def test_an_inferred_switch_is_charged_and_the_print_is_flagged(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(
        tmp_path,
        monkeypatch,
        layer=100,
        layer_grams=LAYERS,
        tool_wires={"0": 2},
        wire_spools={"2": 43, "3": 44},
    )
    FakeDatetime.clock = clock
    monkeypatch.setattr(auto_refill, "datetime", FakeDatetime)
    history = grouped()
    run_frames(history, [printing(2, 40)] + [printing(3, n) for n in range(41, 46)])
    at_layer(inbox, attempt, 100, "completed")
    device = printer()
    device.auto_refill = history
    try:
        await tick(service, inbox, device)
        slot3, slot4 = provider.spools[43]["used_weight"], provider.spools[44]["used_weight"]
        assert slot3 + slot4 == pytest.approx(PLANNED, abs=0.01)
        assert slot3 == pytest.approx(
            sum(layer["0"] for layer in LAYERS[:40]) + LAYERS[40]["0"] / 2, abs=0.01
        )
        assert inbox.accounting_review(attempt) == "refill_inferred_slot_3_to_4"
    finally:
        await service.close()


async def test_a_job_that_wires_the_backup_on_purpose_is_not_a_refill(tmp_path, monkeypatch):
    provider, service, inbox, attempt, clock = await running(
        tmp_path,
        monkeypatch,
        layer=100,
        layer_grams=[{"0": 0.2, "1": 0.2}] * 100,
        tool_wires={"0": 2, "1": 3},
        wire_spools={"2": 43, "3": 44},
    )
    FakeDatetime.clock = clock
    monkeypatch.setattr(auto_refill, "datetime", FakeDatetime)
    history = grouped()
    run_frames(history, [printing(2, 40)] + [printing(3, n) for n in range(41, 46)])
    at_layer(inbox, attempt, 100, "completed")
    device = printer()
    device.auto_refill = history
    try:
        await tick(service, inbox, device)
        assert provider.spools[43]["used_weight"] == pytest.approx(20.0, abs=0.01)
        assert provider.spools[44]["used_weight"] == pytest.approx(20.0, abs=0.01)
        assert not inbox.accounting_review(attempt)
    finally:
        await service.close()


@pytest.mark.parametrize(
    "home_flag",
    [0x7, 0, None, -1, "1024", True],
    ids=["off", "zero", "absent", "neg", "str", "bool"],
)
def test_the_fallback_needs_the_printer_to_report_auto_refill_on(home_flag):
    # A manual move from the primary to the backup with auto-refill off is not a refill.
    history = grouped()
    run_frames(
        history,
        [printing(2, 40)] + [printing(3, n, home_flag=home_flag) for n in range(41, 48)],
    )
    assert history.transitions == []


def test_auto_refill_switched_off_mid_candidate_cancels_it():
    history = grouped()
    run_frames(
        history,
        [printing(2, 40), printing(3, 41), printing(3, 42)]
        + [printing(3, n, home_flag=0x7) for n in range(43, 48)],
    )
    assert history.transitions == []


def test_the_same_frames_with_auto_refill_on_do_infer():
    history = grouped()
    run_frames(history, [printing(2, 40)] + [printing(3, n) for n in range(41, 48)])
    assert len(history.transitions) == 1
