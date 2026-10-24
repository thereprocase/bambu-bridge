"""Fresh, coherent material reports for replay review, independent of UI slot numbering."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import time
from typing import Any

import structlog

log = structlog.get_logger(__name__)
FRAME_KEYS = ("ams", "tray_exist_bits", "ams_exist_bits")
READ_HOLD_S = 120.0  # longest an RFID read is waited out (42 s seen on a P1S)


class MaterialInventory:
    """A temperature heartbeat must never make an old spool inventory look fresh.

    Full AMS frames replace each other. A partial AMS report is applied to the
    last full frame (units and trays matched by id keep the fields it does not
    carry) and does not refresh its time, so a fresh frame still means a full
    report in the last 30 s.

    Unloads are recorded as the printer reports them: a slot seen loaded in a
    valid frame that is then empty, or whose tray_exist_bits bit drops in any
    report. Spool assignments end on these (slot_display); a quiet period
    or a reconnect does not end them.
    """

    def __init__(self) -> None:
        self.ams: dict[str, Any] | None = None
        self.external: dict[str, Any] | None = None
        self.ams_at: float | None = None
        self.external_at: float | None = None
        self.revision = 0
        self._loaded: set[int] = set()
        self._tray_bits: int | None = None
        # wire -> time the unload was reported
        self._unloaded: dict[int, float] = {}
        # wire -> when the AMS began (re)reading its RFID
        self._reads: dict[int, float] = {}

    def clear(self) -> None:
        self.ams = self.external = None
        self.ams_at = self.external_at = None
        # Pending unloads survive a disconnect; nothing is known about the gap.
        self._loaded.clear()
        self._tray_bits = None
        self.revision += 1

    def mark_unloaded(self, unloads: dict[int, float]) -> None:
        """Put unloads back after a consumer could not act on them."""
        for wire, at in unloads.items():
            self._unloaded[wire] = max(at, self._unloaded.get(wire, at))

    def take_unloaded(self) -> dict[int, float]:
        """Wire slots reported unloaded since the last call, with the report time."""
        unloaded, self._unloaded = self._unloaded, {}
        return unloaded

    def _record_unloads(self, wires: set[int]) -> None:
        now = time.time()
        for wire in wires:
            self._unloaded[wire] = now

    def observe(self, report: dict[str, Any]) -> None:
        ams = report.get("ams")
        if report.get("msg") == 0 and not (
            isinstance(ams, dict) and all(key in ams for key in FRAME_KEYS)
        ):
            # A full report (pushall reply) that cannot fill the AMS frame:
            # after a restart nothing else does until the AMS changes.
            log.info(
                "material.full_report_without_ams",
                ams_keys=sorted(ams) if isinstance(ams, dict) else None,
                vt_tray="vt_tray" in report,
            )
        # Any AMS block counts: one carrying only RFID read bits or humidity is
        # applied to the stored frame too (a merge never refreshes its time).
        if isinstance(ams, dict) and ams:
            if isinstance(ams.get("tray_exist_bits"), str):
                with contextlib.suppress(ValueError):
                    bits = _bits(ams["tray_exist_bits"])
                    if self._tray_bits is not None:
                        dropped = {
                            w
                            for w in self._loaded
                            if w != 254 and (self._tray_bits & ~bits) >> w & 1
                        }
                        self._record_unloads(dropped)
                        self._loaded -= dropped
                    self._tray_bits = bits
            if all(key in ams for key in FRAME_KEYS):
                incoming = copy.deepcopy(ams)
                self.ams = _hold_reads(incoming, self.ams, self._track_reads(incoming))
                self.ams_at = time.time()
                self._note_presence(external=False)
            elif self.ams is not None:
                merged = _apply_delta(self.ams, copy.deepcopy(ams))
                self.ams = _hold_reads(merged, self.ams, self._track_reads(merged))
                self._note_presence(external=False)
            self.revision += 1
        if "vt_tray" in report:
            self.external = None
            self.external_at = None
            tray = report["vt_tray"]
            if isinstance(tray, dict) and "id" in tray and "tray_type" in tray:
                self.external = copy.deepcopy(tray)
                self.external_at = time.time()
                self._note_presence(external=True)
            self.revision += 1

    def _track_reads(self, frame: dict[str, Any]) -> int:
        """Wires whose RFID the AMS is reading now, as bits.

        While a tag is read the AMS reports the tray blank for up to a minute
        (about 42 s seen on a P1S) though the spool never moved, so a blank
        tray then is not another spool and must not end an assignment. A read
        starts when a wire's reading bit is set or its read-done bit drops,
        and ends when the reading bit drops, read-done returns, the tray is
        removed, or after READ_HOLD_S. Presence still comes from
        tray_exist_bits, so a real unload still counts.
        """
        now = time.time()
        old = self.ams or {}
        present = _optional_bits(frame.get("tray_exist_bits")) or 0
        reading, was_reading = (
            _optional_bits(frame.get("tray_reading_bits")),
            _optional_bits(old.get("tray_reading_bits")),
        )
        done, was_done = (
            _optional_bits(frame.get("tray_read_done_bits")),
            _optional_bits(old.get("tray_read_done_bits")),
        )
        started = (reading or 0) | ((was_done or 0) & ~(done if done is not None else ~0))
        ended = ((was_reading or 0) & ~(reading if reading is not None else ~0)) | (
            (done or 0) & ~(reading or 0)
        )
        for wire in range(16):
            bit = 1 << wire
            if not present & bit or (ended & bit and not (reading or 0) & bit):
                self._reads.pop(wire, None)
            elif started & bit:
                self._reads.setdefault(wire, now)
        for wire, since in list(self._reads.items()):
            if now - since > READ_HOLD_S:
                del self._reads[wire]
        return sum(1 << wire for wire in self._reads)

    def _note_presence(self, *, external: bool) -> None:
        view = inventory_view(self.snapshot())
        if not external and (
            view["issues"] or not any(slot["wire_id"] != 254 for slot in view["slots"])
        ):
            return  # malformed AMS frame: say nothing rather than "everything unloaded"
        present = {
            slot["wire_id"]
            for slot in view["slots"]
            if (slot["wire_id"] == 254) == external and slot["present"]
        }
        scope = {w for w in self._loaded if (w == 254) == external}
        self._record_unloads(scope - present)
        self._loaded = (self._loaded - scope) | present

    def snapshot(self) -> dict[str, Any]:
        return {
            "ams": copy.deepcopy(self.ams),
            "external": copy.deepcopy(self.external),
            "ams_at": self.ams_at,
            "external_at": self.external_at,
        }


_IDENTITY_KEYS = ("tray_type", "tray_color", "tray_info_idx", "tray_uuid", "tag_uid")


def _by_id(items: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list):
        return {}
    return {str(item.get("id")): item for item in items if isinstance(item, dict)}


def _apply_delta(frame: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """A full AMS frame with a partial report applied, as Orca's parser keeps it.

    Top-level fields (tray_exist_bits, tray_reading_bits, tray_read_done_bits,
    ...) replace those they name. Units are matched by id and keep the fields
    the report does not carry. A tray the report carries replaces the stored
    one whole, since the AMS sends each tray complete and two spools' fields
    must never blend; trays it does not mention stay. A tray whose presence
    bit drops forgets its fields, so the next spool there never inherits them.
    """
    removed = 0
    with contextlib.suppress(ValueError):
        if "tray_exist_bits" in delta:
            removed = _bits(frame.get("tray_exist_bits")) & ~_bits(delta["tray_exist_bits"])
    merged = {**frame, **{k: v for k, v in delta.items() if k != "ams"}}
    units = _by_id(frame.get("ams"))
    for uid, unit in units.items():
        if removed and uid.isdigit() and isinstance(unit.get("tray"), list):
            units[uid] = {
                **unit,
                "tray": [
                    {"id": tray.get("id")}
                    if isinstance(tray, dict)
                    and str(tray.get("id")).isdigit()
                    and removed >> (int(uid) * 4 + int(tray["id"])) & 1
                    else tray
                    for tray in unit["tray"]
                ],
            }
    for uid, unit in _by_id(delta.get("ams")).items():
        old = units.get(uid, {})
        new = {**old, **{k: v for k, v in unit.items() if k != "tray"}}
        if isinstance(unit.get("tray"), list):
            trays = _by_id(old.get("tray"))
            trays.update(_by_id(unit["tray"]))
            new["tray"] = list(trays.values())
        units[uid] = new
    merged["ams"] = list(units.values())
    return merged


def _bits(value: Any) -> int:
    if not isinstance(value, str) or not 1 <= len(value) <= 8:
        raise ValueError("Presence bits are missing")
    return int(value, 16)


def _optional_bits(value: Any) -> int | None:
    with contextlib.suppress(ValueError):
        return _bits(value)
    return None


def _hold_reads(frame: dict[str, Any], before: dict[str, Any] | None, held: int) -> dict[str, Any]:
    """Keep each tray in `held` (wire bits) as `before` last had it."""
    if not held or before is None:
        return frame
    stored = {uid: _by_id(unit.get("tray")) for uid, unit in _by_id(before.get("ams")).items()}
    for unit in frame.get("ams") or []:
        if not isinstance(unit, dict) or not str(unit.get("id")).isdigit():
            continue
        uid = str(unit["id"])
        if not isinstance(unit.get("tray"), list):
            continue
        unit["tray"] = [
            stored.get(uid, {}).get(str(tray.get("id")), tray)
            if isinstance(tray, dict)
            and str(tray.get("id")).isdigit()
            and held >> (int(uid) * 4 + int(tray["id"])) & 1
            else tray
            for tray in unit["tray"]
        ]
    return frame


def _slot(tray: dict[str, Any], wire: int, label: str, present: bool) -> dict[str, Any]:
    identity = {k: tray.get(k) for k in _IDENTITY_KEYS}
    return {
        "wire_id": wire,
        "label": label,
        "present": present and not bool(tray.get("tray_slot_placeholder")),
        "material": str(tray.get("tray_type") or "")[:64],
        "color": str(tray.get("tray_color") or "")[:16],
        "profile_id": str(tray.get("tray_info_idx") or "")[:128],
        # Keep identity change detection without publishing RFID/raw identifiers.
        "identity": hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
    }


def inventory_view(
    frame: dict[str, Any], *, now: float | None = None, stale_ok: bool = False
) -> dict[str, Any]:
    """The slots of a material frame. Only a fresh frame lists slots unless
    `stale_ok`, which lists the last known ones (for display, never to act on).
    """
    now = time.time() if now is None else now
    slots: list[dict[str, Any]] = []
    issues: list[str] = []
    ams = frame.get("ams")
    stamp = frame.get("ams_at")
    ams_fresh = stamp is not None and 0 <= now - stamp <= 30
    if not ams_fresh:
        issues.append("A complete AMS inventory from the last 30 seconds is unavailable.")
    if (ams_fresh or stale_ok) and isinstance(ams, dict):
        try:
            units = ams["ams"]
            unit_bits, tray_bits = _bits(ams["ams_exist_bits"]), _bits(ams["tray_exist_bits"])
            if not isinstance(units, list) or len(units) > 4 or unit_bits & ~15:
                raise ValueError("Unsupported AMS layout")
            seen_units: set[int] = set()
            seen_trays: set[int] = set()
            for unit in units:
                if not isinstance(unit, dict) or not str(unit.get("id", "")).isdigit():
                    raise ValueError("Invalid AMS ID")
                aid = int(unit["id"])
                if aid not in range(4) or aid in seen_units:
                    raise ValueError("Unsupported or duplicate AMS ID")
                seen_units.add(aid)
                if not unit_bits & (1 << aid):
                    continue
                trays = unit["tray"]
                if not isinstance(trays, list) or len(trays) != 4:
                    raise ValueError("Incomplete AMS unit")
                for tray in trays:
                    if not isinstance(tray, dict) or not str(tray.get("id", "")).isdigit():
                        raise ValueError("Invalid tray ID")
                    tid = int(tray["id"])
                    wire = aid * 4 + tid
                    if tid not in range(4) or wire in seen_trays:
                        raise ValueError("Duplicate or invalid tray ID")
                    seen_trays.add(wire)
                    slots.append(
                        _slot(
                            tray,
                            wire,
                            f"AMS {chr(65 + aid)} · slot {tid + 1}",
                            bool(tray_bits & (1 << wire)),
                        )
                    )
            if any(unit_bits & (1 << aid) and aid not in seen_units for aid in range(4)):
                raise ValueError("An attached AMS unit is missing")
            if tray_bits & ~sum(1 << wire for wire in seen_trays):
                raise ValueError("An occupied tray is missing")
        except (KeyError, TypeError, ValueError, OverflowError):
            slots = []
            issues.append("AMS IDs or presence data are incomplete or unsupported; refresh status.")
    external = frame.get("external")
    ext_at = frame.get("external_at")
    external_fresh = ext_at is not None and 0 <= now - ext_at <= 30
    if (
        (external_fresh or stale_ok)
        and isinstance(external, dict)
        and str(external.get("id")) == "254"
    ):
        slots.append(_slot(external, 254, "External spool", bool(external.get("tray_type"))))
    slots.sort(key=lambda slot: slot["wire_id"])
    fingerprint = hashlib.sha256(json.dumps(slots, sort_keys=True).encode()).hexdigest()
    return {
        "slots": slots,
        "fingerprint": fingerprint,
        "issues": issues,
        "ams_fresh": ams_fresh,
        "external_fresh": external_fresh,
        "ams_observed_at": stamp,
        "external_observed_at": ext_at,
    }
