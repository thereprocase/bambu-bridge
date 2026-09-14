# Native 30 FPS video

The optional Linux camera service sends H.264 at 1280x720 and 30 FPS to Orca,
using the latest real camera image with the existing status/AMS/turntable HUD.
The printer camera itself remains at its native rate. A camera image older
than five seconds is replaced by the unavailable view, even while the HUD animates.

Install distribution FFmpeg with libx264 and MediaMTX 1.21.0. The official
linux_amd64 archive SHA256 is
`e02e34c3337a35f20ac9e5aa31524566108964e6e37dbc46cf8292169f6c792b`.
Keep MediaMTX and FFmpeg executable paths in trusted, administrator-owned locations.

Set `BRIDGE_NATIVE_VIDEO=true` to start the additional camera listener. Defaults:
`BRIDGE_MEDIAMTX_PATH=/usr/local/bin/mediamtx`, `BRIDGE_FFMPEG_PATH=/usr/bin/ffmpeg`.
Authenticated loopback HTTP must be enabled for the private RGB encoder feed.
Only after checking client playback, set `BRIDGE_NATIVE_VIDEO_ADVERTISE=true`.
Both settings default false. Existing native JPEG6000, HTTP MJPEG, snapshots,
and raw opt-out remain available and retain source-rate behavior.

The bridge terminates TLS on its configured native host, port322, using the
existing RSA camera certificate. It relays to MediaMTX on **127.0.0.1:18554**.
Do not expose the backend outside loopback. Read authentication uses `bblp`
and the bridge's native access code. Publishing is restricted to loopback and
the single camera path. LL-HLS listens only on127.0.0.1:18888 and is proxied
through the authenticated HTTPS camera API for Android. It remuxes the same
H.264 stream without a second encoder. Other MediaMTX services are disabled. The runtime
configuration is mode0600 in a mode0700 directory and is removed on shutdown.
Native code changes restart this configuration with the native gateway.

TLS termination is deliberate: the installed Orca2.4.2 Windows BambuSource
plugin negotiates MediaMTX1.21's SRTP layer but then yields no frames.
RTP/AVP inside TLS works with the same plugin. No plaintext video is sent
over the LAN. No printer-model spoof or physical-printer configuration is needed.

MediaMTX starts one publisher/encoder on demand and stops it five seconds
after the last reader leaves. Encoder arguments contain no credentials.
The fixed RGB feed uses an owner-authenticated loopback-only API, a single
shared renderer, and latest-only subscriber queues. Encoder output has no
B frames and a keyframe at least every two seconds. Monitor CPU and actual
output cadence on the target host; nominal H.264 FPS is not proof of throughput.

Advertisement follows backend availability; if the process exits, subsequent
printer reports revert to the JPEG capability. Disable advertisement and
restart the bridge to force a rollback. Changing camera transport never
requires sending a stop, resume, replay, or print command.

Acceptance: use the installed Orca plugin to authenticate and decode frames,
verify wrong-code rejection, count decoded frames (not individual NAL samples),
check reconnect and two simultaneous readers, observe encoder shutdown after
disconnect, and recheck native JPEG/MJPEG/snapshot routes and job continuity.

Android uses `/api/v1/printers/{id}/camera/hls/index.m3u8`; every playlist and
segment request needs authentication. Only allowlisted resource names and LL-HLS
query fields are accepted; HTTP and query-token access are rejected. The app
uses its existing scoped/pinned HTTPS client and falls back to JPEG when HLS
is unavailable. The HLS muxer closes ten seconds after its final request,
followed by the shared encoder's five-second idle delay when no Orca viewers remain.

## Shared adaptive video

Android HLS and Orca RTSPS share one on-demand encoder ladder: 360p/350 kbps, 540p/800 kbps and 720p/1600 kbps, all 30 FPS. The HLS master advertises the three aligned renditions; Media3 selects automatically. Orca's on-demand worker copies the high stream into RTSP, without another encode. All overlays use the same RGB renderer.

Authenticated HLS requests and the owner-only loopback Orca lease renew one 20-second idle timer. Once neither client is watching, the encoder group stops and its temporary segments are removed. The RTSP remux worker closes five seconds after the last Orca reader; its last lease can add up to 20 seconds of grace. No video encoding occurs while parked. The small bridge/MediaMTX services remain running to accept viewers.

Playlist and segment URLs stay under the authenticated camera route. No credentials appear in playlists or FFmpeg arguments. Segment names use a generation-distinct epoch sequence; playlists update atomically and retained segments are bounded. This remains H.264 for the verified Orca path; HEVC/AV1 require separate client and encoder qualification.
