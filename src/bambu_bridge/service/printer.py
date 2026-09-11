"""PrinterService — one live printer (spec 3 ``service/printer.py``).

Owns the MQTT link, the merged in-memory state, and a per-printer
:class:`EventBus`. Translates the raw report firehose into the wire schema
(spec 7): a ``snapshot`` on (re)seed, ``delta`` for incremental changes, and
named ``event`` messages on meaningful transitions.

No FastAPI here — the API layer subscribes to the bus and reads
:meth:`snapshot`; the job state machine (M4) keys off the same named events.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

import structlog

from bambu_bridge.db.jobs import FilamentMemory
from bambu_bridge.hms import lookup as hms_lookup
from bambu_bridge.protocol import tls as tls_probe
from bambu_bridge.protocol.camera import CameraStream
from bambu_bridge.protocol.models import GcodeState, ReportMessage, build_command
from bambu_bridge.protocol.mqtt import MqttClient, SessionErrorPhase
from bambu_bridge.service.events import Event, EventBus, diff_state
from bambu_bridge.translate import (
    SnapshotContext,
    _started_at_iso,
    build_print_error,
    engaged_slot,
    translate_snapshot,
)

log = structlog.get_logger(__name__)

TouchCallback = Callable[[], Awaitable[None]]
# Restart-recovery hook: returns the start time (UTC) of the print this printer
# is currently running, recovered from jobs.db, or None when the bridge has no
# record (e.g. a screen/SD-started print, which never creates a job row).
RecoverStartedAt = Callable[[], Awaitable[datetime | None]]
# G3 hooks — filament memory.
# Loader: called once on seed to populate the in-memory cache from the DB.
LoadFilamentMemory = Callable[[], Awaitable[dict[int, FilamentMemory]]]
# Invalidator: called when a slot's tray type changes; removes one DB row.
InvalidateFilamentMemory = Callable[[int], Awaitable[None]]

# Dead-reckon seed values applied on a successful home (G28).
#
# The P1S G28 end position is NOT documented in this repo's research/ or
# CONNECTING.md, and the printer does not report toolhead position over MQTT.
# Per the fail-safe rule we seed the *most-pessimistic* interpretation: bed at
# the nozzle (Z gap closed) and X/Y at frame min. Standard Marlin frame:
# Z=0 = gap closed (bed touching nozzle), Z increases as the bed lowers / gap
# opens. Seeding low can only over-reject gap-closing jogs (harmless); seeding
# high could permit a crash if the real position were lower (unacceptable).
POST_HOME_X = 0.0
POST_HOME_Y = 0.0
POST_HOME_Z = 0.0

# TOFU cert state (contract §4.5 / PR A.2 war-council):
#   "unknown"  — no fingerprint recorded (legacy printer, pre-TOFU; treat
#                as benign — the bridge has never seen this cert before and
#                can't compare).
#   "trusted"  — current cert sha256 matches what we have on disk; the link
#                is what we expected.
#   "changed"  — current cert sha256 differs. Reads/control endpoints
#                refuse with 403 ``printer_cert_changed`` until the operator
#                POSTs /printers/{id}/trust to re-pin.
CertStatus = Literal["unknown", "trusted", "changed"]

# gcode_state values that mean "a print is not actively extruding". A move
# into RUNNING *from* one of these is a fresh start; PAUSE->RUNNING is a
# resume and is deliberately not reported as print_started (contract
# §6.0.1: PREPARE is part of "preparing", not "printing" — the §6.3
# boundary lives at layer_num > 0, fired separately as `print_progress`).
_INACTIVE_STATES = {
    None,
    GcodeState.IDLE,
    GcodeState.PREPARE,
    GcodeState.FINISH,
    GcodeState.FAILED,
    GcodeState.UNKNOWN,
}

# Feed-warning watchdog (contract §12.2): once gcode_state is RUNNING and
# 90s elapse without `ams.tray_now != 255`, fire a non-failing `feed_warning`
# named event. Distinct from JobRun's 600s FED_NO_PROGRESS hard-fail — this
# one is a UX nudge ("Is the filament loaded? AMS not feeding yet"), the
# other is the §6.3 safety abort. The watchdog auto-clears on engagement.
_FEED_WARNING_DELAY_S = 90.0


def _iso(ts: float | None) -> str | None:
    """`time.time()` epoch float → ISO 8601 UTC string, or None pass-through."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def _dt_iso(dt: datetime | None) -> str | None:
    """UTC ``datetime`` → ISO 8601 ``…Z`` string, matching translate's spelling.

    The job.started_at the APK/HA consume is shaped by translate._started_at_iso
    (``%Y-%m-%dT%H:%M:%SZ``); format the bridge-synthesized value identically so
    raw-vs-synthesized starts can't render in two different ISO dialects.
    """
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tray_now(state: dict[str, Any]) -> str | None:
    ams = state.get("ams")
    if not isinstance(ams, dict):
        return None
    val = ams.get("tray_now")
    return str(val) if val is not None else None


