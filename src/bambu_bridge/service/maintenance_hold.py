"""One held-jog segment followed by a fresh firmware execution fence.

M400 finishes motion before a unique M73 P/R pair. Only a fresh report of BOTH
values releases the next segment. HTTP and gcode_line ACKs never do. Marker
pairs are durably reserved before publication and never reused across leases
or restarts. Idle qualification sends no axis movement.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from bambu_bridge.protocol import commands
from bambu_bridge.service.maintenance_motion import LIMITS, MaintenanceMotion


class HoldStopped(Exception):
    pass


class FenceSequence:
    # Conservative M73 minute domain, within its unsigned-16-bit representation.
    # Exact P/R echo is mandatory: ignored/clamped firmware values fail closed.
    MAX_R = 9999
    CAPACITY = MAX_R * 50

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.memory_counter = 0

    def next(self, baseline_percent: int, baseline_remaining: int | None = None) -> tuple[int, int]:
        def pair(counter: int) -> tuple[int, int]:
            group, remainder = divmod(counter, self.MAX_R)
            return group * 2 + (counter % 2) + 1, remainder + 1

        def reserve(counter: int) -> tuple[int, tuple[int, int]]:
            while counter < self.CAPACITY and (
                pair(counter)[0] == baseline_percent or pair(counter)[1] == baseline_remaining
            ):
                counter += 1
            if counter >= self.CAPACITY:
                raise HoldStopped("Execution marker capacity exhausted")
            return counter + 1, pair(counter)

        if self.path is None:  # isolated test/service contexts without persistence
            self.memory_counter, marker = reserve(self.memory_counter)
            return marker
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, isolation_level=None) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS fence_sequence ("
                "id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL)"
            )
            db.execute("INSERT OR IGNORE INTO fence_sequence VALUES (1,0)")
            db.execute("BEGIN IMMEDIATE")
            counter = int(db.execute("SELECT value FROM fence_sequence WHERE id=1").fetchone()[0])
            if not 0 <= counter <= self.CAPACITY:
                raise HoldStopped("Execution marker storage is invalid")
            next_counter, marker = reserve(counter)
            db.execute("UPDATE fence_sequence SET value=? WHERE id=1", (next_counter,))
            db.execute("COMMIT")  # reservation survives a crash before/after publication
            return marker


class MaintenanceHold:
    LEASE_S = 0.75
    SEGMENT_MM = 1.0

    def __init__(
        self,
        motion: MaintenanceMotion,
        ready: Callable[[str], bool],
        raw: Callable[[], dict[str, Any]],
        publish: Callable[[dict[str, Any]], Awaitable[None]],
        move: Callable[[dict[str, Any], dict[str, float]], Awaitable[None]],
        changed: Callable[[], None],
        lock: asyncio.Lock,
        *,
        fence_timeout: float = 4.0,
        marker_path: Path | None = None,
    ) -> None:
        self.motion, self.ready, self.raw = motion, ready, raw
        self.publish, self.move, self.changed, self.lock = publish, move, changed, lock
        self.fence_timeout = fence_timeout
        self._sequence = FenceSequence(marker_path)
        self.qualified = False
        self.phase = "unqualified"
        self.reason = "Enable held jogging when the printer is idle"
        self._token: str | None = None
        self._expires = 0.0
        self._axes = "XYZ"
        self._cancel = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._pending: asyncio.Future[None] | None = None
        self._marker: tuple[int, int] | None = None
        self._received_index = 0
        self._after_report = 0
        self._accept_fence = False
        self._closing = False
        self.segments = 0

    @property
    def active(self) -> bool:
        return self._token is not None or self.phase == "qualifying"

    def snapshot(self) -> dict[str, Any]:
        return {
            "qualified": self.qualified,
            "phase": self.phase,
            "reason": self.reason,
            "active": self.active,
            "lease_ms": int(self.LEASE_S * 1000),
            "segment_mm": self.SEGMENT_MM,
        }

    def invalidate(self, reason: str) -> None:
        if any(v is not None for v in self.motion.targets.values()):
            self.motion.reset(reason)
        self.qualified = False
        self.reason = reason
        self._cancel.set()
        if not self.active:
            self.phase = "unqualified"
        self.changed()

    def received(self) -> int:
        self._received_index += 1
        return self._received_index

    def observe(self, incoming: dict[str, Any], receipt: int | None = None) -> None:
        index = self.received() if receipt is None else receipt
        if self.active and not self.ready(self._axes):
            self.invalidate("Printer must remain idle, connected, fresh and homed")
            return
        if (
            self._accept_fence
            and self._pending is not None
            and not self._pending.done()
            and index > self._after_report
            and "mc_percent" in incoming
            and "mc_remaining_time" in incoming
            and (incoming["mc_percent"], incoming["mc_remaining_time"]) == self._marker
        ):
            self._pending.set_result(None)

    def _baseline(self) -> tuple[int, int]:
        p, r = self.raw().get("mc_percent"), self.raw().get("mc_remaining_time")
        if (
            isinstance(p, bool)
            or not isinstance(p, int)
            or not 0 <= p <= 100
            or isinstance(r, bool)
            or not isinstance(r, int)
            or not 0 <= r <= 65535
        ):
            raise HoldStopped("Progress/time telemetry is unavailable for execution verification")
        return p, r

    def _check(self, *, lease: bool) -> None:
        if self._cancel.is_set() or not self.ready(self._axes):
            raise HoldStopped(self.reason)
        if lease and time.monotonic() >= self._expires:
            raise HoldStopped("Held-jog lease expired")

    async def _restore(self, baseline: tuple[int, int]) -> None:
        # Restoration is NOT a reusable fence. All later fences use fresh pairs.
        await self.publish(commands.gcode_line(f"M400\nM73 P{baseline[0]} R{baseline[1]}"))

    async def _fence(
        self,
        envelope: dict[str, Any] | None = None,
        targets: dict[str, float] | None = None,
        *,
        lease: bool,
    ) -> None:
        self._check(lease=lease)
        marker = self._sequence.next(*self._baseline())
        self._check(lease=lease)  # durable fsync may outlive the short client lease
        self._marker = marker
        self._after_report = self._received_index
        self._pending = asyncio.get_running_loop().create_future()
        self._accept_fence = True
        marker_line = f"M73 P{marker[0]} R{marker[1]}"
        if envelope is None:
            envelope = commands.gcode_line(f"M400\n{marker_line}")
        else:
            envelope["print"]["param"] += marker_line + "\n"
        try:
            if targets is None:
                await self.publish(envelope)
            else:
                await self.move(envelope, targets)
            deadline = time.monotonic() + self.fence_timeout
            cancelled = asyncio.create_task(self._cancel.wait())
            try:
                while not self._pending.done():
                    if not self.ready(self._axes):
                        self.invalidate("Printer readiness was lost while draining a fence")
                        raise HoldStopped(self.reason)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.invalidate(
                            "Printer did not report motion completion; held jogging disabled"
                        )
                        raise HoldStopped(self.reason)
                    stopped = self._cancel.is_set() or (lease and time.monotonic() >= self._expires)
                    if stopped:
                        if self.phase != "stopping":
                            if not self._cancel.is_set():
                                self.reason = "Held-jog lease expired"
                            self._cancel.set()
                            self.phase = "stopping"
                            self.changed()
                        # Retain observation and the motion lock after release.
                        # Only matching completion or invalidation ends the drain.
                        waiting: set[asyncio.Future[Any]] = {self._pending}
                    else:
                        waiting = {self._pending, cancelled}
                        if lease:
                            remaining = min(remaining, self._expires - time.monotonic())
                    await asyncio.wait(
                        waiting, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                    )
                self._check(lease=lease)
                self._pending.result()
            finally:
                cancelled.cancel()
        finally:
            self._pending = None
            self._marker = None
            self._accept_fence = False

    async def qualify(self) -> dict[str, Any]:
        if self.active:
            raise HoldStopped("A held-jog operation is already active")
        self._axes = "XYZ"
        if not self.ready(self._axes):
            raise HoldStopped("Qualification requires an idle printer and homed axes")
        self._closing = False
        self._cancel = asyncio.Event()
        self.phase = "qualifying"
        self.qualified = False
        self.reason = "Verifying idle firmware execution reports"
        self.changed()
        baseline: tuple[int, int] | None = None
        try:
            baseline = self._baseline()
            await self._fence(lease=False)
            self._check(lease=False)
            await self._restore(baseline)
            self.qualified = True
            self.phase = "ready"
            self.reason = "Held jogging enabled; one segment awaits completion at a time"
            if self._watchdog is None or self._watchdog.done():
                self._watchdog = asyncio.create_task(self._watch_status())
        except BaseException as exc:
            self.qualified = False
            self.phase = "unqualified"
            self.reason = (
                str(exc)
                if isinstance(exc, HoldStopped)
                else "Execution verification failed or was cancelled"
            )
            if baseline is not None and self.ready(self._axes) and not self._closing:
                with contextlib.suppress(Exception):
                    await self._restore(baseline)
            if isinstance(exc, HoldStopped | asyncio.CancelledError):
                raise
            raise HoldStopped(self.reason) from exc
        finally:
            self.changed()
        return self.snapshot()

    def start(self, axes: str, dx: int = 0, dy: int = 0, dz: int = 0) -> str:
        if not self.qualified:
            raise HoldStopped("Enable held jogging first")
        if self.active or self.lock.locked():
            raise HoldStopped("A movement command is already active")
        if axes not in {"Z", "XY"} or not self.ready(axes):
            raise HoldStopped("Held jogging requires fresh idle homed axes")
        values = {"Z": dz} if axes == "Z" else {"X": dx, "Y": dy}
        if not any(values.values()) or any(v not in {-1, 0, 1} for v in values.values()):
            raise HoldStopped("Choose a bounded jog direction")
        if any(self.motion.targets[a] is None for a in values):
            raise HoldStopped("Select an absolute maintenance target first")
        self._axes = axes
        self._token = uuid.uuid4().hex
        self._expires = time.monotonic() + self.LEASE_S
        self._cancel = asyncio.Event()
        self.phase = "arming"
        self.reason = "Waiting for earlier absolute movement to finish"
        self.segments = 0
        self._task = asyncio.create_task(self._run(values))
        self.changed()
        return self._token

    def renew(self, token: str) -> None:
        if token != self._token or self._cancel.is_set():
            raise HoldStopped("Held-jog lease is no longer active")
        self._check(lease=True)
        self._expires = time.monotonic() + self.LEASE_S

    def stop(self, token: str) -> None:
        if token == self._token:
            self.reason = "Held jogging released"
            self._cancel.set()
            self.changed()

    async def _run(self, directions: dict[str, int]) -> None:
        baseline: tuple[int, int] | None = None
        try:
            async with self.lock:
                baseline = self._baseline()
                await self._fence(lease=True)  # finish any prior absolute anchor
                while True:
                    self._check(lease=True)
                    targets: dict[str, float] = {}
                    for axis, direction in directions.items():
                        current = self.motion.targets[axis]
                        if current is None:
                            raise HoldStopped("Maintenance reference was invalidated")
                        lo, hi = LIMITS[axis]
                        target = max(lo, min(hi, current + direction * self.SEGMENT_MM))
                        if direction and target == current:
                            raise HoldStopped("Travel limit reached")
                        targets[axis] = target
                    envelope = (
                        commands.move_bed_absolute(targets["Z"])
                        if self._axes == "Z"
                        else commands.move_xy_absolute(targets["X"], targets["Y"])
                    )
                    self.phase = "holding"
                    self.reason = "Held jogging; waiting for each segment to finish"
                    self.changed()
                    self.segments += 1
                    await self._fence(envelope, targets, lease=True)
        except BaseException as exc:
            reason = (
                str(exc) if isinstance(exc, HoldStopped) else "Held jogging failed or was cancelled"
            )
            if not isinstance(exc, HoldStopped) or "did not report" in reason:
                self.invalidate(reason)
            self.reason = reason
        finally:
            if not self._closing and baseline is not None and self.ready(self._axes):
                try:
                    await self._restore(baseline)
                except BaseException:
                    self.invalidate("Idle display restoration failed; verify held jogging again")
            self._token = None
            self.phase = "ready" if self.qualified else "unqualified"
            self.changed()

    async def _watch_status(self) -> None:
        while self.qualified or self.active:
            await asyncio.sleep(0.25)
            if not self.ready("XYZ"):
                self.invalidate("Fresh idle homed status was lost; verify held jogging again")
                return

    async def close(self) -> None:
        self._closing = True
        self.invalidate("Printer connection stopped")
        for task in (self._task, self._watchdog):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
