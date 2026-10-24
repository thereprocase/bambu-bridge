"""Experimental, downstream-only inventory projection for native Orca clients.

This module has no printer or persistence access. Bindings are explicitly supplied
by the compatibility probe; inventory/API activation follows runtime validation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any


class VirtualSpoolStateUnavailable(ValueError):
    """Close a projected client instead of retaining unverifiable virtual state."""


def _index(value: Any, maximum: int = 3) -> int | None:
    if type(value) is int:
        return value if 0 <= value <= maximum else None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        number = int(value)
        return number if 0 <= number <= maximum else None
    return None


def _bits(value: Any) -> int | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{1,4}", value):
        return None
    return int(value, 16)


def _has_tag(tray: dict[str, Any]) -> bool:
    tag = tray.get("tag_uid")
    return isinstance(tag, str) and any(c != "0" for c in tag)


def physical_identity(tray: dict[str, Any]) -> str:
    """Observation fingerprint, not proof of an untagged spool's identity."""
    values = {key: tray.get(key, "") for key in ("tray_type", "tray_color", "tray_info_idx")}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class VirtualSpool:
    provider: str
    spool_id: int
    wire_slot: int
    material_id: str
    material: str
    color: str
    nominal_g: float
    remaining_g: float | None
    observed_identity: str

    def __post_init__(self) -> None:
        if not self.provider or len(self.provider) > 128:
            raise ValueError("A persistent provider identity is required")
        if type(self.spool_id) is not int or self.spool_id < 1:
            raise ValueError("Spool ID must be a positive integer")
        if type(self.wire_slot) is not int or not 0 <= self.wire_slot < 16:
            raise ValueError("Only P1S AMS slots 0–15 are supported by this probe")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.material_id):
            raise ValueError("A stock material ID is required")
        if not re.fullmatch(r"[A-Za-z0-9 +_-]{1,40}", self.material):
            raise ValueError("A material chemistry is required")
        if not re.fullmatch(r"[0-9a-fA-F]{8}", self.color):
            raise ValueError("Color must be RRGGBBAA")
        for value in (self.nominal_g, self.remaining_g):
            if value is not None and (
                type(value) not in (int, float) or not math.isfinite(value) or value < 0
            ):
                raise ValueError("Mass must be finite and nonnegative")
        if not self.nominal_g:
            raise ValueError("Nominal capacity must be positive")
        if not re.fullmatch(r"[0-9a-f]{64}", self.observed_identity):
            raise ValueError("A confirmed physical declaration fingerprint is required")

    @property
    def remaining_percent(self) -> int:
        if self.remaining_g is None:
            return -1  # Stock Orca may paint unknown as full; never report invented 100%.
        return math.floor(min(1.0, self.remaining_g / self.nominal_g) * 100 + 0.5)

    def tag_uid(self, namespace: str) -> str:
        # Fixed 16 hex characters. Retain the legacy client's ordinary tag pattern
        # at byte 6; this must still be verified against pinned upstream binaries.
        digest = (
            hashlib.sha256(
                json.dumps(["virtual-spool-v1", namespace, self.provider, self.spool_id]).encode()
            )
            .hexdigest()
            .upper()
        )
        return digest[:12] + "01" + digest[14:16]


