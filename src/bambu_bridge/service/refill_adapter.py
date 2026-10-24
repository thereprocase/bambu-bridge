"""Explicit, qualified physical declaration writes for the durable refill controller.

No caller is registered by default. This adapter never enables printer Auto
Refill, fabricates RFID, reads client-projected state or retries a send.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import asdict
from typing import TYPE_CHECKING

import structlog

from bambu_bridge.protocol import commands
from bambu_bridge.refill import Declaration, NotSent, RefillController, RefillReport

if TYPE_CHECKING:
    from bambu_bridge.service.printer import PrinterService

log = structlog.get_logger(__name__)

# Changed only by a reviewed commit that cites named target-firmware write and
# readback acceptance. There is deliberately no runtime or configuration switch.
QUALIFIED = True

# Send plus readback. On the P1S (ledger row N9, 2026-10-22 23:31Z) an alias
# read back in about 4 s and its restore in about 6 s; this allows over 3x the
# slower. A timeout leaves the step uncertain for review and is never resent.
READBACK_TIMEOUT_S = 20.0


class PhysicalRefillAdapter:
    def __init__(
        self,
        printer: PrinterService,
        *,
        qualified: bool | None = None,
        timeout: float = READBACK_TIMEOUT_S,
    ):
        self.printer = printer
        self.qualified = QUALIFIED if qualified is None else qualified
        self.timeout = timeout

    async def observe(self) -> RefillReport:
        return self.printer.refill_reports.snapshot(connected=self.printer.connected)

    async def send(self, wire: int, target: Declaration) -> None:
        if not self.qualified:
            raise ValueError("physical refill adapter is not qualified")
        if type(wire) is not int or wire not in range(4):
            raise NotSent("physical refill aliases support only AMS A slots 0–3")
        try:
            target.validate()
        except ValueError as exc:
            raise NotSent(str(exc)) from exc
        before = await self.observe()
        slot = before.slots.get(wire)
        if (
            not before.idle
            or slot is None
            or not slot.editable
            or not RefillController._fresh(before, slot)
        ):
            raise NotSent("fresh idle untagged physical source is required")
        # Subscribe before registering the expected transition and sending, so a
        # synchronous raw readback cannot be missed. The tracker rejects a
        # chemistry change; product, temperatures and colour may differ.
        async with self.printer.raw_bus.subscribe() as reports:
            try:
                self.printer.refill_reports.expect(wire, slot.source_generation, target)
            except ValueError as exc:
                raise NotSent(str(exc)) from exc
            async with asyncio.timeout(self.timeout):
                await self.printer.send_raw(
                    commands.ams_filament_setting(ams_id=0, tray_id=wire, **asdict(target))
                )
                while True:
                    current = await self.observe()
                    observed = current.slots.get(wire)
                    if observed is not None:
                        if observed.source_generation != slot.source_generation:
                            raise ValueError("physical refill source changed during readback")
                        if (
                            observed.receipt > slot.receipt
                            and RefillController._fresh(current, observed)
                            and observed.declaration == target
                        ):
                            return
                    if not current.connected:
                        raise ConnectionError("printer disconnected during refill readback")
                    await reports.get()


def report_listener(controller: RefillController, printer: PrinterService) -> Callable[[], None]:
    """Pass each refill report to controller.note(); a failure is logged once per reason."""
    last: str | None = None

    def note() -> None:
        nonlocal last
        try:
            controller.note(
                printer.serial, printer.refill_reports.snapshot(connected=printer.connected)
            )
            last = None
        except Exception as exc:
            if last != repr(exc):
                last = repr(exc)
                log.exception("refill.note_failed", printer=printer.serial)

    return note


async def restore_when_idle(
    printers: Callable[[], Iterable[PrinterService]],
    controller: RefillController,
    *,
    interval: float = 15,
) -> None:
    """Carry out restorations the operator already requested, once printers are idle,
    and connect each printer's refill reports to the controller (note()).

    Only groups whose stored state is restoration_pending are driven. The
    controller restores declarations it still owns, never resends an uncertain
    write and moves conflicts to needs_attention, which this loop leaves for
    explicit review. Nothing runs unless both refill gates are qualified.
    A send refused before reaching the printer is logged once per reason,
    not every pass, until a drive gets through.
    """
    waiting: dict[str, str] = {}

    # Before the first sleep, so the print history knows the stored group from startup.
    def attach_all() -> None:
        for printer in list(printers()):
            if (history := getattr(printer, "auto_refill", None)) is not None:
                controller.attach(printer.serial, history)

    attach_all()
    while True:
        await asyncio.sleep(interval)
        attach_all()  # a printer added since
        for printer in list(printers()):
            reports = getattr(printer, "refill_reports", None)
            if reports is not None and reports.listener is None:
                # From now on every report the printer sends reaches the
                # controller: re-adoption after a restart, and confirmations
                # remembered though nobody polls.
                note = report_listener(controller, printer)
                reports.listener = note
                note()
            group = controller.store.get(printer.serial)
            if group is None or group["enabled"] or group["state"] != "restoration_pending":
                continue
            adapter = PhysicalRefillAdapter(printer)
            if not (controller.qualified and adapter.qualified):
                continue
            if not (await adapter.observe()).idle:
                continue
            try:
                await controller.drive(
                    printer.serial, adapter.observe, adapter.send, revision=group["revision"]
                )
                waiting.pop(printer.serial, None)
            except NotSent as exc:
                # Nothing was sent and the step is as it was: the next idle pass retries.
                if waiting.get(printer.serial) != str(exc):
                    waiting[printer.serial] = str(exc)
                    log.info("refill.restore_waiting", printer=printer.serial, reason=str(exc))
            except (ValueError, TimeoutError, ConnectionError):
                # Recorded durably by the controller; the app shows needs_attention.
                log.warning("refill.restore_incomplete", printer=printer.serial)
            except Exception:
                log.exception("refill.restore_failed", printer=printer.serial)


@contextlib.asynccontextmanager
async def restoration_worker(
    printers: Callable[[], Iterable[PrinterService]], controller: RefillController
) -> AsyncIterator[None]:
    task = asyncio.create_task(restore_when_idle(printers, controller))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
