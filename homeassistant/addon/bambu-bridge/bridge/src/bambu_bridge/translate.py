"""Pure snapshot translator — raw P1S MQTT push_status → contract §6 shape.

PR B's edge-translation layer. Pure functions only — no service imports,
no I/O, no async. The caller (PrinterService.snapshot) hands in the
current raw state dict + a SnapshotContext with the metadata fields the
translator can't derive from telemetry, and gets back the wire-shaped
snapshot the APK consumes.

Two things this enforces (the war-council critical rules):

1. **The phase rule** (contract §6.0.1). `gcode_state == RUNNING` is not
   "printing" — `RUNNING + layer_num == 0` is heat-soak + bed leveling.
   `job.percent` is exposed only when `phase == "printing"`; during
   `preparing` it's `null` regardless of what `mc_percent` says. This
   is the §6.3 boundary.

2. **AMS dual-emission** (contract §6.1). Every AMS slot ships its
   1-based `physical_slot` (what the APK renders as "Slot N") AND
   the protocol's 0-based `_raw_id`. `tray_now` translation:
   255 → `null`, 254 → `"external"`, 0-3 → 1-4.

The `_raw` block at the snapshot root preserves the keys an APK or
operator would need to debug a misclassification.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from bambu_bridge.hms import (
    JobContext,
    decode_hms_entry,
    stage_text,
)
from bambu_bridge.hms import (
    lookup as hms_lookup,
)
from bambu_bridge.hms import (
    wiki_url as hms_wiki_url,
)

# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #

Phase = Literal[
    "idle", "preparing", "printing", "paused", "completed", "failed", "unknown"
]


@dataclass(slots=True, frozen=True)
class SnapshotContext:
    """Metadata the translator can't derive from raw telemetry alone."""

    printer_id: str
    serial: str
    friendly_name: str
    model: str | None
    connected: bool
    # Bridge-maintained session-health (PR A.2 writes these onto PrinterService;
    # PR B reads them here):
    last_telemetry_at: str | None  # ISO 8601 UTC
    last_connect_attempt: str | None  # ISO 8601 UTC
    last_failure_phase: str | None  # tls_handshake | mqtt_connack | … | None
    # TOFU (PR A.2 writes these onto PrinterService; PR B reads):
    cert_status: Literal["unknown", "trusted", "changed"]
    expected_fingerprint: str | None
    # Bridge-synthesized print start time (ISO 8601 UTC), or None when unknown.
    # The P1S firmware ships NO start-time field in push_status, so the raw
    # `_started_at_iso(raw)` path returns None forever on this hardware; the
    # service stamps this on the gcode_state→RUNNING transition (and recovers
    # it from jobs.db after a restart). `_job_block` prefers a raw value if a
    # future firmware ever supplies one, else falls back to this.
    print_started_at: str | None = None
    # G3 — per-slot filament memory. Keyed by physical_slot (1-based). None
    # means the service has not loaded memory yet (e.g. tests that don't wire
    # it); an empty dict means memory is wired but no labels are stored.
    # `_ams_block` merges this into each slot's dict as "memory": {...}|null.
    filament_memory: dict[int, Any] | None = field(default=None)


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #


def translate_snapshot(raw: dict[str, Any], ctx: SnapshotContext) -> dict[str, Any]:
    """Translate one raw push_status dict → contract §6 snapshot.

    Pure. Total. Never raises on missing fields — every getter has a
    benign default so partial state still produces a coherent shape.
    """
    phase, phase_reason = _phase_and_reason(raw)
    job_ctx = _job_context(raw)
    return {
        "printer_id": ctx.printer_id,
        "serial": ctx.serial,
        "friendly_name": ctx.friendly_name,
        "model": ctx.model,
        "session": {
            "connected": ctx.connected,
            "last_telemetry_at": ctx.last_telemetry_at,
            "last_connect_attempt": ctx.last_connect_attempt,
            "last_failure_phase": ctx.last_failure_phase,
        },
        "cert_status": ctx.cert_status,
        "expected_fingerprint": ctx.expected_fingerprint,
        "phase": phase,
        "phase_reason": phase_reason,
        "headline": _headline(phase, phase_reason, raw, ctx.connected, ctx.last_telemetry_at),
        "job": _job_block(raw, phase, ctx),
        "job_context": job_ctx,
        "job_anomaly": _job_anomaly(raw),
        "temps": _temps(raw),
        "cooling": _cooling(raw),
        "lights": _lights(raw),
        "print_params": _print_params(raw),
        "motion": _motion(raw),
        "ams": _ams(raw, ctx),
        "print_error": _print_error(raw),
        "stage": _stage(raw),
        "hms": _hms_list(raw, job_ctx),
        "_raw": _raw_passthrough(raw),
    }


