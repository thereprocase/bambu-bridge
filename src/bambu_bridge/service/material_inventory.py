"""Fresh, coherent material reports for replay review, independent of UI slot numbering."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from typing import Any


class MaterialInventory:
    """A temperature heartbeat must never make an old spool inventory look fresh.

    Full AMS frames replace each other. Incomplete identity/presence updates
    invalidate the old frame until a complete report arrives. Humidity-only
    updates do not refresh or invalidate the material frame.
    """

    def __init__(self) -> None:
        self.ams: dict[str, Any] | None = None
        self.external: dict[str, Any] | None = None
        self.ams_at: float | None = None
        self.external_at: float | None = None
        self.revision = 0

    def clear(self) -> None:
        self.ams = self.external = None
        self.ams_at = self.external_at = None
        self.revision += 1

    def observe(self, report: dict[str, Any]) -> None:
        ams = report.get("ams")
        if isinstance(ams, dict) and any(
            key in ams for key in ("ams", "tray_exist_bits", "ams_exist_bits")
        ):
            self.ams = None
            self.ams_at = None
            if all(key in ams for key in ("ams", "tray_exist_bits", "ams_exist_bits")):
                self.ams = copy.deepcopy(ams)
                self.ams_at = time.time()
            self.revision += 1
        if "vt_tray" in report:
            self.external = None
            self.external_at = None
            tray = report["vt_tray"]
            if isinstance(tray, dict) and "id" in tray and "tray_type" in tray:
                self.external = copy.deepcopy(tray)
                self.external_at = time.time()
            self.revision += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "ams": copy.deepcopy(self.ams),
            "external": copy.deepcopy(self.external),
            "ams_at": self.ams_at,
            "external_at": self.external_at,
        }


def _bits(value: Any) -> int:
    if not isinstance(value, str) or not 1 <= len(value) <= 8:
        raise ValueError("Presence bits are missing")
    return int(value, 16)


def _slot(tray: dict[str, Any], wire: int, label: str, present: bool) -> dict[str, Any]:
    identity = {
        k: tray.get(k)
        for k in (
            "tray_type",
            "tray_color",
            "tray_info_idx",
            "tray_uuid",
            "tag_uid",
        )
    }
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


def inventory_view(frame: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    slots: list[dict[str, Any]] = []
    issues: list[str] = []
    ams = frame.get("ams")
    stamp = frame.get("ams_at")
    ams_fresh = stamp is not None and 0 <= now - stamp <= 30
    if not ams_fresh:
        issues.append("A complete AMS inventory from the last 30 seconds is unavailable.")
    elif isinstance(ams, dict):
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
    if external_fresh and isinstance(external, dict) and str(external.get("id")) == "254":
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
