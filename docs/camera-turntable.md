# Finished-shape camera inset

The shared camera HUD includes a compact bottom-right preview for a current native bridge upload whose verified local archive is still retained. It draws the finished outer walls and the physical 256 mm plate, rotating together at one revolution per minute. Projection is orthographic with constant elevation and a rotation-invariant scale: there is no zoom animation or per-angle reframing. The model retains its actual position on the plate.

Geometry is prepared once off the camera loop from the immutable archive matching the printer's exact filename and identity. Source integrity is checked before reading. G-code feature filtering preserves all modal motion state while retaining only outer-wall segments. Rendering is capped at6000 segments; the preview is a simplified finished toolpath outline, not live XY tracking. Missing local archives or missing outer-wall annotations hide the inset. No printer download or print command is issued to build it.

New or interrupted jobs clear the old preview immediately, and late background results cannot restore it. Camera composition remains latest-frame-only. Native camera, HTTP MJPEG and snapshots use the same shared inset; `?overlay=false` remains raw. The full-screen interactive viewer is unchanged.