# --------------------------------------------------------------------------- #
# Phase + reason — the §6.0.1 boundary
# --------------------------------------------------------------------------- #


def _phase_and_reason(raw: dict[str, Any]) -> tuple[Phase, str | None]:
    gs = raw.get("gcode_state")
    layer_num = _as_int(raw.get("layer_num")) or 0
    perr = _print_error_active(raw)

    # An active print_error overrides phase classification — failure is sticky.
    if perr and gs in ("FAILED", "IDLE", "FINISH"):
        return "failed", "print_error"

    if gs is None or gs == "UNKNOWN":
        return "unknown", None
    if gs == "IDLE":
        return "idle", None
    if gs == "PAUSE":
        return "paused", None
    if gs == "FINISH":
        return "completed", "cooling_down"
    if gs == "FAILED":
        return "failed", "print_error" if perr else None
    if gs in ("PREPARE",):
        return "preparing", _preparing_reason(raw)
    if gs == "RUNNING":
        if layer_num > 0:
            return "printing", "printing_layers"
        return "preparing", _preparing_reason(raw)
    return "unknown", None


def _preparing_reason(raw: dict[str, Any]) -> str:
    """Derive a phase_reason for the preparing block.

    Priority: feed > nozzle heat > bed heat > leveling (catch-all).
    """
    # tray_now is the AMS engagement signal (§6.3). "255" = nothing engaged.
    ams = raw.get("ams")
    if isinstance(ams, dict):
        tn = str(ams.get("tray_now", "")).strip()
        if tn == "255" or tn == "":
            # Still preparing — feed hasn't engaged yet. Heuristic only;
            # the 90s feed_warning watchdog is the alerting path.
            nozzle_c = _as_float(raw.get("nozzle_temper"))
            nozzle_target = _as_float(raw.get("nozzle_target_temper"))
            if nozzle_c is not None and nozzle_target is not None and nozzle_c < nozzle_target - 5:
                return "heating_nozzle"
            bed_c = _as_float(raw.get("bed_temper"))
            bed_target = _as_float(raw.get("bed_target_temper"))
            if bed_c is not None and bed_target is not None and bed_c < bed_target - 5:
                return "heating_bed"
            return "leveling"

    # tray engaged — heating is the most likely reason for layer 0
    nozzle_c = _as_float(raw.get("nozzle_temper"))
    nozzle_target = _as_float(raw.get("nozzle_target_temper"))
    if nozzle_c is not None and nozzle_target is not None and nozzle_c < nozzle_target - 5:
        return "heating_nozzle"
    bed_c = _as_float(raw.get("bed_temper"))
    bed_target = _as_float(raw.get("bed_target_temper"))
    if bed_c is not None and bed_target is not None and bed_c < bed_target - 5:
        return "heating_bed"
    return "purging"


def _print_error_active(raw: dict[str, Any]) -> bool:
    perr = raw.get("print_error")
    if perr in (None, 0, "0", ""):
        code = raw.get("mc_print_error_code")
        return code not in (None, 0, "0", "")
    return True


# ---------------------------------------------------------------------------
# Stage ids that correspond to filament unload/retract/cut operations.
# These indicate the printer is in the "finishing" unload phase even when
# gcode_state may still read RUNNING or PAUSE.
# Source: hms.py _STAGE_TEXT table.
#   22 = "Filament unloading"
#    4 = "Changing filament" (covers mid-print filament swap unload+load)
# ---------------------------------------------------------------------------
_UNLOAD_STAGE_IDS: frozenset[int] = frozenset({4, 22})