def _ams_currently_engaged(state: dict[str, Any]) -> bool:
    """tray_now != 255 (and is a digit) means the feed is engaged."""
    tn = _tray_now(state)
    if tn is None or tn == "":
        return False
    return tn.isdigit() and tn != "255"


def _per_slot_types(state: dict[str, Any]) -> dict[int, str]:
    """Return {physical_slot: tray_type} for all non-empty AMS slots.

    Used by the filament-memory invalidation check: when a slot's tray_type
    changes to a *different* non-empty value the old label is stale.
    Maps physical_slot (1-based) to the raw tray_type string.
    Only includes slots where tray_type is a non-empty string.
    """
    ams = state.get("ams")
    if not isinstance(ams, dict):
        return {}
    # Try ams.ams[0].tray[] first (the standard P1S layout).
    units = ams.get("ams")
    trays: list[Any] = []
    if isinstance(units, list) and units and isinstance(units[0], dict):
        trays = units[0].get("tray") or []
    elif isinstance(ams.get("tray"), list):
        trays = ams["tray"]
    result: dict[int, str] = {}
    for i, t in enumerate(trays):
        if isinstance(t, dict):
            tt = t.get("tray_type")
            if tt and isinstance(tt, str):
                result[i + 1] = tt  # physical_slot = array_idx + 1
    return result


