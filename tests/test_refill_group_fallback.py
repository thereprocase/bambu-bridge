"""The stored refill group tells the print history which pair is lying, with no app polling."""

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from bambu_bridge.refill import RefillController, RefillStore
from bambu_bridge.service.auto_refill import AutoRefillHistory
from bambu_bridge.service.refill_adapter import restore_when_idle
from tests.test_usage_refill_replay import printing, run_frames


def group(states=("owned", "owned"), *, enabled=True, wires=(2, 3)):
    return {
        "enabled": enabled,
        "state": "pending",
        "steps": [
            {"wire": wire, "state": state, "before": {}, "target": {}, "source_generation": "g"}
            for wire, state in zip(wires, states, strict=True)
        ],
    }


def stored(tmp_path, body, printer="p1"):
    store = RefillStore(tmp_path)
    store.save(printer, body, 0)
    return store


@pytest.mark.parametrize(
    "body, expected",
    [
        (group(), (3, 4)),
        (group(wires=(0, 1)), (1, 2)),
        (group(("owned", "pending")), None),  # still being written
        (group(("owned", "uncertain")), None),  # in doubt
        (group(("owned", "applying")), None),
        (group(("restoring", "owned")), None),
        (group(("owned", "owned"), enabled=False), None),  # being restored: no longer lying
        (group(("owned",), wires=(2,)), None),  # a pair needs two members
        ({"enabled": True, "steps": []}, None),
        (None, None),
    ],
)
def test_only_a_fully_written_enabled_pair_is_a_group(body, expected):
    assert RefillController.fallback_group(body) == expected


def test_every_saved_change_reaches_the_attached_history(tmp_path):
    store = RefillStore(tmp_path)
    controller = RefillController(store)
    history = AutoRefillHistory()
    controller.attach("p1", history)
    assert history.group is None  # nothing stored yet
    saved = store.save("p1", group(("pending", "pending")), 0)
    assert history.group is None
    saved = store.save("p1", group(), saved["revision"])
    assert history.group == (3, 4)
    saved = store.save("p1", group(("owned", "uncertain")), saved["revision"])
    assert history.group is None
    saved = store.save("p1", group(), saved["revision"])
    controller.disable("p1", revision=saved["revision"])
    assert history.group is None
    store.close()


def test_another_printers_save_does_not_move_this_history(tmp_path):
    store = RefillStore(tmp_path)
    controller = RefillController(store)
    mine, other = AutoRefillHistory(), AutoRefillHistory()
    controller.attach("p1", mine)
    controller.attach("p2", other)
    store.save("p2", group(), 0)
    assert mine.group is None and other.group == (3, 4)
    store.close()


async def test_a_restart_restores_the_group_before_anything_asks(tmp_path):
    first = stored(tmp_path, group())
    first.close()  # the bridge stops; the group stays in refill.sqlite3
    controller = RefillController(RefillStore(tmp_path))
    history = AutoRefillHistory()
    printer = SimpleNamespace(serial="p1", auto_refill=history)
    # A long interval: the group must be known without waiting for any pass.
    task = asyncio.create_task(restore_when_idle(lambda: [printer], controller, interval=3600))
    try:
        await asyncio.sleep(0.05)
        assert history.group == (3, 4)
        # And it works on a print that starts after the restart: a silent refill is inferred.
        run_frames(history, [printing(2, 40)] + [printing(3, n) for n in range(41, 46)])
        assert [(r["from_slot"], r["to_slot"], r["review"]) for r in history.transitions] == [
            (3, 4, True)
        ]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        controller.store.close()


async def test_a_printer_added_after_startup_is_attached_on_a_later_pass(tmp_path):
    controller = RefillController(stored(tmp_path, group()))
    printers: list = []
    task = asyncio.create_task(restore_when_idle(lambda: printers, controller, interval=0.02))
    try:
        await asyncio.sleep(0.05)
        late = SimpleNamespace(serial="p1", auto_refill=AutoRefillHistory())
        printers.append(late)
        await asyncio.sleep(0.1)
        assert late.auto_refill.group == (3, 4)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        controller.store.close()


async def test_a_printer_without_a_print_history_is_skipped(tmp_path):
    controller = RefillController(stored(tmp_path, group()))
    task = asyncio.create_task(
        restore_when_idle(lambda: [SimpleNamespace(serial="p1")], controller, interval=0.02)
    )
    try:
        await asyncio.sleep(0.1)
        assert not task.done()  # the worker survived
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        controller.store.close()
