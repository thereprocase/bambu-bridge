"""M2: EventBus fan-out, per-subscriber isolation, drop-oldest on overflow."""

from __future__ import annotations

import pytest

from bambu_bridge.service.events import Event, EventBus


@pytest.mark.asyncio
async def test_fan_out_to_all_subscribers() -> None:
    bus = EventBus()
    async with bus.subscribe() as a, bus.subscribe() as b:
        bus.publish(Event("delta", {"mc_percent": 5}))
        ea = await a.get()
        eb = await b.get()
    assert ea.data == eb.data == {"mc_percent": 5}


@pytest.mark.asyncio
async def test_unsubscribe_on_context_exit() -> None:
    bus = EventBus()
    async with bus.subscribe():
        assert bus.subscriber_count == 1
    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_full_queue_drops_oldest() -> None:
    bus = EventBus(maxsize=2)
    async with bus.subscribe() as sub:
        bus.publish(Event("delta", {"n": 1}))
        bus.publish(Event("delta", {"n": 2}))
        bus.publish(Event("delta", {"n": 3}))  # evicts n=1
        first = await sub.get()
        second = await sub.get()
    assert [first.data["n"], second.data["n"]] == [2, 3]


def test_event_to_wire_shapes() -> None:
    assert Event("snapshot", {"a": 1}).to_wire() == {
        "type": "snapshot",
        "data": {"a": 1},
    }
    assert Event("event", {"job_id": "x"}, name="print_started").to_wire() == {
        "type": "event",
        "event": "print_started",
        "data": {"job_id": "x"},
    }
