"""SpaghettiDetector — the adaptive-baseline + debounce + gate state machine.

Pure and deterministic, so it gets the exhaustive treatment (the same
philosophy as test_jobs_watchdog): a single bad frame must NEVER fail a good
print; sustained chaos under healthy telemetry MUST fire exactly once.
"""

from __future__ import annotations

from bambu_bridge.vision.detector import DetectorConfig, SpaghettiDetector, Verdict
from bambu_bridge.vision.frame import FrameComplexity


def _fc(score: float) -> FrameComplexity:
    return FrameComplexity(score=score, scan_len=int(score), entropy=7.5)


def _feed(det: SpaghettiDetector, score: float, *, ok: bool = True) -> Verdict:
    return det.observe(_fc(score), printing_ok=ok)


def _warm(det: SpaghettiDetector, level: float, n: int) -> None:
    """Establish a stable baseline at ``level`` over ``n`` healthy frames."""
    for _ in range(n):
        _feed(det, level)


def test_first_healthy_frame_is_warming_then_settles_ok() -> None:
    det = SpaghettiDetector()
    assert _feed(det, 1000) is Verdict.WARMING
    for _ in range(DetectorConfig().min_samples):
        _feed(det, 1000)
    assert _feed(det, 1000) is Verdict.OK


def test_not_printing_or_unparseable_is_skipped() -> None:
    det = SpaghettiDetector()
    _warm(det, 1000, 20)
    assert _feed(det, 9_000_000, ok=False) is Verdict.SKIPPED  # gate shut
    assert det.observe(None, printing_ok=True) is Verdict.SKIPPED  # no frame


def test_sustained_chaos_fires_once_and_latches() -> None:
    det = SpaghettiDetector(DetectorConfig(consecutive=5))
    _warm(det, 1000, 25)
    huge = 1_000_000.0
    out = [_feed(det, huge) for _ in range(5)]
    assert out[:4] == [Verdict.OK] * 4  # building the debounce run
    assert out[4] is Verdict.SPAGHETTI
    # Latched: even a perfectly normal frame keeps returning SPAGHETTI.
    assert _feed(det, 1000) is Verdict.SPAGHETTI
    assert det.observe(None, printing_ok=False) is Verdict.SPAGHETTI


def test_single_spike_does_not_trigger_debounce() -> None:
    det = SpaghettiDetector(DetectorConfig(consecutive=5))
    _warm(det, 1000, 25)
    assert _feed(det, 1_000_000) is Verdict.OK  # one chaotic frame
    for _ in range(10):
        assert _feed(det, 1000) is Verdict.OK  # back to normal, no fire
    assert det.baseline is not None


def test_gap_in_telemetry_resets_the_debounce_run() -> None:
    det = SpaghettiDetector(DetectorConfig(consecutive=5))
    _warm(det, 1000, 25)
    for _ in range(4):  # 4/5 of the way to a trigger
        assert _feed(det, 1_000_000) is Verdict.OK
    assert _feed(det, 1_000_000, ok=False) is Verdict.SKIPPED  # gate breaks run
    # Streak reset: it needs a fresh full run of `consecutive` to fire.
    for _ in range(4):
        assert _feed(det, 1_000_000) is Verdict.OK
    assert _feed(det, 1_000_000) is Verdict.SPAGHETTI


def test_slow_drift_within_band_never_triggers() -> None:
    # A gradual rise where each step stays inside the adaptive band must be
    # tracked away, not alarmed (lighting/print-area growth, not spaghetti).
    det = SpaghettiDetector()
    _warm(det, 1000, 15)
    score = 1000.0
    for _ in range(200):
        score *= 1.01  # +1% per frame — slow relative to the band
        assert _feed(det, score) is not Verdict.SPAGHETTI
    assert det.baseline is not None and det.baseline > 1000


def test_floor_ratio_blocks_trigger_on_a_tiny_constant_offset() -> None:
    # Constant baseline => spread collapses to ~0, so `mean + sigma*spread`
    # is barely above mean. The absolute floor_ratio guard must still stop a
    # score that is only marginally above the mean from ever firing.
    det = SpaghettiDetector(DetectorConfig(consecutive=3, floor_ratio=1.6))
    _warm(det, 1000, 30)  # spread -> ~0
    for _ in range(20):
        # 1001 > mean(+~0 band) but 1001 < 1000 * 1.6 -> floor blocks it.
        assert _feed(det, 1001) is not Verdict.SPAGHETTI
