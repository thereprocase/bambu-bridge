# Native camera status overlay

Implemented on fix/native-start-wedge; deployment is deferred while the matching
right-arm print is active. The running bridge has not been restarted or changed.

The native camera pane in Orca receives ordinary JPEG keyframes with a bottom
status strip. It displays measured printer phase, layer/progress, nozzle and bed
actual/target temperatures, estimated remaining minutes, bridge upload/start
milestones, and telemetry/camera age. Preparation is distinguished from printing;
upload delivery, printer acknowledgement, and active telemetry are separate.
Finished means waiting for the next job; no Stop action is required by this HUD.
Missing values display --. Stale or disconnected telemetry is explicitly labelled
as last-known; estimates are never an assurance of completion. Printer errors,
active HMS codes, short-finish anomalies, and receipt reconciliation gaps warn.

A missing/invalid camera frame produces a clearly labelled status card. After
five seconds without frames the stale photograph is removed; telemetry continues
to update. Telemetry older than 60 seconds is labelled stale. Normal camera frames
resume automatically. The HUD never sends a printer command or polls telemetry.

One renderer and one shared raw-camera subscription serve all native viewers at
at most one frame per second. Each viewer retains only the latest encoded frame;
slow clients cannot accumulate frames. Pillow encoding runs in a worker thread.
The last viewer leaving stops this worker and releases its raw subscription;
normal camera linger still applies. Original HTTP MJPEG/snapshots and the vision
watcher continue consuming untouched raw frames.

Configuration: BRIDGE_NATIVE_CAMERA_OVERLAY=true (default). Set false to restore
native JPEG passthrough on the next service start. The locked runtime dependency
is Pillow 12.3.0. Deployment must install from the updated lock into a new release
environment; do not modify the active/shared live venv during the print.

Validation: 69 camera/overlay regression tests passed; overlay mypy, Ruff, and dependency lock checks passed. Synthetic JPEG rendering and visual review; state/staleness/HMS tests;
shared-viewer and camera-loss/recovery tests; TLS native-camera test validates JPEG
length and keyframe flags and decodes the resulting frame. Local 1280x720 rendering
measured 6.67 ms median / 8.68 ms maximum across 30 frames. This is a local CPU
measurement, not a PVE benchmark. Live Orca display verification remains pending.

After a matching completed print is verified, deploy with the existing backed-up
release workflow, verify the release and listener health, then open Orca's existing
Device camera to validate the overlay. No test print or automatic start is needed.
Keep the prior release and its venv for rollback. Monitor instructions are in
F:/Code/bambu-job-diagnosis-20260912/MONITOR.md; the heartbeat remains read-only.

Synthetic previews and local benchmark:
F:/Code/bambu-job-diagnosis-20260912/overlay-preview/

Renderer API reference: https://pillow.readthedocs.io/en/stable/reference/ImageDraw.html
