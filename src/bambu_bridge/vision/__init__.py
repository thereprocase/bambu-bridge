"""Print-failure (spaghetti) detection — Phase 1: zero-dependency heuristic.

This is *not* the air-print watchdog. Air prints are caught by telemetry
(``ams.tray_now`` + ``print_error`` + layer-vs-extrusion, see
:mod:`bambu_bridge.service.jobs`). Spaghetti is the opposite problem: the
part detaches or droops, the nozzle keeps extruding into a tangle, and
*telemetry stays perfectly healthy* (layers tick, no error, tray engaged).
Only the camera sees it.

Phase 1 deliberately adds **no image dependency** (the project is a lean
async-stdlib stack — no numpy/PIL/cv2). It works on the *compressed* JPEG
bytes: a clean partial print compresses small and smooth; a chaotic stringy
scene carries far more high-frequency AC energy and a longer entropy-coded
scan. That signal is coarse and lighting-confounded on its own, so it is
always (a) anchored to telemetry ("the printer says it should be cleanly
extruding right now") and (b) debounced against an adaptive baseline. Phase 2
(an embedded CPU ML model — onnxruntime + a decoder) is what buys real
spaghetti specificity; this is the safe, reviewable seam it plugs into.

Public surface:

* :func:`scan_complexity` — pure JPEG-bytes -> a complexity scalar (or None).
* :class:`SpaghettiDetector` — stateful adaptive-baseline + debounce logic.
* :class:`SpaghettiMonitor` — async glue: a :class:`CameraStream` + a
  telemetry-ok predicate driving the detector, one-shot ``on_spaghetti``.
"""

from __future__ import annotations

from bambu_bridge.vision.detector import SpaghettiDetector, Verdict
from bambu_bridge.vision.frame import FrameComplexity, scan_complexity
from bambu_bridge.vision.monitor import SpaghettiMonitor

__all__ = [
    "FrameComplexity",
    "SpaghettiDetector",
    "SpaghettiMonitor",
    "Verdict",
    "scan_complexity",
]
