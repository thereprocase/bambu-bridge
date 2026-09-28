# Video resource usage

The shared HLS encoder produces 360p, 540p and 720p H.264 renditions at 30 FPS.
The Orca compatibility stream reuses the high rendition. The encoder parks after
20 seconds without a viewing request.

The video compositor keeps one decoded camera image and one static composition.
Status and AMS changes invalidate the composition. Animated model geometry is
drawn on a copy each tick. Camera loss replaces the cached image with the existing
unavailable-camera panel after the freshness timeout. Cache memory is released
when the stream stops.

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

Validation should include decoding all three renditions, real-camera visual
quality, rendition switching, Orca playback, camera-loss status, multiple viewers,
and idle shutdown. Measure CPU using interval deltas; process lifetime CPU is
unsuitable for before/after comparisons. Summed RSS includes shared pages.

References: [Intel media driver](https://github.com/intel/media-driver),
[FFmpeg VAAPI encoders](https://ffmpeg.org/ffmpeg-codecs.html#VAAPI-encoders).