def _job_context(raw: dict[str, Any]) -> JobContext:
    """Derive the job_context root field.

    Returns one of "no_job" | "printing" | "finishing" | "done".

    Classification rules:
    - "no_job":    gcode_state IDLE (or absent/unknown with no job indicators).
    - "done":      gcode_state FINISH.
    - "finishing": gcode_state RUNNING or PAUSE with mc_percent >= 97, OR
                   stg_cur is a filament-unload/cut/retract-family stage id
                   (see _UNLOAD_STAGE_IDS).  PAUSE at 99–100% falls here —
                   that is the primary operator scenario (AMS retract jam after
                   last layer, printer paused waiting for the user).
    - "printing":  gcode_state RUNNING/PAUSE/PREPARE/SLICING with percent < 97
                   (or percent absent), and stg_cur is not an unload stage.

    Edge choices:
    - PREPARE/SLICING (no percent): classified "printing" because a job is in
      progress and the operator needs to treat errors as actionable.
    - Unknown/absent gcode_state: classified "no_job" — insufficient signal.
    - Percent coercion via _as_int handles string/float values from the P1S.
    """
    gs = raw.get("gcode_state")

    if gs == "FINISH":
        return "done"

    if gs in (None, "UNKNOWN", "IDLE"):
        return "no_job"

    # For all active states, check stg_cur for unload-family stages first —
    # these dominate regardless of percent.
    stage_id = _as_int(raw.get("stg_cur"))
    if stage_id is not None and stage_id in _UNLOAD_STAGE_IDS:
        return "finishing"

    # Check percent for RUNNING/PAUSE/FAILED at >= 97. FAILED deliberately
    # classifies by job position, NOT "no_job": a failed print's own HMS
    # entries must never be marked stale (stale is no_job-only), and a
    # failure during end-of-print cleanup still earns the finishing-context
    # advice ("your part is complete").
    if gs in ("RUNNING", "PAUSE", "FAILED"):
        pct = _as_int(raw.get("mc_percent"))
        if pct is not None and pct >= 97:
            return "finishing"

    return "printing"


