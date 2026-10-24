"""Physical-only refill receipts. Never feed client-projected or merged state here."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from bambu_bridge.inventory_sources import spool_key
from bambu_bridge.refill import RUN, Declaration, RefillReport, SlotReport

_FIELDS = tuple(Declaration.__dataclass_fields__)


def _generation() -> str:
    """A new source generation, marked with this bridge run (refill.RUN)."""
    return f"{RUN}-{uuid4().hex}"


def _bits(value: Any) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9A-Fa-f]{1,8}", value):
        return int(value, 16)
    return None


class RefillReports:
    def __init__(self) -> None:
        # What each tray last was, kept across reconnects: a tray that comes
        # back unchanged keeps its generation, so owned aliases stay restorable.
        self.known: dict[int, SlotReport] = {}
        self.identities: dict[int, dict[str, Any]] = {}
        self.generations: dict[int, str] = {}
        # wire -> generation of the slot's first complete report this run (the
        # startup invalidations come before it; any later change moves past it)
        self.first: dict[int, str] = {}
        # Called after each report (RefillController.note), when set.
        self.listener: Callable[[], None] | None = None
        self.expected: dict[int, tuple[str, dict[str, Any]]] = {}
        self.serial: str | None = None
        # Whether this run has named the AMS yet. A P1S names it only in the
        # version reply, which lands after the first pushall: learning the name
        # then is not a change of hardware, so it must not retire generations
        # (that left every slot's first_of_run false, and no stored group was
        # ever re-adopted after a restart). A later, different name still is.
        self.identified = False
        self.last_presence: int | None = None
        self.counter = 0  # receipts only ever grow
        self.clear()

    def clear(self) -> None:
        """Forget what is current, on connect and on any session loss.

        Only a change seen in a later report (another declaration, RFID identity,
        AMS serial or a tray leaving) starts a new generation.
        """
        self.slots: dict[int, SlotReport] = {}
        self.hardware: tuple[str, float] | None = None
        self.presence: int | None = None
        self.unit_present: bool | None = None
        self.presence_at: float | None = None
        self.unit_at: float | None = None
        self.auto: bool | None = None
        self.auto_at: float | None = None
        self.backups: tuple[int, ...] | None = None
        self.backups_at: float | None = None
        self.state: str | None = None
        self.state_at: float | None = None
        self.reading = 0

    def _invalidate(self, wire: int) -> None:
        self.reading &= ~(1 << wire)  # whatever was being read there is gone
        self.slots.pop(wire, None)
        self.known.pop(wire, None)
        self.identities.pop(wire, None)
        self.expected.pop(wire, None)
        self.generations[wire] = _generation()

    def expect(self, wire: int, generation: str, target: Declaration) -> None:
        """Reserve exactly one declaration transition before a qualified adapter sends.

        A refill alias may declare another product, temperature range and
        colour, but never another chemistry: that would be another spool.
        """
        slot = self.slots.get(wire)
        if slot is None or slot.source_generation != generation or not slot.editable:
            raise ValueError("refill physical source changed")
        target.validate()
        before, after = asdict(slot.declaration), asdict(target)
        if before["tray_type"].strip().upper() != after["tray_type"].strip().upper():
            raise ValueError("a refill alias never changes the declared material")
        self.expected[wire] = (generation, after)

    def observe(self, raw: dict[str, Any]) -> None:
        self._observe(raw)
        # Applied after the trays, so a tray invalidated by this same frame (a
        # tag re-read changes its identity) keeps the reading bit it carries.
        ams = raw.get("print", {}).get("ams") if isinstance(raw.get("print"), dict) else None
        if isinstance(ams, dict) and "tray_reading_bits" in ams:
            self.reading = _bits(ams["tray_reading_bits"]) or 0
        if self.listener is not None:
            self.listener()

    def _observe(self, raw: dict[str, Any]) -> None:
        now = time.monotonic()
        info = raw.get("info")
        if isinstance(info, dict) and isinstance(info.get("module"), list):
            for module in info["module"]:
                if isinstance(module, dict) and module.get("name") == "ams/0":
                    self._hardware(module.get("sn"), now)
            if not any(isinstance(m, dict) and m.get("name") == "ams/0" for m in info["module"]):
                self._hardware(None, now)
        report = raw.get("print")
        if not isinstance(report, dict):
            return
        if "gcode_state" in report:
            self.state = str(report["gcode_state"])
            self.state_at = now
        if "home_flag" in report:
            value = report["home_flag"]
            self.auto = bool(value & (1 << 10)) if type(value) is int and value >= 0 else None
            self.auto_at = now
        if "filam_bak" in report:
            values = report["filam_bak"]
            self.backups = (
                tuple(values)
                if isinstance(values, list)
                and all(type(v) is int and 0 <= v < (1 << 24) for v in values)
                else None
            )
            self.backups_at = now
        ams = report.get("ams")
        if not isinstance(ams, dict):
            return
        if "ams_exist_bits" in ams:
            unit_bits = _bits(ams["ams_exist_bits"])
            present = bool(unit_bits & 1) if unit_bits is not None else None
            if present is not True:
                self.reading = 0
                for wire in range(4):
                    self._invalidate(wire)
            self.unit_present = present
            self.unit_at = now
            if present and self.serial is not None:
                # A P1S names its AMS only in the version reply, never in a
                # push; a unit still reported attached is that same AMS.
                self.hardware = (self.serial, now)
        if "tray_exist_bits" in ams:
            bits = _bits(ams["tray_exist_bits"])
            for wire in range(4):
                was = self.last_presence is not None and bool(self.last_presence & (1 << wire))
                is_present = bits is not None and bool(bits & (1 << wire))
                if not is_present or not was:
                    self._invalidate(wire)
            self.presence = self.last_presence = bits
            self.presence_at = now
        units = ams.get("ams")
        if not isinstance(units, list):
            return
        for unit in units:
            if not isinstance(unit, dict) or str(unit.get("id")) != "0":
                continue
            if "sn" in unit:
                self._hardware(unit["sn"], now)
            trays = unit.get("tray")
            if not isinstance(trays, list):
                continue
            for tray in trays:
                if not isinstance(tray, dict) or str(tray.get("id")) not in {"0", "1", "2", "3"}:
                    continue
                self._tray(int(tray["id"]), tray, now)

    def _hardware(self, serial: Any, now: float) -> None:
        if not isinstance(serial, str) or not serial or len(serial) > 80:
            self.hardware = self.serial = None
            for wire in range(4):
                self._invalidate(wire)
            return
        if self.serial != serial and self.identified:
            for wire in range(4):
                self._invalidate(wire)
        self.hardware, self.serial = (serial, now), serial
        self.identified = True

    def _tray(self, wire: int, tray: dict[str, Any], now: float) -> None:
        identity = {k: tray[k] for k in ("tag_uid", "tray_uuid", "tray_sub_brands") if k in tray}
        previous = self.identities.get(wire, {})
        if any(k in previous and previous[k] != v for k, v in identity.items()):
            self._invalidate(wire)
            previous = {}
        self.identities[wire] = {**previous, **identity}
        prior = self.known.get(wire)
        fields = {key: tray[key] for key in _FIELDS if key in tray}
        for key in ("nozzle_temp_min", "nozzle_temp_max"):
            if isinstance(fields.get(key), str) and re.fullmatch(r"[0-9]{1,3}", fields[key]):
                fields[key] = int(fields[key])
        if isinstance(fields.get("tray_color"), str):
            fields["tray_color"] = fields["tray_color"].upper()
        tag = tray.get("tag_uid")
        editable = isinstance(tag, str) and bool(tag) and set(tag) == {"0"}
        tagged = isinstance(tag, str) and bool(tag.strip().strip("0"))
        expected = self.expected.get(wire)
        if prior is not None:
            changed = any(asdict(prior.declaration)[key] != value for key, value in fields.items())
            allowed = (
                expected is not None
                and expected[0] == prior.source_generation
                and all(expected[1][key] == value for key, value in fields.items())
            )
            if (changed and not allowed) or ("tag_uid" in tray and editable != prior.editable):
                self._invalidate(wire)
                prior = None
        if set(fields) != set(_FIELDS) or "tag_uid" not in tray:
            # Humidity-only updates do not invalidate. Partial declaration updates
            # cannot refresh the complete declaration receipt, even when expected.
            if fields or "tag_uid" in tray:
                self.slots.pop(wire, None)
            return
        try:
            declaration = Declaration(**fields)
            declaration.validate()
        except (ValueError, TypeError):
            self._invalidate(wire)
            return
        generation = self.generations.setdefault(wire, _generation())
        self.first.setdefault(wire, generation)
        self.counter += 1
        self.slots[wire] = SlotReport(
            declaration,
            generation,
            self.counter,
            now,
            editable,
            tagged,
            spool_key(tray) if tagged else "",
            generation == self.first.get(wire),
        )
        self.known[wire] = self.slots[wire]
        self.identities[wire] = {**self.identities.get(wire, {}), **identity}
        if expected and fields == expected[1]:
            self.expected.pop(wire, None)

    def snapshot(self, *, connected: bool) -> RefillReport:
        now = time.monotonic()
        hardware_fresh = self.hardware is not None and 0 <= now - self.hardware[1] <= 30
        slots = {
            wire: slot
            for wire, slot in self.slots.items()
            if hardware_fresh
            and self.unit_present is True
            and self.presence is not None
            and self.presence & (1 << wire)
        }
        # Hardware receipt age is independently bounded; never reset slot receipt.
        if self.hardware is not None and self.presence_at is not None and self.unit_at is not None:
            slots = {
                wire: SlotReport(
                    s.declaration,
                    s.source_generation,
                    s.receipt,
                    min(s.reported_at, self.hardware[1], self.presence_at, self.unit_at),
                    s.editable,
                    s.tagged,
                    s.tag_key,
                    s.first_of_run,
                )
                for wire, s in slots.items()
            }
        eligibility_at = (
            min(self.auto_at, self.backups_at)
            if self.auto_at is not None and self.backups_at is not None
            else None
        )
        idle = (
            self.state in {"IDLE", "FINISH", "FAILED"}
            and self.state_at is not None
            and 0 <= now - self.state_at <= 30
        )
        # A tray that is not there is not being read, whatever bit was last sent.
        present = self.presence or 0
        reading = frozenset(w for w in range(4) if (self.reading & present) >> w & 1)
        # Positive evidence of an empty slot: a fresh presence report from the
        # identified, attached AMS with the slot's bit clear. A slot merely
        # missing from `slots` may only be silent or mid-update.
        empty = (
            frozenset(w for w in range(4) if not present >> w & 1)
            if connected
            and hardware_fresh
            and self.unit_present is True
            and self.presence is not None
            and self.presence_at is not None
            and 0 <= now - self.presence_at <= 30
            else None
        )
        return RefillReport(
            connected, idle, slots, self.auto, self.backups, eligibility_at, reading, empty
        )
