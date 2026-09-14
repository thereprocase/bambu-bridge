# Finished-part camera inset

The bottom-right fixed-scale, one-RPM platter uses the peg-board50
`bench-ink-v1` presentation: charcoal backdrop, teal material, four discrete
camera-relative light bands (0.22, 0.48, 0.76, 1.0), and dark silhouette ink.
This is a lightweight Pillow adaptation, not Blender running on the bridge.
Colors are presentation colors, not a claim about loaded filament.

Sliced uploads often have no native mesh. The preview reconstructs thin
exterior-wall and skin ribbons from the existing feature-filtered G-code.
Identical stacked walls merge vertically; travel gaps and cutouts stay empty.
No sparse infill, internal walls, supports, brim, or purge moves are added.
This remains an approximate finished toolpath preview, not an exported CAD solid.
A bounded 6,000-face budget preserves frame delivery on complex jobs; dense
jobs can have visible omissions. Missing geometry keeps the existing fallback.

Geometry is prepared asynchronously for the matching job. Rendering runs only
while the shared camera pipeline has viewers; existing encoder parking and raw
`overlay=false` routes are unchanged. Android, HLS, native Orca, MJPEG and
snapshots inherit the same style without an APK update.