class NativeSpoolOverlay:
    """Project coherent tray records without changing physical observations.

    The private raw-tray cache merges identified partial records. Presence bits
    discard removed trays before later reinsertion, so stale identity cannot be
    revived across an observed unload. Same-material swaps still need assignment.
    """

    def __init__(self, namespace: str, bindings: tuple[VirtualSpool, ...] = ()) -> None:
        if not namespace:
            raise ValueError("A persistent bridge namespace is required")
        self.namespace = namespace
        self.enabled = False
        self.bindings: dict[int, VirtualSpool] = {}
        self._trays: dict[int, dict[str, Any]] = {}
        self._invalidated: set[int] = set()
        self._projected_slots: set[int] = set()
        self.replace_bindings(bindings)

    def replace_bindings(
        self, bindings: tuple[VirtualSpool, ...], *, confirm: bool = False
    ) -> None:
        slots = [b.wire_slot for b in bindings]
        identities = [(b.provider, b.spool_id) for b in bindings]
        if len(set(slots)) != len(slots) or len(set(identities)) != len(identities):
            raise ValueError("A slot and a physical spool may each be assigned only once")
        tags = [b.tag_uid(self.namespace) for b in bindings]
        if len(set(tags)) != len(tags):
            raise ValueError("Synthetic tag collision")
        new = {b.wire_slot: b for b in bindings}
        for slot, binding in new.items():
            previous = self.bindings.get(slot)
            if (
                confirm
                or previous is None
                or (previous.provider, previous.spool_id, previous.observed_identity)
                != (binding.provider, binding.spool_id, binding.observed_identity)
            ):
                self._invalidated.discard(slot)
        self.bindings = new

    def observe_physical(self, snapshot: dict[str, Any]) -> None:
        """Called at raw-report receipt even when no native client is subscribed."""
        if self._physical_ams(snapshot) is None:
            self._invalidated.update(self.bindings)

    def invalidate(self) -> None:
        """A physical disconnect makes prior loading confirmations untrustworthy."""
        self._invalidated.update(self.bindings)
        self._trays.clear()

    def _physical_ams(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        section = snapshot.get("print")
        ams = section.get("ams") if isinstance(section, dict) else None
        if not isinstance(ams, dict) or not isinstance(ams.get("ams"), list):
            self._trays.clear()
            return None
        unit_bits = _bits(ams.get("ams_exist_bits"))
        tray_bits = _bits(ams.get("tray_exist_bits"))
        if unit_bits is None or unit_bits > 15 or tray_bits is None:
            self._trays.clear()
            return None
        for wire in self.bindings:
            if not (unit_bits & (1 << (wire // 4)) and tray_bits & (1 << wire)):
                self._invalidated.add(wire)
        for wire in list(self._trays):
            if not (unit_bits & (1 << (wire // 4)) and tray_bits & (1 << wire)):
                del self._trays[wire]
        units: dict[int, dict[str, Any]] = {}
        seen: set[int] = set()
        for unit in ams["ams"]:
            if not isinstance(unit, dict) or (aid := _index(unit.get("id"))) is None:
                self._trays.clear()
                return None
            if aid in units or not isinstance(unit.get("tray"), list):
                self._trays.clear()
                return None
            units[aid] = copy.deepcopy(unit)
            for tray in unit["tray"]:
                if not isinstance(tray, dict) or (tid := _index(tray.get("id"))) is None:
                    self._trays.clear()
                    return None
                wire = aid * 4 + tid
                if wire in seen:
                    self._trays.clear()
                    return None
                seen.add(wire)
                if unit_bits & (1 << aid) and tray_bits & (1 << wire):
                    self._trays[wire] = {**self._trays.get(wire, {}), **copy.deepcopy(tray)}
                    binding = self.bindings.get(wire)
                    current = self._trays[wire]
                    if binding and (
                        _has_tag(current)
                        or (
                            all(k in current for k in ("tray_type", "tray_color", "tray_info_idx"))
                            and physical_identity(current) != binding.observed_identity
                        )
                    ):
                        self._invalidated.add(wire)
        result = copy.deepcopy(ams)
        # Pinned legacy protocol's default is false. Emit it explicitly when
        # restoring a flag we previously supplied only for client presentation.
        result.setdefault("calibrate_remain_flag", False)
        result["ams"] = []
        for aid in range(4):
            if not unit_bits & (1 << aid):
                continue
            unit = units.get(aid, {"id": str(aid)})
            unit["tray"] = []
            for tid in range(4):
                wire = aid * 4 + tid
                # Explicit empty/default fields clear previously projected client
                # values. Never invent physical presence or RFID read-done bits.
                tray = {
                    "id": str(tid),
                    "tag_uid": "0",
                    "tray_uuid": "0" * 32,
                    "tray_info_idx": "",
                    "tray_type": "",
                    "tray_color": "FFFFFFFF",
                    "remain": -1,
                }
                tray.update(copy.deepcopy(self._trays.get(wire, {})))
                unit["tray"].append(tray)
            result["ams"].append(unit)
        return result

    def _eligible(self, wire: int, tray: dict[str, Any]) -> VirtualSpool | None:
        binding = self.bindings.get(wire) if self.enabled else None
        if (
            binding is None
            or wire in self._invalidated
            or _has_tag(tray)
            or tray.get("tray_type") != binding.material
            or physical_identity(tray) != binding.observed_identity
        ):
            return None
        return binding

    def project(
        self,
        payload: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        connected: bool = True,
        projected_slots: set[int] | None = None,
    ) -> dict[str, Any]:
        result = copy.deepcopy(payload)
        section = result.get("print")
        if not isinstance(section, dict) or section.get("command", "push_status") != "push_status":
            return result
        protected = self._projected_slots if projected_slots is None else projected_slots
        if not connected:
            self._invalidated.update(self.bindings)
            if protected:
                raise VirtualSpoolStateUnavailable("BBVIRTUAL_PHYSICAL_DISCONNECTED")
            return result
        physical = self._physical_ams(snapshot)
        if physical is None:
            self._invalidated.update(self.bindings)
            if protected:
                raise VirtualSpoolStateUnavailable("BBVIRTUAL_STATUS_RESYNC_REQUIRED")
            return result
        projected = False
        for unit in physical["ams"]:
            for tray in unit["tray"]:
                wire = int(unit["id"]) * 4 + int(tray["id"])
                if (binding := self._eligible(wire, tray)) is not None:
                    protected.add(wire)
                    tray.update(
                        tag_uid=binding.tag_uid(self.namespace),
                        tray_uuid=hashlib.sha256(
                            binding.tag_uid(self.namespace).encode()
                        ).hexdigest()[:32],
                        tray_info_idx=binding.material_id,
                        tray_type=binding.material,
                        tray_color=binding.color.upper(),
                        remain=binding.remaining_percent,
                    )
                    projected = True
        # Only client-facing presentation flag; never send ams_user_setting.
        if projected:
            physical["calibrate_remain_flag"] = True
        section["ams"] = physical
        return result

    def command_response(
        self,
        payload: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        protected_slots: set[int] | None = None,
    ) -> dict[str, Any] | None:
        """Reject virtual material edits locally, leaving real print controls alone."""
        protected = self._projected_slots if protected_slots is None else protected_slots
        physical = self._physical_ams(snapshot)
        active = protected | {
            int(unit["id"]) * 4 + int(tray["id"])
            for unit in (physical or {}).get("ams", [])
            for tray in unit["tray"]
            if self._eligible(int(unit["id"]) * 4 + int(tray["id"]), tray)
        }
        section = payload.get("print")
        if not active or not isinstance(section, dict):
            return None
        command = section.get("command")
        blocked = command in ("ams_user_setting", "ams_get_rfid", "ams_calibrate")
        if command == "ams_get_rfid":
            aid = _index(section.get("ams_id"))
            tid = _index(section.get("slot_id", section.get("tray_id")))
            if aid is not None and tid is not None:
                blocked = aid * 4 + tid in active
        if command == "ams_calibrate":
            aid = _index(section.get("ams_id"))
            if aid is not None:
                blocked = any(slot // 4 == aid for slot in active)
        if command == "ams_filament_setting":
            aid, tid = _index(section.get("ams_id")), _index(section.get("slot_id"))
            blocked = aid is None or tid is None or aid * 4 + tid in active
        if command in ("extrusion_cali_set", "extrusion_cali_sel"):
            # Stock Orca sends these from the virtual spool's material dialog,
            # including k_value 0 for "clear filament information". They would
            # change the physical slot's calibration, so they stay local while
            # the overlay owns that slot; with it off the slot is edited normally.
            aid, sid = _index(section.get("ams_id")), _index(section.get("slot_id"))
            wire = aid * 4 + sid if aid is not None and sid is not None else None
            if wire is None:
                tray = _index(section.get("tray_id"), 254)
                # Global tray IDs: 0-15 across four AMS units, 254 external.
                wire = tray if tray is not None and (tray <= 15 or tray == 254) else None
            blocked = wire is None or wire in active
        if command == "print_option" and "calibrate_remain_flag" in section:
            blocked = True
        if not blocked:
            return None
        return {
            "print": {
                "command": command,
                "sequence_id": section.get("sequence_id", "0"),
                "result": "FAIL",
                "reason": "BBVIRTUAL_SPOOL_READ_ONLY",
            }
        }
