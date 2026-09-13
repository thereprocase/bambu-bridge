# Camera overlay: native and HTTP

The configured native printer's Orca, HTTP MJPEG and HTTP snapshot routes share
one overlay renderer and one upstream camera subscription. HTTP clients (bridge
web app, APK and Home Assistant) receive the overlay by default. Use
?overlay=false on either HTTP camera endpoint for untouched raw JPEGs. Responses
identify the selected route with X-Camera-Overlay: true/false. Other printers
without the configured native gateway retain their existing raw camera route.
BRIDGE_NATIVE_CAMERA_OVERLAY=false disables the shared HUD.

Fresh camera frames are rendered immediately, with latest-only bounded fanout.
There is no one-FPS video cap and no duplicated healthy frames between camera
arrivals. Status snapshots refresh at most once per second; only the camera-loss
card is emitted at a one-second cadence. Actual rate remains bounded by the
printer camera, network and client. Pillow encoding stays off the event loop.

The small translucent bottom-left panel normally shows two lines: measured
phase/layer/progress, temperatures and estimated time remaining. Routine bridge
confirmation and age text are hidden. Pending starts, errors, active HMS codes,
stale/disconnected telemetry and receipt inconsistencies reveal extra details.
Missing camera frames become a labelled status card after five seconds. The raw
camera and vision watcher remain unchanged; no printer commands are generated.

Verification includes HTTP overlay/raw selection, native TLS keyframe decoding,
shared subscribers, camera loss/recovery, source-rate forwarding, no duplicate
live frames, and visual inspection of synthetic previews. Live deployment and
frame-rate measurements are recorded in the operator MONITOR.md runbook.

The previous 74795b0 release capped the native feed at one FPS and left HTTP raw.
This follow-up removes those limitations and reduces normal overlay coverage from
about 20% of the image to about 4% at 1280x720. Retain the prior release for rollback.

Completion estimates use BRIDGE_CAMERA_TIMEZONE (IANA name; default UTC). The
local deployment uses America/New_York. The panel shows Finishes ~3:30 PM, adding
the weekday for a different local date. Paused/stale/disconnected or missing
estimates show Finish time -- instead of a moving, unsupported ETA.
