"""Adaptive-baseline + debounce decision logic. Pure, deterministic, no I/O.

The raw scan-complexity series (:mod:`bambu_bridge.vision.frame`) is noisy
and lighting-confounded, so a bare threshold is useless. This class turns it
into a verdict with three guards, in order of importance:

1. **Telemetry gate.** Only frames where the caller asserts the printer
   *should be cleanly extruding right now* (RUNNING, tray engaged, no error,
   layers advancing) are considered. Anything else is ``SKIPPED`` — we never
   reason about complexity during heat-soak, pauses, or a state the air
   watchdog already owns.
2. **Adaptive baseline.** An EWMA of mean and absolute spread, learned
   **only from non-alarming gated frames**. A new lighting regime
   re-baselines within a few frames instead of alarming forever; but while a
   sample sits above the trigger band the baseline is *frozen*, so a slow
   ramp into spaghetti can't be tracked away.
3. **Debounce.** A trigger fires only after ``consecutive`` gated frames
   *in a row* exceed ``mean + trigger_sigma * spread``. One blurry,
   head-occluded, or motion-smeared frame (these chamber frames are full of
   them) cannot trip it; sustained chaos can.

``SPAGHETTI`` latches — once returned it keeps returning, so the caller acts
exactly once. This is Phase 1: coarse by construction. Phase 2's ML model
replaces the *signal*, not this state machine.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from bambu_bridge.vision.frame import FrameComplexity


class Verdict(enum.Enum):
    """Outcome of one :meth:`SpaghettiDetector.observe` call."""

    SKIPPED = "skipped"  # not gated, or frame unparseable — no opinion
    WARMING = "warming"  # gated but baseline not yet established
    OK = "ok"  # gated, within the normal band
    SPAGHETTI = "spaghetti"  # debounced trigger (latched)


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    baseline_alpha: float = 0.15  # EWMA weight of each new healthy sample
    trigger_sigma: float = 6.0  # band width: mean + sigma * abs-spread
    consecutive: int = 5  # gated frames in a row above band -> fire
    min_samples: int = 8  # healthy frames before any trigger is possible
    floor_ratio: float = 1.6  # also require score >= mean * this (abs guard)


class SpaghettiDetector:
    """Feed it complexity + a telemetry-ok flag; it returns a :class:`Verdict`."""

    def __init__(self, config: DetectorConfig | None = None) -> None:
        self._cfg = config or DetectorConfig()
        self._mean: float | None = None
        self._spread: float = 0.0
        self._samples = 0
        self._streak = 0
        self._latched = False

    # ----------------------------------------------------------------- #

    @property
    def baseline(self) -> float | None:
        """Current learned mean complexity (None until first healthy frame)."""
        return self._mean

    def observe(self, complexity: FrameComplexity | None, *, printing_ok: bool) -> Verdict:
        """Fold one frame into the model and return the current verdict."""
        if self._latched:
            return Verdict.SPAGHETTI
        if not printing_ok or complexity is None:
            self._streak = 0  # gap breaks the debounce run
            return Verdict.SKIPPED

        score = complexity.score
        cfg = self._cfg

        if self._mean is None:
            self._mean = score
            self._samples = 1
            return Verdict.WARMING

        over_band = score > self._mean + cfg.trigger_sigma * self._spread
        over_floor = score >= self._mean * cfg.floor_ratio
        warmed = self._samples >= cfg.min_samples

        if over_band and over_floor and warmed:
            self._streak += 1
            if self._streak >= cfg.consecutive:
                self._latched = True
                return Verdict.SPAGHETTI
            # Elevated: freeze the baseline (don't learn the anomaly away).
            return Verdict.OK

        # Normal frame: reset the run and let the baseline track it.
        self._streak = 0
        self._update_baseline(score)
        return Verdict.WARMING if self._samples < cfg.min_samples else Verdict.OK

    # ----------------------------------------------------------------- #

    def _update_baseline(self, score: float) -> None:
        assert self._mean is not None
        a = self._cfg.baseline_alpha
        dev = abs(score - self._mean)
        self._mean = (1 - a) * self._mean + a * score
        self._spread = (1 - a) * self._spread + a * dev
        self._samples += 1
