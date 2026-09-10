"""Service-level filament memory tests (G3).

Tests the invalidation logic and snapshot integration by driving the
PrinterService with synthetic MQTT reports — the same pattern as
tests/service/test_printer.py. No live network; uses the conftest fixtures.

Key behaviours:
 - On seed: filament memory is loaded from the DB loader hook.
 - Per-report: a slot's type changing to a DIFFERENT non-empty type invalidates.
 - Per-report: same type re-appearing does NOT invalidate.
 - Per-report: empty/absent type does NOT invalidate.
 - Snapshot: memory fields appear per slot when the cache is populated.
 - Snapshot: memory is null per slot when cache not wired (None).
"""

from __future__ import annotations

import asyncio

import pytest

from bambu_bridge.db.jobs import FilamentMemory
from bambu_bridge.service.events import Event
from bambu_bridge.service.printer import PrinterService
from tests.conftest import ACCESS_CODE, SERIAL, MockPrinter


def _service(port: int, **kwargs: object) -> PrinterService:
    return PrinterService(
        SERIAL,
        "127.0.0.1",
        ACCESS_CODE,
        friendly_name="Workshop P1S",
        model="P1S",
        mqtt_port=port,
        **kwargs,
    )


async def _next(sub: object, predicate: object, timeout: float = 10.0) -> Event:
    async with asyncio.timeout(timeout):
        while True:
            ev = await sub.get()
            if predicate(ev):
                return ev


def _ams_report(slot_types: dict[int, str]) -> dict[str, object]:
    """Build a minimal push_status with AMS trays having the given types.

    slot_types: {physical_slot (1-based): tray_type_str}
    """
    trays = []
    for i in range(4):
        physical = i + 1
        tt = slot_types.get(physical, "")
        trays.append({"id": str(i), "tray_type": tt, "tray_color": "FFFFFFFF", "remain": 50})
    return {
        "print": {
            "command": "push_status",
            "sequence_id": "1",
            "gcode_state": "IDLE",
            "ams": {
                "ams": [{"id": "0", "tray": trays}],
                "tray_now": "255",
            },
        }
    }


