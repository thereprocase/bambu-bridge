# Video resource usage

The shared HLS encoder produces 360p, 540p and 720p H.264 renditions at 30 FPS.
The Orca compatibility stream reuses the high rendition. The encoder parks after
20 seconds without a viewing request.

The video compositor keeps one decoded camera image and one static composition.
Status and AMS changes invalidate the composition. Animated model geometry is
rendered once into a 360-position rotation cache. Each tick blends neighboring
2D panels and composites the result. Camera loss replaces the cached image with the existing
unavailable-camera panel after the freshness timeout. Cache memory is released
when the stream stops.

The preview acquires the active job from a verified local upload or printer
storage, including printer-started jobs. It selects the reported plate, retries
failed transfers, and retains content-verified sources, parsed geometry and
versioned rotation frames across restarts. The dashboard and Android show source
and rendering status with a manager-authorized retry action.

Source, geometry and compressed rotation disk budgets are 256, 96 and 128 MiB.
One source/geometry acquisition runs at a time, and one rotation-generation
thread fills coarse angles before intermediate positions. Two active sequences
retain at most 16 MiB compressed each and eight decoded panels each. Geometry
parsing runs in a disposable process with a 120-second wall timeout and a
100-second CPU limit; its large working heap is released after the job.

G-code exterior bodies preserve holes and connected-part colors. Continuous-Z
paths retain endpoint heights; XY arcs are tessellated. Slicer line-width and
layer-height headers set material tolerances. Dense surfaces receive per-part
detail reduction before rendering. Mesh-only archives also receive per-part
colors. Preview generation never issues a printer command.

A private 3.2 MB live-job qualification produced about 5,500 preview faces.
Geometry reconstruction took 16.7 seconds, the full rotation 13.3 seconds, and
cached geometry reload 47 ms. Cached 720p compositing averaged 0.37 ms/frame.
The rotation occupied 6.45 MiB compressed. These measurements describe one
dense job; first-time reconstruction exceeds the initial five-second target.
Upload prewarming and persistent caches reduce the interactive wait. Real files,
camera captures and deployment credentials remain outside the repository.

## Optional Intel/VAAPI encoding

Set `BRIDGE_VIDEO_VAAPI_DEVICE=/dev/dri/renderD128` to request hardware encoding.
Select the actual Intel render node on the host; numbering varies by machine.
The service account needs read/write access to that render node, and containers
need an explicit device mapping. Expose only the required render node.

Install the appropriate VAAPI driver in the service environment. On Debian,
Intel's full-feature `intel-media-va-driver-non-free` package provides bitrate
control needed by this ladder; availability depends on configured repositories.
The free-kernel driver may expose only constant-quantizer encoding.

Each encoder startup tests the complete three-rendition pipeline with a single
synthetic frame under the worker's permissions, with a five-second timeout.
Temporary probe files are cleaned up. A failed probe selects the software encoder.
An empty setting also selects software. Both paths retain the same resolutions,
bitrate targets, one-second GOPs, HLS segment format, and viewing leases.

`BRIDGE_VIDEO_RATE_CONTROL=VBR` selects capped variable bitrate for VAAPI. The
default remains CBR. The startup probe tests the requested mode; unsupported
hardware/driver combinations select software. Resolution, 30 FPS, one-second
GOPs and peak bitrate caps remain unchanged.

A three-second static sample captured from the live camera produced:

| FPS | Mode | Encoder CPU seconds | 720p kbit/s | PSNR dB |
| --- | --- | --- | --- | --- |
| 15 | CBR | 0.348 | 1336 | 43.43 |
| 15 | VBR | 0.322 | 171 | 43.43 |
| 30 | CBR | 0.469 | 1336 | 42.82 |
| 30 | VBR | 0.491 | 159 | 42.82 |

All three renditions decoded in every case. This supports selecting VBR on the
qualified Intel host while retaining 30 FPS; it measures a static sample, rather
than establishing quality or bitrate across every moving scene.

Validation should include decoding all three renditions, real-camera visual
quality, rendition switching, Orca playback, camera-loss status, multiple viewers,
and idle shutdown. Measure CPU using interval deltas; process lifetime CPU is
unsuitable for before/after comparisons. Summed RSS includes shared pages.

References: [Intel media driver](https://github.com/intel/media-driver),
[FFmpeg VAAPI encoders](https://ffmpeg.org/ffmpeg-codecs.html#VAAPI-encoders).