def _deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge ``incoming`` into ``base`` (Bambu sends partial push_status).

    Nested dicts merge recursively; lists/scalars replace. Lists (e.g. the AMS
    tray array) arrive whole in ``pushall`` and replacing them on partial
    updates is correct here — element-wise patching would need indices Bambu
    does not always send.
    """
    out = dict(base)
    for key, value in incoming.items():
        cur = out.get(key)
        if isinstance(cur, dict) and isinstance(value, dict):
            out[key] = _deep_merge(cur, value)
        else:
            out[key] = value
    return out


class PrinterService:
    """Holds connection + state for one printer; emits wire-schema events."""

    def __init__(
        self,
        serial: str,
        ip: str,
        access_code: str,
        *,
        friendly_name: str,
        model: str | None = None,
        on_seen: TouchCallback | None = None,
        mqtt_port: int = 8883,
        camera_port: int = 6000,
        camera_linger_s: float = 10.0,
        expected_fingerprint: str | None = None,
        recover_started_at: RecoverStartedAt | None = None,
        load_filament_memory: LoadFilamentMemory | None = None,
        invalidate_filament_memory: InvalidateFilamentMemory | None = None,
        nozzle_type: str | None = None,
    ) -> None:
        self.serial = serial
        self.ip = ip
        self.access_code = access_code
        self.friendly_name = friendly_name
        self.model = model
        # Wave-1: "hardened_steel" allows 300 °C nozzle target; None or any
        # other value falls back to the 280 °C stainless ceiling.
        self.nozzle_type = nozzle_type
        self._mqtt_port = mqtt_port
        self._camera_port = camera_port
        self._camera_linger_s = camera_linger_s
        self._camera: CameraStream | None = None

        self.bus = EventBus()
        self.start_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self.raw_bus = EventBus()  # native P1S clients need every ack, even unchanged reports
        self._native_categories: dict[str, dict[str, Any]] = {}
        self._on_seen = on_seen
        self._state: dict[str, Any] = {}
        self._connected = False
        self._ever_connected = False
        self._need_seed = True
        self._gcode_state: GcodeState | None = None
        self._error_signature: tuple[Any, Any] | None = None
        # Bridge-synthesized print start time (UTC). The P1S ships NO start-time
        # field in push_status (verified on hardware: 64 raw keys, none of them
        # gcode_start_time/start_time), so the bridge is the only source of
        # job.started_at. Stamped on the gcode_state→RUNNING transition (a fresh
        # start, not a PAUSE→RUNNING resume — _INACTIVE_STATES excludes PAUSE),
        # cleared when the job ends (FINISH/FAILED), and recovered from jobs.db
        # if the bridge restarts mid-print (deploy) and finds itself already
        # RUNNING. Never re-stamped on pause/resume.
        self._print_started_at: datetime | None = None
        self._recover_started_at = recover_started_at
        # G3 — filament memory cache. Populated on the first seed from the DB
        # loader; updated on PUT /filament-memory; cleared per-slot on tray swap.
        # None until the seed loader runs (tests without wiring get None → the
        # snapshot ctx passes None → slot["memory"] is null for every slot).
        self._filament_memory: dict[int, FilamentMemory] | None = None
        self._load_filament_memory = load_filament_memory
        self._invalidate_filament_memory = invalidate_filament_memory
        # True once we've absorbed the *first* report after a (re)connect.
        # Named events are *transitions*, not seed observations — a printer
        # that's already RUNNING/FINISH/FAILED when the bridge connects must
        # NOT fire spurious print_started / print_completed / error events
        # (Sauron war-council CRIT #2; would let a fresh JobRun consume some
        # other transition as its own RUNNING signal).
        self._events_seeded = False

        # TOFU cert state (PR A.2; contract §4.5). `expected_fingerprint` is
        # what the registry loaded from `printers.cert_fingerprint`; `None`
        # means a pre-PR-A printer with no recorded fingerprint — degrade
        # gracefully to "unknown" (read-only-from-TLS-policy POV) so the
        # APK doesn't lock the operator out of legacy rows.
        self.expected_fingerprint = expected_fingerprint
        self.current_fingerprint: str | None = None
        self.cert_status: CertStatus = "unknown"
        # Session-health surface (PR A.2 writes; PR B reads in snapshot()).
        # ISO at read time; monotonic-ish at write. `last_failure_phase` is
        # cleared on the next successful connect — staleness would be
        # actively misleading to the APK's status dot.
        self.last_failure_phase: SessionErrorPhase | None = None
        self.last_failure_text: str | None = None
        self.last_failure_at: float | None = None

        # PR B bookkeeping:
        # - `_last_telemetry_at` bumps on every successful report; the APK
        #   computes "Last update Ns ago" off it (contract §6.2 disconnect
        #   override). unix epoch seconds, float (ISO converted at read).
        # - `_last_layer_num` is the previous report's layer_num so we can
        #   detect the 0 → positive transition that emits `print_progress`
        #   (the §6.0.1 "now actually printing" signal).
        # - `_feed_warning_*` drives the 90s `tray_now == 255` watchdog
        #   that fires `feed_warning` once per RUNNING entry, then clears.
        self._last_telemetry_at: float | None = None
        self._last_layer_num: int = 0
        self._feed_warning_task: asyncio.Task[None] | None = None
        self._feed_warning_fired: bool = False
        self._last_connect_attempt_at: float | None = None
        # Epoch of the last MQTT drop — `connection_restored` reports
        # `missed_ms` (offline duration) off it (contract §12.2).
        self._disconnected_at: float | None = None

        # Dead-reckoned toolhead position (crash-prevention; jog guard).
        #
        # The P1S does NOT report toolhead X/Y/Z in its MQTT push_status —
        # `home_flag` (a homing *bitmask*) is the only motion-state signal it
        # sends. There is no x/y/z field to clamp against. So the bridge must
        # track its own best estimate or the envelope guard is dead code
        # (proven on hardware: four consecutive Z-50 jogs were accepted and
        # drove the bed ~200 mm into the toolhead because the clamp never had
        # a position to compare against).
        #
        # Contract: `None` means UNKNOWN — the guard fails closed on unknown.
        # A float is the bridge's best estimate in the standard Marlin frame
        # (Z=0 = bed at nozzle / gap closed; Z=256 = bed fully lowered / gap
        # open). Set on a successful home, advanced by the signed delta on a
        # successful jog, and reset to None on any desync event (disconnect,
        # print start/stop, MQTT session loss) because a move we can't see
        # invalidates the estimate.
        self._tracked_pos: dict[str, float | None] = {"X": None, "Y": None, "Z": None}

        self._mqtt = self._build_mqtt()
        self._task: asyncio.Task[None] | None = None
        self._log = log.bind(printer_id=serial)

    # ----------------------------------------------------------------- #
    # Lifecycle
    # ----------------------------------------------------------------- #

    def _build_mqtt(self) -> MqttClient:
        return MqttClient(
            self.ip,
            self.serial,
            self.access_code,
            on_report=self._handle_report,
            on_connected=self._handle_connected,
            on_lost=self._handle_lost,
            on_session_error=self._handle_session_error,
            port=self._mqtt_port,
        )

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._mqtt.run())
            self._log.info("printer.started")

    async def stop(self) -> None:
        self._cancel_feed_warning_watchdog()
        if self._camera is not None:
            await self._camera.aclose()
            self._camera = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
            self._log.info("printer.stopped")

    @property
    def camera(self) -> CameraStream:
        """Lazily-created camera multiplexer (upstream opens on first viewer)."""
        if self._camera is None:
            self._camera = CameraStream(
                self.ip,
                self.access_code,
                port=self._camera_port,
                linger_s=self._camera_linger_s,
            )
        return self._camera

    async def reconnect(self) -> None:
        """Re-establish the link after an ip/access_code change (open Q #4)."""
        await self.stop()
        self._mqtt = self._build_mqtt()
        self._need_seed = True
        await self.start()

    # ----------------------------------------------------------------- #
    # Reads
    # ----------------------------------------------------------------- #

    @property
    def connected(self) -> bool:
        return self._connected

    def snapshot(self) -> dict[str, Any]:
        """Translated snapshot — the §6 wire shape consumed by the APK.

        Pure on the read side: builds a :class:`SnapshotContext` from the
        current PR A.2 + PR B service attrs and delegates the actual
        shape-building to :func:`bambu_bridge.translate.translate_snapshot`
        (where every wire-shape test lives). The raw `state` dict is
        preserved under `_raw` by the translator so nothing the printer
        said is dropped.
        """
        ctx = SnapshotContext(
            printer_id=self.serial,
            serial=self.serial,
            friendly_name=self.friendly_name,
            model=self.model,
            connected=self._connected,
            last_telemetry_at=_iso(self._last_telemetry_at),
            last_connect_attempt=_iso(self._last_connect_attempt_at),
            last_failure_phase=self.last_failure_phase,
            cert_status=self.cert_status,
            expected_fingerprint=self.expected_fingerprint,
            print_started_at=_dt_iso(self._print_started_at),
            filament_memory=self._filament_memory,
        )
        return translate_snapshot(self._state, ctx)

    def native_snapshot(self) -> dict[str, Any]:
        """Protocol-shaped state for native LAN clients, including AMS/virtual tray."""
        snapshot = dict(self._native_categories)
        if "print" in snapshot:
            snapshot["print"] = {**snapshot["print"], "command": "push_status", "msg": 0}
        return snapshot

    def summary(self) -> dict[str, Any]:
        """Compact last-known state for the printer list (spec 6 GET /printers)."""
        return {
            "printer_id": self.serial,
            "serial": self.serial,
            "friendly_name": self.friendly_name,
            "model": self.model,
            "connected": self._connected,
            "gcode_state": self._gcode_state.value if self._gcode_state else None,
            "progress_pct": self._state.get("mc_percent"),
            "subtask_name": self._state.get("subtask_name"),
        }

    # ----------------------------------------------------------------- #
    # G3 — filament memory cache (public surface for API + registry)
    # ----------------------------------------------------------------- #

    @property
    def filament_memory(self) -> dict[int, FilamentMemory]:
        """Current in-memory cache of filament labels, keyed by physical_slot."""
        return dict(self._filament_memory) if self._filament_memory is not None else {}

    def set_filament_memory_entry(self, slot: int, entry: FilamentMemory) -> None:
        """Update one slot's cache entry after a successful DB upsert."""
        if self._filament_memory is None:
            self._filament_memory = {}
        self._filament_memory[slot] = entry

    def delete_filament_memory_entry(self, slot: int) -> None:
        """Remove one slot's cache entry (user DELETE or tray invalidation)."""
        if self._filament_memory is not None:
            self._filament_memory.pop(slot, None)

    # ----------------------------------------------------------------- #
    # Commands (thin passthrough; typed helpers added with M3 endpoints)
    # ----------------------------------------------------------------- #

    async def send_command(
        self, category: str, command: str, *,
        before_publish: Callable[[], None] | None = None, **fields: Any
    ) -> None:
        envelope = build_command(category, command, **fields)
        if before_publish is None:
            await self._mqtt.publish(envelope)
        else:
            await self._mqtt.publish(envelope, before_publish=before_publish)

    async def send_raw(self, envelope: dict[str, Any]) -> None:
        """Publish a pre-built command envelope (e.g. from protocol.commands).

        The envelope already carries its ``sequence_id``; this is the path the
        typed control endpoints use so the builder stays the single source of
        wire truth.
        """
        from bambu_bridge.service.command_guard import guard_passthrough

        guard_passthrough(envelope)
        command = envelope.get("print")
        if isinstance(command, dict) and command.get("command") == "gcode_file":
            raise ConnectionError(
                "Raw gcode-file starts bypass slice validation; use managed 3MF starts"
            )
        if isinstance(command, dict) and command.get("command") == "project_file":
            handler = getattr(self, "start_handler", None)
            if handler is None:
                raise ConnectionError("Managed print starts are unavailable")
            await handler(command)
            return
        await self._mqtt.publish(envelope)

    # ----------------------------------------------------------------- #
    # Dead-reckoned motion state (jog crash-prevention)
    # ----------------------------------------------------------------- #

    def tracked_position(self, axis: str) -> float | None:
        """Bridge's best-estimate position for ``axis`` (mm), or None=unknown.

        The P1S never reports toolhead position, so this is dead-reckoned —
        seeded on a successful home, advanced on each successful jog, reset to
        None on any event that could desync the estimate.
        """
        return self._tracked_pos.get(axis.upper())

    def mark_homed(self) -> None:
        """Record a successful home: seed the dead-reckon estimate.

        Post-home Z is set to the *most-pessimistic safe* value (gap closed,
        Z=0). The P1S G28 end position is not documented in this repo and the
        firmware does not report it, so we deliberately pick the assumption
        that fails safe if wrong: tracking Z low can only over-reject
        gap-closing (Z-) jogs (annoyance), never permit a crash. Tracking it
        high would let a gap-closing jog run into the bed if the real position
        were lower — unacceptable. X/Y home to their min (0) for the same
        reason; over-rejecting a frame-ward X/Y jog is harmless.
        """
        self._tracked_pos = {"X": POST_HOME_X, "Y": POST_HOME_Y, "Z": POST_HOME_Z}

    def apply_jog(self, axis: str, distance_mm: float) -> None:
        """Advance the dead-reckon estimate after a *successful* jog publish.

        No-op when the axis is currently unknown — we never invent a position;
        an unknown axis stays unknown until the next home.
        """
        axis = axis.upper()
        cur = self._tracked_pos.get(axis)
        if cur is not None:
            self._tracked_pos[axis] = cur + distance_mm

    def reset_motion_state(self, reason: str) -> None:
        """Forget the dead-reckon estimate — any desync event calls this.

        After a disconnect, MQTT session loss, or print start/stop the
        toolhead may have moved in ways the bridge cannot see, so the only
        safe estimate is "unknown". The jog guard then fails closed until the
        operator re-homes.
        """
        if any(v is not None for v in self._tracked_pos.values()):
            self._log.info("motion.reset", reason=reason)
        self._tracked_pos = {"X": None, "Y": None, "Z": None}

    # ----------------------------------------------------------------- #
    # MQTT callbacks
    # ----------------------------------------------------------------- #

    async def _handle_connected(self) -> None:
        self._connected = True
        self._need_seed = True
        self._last_connect_attempt_at = time.time()
        # Re-seed event classification after a (re)connect — the printer may
        # have transitioned states while we were disconnected; absorb the
        # first post-reconnect report as the new baseline, don't replay it.
        self._events_seeded = False
        # Successful connect clears the previous session_error surface — a
        # stale "tls_handshake failed 10 min ago" would actively mislead
        # the APK's status dot once the link is back up.
        self.last_failure_phase = None
        self.last_failure_text = None
        self.last_failure_at = None
        # TOFU compare-on-connect (contract §4.5; war-council PR A.2). Do
        # this *before* we announce connection_restored so the API edge
        # never sees a "connected + status=trusted" window that briefly
        # contradicts itself. The probe is best-effort: a hiccup here must
        # not tear down a healthy MQTT session — fall back to the previous
        # status if the handshake glitches.
        await self._tofu_compare()
        if self._ever_connected:
            now = time.time()
            missed_ms = (
                int((now - self._disconnected_at) * 1000)
                if self._disconnected_at is not None
                else 0
            )
            self.bus.publish(
                Event(
                    "event",
                    {"at": _iso(now), "missed_ms": missed_ms},
                    name="connection_restored",
                )
            )
        self._ever_connected = True
        self._disconnected_at = None
        # One-shot device identity (firmware + per-module versions/serials).
        # pushall does NOT include it; ask explicitly. The reply is category
        # ``info`` and lands at ``state["info"]`` via the passthrough below.
        # (cf. protocol.commands.get_version — same envelope.)
        with contextlib.suppress(Exception):
            await self.send_command("info", "get_version")

    async def _handle_lost(self) -> None:
        self._connected = False
        # A print may end and another start while offline. Never retain a
        # session identity that could authorize a stale worker's stop.
        self.bus.session_id = None
        self._disconnected_at = time.time()
        # Desync: while disconnected the toolhead may move (firmware recovery,
        # operator at the screen, a print starting) without us seeing it. Drop
        # the dead-reckon estimate so the jog guard fails closed until re-home.
        self.reset_motion_state("connection_lost")
        self.bus.publish(
            Event("event", {"at": _iso(self._disconnected_at)}, name="connection_lost")
        )

    async def _handle_session_error(self, phase: str, human: str) -> None:
        """Hook fired by :class:`MqttClient` on each failed session.

        ``phase`` is the stable enum from
        :func:`bambu_bridge.protocol.mqtt.classify_mqtt_error`; the value
        sticks on the service until the next successful connect clears it.
        """
        import time as _time

        # `phase` arrives as the stable enum from classify_mqtt_error; mypy
        # sees it as plain str through the Callable boundary so cast on the way in.
        self.last_failure_phase = phase  # type: ignore[assignment]
        self.last_failure_text = human
        self.last_failure_at = _time.time()
        # A failed MQTT session means we may have missed motion; forget the
        # dead-reckon estimate (fail closed) until the next successful home.
        self.reset_motion_state("session_error")

    async def _tofu_compare(self) -> None:
        """Re-fetch the leaf cert and update ``cert_status`` accordingly.

        Stays best-effort: a transient handshake failure during this probe
        leaves the previous status untouched (the failure will have already
        torn down the MQTT session anyway — that's reported via the
        ``session_error`` path, not here).
        """
        try:
            cert = await tls_probe.leaf_cert_fingerprint(self.ip, self._mqtt_port)
        except (ConnectionError, OSError):
            self._log.debug("tofu.probe_failed_leaving_status_unchanged")
            return

        self.current_fingerprint = cert.fingerprint_sha256
        previous = self.cert_status

        if self.expected_fingerprint is None:
            # Legacy row — no pin to compare against. Quietly stay "unknown"
            # until the operator hits POST /trust (which sets the pin) or
            # re-registers the printer.
            self.cert_status = "unknown"
            return

        if cert.fingerprint_sha256 == self.expected_fingerprint:
            self.cert_status = "trusted"
            if previous == "changed":
                # operator hit /trust → pin updated → next connect cleared
                self.bus.publish(
                    Event("event", {"fingerprint": cert.fingerprint_sha256},
                          name="cert_trusted")
                )
            return

        # Mismatch — fence reads/control until /trust is hit. Fire once
        # per transition so the APK can show the in-app TOFU prompt
        # without re-spamming on every reconnect.
        self.cert_status = "changed"
        if previous != "changed":
            self.bus.publish(
                Event(
                    "event",
                    {
                        "previous_fingerprint": self.expected_fingerprint,
                        "current_fingerprint": cert.fingerprint_sha256,
                    },
                    name="cert_changed",
                )
            )

    async def _handle_report(self, report: ReportMessage) -> None:
        raw = report.model_dump(mode="json", exclude_none=True, exclude_unset=True)
        for category, payload in raw.items():
            if isinstance(payload, dict):
                self._native_categories[category] = _deep_merge(
                    self._native_categories.get(category, {}), payload
                )
        self.raw_bus.publish(Event("snapshot", raw))
        # Full passthrough: ``print`` stays flattened at the state root (the
        # established wire contract — clients read state.gcode_state etc.);
        # every *other* category the P1S emits (info, system, mc_print,
        # liveview, …) is preserved verbatim under its own key. Nothing the
        # printer says is dropped any more.
        incoming: dict[str, Any] = {}
        if report.print is not None:
            # mode="json": enums -> str, so snapshots/deltas are wire-ready
            # and diff_state compares like-typed values across reports.
            incoming = report.print.model_dump(mode="json", exclude_none=True)
        for category, payload in (report.model_extra or {}).items():
            if isinstance(payload, dict):
                incoming[category] = payload

        if not incoming:
            return
        if self._on_seen is not None:
            await self._on_seen()

        # Bump telemetry watermark — every report counts, even ones that
        # produce an empty delta. The APK's "Last update Ns ago" subtitle
        # reads this to render the disconnect headline (contract §6.2).
        self._last_telemetry_at = time.time()

        prev_state = self._state
        prev_gcode = self._gcode_state
        prev_layer_num = self._last_layer_num

        if self._need_seed:
            self._state = incoming
            self._need_seed = False
            self._refresh_gcode_state()
            await self._maybe_recover_started_at()
            await self._maybe_load_filament_memory()
            self.bus.publish(Event("snapshot", dict(self._state)))
        else:
            merged = _deep_merge(prev_state, incoming)
            delta = diff_state(prev_state, merged)
            self._state = merged
            self._refresh_gcode_state()
            if delta:
                self.bus.publish(Event("delta", delta))

        # Re-read layer_num *after* the merge so we see the printer's view.
        try:
            self._last_layer_num = int(self._state.get("layer_num") or 0)
        except (TypeError, ValueError):
            self._last_layer_num = 0

        await self._maybe_invalidate_filament_memory()
        self._emit_named_events(prev_gcode, prev_layer_num)

    # ----------------------------------------------------------------- #
    # Event classification
    # ----------------------------------------------------------------- #

    def _refresh_gcode_state(self) -> None:
        previous = self._gcode_state
        raw = self._state.get("gcode_state")
        self._gcode_state = GcodeState(raw) if raw is not None else None
        if self._gcode_state is GcodeState.RUNNING and previous in _INACTIVE_STATES:
            # Local observation generation, NOT a firmware command receipt.
            # Stamped before delta and named events so old queued events cannot
            # complete/cancel the following same-file print.
            self.bus.session_id = uuid.uuid4().hex

    # gcode_state values where a print is in flight — used to decide whether a
    # seed-time restart should recover started_at. PAUSE counts: a print paused
    # across a deploy is still an in-flight run whose start time we want back.
    _ACTIVE_ON_SEED = frozenset({GcodeState.RUNNING, GcodeState.PAUSE})

    async def _maybe_recover_started_at(self) -> None:
        """Restart resilience: recover a mid-print start time from jobs.db.

        Called once, on the first report after a (re)connect, while seeding.
        If the printer is already RUNNING/PAUSE (a print that began before this
        bridge process started — the deploy-mid-print case) and we have no
        in-memory start time, ask the injected recovery hook for the start time
        of the live job row in jobs.db.

        Honest-unknown rule: if recovery yields nothing (no job row — e.g. the
        print was started from the printer's own screen/SD, which never creates
        a job row), leave ``_print_started_at`` at None. We do NOT stamp
        ``now()`` on recovery — that would understate the elapsed time by up to
        the entire print duration and lie to the APK's timestamp sensor.
        """
        if self._recover_started_at is None:
            return
        if self._gcode_state not in self._ACTIVE_ON_SEED:
            return
        if self._print_started_at is not None:
            return
        try:
            recovered = await self._recover_started_at()
        except Exception:  # noqa: BLE001 — recovery is best-effort, never fatal
            self._log.warning("started_at.recover_failed")
            return
        if recovered is not None:
            self._print_started_at = recovered.astimezone(UTC)
            self._log.info("started_at.recovered", started_at=_dt_iso(recovered))

    async def _maybe_load_filament_memory(self) -> None:
        """Seed the in-memory filament-memory cache from the DB once on start/attach.

        Called on the first report after a (re)connect, alongside
        `_maybe_recover_started_at`. If no loader was injected (tests that
        don't wire the DB) the cache stays None and every slot gets
        ``"memory": null`` in the snapshot — that is the correct no-op behaviour.
        """
        if self._load_filament_memory is None:
            return
        if self._filament_memory is not None:
            return  # already loaded (reconnect path — don't overwrite live cache)
        try:
            self._filament_memory = await self._load_filament_memory()
        except Exception:  # noqa: BLE001 — best-effort; cache stays None
            self._log.warning("filament_memory.load_failed")

    async def _maybe_invalidate_filament_memory(self) -> None:
        """Per-report tray-type check — invalidate stale labels on tray swaps.

        For every slot in the current AMS state: if we have a cached label AND
        the slot's current tray_type is non-empty AND it differs from the
        ``tray_type_seen`` recorded when the label was written, the user has
        swapped to a different material. Drop the label from cache + DB.

        Empty/absent tray does NOT invalidate — the slot might just be unloaded
        briefly; the same material may come back. Only a confirmed different
        non-empty type triggers invalidation.
        """
        if self._filament_memory is None or not self._filament_memory:
            return
        current_types = _per_slot_types(self._state)
        for slot, entry in list(self._filament_memory.items()):
            current_type = current_types.get(slot)
            if not current_type:
                continue  # slot empty/absent — leave label intact
            recorded = entry.tray_type_seen
            if recorded and current_type != recorded:
                # Different non-empty type confirmed — invalidate.
                self._filament_memory.pop(slot, None)
                self._log.info(
                    "filament_memory.invalidated",
                    slot=slot,
                    was=recorded,
                    now=current_type,
                )
                if self._invalidate_filament_memory is not None:
                    try:
                        await self._invalidate_filament_memory(slot)
                    except Exception:  # noqa: BLE001 — best-effort; log and move on
                        self._log.warning(
                            "filament_memory.invalidate_db_failed", slot=slot
                        )

    def _emit_named_events(
        self, prev: GcodeState | None, prev_layer_num: int
    ) -> None:
        if not self._events_seeded:
            # First report after (re)connect — establish the baseline silently.
            # Seed the error signature too so _maybe_emit_error doesn't fire
            # on a pre-existing print_error left over from before we connected.
            self._events_seeded = True
            code = self._state.get("mc_print_error_code")
            perr = self._state.get("print_error")
            has_error = (code not in (None, "0", "")) or bool(perr)
            self._error_signature = (code, perr) if has_error else None
            return
        cur = self._gcode_state
        if cur is not None and cur != prev:
            # Any print lifecycle transition drives the toolhead with the
            # job's own G-code — motion the bridge can't account for. Forget
            # the dead-reckon estimate on every state change so a jog issued
            # mid/post-print can't trust a stale position. Fail closed until
            # the operator re-homes.
            self.reset_motion_state(f"gcode_state:{prev}->{cur}")
            if cur is GcodeState.RUNNING and prev in _INACTIVE_STATES:
                # Fresh start (PAUSE is excluded from _INACTIVE_STATES so a
                # resume never lands here). Stamp the bridge-synthesized start
                # time now — the P1S gives us nothing — preferring a raw value
                # if a future firmware ever supplies one. The event payload
                # carries the same synthesized value the snapshot will expose.
                self._print_started_at = datetime.now(UTC)
                self.bus.publish(
                    Event(
                        "event",
                        {
                            "subtask_name": self._state.get("subtask_name"),
                            "started_at": _started_at_iso(self._state)
                            or _dt_iso(self._print_started_at),
                        },
                        name="print_started",
                    )
                )
                # RUNNING entered — start the feed-warning watchdog. If
                # AMS engages within 90s the watchdog cancels itself; else
                # it fires `feed_warning` once.
                self._start_feed_warning_watchdog()
            elif cur is GcodeState.FINISH:
                self._print_started_at = None
                self.bus.publish(Event("event", self._completion_data(), name="print_completed"))
                self._cancel_feed_warning_watchdog()
            elif cur is GcodeState.FAILED:
                self._print_started_at = None
                self.bus.publish(
                    Event(
                        "event",
                        {
                            "print_error": build_print_error(
                                self._state.get("mc_print_error_code")
                                or self._state.get("print_error")
                            ),
                            "layer_num": self._state.get("layer_num"),
                        },
                        name="print_failed",
                    )
                )
                self._cancel_feed_warning_watchdog()

        # `print_progress` — layer_num crossing 0 → positive (contract
        # §6.0.1). This is the canonical "printing actually began" signal
        # the job FSM consumes to move PREPARING → PRINTING. Fire once
        # per crossing; further layer increments do not re-emit.
        if prev_layer_num == 0 and self._last_layer_num > 0:
            self.bus.publish(
                Event(
                    "event",
                    {
                        "layer_num": self._last_layer_num,
                        "total_layer_num": self._state.get("total_layer_num"),
                    },
                    name="print_progress",
                )
            )

        # Feed-warning watchdog auto-clear: if the AMS just engaged
        # (tray_now flipped off 255), cancel the pending watchdog.
        if self._feed_warning_task is not None and _ams_currently_engaged(self._state):
            self._cancel_feed_warning_watchdog()

        self._maybe_emit_error()

    def _completion_data(self) -> dict[str, Any]:
        return {
            "subtask_name": self._state.get("subtask_name"),
            "layer_num": self._state.get("layer_num"),
            "total_layer_num": self._state.get("total_layer_num"),
        }

    def _maybe_emit_error(self) -> None:
        """Emit on the *transition* into an error, not every push while it persists.

        The ``error`` event carries the structured §6 ``print_error`` object
        (built by the shared :func:`build_print_error`, so the snapshot's
        error object and the event's can never drift). ``filament_runout``
        carries the minimal §12.2 ``{code, slot}`` — the engaged physical
        slot — leaving runout copy to the APK / the §8.4 feed renderer.
        The persisted row's severity bucket (info/warn/error) is assigned
        downstream by the EventPersister for the NotificationsScreen filter.
        """
        code = self._state.get("mc_print_error_code")
        perr = self._state.get("print_error")
        has_error = (code not in (None, "0", "")) or bool(perr)
        signature = (code, perr) if has_error else None
        if signature == self._error_signature:
            return  # unchanged — already reported (or still clear)
        self._error_signature = signature
        if not has_error:
            return  # error just cleared
        raw_code = code or perr
        entry = hms_lookup(raw_code)
        # Bucketed event name — the §8.4 catalog distinguishes
        # `filament_runout` from generic `error` so the APK can render
        # an actionable warning rather than a red alarm.
        if entry["category"] == "ams" and "runout" in entry["user_message"].lower():
            self.bus.publish(
                Event(
                    "event",
                    {"code": str(raw_code), "slot": engaged_slot(_tray_now(self._state))},
                    name="filament_runout",
                )
            )
        else:
            self.bus.publish(
                Event(
                    "event",
                    {"print_error": build_print_error(raw_code)},
                    name="error",
                )
            )

    # ----------------------------------------------------------------- #
    # Feed-warning watchdog (contract §12.2)
    # ----------------------------------------------------------------- #

    def _start_feed_warning_watchdog(self) -> None:
        """Spawn (or restart) the 90s `tray_now == 255` watchdog.

        Re-entry is safe: a fresh RUNNING transition cancels any prior
        watchdog so the timer always reflects the latest start.
        """
        self._cancel_feed_warning_watchdog()
        self._feed_warning_fired = False
        self._feed_warning_task = asyncio.create_task(self._feed_warning_after_delay())

    def _cancel_feed_warning_watchdog(self) -> None:
        task = self._feed_warning_task
        if task is not None:
            task.cancel()
            self._feed_warning_task = None

    async def _feed_warning_after_delay(self) -> None:
        try:
            await asyncio.sleep(_FEED_WARNING_DELAY_S)
        except asyncio.CancelledError:
            return
        if self._feed_warning_fired:
            return
        # Re-check the engagement condition at fire time — the loop above
        # may have raced ahead.
        if _ams_currently_engaged(self._state):
            return
        self._feed_warning_fired = True
        self.bus.publish(
            Event(
                "event",
                {
                    "since_ms": int(_FEED_WARNING_DELAY_S * 1000),
                    "advice": "look at the plate",
                },
                name="feed_warning",
            )
        )
