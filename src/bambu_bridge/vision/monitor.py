"""Async glue: a camera stream + a telemetry predicate -> a one-shot trigger.

Thin on purpose. All the judgement lives in the pure :class:`SpaghettiDetector`
(unit-tested exhaustively); this just pumps frames into it, throttled, and
fires ``on_spaghetti`` exactly once. It owns no abort logic — the caller
(:class:`bambu_bridge.service.jobs.JobRun`) turns the callback into the same
``print.stop`` + ``_fail`` path the air watchdog uses.

The chamber camera runs at several fps; analysing every frame is pointless
(and the entropy pass is O(scan bytes)). We sample one frame per
``interval_s`` — spaghetti develops over many seconds, not frames.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable

import structlog

from bambu_bridge.protocol.camera import CameraStream
from bambu_bridge.vision.detector import SpaghettiDetector, Verdict
from bambu_bridge.vision.frame import FrameComplexity, scan_complexity

log = structlog.get_logger(__name__)

PrintingOk = Callable[[], bool]
OnSpaghetti = Callable[[FrameComplexity | None], None]


class SpaghettiMonitor:
    """Drive a :class:`SpaghettiDetector` off a live :class:`CameraStream`."""

    def __init__(
        self,
        camera: CameraStream,
        printing_ok: PrintingOk,
        on_spaghetti: OnSpaghetti,
        *,
        detector: SpaghettiDetector | None = None,
        interval_s: float = 8.0,
        printer_id: str = "",
    ) -> None:
        self._camera = camera
        self._printing_ok = printing_ok
        self._on_spaghetti = on_spaghetti
        self._detector = detector or SpaghettiDetector()
        self._interval_s = interval_s
        self._log = log.bind(printer_id=printer_id, component="spaghetti")

    async def run(self) -> None:
        """Subscribe, sample, judge. Returns when it fires or is cancelled."""
        self._log.info("spaghetti.monitor_started", interval_s=self._interval_s)
        try:
            async with self._camera.subscribe() as queue:
                last = 0.0
                while True:
                    jpeg = await queue.get()
                    now = time.monotonic()
                    if now - last < self._interval_s:
                        continue  # throttle: video rate -> one/interval
                    last = now
                    if self._step(jpeg):
                        return
        except asyncio.CancelledError:
            self._log.info("spaghetti.monitor_stopped")
            raise

    # ----------------------------------------------------------------- #

    def _step(self, jpeg: bytes) -> bool:
        """One sampled frame. True once a (latched) trigger has fired."""
        complexity = scan_complexity(jpeg)
        verdict = self._detector.observe(complexity, printing_ok=self._printing_ok())
        if verdict is Verdict.SPAGHETTI:
            self._log.warning(
                "spaghetti.detected",
                score=complexity.score if complexity else None,
                scan_len=complexity.scan_len if complexity else None,
                baseline=self._detector.baseline,
            )
            with contextlib.suppress(Exception):
                self._on_spaghetti(complexity)
            return True
        return False
