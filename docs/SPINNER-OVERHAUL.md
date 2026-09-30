# Spinner and video overhaul

Deliver a correctly associated spinner for every print. Preserve the current
one-revolution-per-minute speed; optimize generation and smooth playback.

## Delivery gates

1. Shared source acquisition: local upload, library, printer storage; correct
   plate and content identity; retry interrupted access; restart persistence.
2. Geometry: mesh or reconstructed extrusion bodies, plain G-code support,
   disconnected-part colors, holes and continuous-Z paths preserved.
3. Appearance: fixed orthographic framing, flat cel shading, broad key and fill
   lighting, black silhouette and crease edges, antialiasing, quiet plate.
4. Playback: background generation of one rotation, shared bounded compressed
   frame cache, cheap compositing, progressive availability, cancellation and
   persistent assets keyed by content, plate, dimensions and renderer version.
5. Visibility: Loading, Ready, Retrying, Source unavailable and Preview error;
   actionable diagnostics and retry through dashboard and Android.
6. Efficiency: compare 15/30 FPS and capped variable/constant bitrate on real
   camera clips; measure the raw relay before replacing it. Verify all three
   renditions, browser, Android, Orca, camera recovery and idle shutdown.
7. Qualification: synthetic regression corpus and actual printer jobs covering
   slicer variants, ordinary names, multiple plates, large/corrupt files,
   printer-started jobs, replacements, outages, eviction and restarts.

Initial targets: cached preview within one second, first preview within five
seconds of obtaining a typical source, compositing below two milliseconds,
decoded frame memory below 32 MiB per active printer, one generation worker.
Measure these targets and record results before declaring completion.

Track completion against implementation, tests, visual artifacts and runtime
measurements. A passing subset of tests establishes only its covered gates.

## Implementation checkpoint

The first increment adds printer-storage acquisition for ordinary filenames,
single-flight source loads, content-addressed retained sources with a disk cap,
plain G-code and selected-plate parsing, fallback extrusion previews, preparing
job detection, distinct reprint/plate identities and retry backoff with logs.

Video playback uses a shared 360-position revolution cache, one background
generation worker, progressive coarse-to-fine coverage, compressed frames and
eight decoded frames per sequence. Rotation remains one revolution per minute.

Synthetic measurement on a 120-face shape: 360 frames generated in 1.11 seconds;
cached lookup and compositing 0.19 ms versus direct panel rendering 2.73 ms;
1.88 MiB compressed and 1.37 MiB decoded. This does not qualify dense models,
live streaming, physical part connectivity or visual appearance.

The second increment adds opaque source reference manifests and same-job restart
reuse, content verification and repair, shared source acquisition with interactive
mesh/toolpath loaders, and persistent versioned rotation archives. Corrupt frames
regenerate. Source and frame disk caches are bounded separately.

G-code exterior loops now reconstruct closed bodies with even-odd holes. Material
touching between adjacent layers belongs to one component; disconnected bodies
receive stable distinct colors. Identical sections merge into tall surfaces.
Constrained triangulation caps exposed surfaces while preserving cutouts. Ink
marks silhouettes and creases, and leaves coplanar triangulation plain. Panels
are rendered at twice display resolution and downsampled for antialiasing.

The implementation uses Shapely/GEOS for polygonization, spatial connectivity and
constrained triangulation. References: [polygonization](https://shapely.readthedocs.io/en/stable/reference/shapely.polygonize.html),
[constrained triangulation](https://shapely.readthedocs.io/en/stable/reference/shapely.constrained_delaunay_triangles.html),
[spatial index](https://shapely.readthedocs.io/en/stable/strtree.html).

The implementation now includes mesh and continuous-Z reconstruction, XY arcs,
slicer-header material tolerances, per-part dense-model simplification, upload
prewarming, parsed-geometry persistence, and dashboard/Android diagnostics.
Paired phones can retry previews through the existing manager authorization.
The disposable geometry worker bounds CPU/time and releases its working heap.
Cached neighboring angles blend at video rate; rotation remains one RPM.

The full server regression run passed 1,373 tests with 10 skips before the final
focused refinements; 148 focused parser/rendering/gateway tests passed afterward.
Android passed 502 JavaScript tests and its signed release/native unit-test build.
Final deployment checks remain to be recorded. Measurements and performance
limits are documented in [video-performance.md](video-performance.md).

Visual review corrected the view-depth and front-face conventions to match the
orthographic projection. Closed bodies cull rear faces; bottom caps cannot paint
diagonal patches across visible walls. A pixel regression checks the plain side
face through its height. A two-part, hollow-body rotation contact sheet was
inspected at eight angles.

The video work now lives on `feat/spinner-overhaul`, based on current main with
the multi-filament slice-validation merge. It also carries the deployed shared
start-readiness helper and HTTP route changes accepting FAILED alongside IDLE
and FINISH. Freshness and connection guards remain covered. This patch must be
preserved when the overhaul is published and deployed.