def _job_anomaly(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Detect a silent-stop anomaly: FINISH with percent well short of 100.

    Returns a structured anomaly object when gcode_state == FINISH and
    coerced mc_percent is present and < 99, AND the layer data does not
    indicate a complete print.  Returns None in all other cases.

    The most common root cause is SD-card degradation (0x0500C011): the
    printer stops mid-print but reports FINISH/IDLE with a low percent —
    no error code is raised.  This is a pure translate-layer derivation
    (stateless); the APK renders it as a warning card.

    Layer-completion guard (device-proven bug fix): the P1S under-reports
    mc_percent after FINISH — a cleanly completed 581/581-layer print was
    observed at ~97% after FINISH.  When both layer_num and total_layer_num
    are present and total > 0 and layer_num >= total_layer_num, the print
    completed all layers and the low percent is a firmware under-count, not
    a silent stop.  Suppress the anomaly in that case.
    """
    gs = raw.get("gcode_state")
    if gs != "FINISH":
        return None
    pct = _as_int(raw.get("mc_percent"))
    if pct is None:
        return None
    if pct >= 99:
        return None
    # Layer-completion guard: if layer_num >= total_layer_num (both present,
    # total > 0) the printer reached its final layer — suppress the false
    # positive that fires when mc_percent is under-reported after FINISH.
    layer = _as_int(raw.get("layer_num"))
    total = _as_int(raw.get("total_layer_num"))
    if layer is not None and total is not None and total > 0 and layer >= total:
        return None
    return {
        "type": "short_finish",
        "text": (
            f"Print ended early — printer reports finished at {pct}%. "
            "This can indicate a silent SD-card failure."
        ),
        "percent": pct,
    }


# --------------------------------------------------------------------------- #
# Headline — APK renders verbatim
# --------------------------------------------------------------------------- #


def _headline(
    phase: Phase,
    phase_reason: str | None,
    raw: dict[str, Any],
    connected: bool,
    last_telemetry_at: str | None,
) -> dict[str, Any]:
    if not connected:
        return {
            "title": "Reconnecting…",
            "subtitle": _reconnect_subtitle(last_telemetry_at),
            "indicator": "indeterminate",
        }
    subtask = raw.get("subtask_name") or ""
    if phase == "idle":
        return {"title": "Ready", "subtitle": "Ready to print", "indicator": "none"}
    if phase == "preparing":
        return {
            "title": "Preparing",
            "subtitle": _preparing_subtitle(phase_reason, raw),
            "indicator": "indeterminate",
        }
    if phase == "printing":
        layer = _as_int(raw.get("layer_num")) or 0
        total = _as_int(raw.get("total_layer_num")) or 0
        pct = _as_int(raw.get("mc_percent")) or 0
        rem = _as_int(raw.get("mc_remaining_time"))
        rem_part = f" · ~{rem} min left" if rem is not None else ""
        return {
            "title": "Printing",
            "subtitle": f"Layer {layer}/{total} · {pct}%{rem_part}",
            "indicator": "progress",
        }
    if phase == "paused":
        return {"title": "Paused", "subtitle": "Tap Resume to continue", "indicator": "amber"}
    if phase == "completed":
        return {
            "title": "Done",
            "subtitle": f"{subtask} · completed" if subtask else "Print completed",
            "indicator": "green",
        }
    if phase == "failed":
        entry = hms_lookup(raw.get("mc_print_error_code") or raw.get("print_error"))
        return {
            "title": "Print failed",
            "subtitle": entry["user_message"],
            "indicator": "red",
        }
    # unknown
    return {
        "title": "Connecting…",
        "subtitle": "Loading status from your P1S",
        "indicator": "indeterminate",
    }


def _preparing_subtitle(phase_reason: str | None, raw: dict[str, Any]) -> str:
    nozzle_c = _as_int(raw.get("nozzle_temper"))
    nozzle_target = _as_int(raw.get("nozzle_target_temper"))
    bed_c = _as_int(raw.get("bed_temper"))
    bed_target = _as_int(raw.get("bed_target_temper"))
    if phase_reason == "heating_nozzle" and nozzle_c is not None and nozzle_target:
        return f"Heating nozzle {nozzle_c}/{nozzle_target} °C"
    if phase_reason == "heating_bed" and bed_c is not None and bed_target:
        return f"Heating bed {bed_c}/{bed_target} °C"
    if phase_reason == "leveling":
        return "Leveling bed"
    if phase_reason == "purging":
        return "Purging filament"
    return "Heating & leveling"


def _reconnect_subtitle(last_telemetry_at: str | None) -> str:
    if last_telemetry_at is None:
        return "No telemetry yet"
    return f"Last update at {last_telemetry_at}"


# --------------------------------------------------------------------------- #
# Block translators
# --------------------------------------------------------------------------- #


def _job_block(raw: dict[str, Any], phase: Phase, ctx: SnapshotContext) -> dict[str, Any]:
    layer = _as_int(raw.get("layer_num"))
    total = _as_int(raw.get("total_layer_num"))
    # §6.0.1: percent is NULL outside `printing`. Exposing mc_percent while
    # preparing is the lie that masked the §6.3 air-print for 17 minutes.
    percent = _as_int(raw.get("mc_percent")) if phase == "printing" else None
    # started_at: raw wins if a future firmware ever supplies gcode_start_time
    # (forward-compat); otherwise fall back to the bridge-synthesized value the
    # service stamped on the RUNNING transition / recovered from jobs.db. On
    # current P1S firmware the raw path is always None, so ctx is the source.
    started_at = _started_at_iso(raw) or ctx.print_started_at
    return {
        "subtask_name": raw.get("subtask_name"),
        "layer_num": layer,
        "total_layer_num": total,
        "percent": percent,
        "estimate_total_min": _as_int(raw.get("mc_estimated_time")),
        "remaining_min": _as_int(raw.get("mc_remaining_time")),
        "started_at": started_at,
    }


def _temps(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "nozzle": {
            "current_c": _as_float(raw.get("nozzle_temper")),
            "target_c": _as_float(raw.get("nozzle_target_temper")),
        },
        "bed": {
            "current_c": _as_float(raw.get("bed_temper")),
            "target_c": _as_float(raw.get("bed_target_temper")),
        },
        "chamber": {
            "current_c": _as_float(raw.get("chamber_temper")),
            "target_c": _as_float(raw.get("chamber_target_temper")),
        },
    }


def _cooling(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "part_fan": _fan(raw.get("cooling_fan_speed")),
        "aux_fan": _fan(raw.get("big_fan1_speed")),
        "chamber_fan": _fan(raw.get("big_fan2_speed")),
    }


def _fan(raw_speed: Any) -> dict[str, Any]:
    """P1S fan native: 0-15 string. Percent = round(raw * 100 / 15)."""
    if raw_speed in (None, ""):
        return {"percent": None, "_raw": None}
    try:
        n = int(str(raw_speed))
    except (ValueError, TypeError):
        return {"percent": None, "_raw": str(raw_speed)}
    pct = round(n * 100 / 15)
    return {"percent": max(0, min(100, pct)), "_raw": str(raw_speed)}


def _lights(raw: dict[str, Any]) -> dict[str, Any]:
    """Chamber light. P1S sends `lights_report:[{node:"chamber_light", mode:"on"|"off"}].

    Tolerant: missing array → None, unknown mode → None.
    """
    lights = raw.get("lights_report")
    if not isinstance(lights, list):
        return {"chamber_on": None}
    for entry in lights:
        if isinstance(entry, dict) and entry.get("node") == "chamber_light":
            mode = entry.get("mode")
            if mode == "on":
                return {"chamber_on": True}
            if mode == "off":
                return {"chamber_on": False}
            return {"chamber_on": None}
    return {"chamber_on": None}


def _print_params(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "speed_mm_s": _as_int(raw.get("mc_print_speed") or raw.get("spd_mag")),
        "flow_pct": _as_int(raw.get("flow_rate") or raw.get("m1")),
    }


def _motion(raw: dict[str, Any]) -> dict[str, Any]:
    # The P1S doesn't ship live x/y/z in push_status; surfacing these is a
    # v0.1 follow-up. Return the block with nulls so the shape is stable.
    return {
        "x": _as_float(raw.get("x")),
        "y": _as_float(raw.get("y")),
        "z": _as_float(raw.get("z")),
        "e": _as_float(raw.get("e")),
    }


# --------------------------------------------------------------------------- #
# AMS — dual-emission per §6.1
# --------------------------------------------------------------------------- #


def _ams(raw: dict[str, Any], ctx: SnapshotContext) -> dict[str, Any]:
    ams = raw.get("ams")
    if not isinstance(ams, dict):
        return {
            "present": False,
            "engaged_slot": None,
            "slots": [],
            "external_spool": _external_default(),
        }

    engaged = engaged_slot(ams.get("tray_now"))
    slots = _ams_slots(ams, ctx.filament_memory)
    external = _external_from_ams(ams)
    return {
        "present": _ams_present(ams),
        "engaged_slot": engaged,
        "slots": slots,
        "external_spool": external,
    }


def _ams_present(ams: dict[str, Any]) -> bool:
    """Is AMS hardware physically attached? (§6.1, the re-scan boundary).

    Derived from ``ams_exist_bits`` — a hex-ish bitmask string the P1S ships
    where each bit is one attached AMS unit (``"1"`` = unit 0 present). It is
    independent of ``ams.ams[]``, which transiently empties to ``[]`` during an
    RFID re-scan (physically touching the AMS) while the hardware stays
    attached. So ``present`` stays ``True`` across a scan even though ``slots``
    momentarily becomes ``[]`` — that is the signal clients use to hold the
    previous slot view instead of latching "No AMS detected".

    Absent key, empty, or all-zero → ``False`` (no AMS hardware).
    """
    bits = ams.get("ams_exist_bits")
    if bits is None:
        return False
    s = str(bits).strip().lstrip("#")
    if s == "":
        return False
    try:
        return int(s, 16) != 0
    except ValueError:
        # Not parseable as hex — treat any non-empty, non-zero-looking value as
        # present rather than dropping a real AMS over an unexpected format.
        return s.strip("0") != ""


def engaged_slot(tray_now: Any) -> int | str | None:
    """255 → None, 254 → "external", 0-3 → 1-4, 16+ → multi-AMS untranslated.

    Public: the snapshot's `ams.engaged_slot` and the §12.2 `filament_runout`
    event's `slot` field both come through here so they can't disagree.
    """
    if tray_now is None or tray_now == "":
        return None
    s = str(tray_now).strip()
    if not s.isdigit():
        return None
    n = int(s)
    if n == 255:
        return None
    if n == 254:
        return "external"
    if 0 <= n <= 3:
        return n + 1
    # Multi-AMS or unknown — surface raw int so APK can render "Slot ?"
    return n


def _ams_slots(
    ams: dict[str, Any],
    filament_memory: dict[int, Any] | None,
) -> list[dict[str, Any]]:
    """Translate ams.tray[] (or first AMS unit's tray[]).

    P1S without a second AMS sends `ams.ams[0].tray[]`. With multi-AMS,
    each unit has its own tray[]. v0 surfaces the first unit only;
    multi-AMS is a v0.1 follow-up.
    """
    units = ams.get("ams")
    if isinstance(units, list) and units:
        first = units[0]
        if isinstance(first, dict):
            trays = first.get("tray")
            if isinstance(trays, list):
                return [
                    _ams_slot(t, i, filament_memory)
                    for i, t in enumerate(trays)
                    if isinstance(t, dict)
                ]
    # Some firmwares put tray[] directly on `ams`:
    trays = ams.get("tray")
    if isinstance(trays, list):
        return [
            _ams_slot(t, i, filament_memory)
            for i, t in enumerate(trays)
            if isinstance(t, dict)
        ]
    return []


def _ams_slot(
    tray: dict[str, Any],
    array_idx: int,
    filament_memory: dict[int, Any] | None,
) -> dict[str, Any]:
    """One AMS tray → contract §6.1 slot.

    `physical_slot = array_idx + 1` — the array position is definitionally
    0-based for tray[], unrelated to the parked `ams_mapping` 1-vs-0
    question (which lives on the SUBMISSION side, not the snapshot side).
    """
    raw_id = _as_int(tray.get("id"))
    has_filament = bool(tray.get("tray_type"))
    physical_slot = array_idx + 1
    # G3 — merge filament memory label when available.
    mem: dict[str, Any] | None = None
    if filament_memory is not None:
        entry = filament_memory.get(physical_slot)
        if entry is not None:
            mem = {
                "make": entry.make if hasattr(entry, "make") else entry.get("make"),
                "model": entry.model if hasattr(entry, "model") else entry.get("model"),
                "profile": entry.profile if hasattr(entry, "profile") else entry.get("profile"),
            }
    return {
        "physical_slot": physical_slot,
        "type": tray.get("tray_type") or None,
        "color": _normalize_color(tray.get("tray_color")),
        "rfid_tray": _rfid_tray(tray),
        "state": "loaded" if has_filament else "empty",
        "remaining_g": _as_int(tray.get("remain")) if has_filament else None,
        "remaining_pct": _remaining_pct(tray, has_filament),
        "_raw_id": raw_id if raw_id is not None else array_idx,
        "memory": mem,
    }


def _rfid_tray(tray: dict[str, Any]) -> str | None:
    """RFID identifier for a slot, or ``None`` for a tagless spool.

    Tagless spools (no Bambu RFID tag — very common with third-party filament)
    report an all-zero ``tray_uuid`` and an empty ``tray_id_name``. Treat those
    sentinels as "no tag" so the contract's ``rfid_tray: null`` holds and the
    client never renders a tag-derived name. ``tray_sub_brands`` (a real brand
    string when present) wins over the UUID.
    """
    sub = tray.get("tray_sub_brands")
    if isinstance(sub, str) and sub.strip():
        return sub
    uuid = tray.get("tray_uuid")
    if isinstance(uuid, str):
        stripped = uuid.strip()
        if stripped and stripped.strip("0") != "":
            return uuid
    return None


def _remaining_pct(tray: dict[str, Any], has_filament: bool) -> int | None:
    if not has_filament:
        return None
    # P1S sometimes ships `remain` as grams (0-1000ish), sometimes as %.
    # If <= 100, treat as a percent; otherwise derive from a ~1000g spool.
    remain = _as_int(tray.get("remain"))
    if remain is None:
        return None
    if remain <= 100:
        return max(0, min(100, remain))
    return max(0, min(100, round(remain / 10)))


def _normalize_color(raw_color: Any) -> str | None:
    """P1S sends `RRGGBBAA` or `RRGGBB` (no `#`). Contract expects `#RRGGBB`."""
    if not raw_color:
        return None
    s = str(raw_color).strip().lstrip("#")
    if len(s) < 6 or not all(c in "0123456789abcdefABCDEF" for c in s):
        return None
    return "#" + s[:6].upper()


def _external_default() -> dict[str, Any]:
    return {"in_use": False, "type": None, "color": None, "_raw_id": 254}


def _external_from_ams(ams: dict[str, Any]) -> dict[str, Any]:
    """vt_tray is the external spool entry (raw_id 254)."""
    vt = ams.get("vt_tray")
    if not isinstance(vt, dict):
        return _external_default()
    tray_now = str(ams.get("tray_now", "")).strip()
    return {
        "in_use": tray_now == "254",
        "type": vt.get("tray_type") or None,
        "color": _normalize_color(vt.get("tray_color")),
        "_raw_id": 254,
    }


# --------------------------------------------------------------------------- #
# print_error decoding
# --------------------------------------------------------------------------- #


def build_print_error(code: Any) -> dict[str, Any] | None:
    """Structured §6 ``print_error`` object from a raw P1S error code.

    Single source of the ``{code, hex, text, category, severity,
    remediation, _raw}`` shape — consumed both by the snapshot translator
    (``print_error`` block) and by the named ``error`` / ``print_failed``
    WS events (contract §12.2). Sharing one builder is what keeps the
    snapshot's error object and the event's error object from drifting.

    Returns ``None`` for a benign code (``0`` / ``"0"`` / ``None`` / ``""``).
    """
    if code in (None, 0, "0", ""):
        return None
    entry = hms_lookup(code)
    # The hex form is what hms.lookup normalized; surface it back so the
    # APK can render "Code: 0300_0d00_0003_0001" if it wants.
    if isinstance(code, str):
        hex_form = code
    elif isinstance(code, int):
        hex_form = f"{code & 0xFFFFFFFF:08x}"
    else:
        hex_form = str(code)
    return {
        "code": str(code),
        "hex": hex_form,
        "text": entry["user_message"],
        "category": entry["category"],
        "severity": entry["severity"],
        "remediation": entry["remediation"],
        "wiki_url": hms_wiki_url(hex_form),
        "_raw": code,
    }


def _print_error(raw: dict[str, Any]) -> dict[str, Any] | None:
    return build_print_error(raw.get("mc_print_error_code") or raw.get("print_error"))


# --------------------------------------------------------------------------- #
# stage block — stg_cur sub-stage decoding
# --------------------------------------------------------------------------- #


def _stage(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate ``stg_cur`` → ``{"id": <int|None>, "text": <str|None>}``.

    The P1S ships ``stg_cur`` as a number-or-string (coerced via ``_as_int``).
    255 / -1 are idle sentinels: id is preserved but text is ``None`` so
    clients can distinguish "idle/no sub-stage" from an unknown id.
    """
    raw_val = raw.get("stg_cur")
    stage_id = _as_int(raw_val)
    if stage_id is None:
        return {"id": None, "text": None}
    return {"id": stage_id, "text": stage_text(stage_id)}


# --------------------------------------------------------------------------- #
# hms block — active HMS health-management entries
# --------------------------------------------------------------------------- #


def _hms_list(raw: dict[str, Any], job_context: JobContext | None = None) -> list[dict[str, Any]]:
    """Decode the MQTT ``hms[]`` array into a list of structured entries.

    Each entry in the printer's ``hms`` payload is ``{"attr": int, "code": int}``.
    The P1S can ship these as ints or as strings — both are coerced.

    Returns an empty list when the printer reports no active HMS entries.
    Each decoded entry mirrors the shape of ``build_print_error`` plus
    ``wiki_url`` and the phase-aware fields: ``{code, hex, text, category,
    severity, remediation, wiki_url, context_note, stale, _raw}``.

    ``job_context`` is passed through to ``decode_hms_entry`` to populate
    ``context_note`` and ``stale``.  When None (the default) those fields
    default to null/false for backward compatibility.
    """
    hms_raw = raw.get("hms")
    if not isinstance(hms_raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in hms_raw:
        if not isinstance(item, dict):
            continue
        attr = _as_int(item.get("attr"))
        code = _as_int(item.get("code"))
        if attr is None or code is None:
            continue
        decoded = decode_hms_entry(attr, code, job_context)
        result.append({
            "code": f"{attr}:{code}",
            "hex": decoded["hex"],
            "text": decoded["user_message"],
            "category": decoded["category"],
            "severity": decoded["severity"],
            "remediation": decoded["remediation"],
            "wiki_url": decoded["wiki_url"],
            "context_note": decoded["context_note"],
            "stale": decoded["stale"],
            "_raw": item,
        })
    return result


# --------------------------------------------------------------------------- #
# _raw passthrough
# --------------------------------------------------------------------------- #


def _raw_passthrough(raw: dict[str, Any]) -> dict[str, Any]:
    """Full passthrough — nothing the P1S says is dropped.

    The translated blocks above are the curated APK surface; `_raw` is
    the safety net: any field the bridge does not yet model (a future
    firmware addition, an obscure category like `system` or `liveview`,
    an undocumented count) lives here for the APK's dev-mode inspector
    and the operator's debug log.

    Non-finite floats are scrubbed to None: json.dumps renders them as
    Infinity/NaN, which JSON.parse rejects — one such value anywhere in
    the passthrough would drop the whole WS snapshot client-side.
    """
    return {k: _scrub_nonfinite(v) for k, v in raw.items()}


def _scrub_nonfinite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _scrub_nonfinite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_nonfinite(v) for v in value]
    return value


# --------------------------------------------------------------------------- #
# Type coercion helpers — P1S ships numbers as strings, ints as floats, …
# --------------------------------------------------------------------------- #


def _started_at_iso(raw: dict[str, Any]) -> str | None:
    """job.started_at: ISO-8601 UTC from the printer's gcode_start_time.

    The P1S reports the print start as epoch seconds in ``gcode_start_time``
    (digit string; "0"/absent when idle). ``start_time`` is accepted as a
    fallback spelling. Already-ISO strings pass through untouched.
    """
    v = raw.get("gcode_start_time")
    if v in (None, "", 0, "0"):
        v = raw.get("start_time")
    if v in (None, "", 0, "0"):
        return None
    if isinstance(v, int | float) or (isinstance(v, str) and v.isdigit()):
        return (
            datetime.fromtimestamp(float(v), tz=UTC)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    if isinstance(v, str) and "T" in v:
        return v  # already ISO from a future firmware/bridge
    return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None  # bool is int subclass — exclude
    try:
        return int(float(value))
    except (ValueError, TypeError, OverflowError):
        # OverflowError: int(float("inf")) / int(float("-inf")) — not caught by
        # ValueError or TypeError; treat infinity as unparseable (return None).
        return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    # json.dumps renders inf/nan as the non-standard tokens Infinity/NaN,
    # which JSON.parse rejects — one such value drops the whole WS snapshot.
    return result if math.isfinite(result) else None