# --------------------------------------------------------------------------- #
# Memory loading on seed
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_memory_loaded_from_hook_on_seed(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """The loader is called once on the first report; the cache is populated."""
    loaded: list[int] = []
    initial = {
        1: FilamentMemory(slot=1, make="Bambu", model="PLA", profile=None,
                          tray_type_seen="PLA", updated_at=0),
        3: FilamentMemory(slot=3, make="Polymaker", model="PETG", profile=None,
                          tray_type_seen="PETG", updated_at=0),
    }

    async def _load() -> dict[int, FilamentMemory]:
        loaded.append(1)
        return dict(initial)

    svc = _service(mqtt_broker, load_filament_memory=_load)
    async with svc.bus.subscribe() as sub:
        await svc.start()
        try:
            await _next(sub, lambda e: e.type == "snapshot")
            assert loaded == [1], "loader called exactly once on seed"
            assert svc.filament_memory[1].make == "Bambu"
            assert svc.filament_memory[3].make == "Polymaker"
        finally:
            await svc.stop()


@pytest.mark.asyncio
async def test_memory_cache_none_when_no_loader(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Without a loader hook, the cache stays None (snapshot gets null memory per slot)."""
    svc = _service(mqtt_broker)  # no load_filament_memory
    async with svc.bus.subscribe() as sub:
        await svc.start()
        try:
            await _next(sub, lambda e: e.type == "snapshot")
            assert svc._filament_memory is None  # noqa: SLF001
        finally:
            await svc.stop()


@pytest.mark.asyncio
async def test_loader_called_only_once_on_reconnect(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """The loader must not overwrite the live cache on reconnect (cache persists)."""
    call_count = [0]
    initial = {
        1: FilamentMemory(slot=1, make="Bambu", model=None, profile=None,
                          tray_type_seen="PLA", updated_at=0),
    }

    async def _load() -> dict[int, FilamentMemory]:
        call_count[0] += 1
        return dict(initial)

    svc = _service(mqtt_broker, load_filament_memory=_load)
    async with svc.bus.subscribe() as sub:
        await svc.start()
        try:
            await _next(sub, lambda e: e.type == "snapshot")
            # Manually inject a cache update (simulates a PUT after seed).
            svc.set_filament_memory_entry(
                2, FilamentMemory(slot=2, make="PM", model=None, profile=None,
                                  tray_type_seen="PETG", updated_at=0)
            )
            # Push a delta — this does NOT call the loader again.
            await mock_printer.push_report({"print": {"mc_percent": 10}})
            await _next(sub, lambda e: e.type == "delta")
            # Slot 2 entry must still be present (loader not re-called).
            assert call_count[0] == 1
            assert svc.filament_memory.get(2) is not None
        finally:
            await svc.stop()


# --------------------------------------------------------------------------- #
# Invalidation on tray type change
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_invalidation_on_different_type(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """When slot 1 changes from PLA to PETG, its label is invalidated."""
    invalidated_slots: list[int] = []

    async def _load() -> dict[int, FilamentMemory]:
        return {
            1: FilamentMemory(slot=1, make="Bambu", model=None, profile=None,
                              tray_type_seen="PLA", updated_at=0),
        }

    async def _invalidate(slot: int) -> None:
        invalidated_slots.append(slot)

    svc = _service(
        mqtt_broker,
        load_filament_memory=_load,
        invalidate_filament_memory=_invalidate,
    )
    async with svc.bus.subscribe() as sub:
        await svc.start()
        try:
            await _next(sub, lambda e: e.type == "snapshot")
            assert svc.filament_memory.get(1) is not None

            # Slot 1 now reports PETG instead of PLA.
            await mock_printer.push_report(_ams_report({1: "PETG"}))
            await _next(sub, lambda e: e.type in ("delta", "snapshot"))

            assert 1 in invalidated_slots, "invalidator called for slot 1"
            assert svc.filament_memory.get(1) is None, "cache entry removed"
        finally:
            await svc.stop()


@pytest.mark.asyncio
async def test_no_invalidation_on_same_type(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Same tray_type as recorded — label must NOT be invalidated."""
    invalidated_slots: list[int] = []

    async def _load() -> dict[int, FilamentMemory]:
        return {
            1: FilamentMemory(slot=1, make="Bambu", model=None, profile=None,
                              tray_type_seen="PLA", updated_at=0),
        }

    async def _invalidate(slot: int) -> None:
        invalidated_slots.append(slot)

    svc = _service(
        mqtt_broker,
        load_filament_memory=_load,
        invalidate_filament_memory=_invalidate,
    )
    async with svc.bus.subscribe() as sub:
        await svc.start()
        try:
            await _next(sub, lambda e: e.type == "snapshot")
            # Push same PLA type.
            await mock_printer.push_report(_ams_report({1: "PLA"}))
            await _next(sub, lambda e: e.type in ("delta", "snapshot"))
            assert invalidated_slots == [], "no invalidation for same type"
            assert svc.filament_memory.get(1) is not None
        finally:
            await svc.stop()


@pytest.mark.asyncio
async def test_no_invalidation_on_empty_slot(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Empty/absent slot (tray_type = '') must NOT trigger invalidation.

    The user may have pulled the spool briefly; the same material may return.
    """
    invalidated_slots: list[int] = []

    async def _load() -> dict[int, FilamentMemory]:
        return {
            1: FilamentMemory(slot=1, make="Bambu", model=None, profile=None,
                              tray_type_seen="PLA", updated_at=0),
        }

    async def _invalidate(slot: int) -> None:
        invalidated_slots.append(slot)

    svc = _service(
        mqtt_broker,
        load_filament_memory=_load,
        invalidate_filament_memory=_invalidate,
    )
    async with svc.bus.subscribe() as sub:
        await svc.start()
        try:
            await _next(sub, lambda e: e.type == "snapshot")
            # Slot 1 now reports empty (tray_type = "").
            await mock_printer.push_report(_ams_report({1: ""}))
            await _next(sub, lambda e: e.type in ("delta", "snapshot"))
            assert invalidated_slots == [], "empty slot must not invalidate"
            assert svc.filament_memory.get(1) is not None
        finally:
            await svc.stop()


@pytest.mark.asyncio
async def test_no_invalidation_when_recorded_type_is_none(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """If tray_type_seen is None (slot was empty when label was written),
    a non-empty type arriving does NOT invalidate — we can't tell if this
    is the same or a different material (no recorded baseline to compare against).
    """
    invalidated_slots: list[int] = []

    async def _load() -> dict[int, FilamentMemory]:
        return {
            1: FilamentMemory(slot=1, make="Bambu", model=None, profile=None,
                              tray_type_seen=None, updated_at=0),
        }

    async def _invalidate(slot: int) -> None:
        invalidated_slots.append(slot)

    svc = _service(
        mqtt_broker,
        load_filament_memory=_load,
        invalidate_filament_memory=_invalidate,
    )
    async with svc.bus.subscribe() as sub:
        await svc.start()
        try:
            await _next(sub, lambda e: e.type == "snapshot")
            # Any type — can't compare to None.
            await mock_printer.push_report(_ams_report({1: "PLA"}))
            await _next(sub, lambda e: e.type in ("delta", "snapshot"))
            assert invalidated_slots == [], "no invalidation when recorded type is None"
            assert svc.filament_memory.get(1) is not None
        finally:
            await svc.stop()


@pytest.mark.asyncio
async def test_invalidation_is_per_slot(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Only the slot whose type changes is invalidated; others remain intact."""
    invalidated_slots: list[int] = []

    async def _load() -> dict[int, FilamentMemory]:
        return {
            1: FilamentMemory(slot=1, make="A", model=None, profile=None,
                              tray_type_seen="PLA", updated_at=0),
            2: FilamentMemory(slot=2, make="B", model=None, profile=None,
                              tray_type_seen="PETG", updated_at=0),
        }

    async def _invalidate(slot: int) -> None:
        invalidated_slots.append(slot)

    svc = _service(
        mqtt_broker,
        load_filament_memory=_load,
        invalidate_filament_memory=_invalidate,
    )
    async with svc.bus.subscribe() as sub:
        await svc.start()
        try:
            await _next(sub, lambda e: e.type == "snapshot")
            # Slot 1 changes to ABS; slot 2 stays PETG.
            await mock_printer.push_report(_ams_report({1: "ABS", 2: "PETG"}))
            await _next(sub, lambda e: e.type in ("delta", "snapshot"))
            assert invalidated_slots == [1], "only slot 1 invalidated"
            assert svc.filament_memory.get(1) is None
            assert svc.filament_memory.get(2) is not None
        finally:
            await svc.stop()


# --------------------------------------------------------------------------- #
# Snapshot integration
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_snapshot_exposes_memory_when_cached(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """After seed, slot dict in snapshot carries memory fields."""

    async def _load() -> dict[int, FilamentMemory]:
        return {
            1: FilamentMemory(slot=1, make="Bambu", model="PLA Matte", profile="0.20 Standard",
                              tray_type_seen="PLA", updated_at=0),
        }

    svc = _service(mqtt_broker, load_filament_memory=_load)
    async with svc.bus.subscribe() as sub:
        await svc.start()
        try:
            await _next(sub, lambda e: e.type == "snapshot")
            snap = svc.snapshot()
            slots = snap["ams"]["slots"]
            # SAMPLE_PUSH_STATUS has 2 slots; slot 1 (physical_slot=1) should have memory.
            slot1 = next((s for s in slots if s["physical_slot"] == 1), None)
            assert slot1 is not None
            assert slot1["memory"] is not None
            assert slot1["memory"]["make"] == "Bambu"
            assert slot1["memory"]["model"] == "PLA Matte"
            assert slot1["memory"]["profile"] == "0.20 Standard"
        finally:
            await svc.stop()


@pytest.mark.asyncio
async def test_snapshot_memory_null_when_no_cache(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """When no loader is wired (cache = None), all slots get memory: null."""
    svc = _service(mqtt_broker)  # no load_filament_memory
    async with svc.bus.subscribe() as sub:
        await svc.start()
        try:
            await _next(sub, lambda e: e.type == "snapshot")
            snap = svc.snapshot()
            for slot in snap["ams"]["slots"]:
                assert slot["memory"] is None, f"slot {slot['physical_slot']} should be null"
        finally:
            await svc.stop()


@pytest.mark.asyncio
async def test_snapshot_memory_null_for_unlabelled_slots(
    mqtt_broker: int, mock_printer: MockPrinter
) -> None:
    """Cache is wired but slot 2 has no label → slot 2 memory is null."""

    async def _load() -> dict[int, FilamentMemory]:
        return {
            1: FilamentMemory(slot=1, make="Bambu", model=None, profile=None,
                              tray_type_seen="PLA", updated_at=0),
        }

    svc = _service(mqtt_broker, load_filament_memory=_load)
    async with svc.bus.subscribe() as sub:
        await svc.start()
        try:
            await _next(sub, lambda e: e.type == "snapshot")
            snap = svc.snapshot()
            slot2 = next(
                (s for s in snap["ams"]["slots"] if s["physical_slot"] == 2), None
            )
            if slot2 is not None:
                assert slot2["memory"] is None
        finally:
            await svc.stop()
