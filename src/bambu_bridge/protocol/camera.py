"""P1S chamber camera (spec 5.3).

Not RTSP. A raw TLS TCP socket on :6000 that accepts **one** client, takes an
80-byte auth blob, then streams ``[16-byte header][JPEG]`` frames forever.

Two pieces:

* :class:`CameraClient` — the upstream link. Connects, authenticates, parses
  frames, hands each JPEG to a callback. Reconnects with backoff while it is
  meant to be running (the camera is flaky, gotcha re: persistent connections).
* :class:`CameraStream` — the multiplexer. One upstream :class:`CameraClient`
  fanned out to N HTTP clients; a tiny ring buffer backs the snapshot
  endpoint. Reference-counts subscribers and **closes the printer-side
  connection when the last one leaves** (spec 5.3: don't hold the camera open
  needlessly).

Byte layout reimplemented from the OpenBambuAPI description — not lifted.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import structlog

from bambu_bridge.protocol.tls import insecure_tls_context

log = structlog.get_logger(__name__)

CAMERA_PORT = 6000
_USERNAME = "bblp"
_HEADER_LEN = 16
_MAX_JPEG = 8 * 1024 * 1024  # sanity cap; a chamber frame is ~tens of KB
_JPEG_SOI = b"\xff\xd8"

_BACKOFF_START = 1.0
_BACKOFF_CAP = 30.0
_CONNECT_TIMEOUT_S = 10.0
_FRAME_TIMEOUT_S = 10.0
_FRAME_FRESH_S = 5.0

FrameHandler = Callable[[bytes], None]


def build_auth_packet(username: str, access_code: str) -> bytes:
    """The 80-byte handshake: 16-byte header + 32 user + 32 code (spec 5.3).

    Header is four little-endian uint32s per the OpenBambuAPI description;
    username and access code are null-padded to 32 bytes each.
    """
    header = struct.pack("<IIII", 0x40, 0x3000, 0x00, 0x00)
    user = username.encode("ascii").ljust(32, b"\x00")[:32]
    code = access_code.encode("ascii").ljust(32, b"\x00")[:32]
    return header + user + code


class CameraClient:
    """One upstream link to the printer camera. ``run()`` loops until stopped."""

    def __init__(
        self,
        ip: str,
        access_code: str,
        *,
        on_frame: FrameHandler,
        port: int = CAMERA_PORT,
    ) -> None:
        self.ip = ip
        self.port = port
        self._access_code = access_code
        self._on_frame = on_frame
        self._stop = asyncio.Event()
        self._session_had_frame = False
        self._log = log.bind(ip=ip, component="camera")

    async def run(self) -> None:
        delay = _BACKOFF_START
        while not self._stop.is_set():
            self._session_had_frame = False
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — keep retrying
                self._log.warning("camera.session_error", error=str(exc))
            if self._stop.is_set():
                break
            if self._session_had_frame:
                delay = _BACKOFF_START
            await asyncio.sleep(delay)
            delay = min(_BACKOFF_CAP, delay * 2)

    def stop(self) -> None:
        self._stop.set()

    async def _session(self) -> None:
        self._log.info("camera.connecting", port=self.port)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.ip, self.port, ssl=insecure_tls_context()),
            timeout=_CONNECT_TIMEOUT_S,
        )
        try:
            writer.write(build_auth_packet(_USERNAME, self._access_code))
            await asyncio.wait_for(writer.drain(), _CONNECT_TIMEOUT_S)
            self._log.info("camera.connected")
            while not self._stop.is_set():
                # A half-open socket must not leave every viewer frozen forever.
                # One deadline covers the header AND body, including slow drips.
                async with asyncio.timeout(_FRAME_TIMEOUT_S):
                    header = await reader.readexactly(_HEADER_LEN)
                    payload_len = struct.unpack_from("<I", header, 0)[0]
                    if not 0 < payload_len <= _MAX_JPEG:
                        raise ValueError(f"implausible frame length {payload_len}")
                    jpeg = await reader.readexactly(payload_len)
                if jpeg[:2] != _JPEG_SOI:
                    self._log.warning("camera.bad_frame", head=jpeg[:2].hex())
                    continue
                self._session_had_frame = True
                self._on_frame(jpeg)
        finally:
            writer.close()
            # wait_closed() can hang forever on a half-open TLS socket if the
            # peer never sends close_notify — the P1S camera is exactly that
            # flaky. Bound it; we don't care if the FIN/shutdown completes.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=2.0)


_DEFAULT_LINGER_S = 10.0


class CameraStream:
    """Single upstream fanned out to N subscribers; ring buffer for snapshots.

    Subscriber-refcounted: the printer-side connection exists only while at
    least one HTTP client is attached (spec 5.3).

    **Linger window** (``linger_s``, default 10 s, env ``BRIDGE_CAMERA_LINGER_S``):
    After the last subscriber leaves the upstream is *not* torn down
    immediately. If a new subscriber arrives within ``linger_s`` it reuses the
    live connection and the most-recent buffered frame — eliminating the
    per-snapshot TCP+TLS handshake that caused the 1.5 s per-frame latency and
    client-visible flicker at 1 fps.

    **Consumption-extended linger**: ``wait_for_frame`` (the snapshot path)
    bumps a monotonic ``_last_consumed_at`` timestamp on every call, even when
    it returns a buffered frame without subscribing. The linger teardown task
    checks that timestamp after each sleep slice and re-sleeps the remaining
    deadline rather than stopping early. This means steady 1 fps snapshot
    polling keeps the upstream alive continuously: buffer hits count as
    consumption. The upstream tears down only after a full ``linger_s`` of
    silence with no calls to ``wait_for_frame``.

    Concurrency is safe: only one upstream task ever runs; the linger task is
    cancelled on re-subscribe.
    """

    def __init__(
        self,
        ip: str,
        access_code: str,
        *,
        port: int = CAMERA_PORT,
        buffer_size: int = 1,
        linger_s: float = _DEFAULT_LINGER_S,
    ) -> None:
        self._ip = ip
        self._access_code = access_code
        self._port = port
        self._linger_s = max(0.0, linger_s)
        self._ring: deque[bytes] = deque(maxlen=max(1, buffer_size))
        self._subscribers: set[asyncio.Queue[bytes]] = set()
        self._client: CameraClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._linger_task: asyncio.Task[None] | None = None
        # Monotonic timestamp of the last wait_for_frame call. Set on every
        # consumption (buffer hit or subscribe path) so the linger deadline
        # is extended by any snapshot poll, not just subscribed viewers.
        self._last_consumed_at: float = 0.0
        self._last_frame_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def upstream_active(self) -> bool:
        return self._task is not None and not self._task.done()

    def latest(self) -> bytes | None:
        if time.monotonic() - self._last_frame_at > _FRAME_FRESH_S:
            return None
        return self._ring[-1] if self._ring else None

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[bytes]]:
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=8)
        async with self._lock:
            self._subscribers.add(queue)
            # Cancel any pending linger teardown — a new viewer arrived
            # before the window expired; the upstream stays up.
            self._cancel_linger()
            if self._client is not None and not self.upstream_active:
                await self._stop_upstream()
            if self._client is None:
                self._start_upstream()
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers.discard(queue)
                if not self._subscribers:
                    self._last_consumed_at = time.monotonic()
                    # Last subscriber gone — schedule deferred teardown
                    # instead of stopping immediately (linger window).
                    self._schedule_linger()

    async def aclose(self) -> None:
        """Force the upstream down regardless of subscribers (printer removed)."""
        async with self._lock:
            self._subscribers.clear()
            self._cancel_linger()
            await self._stop_upstream()

    async def wait_for_frame(self, timeout: float) -> bytes | None:
        """Return the most recent frame, opening the upstream if necessary.

        Every call — whether it returns a buffered frame immediately or waits
        for a fresh one via subscribe — extends the linger deadline. This
        ensures steady snapshot polling keeps the upstream alive even when all
        calls hit the ring buffer without entering subscribe().
        """
        self._last_consumed_at = time.monotonic()
        if (frame := self.latest()) is not None:
            return frame
        async with self.subscribe() as queue:
            with contextlib.suppress(TimeoutError):
                return await asyncio.wait_for(queue.get(), timeout)
        return self.latest()

    # ------------------------------------------------------------------ #

    def _start_upstream(self) -> None:
        self._client = CameraClient(
            self._ip, self._access_code, on_frame=self._on_frame, port=self._port
        )
        self._task = asyncio.create_task(self._client.run())
        log.info("camera.upstream_started", ip=self._ip)

    async def _stop_upstream(self) -> None:
        if self._client is not None:
            self._client.stop()
        if self._task is not None:
            self._task.cancel()
            # Bounded: a wedged upstream must not block teardown / the
            # subscribe() lock (and thus every other viewer). The task is
            # already cancelled; if it won't unwind in time, let it go.
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(self._task, return_exceptions=True), timeout=3.0
                )
        self._client = None
        self._task = None
        self._ring.clear()
        log.info("camera.upstream_stopped", ip=self._ip)

    def _schedule_linger(self) -> None:
        """Start the idle-linger countdown (called under lock, last subscriber gone)."""
        self._linger_task = asyncio.create_task(self._linger_then_stop())

    def _cancel_linger(self) -> None:
        """Cancel a pending linger teardown (new subscriber arrived, or aclose)."""
        if self._linger_task is not None:
            self._linger_task.cancel()
            self._linger_task = None

    async def _linger_then_stop(self) -> None:
        """Deadline-following idle teardown.

        Sleeps until ``linger_s`` has elapsed since the last consumption event
        (``_last_consumed_at``). If ``wait_for_frame`` is called during the
        sleep — even for a buffer hit that never enters ``subscribe()`` — the
        timestamp advances and the loop re-sleeps the remaining gap instead of
        stopping. Teardown only fires when a full ``linger_s`` passes with no
        consumption at all.

        This correctly handles the 1 fps snapshot pattern: buffer-hit calls
        extend the deadline continuously, so the upstream stays up for as long
        as the client keeps polling.
        """
        try:
            while True:
                # How long until the current deadline expires?
                deadline = self._last_consumed_at + self._linger_s
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
                # Re-check: a buffer-hit call may have pushed the deadline
                # forward while we were sleeping.
        except asyncio.CancelledError:
            return
        async with self._lock:
            self._linger_task = None
            # Belt-and-suspenders: a new subscriber or consumption event may
            # have arrived in the window between the loop exit and lock
            # acquisition.
            if not self._subscribers and (
                time.monotonic() - self._last_consumed_at >= self._linger_s
            ):
                await self._stop_upstream()

    def _on_frame(self, jpeg: bytes) -> None:
        self._last_frame_at = time.monotonic()
        self._ring.append(jpeg)
        for queue in self._subscribers:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()  # video: drop the stale frame
            queue.put_nowait(jpeg)
