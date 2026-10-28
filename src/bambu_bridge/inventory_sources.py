"""Physical source choices for inventory assignment, without synthetic data."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import time
from typing import Any

import structlog

from bambu_bridge.service.material_inventory import inventory_view

log = structlog.get_logger(__name__)

# Complete-frame age accepted without asking for a new one; below the 30 s
# limit in inventory_view so the choices outlive the request that used them.
FRESH_FRAME_S = 20.0
FULL_REPORT_WAIT_S = 5.0
# A present AMS with no serial: ask for version info up to VERSION_ATTEMPTS times. Each wait
# is three times the printer's last measured get_version round trip (owner, 2026-10-28), so a
# missed reply recovers quickly; VERSION_WAIT_S applies until a reply has been timed.
VERSION_ATTEMPTS = 3
VERSION_WAIT_S = 3.0
VERSION_RTT_FACTOR = 3.0
VERSION_WAIT_MIN_S = 0.5
_version_rtt_s: dict[str, float] = {}


def _frame_fresh(service: Any) -> bool:
    stamp = service.material_inventory.snapshot().get("ams_at")
    return stamp is not None and 0 <= time.time() - stamp <= FRESH_FRAME_S


async def refresh_source_frame(service: Any, *, wait_s: float = FULL_REPORT_WAIT_S) -> None:
    """Make the next source_choices() use a recent complete AMS frame when possible.

    Requests a full report when the frame is older than FRESH_FRAME_S and waits
    up to wait_s for it; without a reply the stale view stands.
    """
    request = getattr(service, "request_full_report", None)
    if request is not None and service.connected and not _frame_fresh(service):
        stamp = service.material_inventory.snapshot().get("ams_at")
        seen = 0
        # The material frame is updated before the raw report is published.
        async with service.raw_bus.subscribe() as reports:
            sent = await request()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(wait_s):
                    async for _ in reports:
                        seen += 1
                        if _frame_fresh(service):
                            break
        if not _frame_fresh(service):
            # A slot that stays stale for a long time is otherwise unexplained:
            # say whether the request went out and whether anything came back.
            log.info(
                "inventory.ams_refresh_unanswered",
                printer=getattr(service, "serial", None),
                requested=sent,
                reports_seen=seen,
                frame_age_s=round(time.time() - stamp, 1) if stamp is not None else None,
            )
    # A present AMS with no serial makes every slot in it unassignable; ask for it.
    await ensure_ams_serials(service)


def ams_units_without_serial(service: Any) -> list[str]:
    """AMS unit ids the printer reports as present but with no hardware serial yet.

    The serial comes from the unit itself or from the ``ams/<id>`` module in the
    printer's version info. Without it every slot in that unit is unassignable.
    """
    units = (service.material_inventory.snapshot().get("ams") or {}).get("ams", [])
    modules = (service.native_snapshot().get("info") or {}).get("module", [])
    if not isinstance(modules, list):
        modules = []
    missing = []
    for unit in units if isinstance(units, list) else []:
        if not isinstance(unit, dict):
            continue
        aid = str(unit.get("id"))
        if unit.get("sn") or any(
            isinstance(m, dict) and m.get("name") == f"ams/{aid}" and m.get("sn") for m in modules
        ):
            continue
        missing.append(aid)
    return missing


def version_wait_s(key: str) -> float:
    """How long to wait for a get_version reply from this printer before asking again."""
    rtt = _version_rtt_s.get(key)
    return VERSION_WAIT_S if rtt is None else max(VERSION_WAIT_MIN_S, VERSION_RTT_FACTOR * rtt)


async def ensure_ams_serials(
    service: Any, *, attempts: int = VERSION_ATTEMPTS, wait_s: float | None = None
) -> None:
    """Ask for version info when a present AMS has no serial, until it arrives.

    The bridge sends ``get_version`` once on connect, fire-and-forget. On
    2026-10-27/28 that reply never arrived across a reconnect and a printer power
    cycle, so every AMS slot stayed unassignable until a manual get_version. This
    sends only when a connected printer reports an AMS unit without a serial: up to
    ``attempts`` requests, each waiting ``wait_s`` for the reply, stopping as soon as
    every present unit has one. A disconnected printer or a complete set sends nothing.
    """
    send = getattr(service, "send_command", None)
    native = getattr(service, "native_snapshot", None)
    if send is None or native is None:
        return
    key = str(getattr(service, "serial", id(service)))
    for attempt in range(1, attempts + 1):
        if not service.connected or not ams_units_without_serial(service):
            return
        wait = version_wait_s(key) if wait_s is None else wait_s
        replied_s: float | None = None
        async with service.raw_bus.subscribe() as reports:
            sent_at = time.monotonic()
            try:
                await send("info", "get_version")
            except Exception as exc:  # noqa: BLE001 - link down: the issue stays reported
                log.info("inventory.ams_serial_request_failed", printer=key, error=str(exc))
                return
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(wait):
                    async for _ in reports:
                        if not ams_units_without_serial(service):
                            replied_s = time.monotonic() - sent_at
                            _version_rtt_s[key] = replied_s
                            break
        log.info(
            "inventory.ams_serial_requested",
            printer=key,
            attempt=attempt,
            wait_s=round(wait, 3),
            reply_s=None if replied_s is None else round(replied_s, 3),
            still_missing=ams_units_without_serial(service),
        )


def declared_identity(tray: dict[str, Any]) -> str:
    """The material the AMS reports for a slot, as one fingerprint.

    Material and SKU, plus the tag for RFID spools; colour is left out. This
    keys the slot history (which spool last sat behind a declaration or a tag)
    and is what assignments stored before spool_key carry. It no longer decides
    whether an assignment stands: see same_spool.
    """
    values = {
        "tray_type": tray.get("tray_type") or "",
        "tray_info_idx": tray.get("tray_info_idx") or "",
        "tag_uid": tray.get("tag_uid") or "",
        "tray_uuid": tray.get("tray_uuid") or "",
    }
    return hashlib.sha256(("source-v3:" + json.dumps(values, sort_keys=True)).encode()).hexdigest()


UNTAGGED = "untagged"


def spool_key(tray: dict[str, Any]) -> str:
    """What physically tells one spool from another in a slot: its RFID tag.

    Every untagged spool has the same key, so for them only an unload (or a
    change of chemistry) can show that the spool changed. Nothing the bridge
    or the printer screen declares about a slot changes this key.
    """
    tag = str(tray.get("tag_uid") or "").strip().upper()
    if not tag.strip("0"):
        return UNTAGGED
    # The tag alone: tray_uuid can arrive a frame after tag_uid, and a key that
    # changed then would release an assignment made in between.
    return hashlib.sha256(("spool-v1:" + tag).encode()).hexdigest()


def same_spool(assignment: Any, row: dict[str, Any]) -> bool | None:
    """Whether a slot still holds the spool `assignment` was made against.

    `row` is the slot as source_choices (or slot_identities) reports it.

    True or False when the slot's report proves it, None when it cannot say:
    a blank tray (no material) or a tag being read shows nothing about the
    spool, so neither keeps nor ends an assignment. A different tag, or a
    different chemistry (tray_type: a PLA slot now PETG is another spool, since
    relabels and refill never change chemistry), is another spool. Product,
    temperatures and colour are declarations and never count.

    Between the reading bit dropping and read-done returning the tray can show
    untagged or in pieces, so no tag is judged until the read is done; two
    untagged spools are still judged, for firmware that never marks a bare
    spool read. On such firmware a tagged spool swapped for a bare one without
    a presence drop stays undecided, so its assignment is kept.

    Assignments stored before spool_key existed compare the declared identity
    once; the seating pass then stamps them (or releases them, as before).
    """
    material = str(row["material"] or "").strip().upper()
    if not material or row.get("tag_reading"):
        return None
    reading = row.get("tag_read_done") is False
    if assignment.spool_key is None:
        # Mid-read, a matching report may still stamp it; a mismatch waits.
        same = row["observed_identity"] == assignment.observed_identity
        return True if same else (None if reading else False)
    if reading and (assignment.spool_key != UNTAGGED or row["spool_key"] != UNTAGGED):
        return None
    return bool(row["spool_key"] == assignment.spool_key and material == assignment.tray_type)


def tag_details(tray: dict[str, Any]) -> dict[str, Any] | None:
    """What an RFID tag says about its spool; None for an untagged tray."""
    if not str(tray.get("tag_uid") or "").strip("0"):
        return None
    color = str(tray.get("tray_color") or "").strip().upper()
    color = color[:6] if re.fullmatch(r"[0-9A-F]{6}([0-9A-F]{2})?", color) else ""
    try:
        weight = int(float(tray.get("tray_weight") or 0)) or None
    except (TypeError, ValueError):
        weight = None
    try:
        remain = int(float(tray.get("remain")))
    except (TypeError, ValueError):
        remain = -1
    return {
        "brand": str(tray.get("tray_sub_brands") or "").strip()[:64],
        "material": str(tray.get("tray_type") or "").strip().upper()[:64],
        "color": color,
        "weight_g": weight,
        "remain_percent": remain if 0 <= remain <= 100 else None,
    }


def _bit(field: Any, index: int) -> bool | None:
    """One bit of an AMS hex bit field (index ams_id*4 + tray_id); None when absent."""
    if not isinstance(field, str) or not 1 <= len(field) <= 8:
        return None
    try:
        return bool(int(field, 16) >> index & 1)
    except ValueError:
        return None


def tag_reading(ams: dict[str, Any], wire: int, present: bool) -> bool:
    """Whether the AMS is reading this tray's RFID tag right now (for display).

    While it reads, the tray's bit is set in tray_reading_bits, and the tray
    usually still looks untagged, so this says "reading tag…" before the tag
    shows. Firmware without the field never reads as reading.
    """
    return present and wire != 254 and _bit(ams.get("tray_reading_bits"), wire) is True


def tag_read_done(ams: dict[str, Any], wire: int) -> bool | None:
    """Whether the AMS has finished reading this tray's tag; None without the field.

    A finished read sets the tray's bit in tray_read_done_bits. Seating a tagged
    spool waits for it where the firmware reports it, since reports in between
    can show the tray untagged or in pieces.
    """
    if wire == 254:
        return None
    return _bit(ams.get("tray_read_done_bits"), wire)


def tray_setting(tray: dict[str, Any]) -> dict[str, Any] | None:
    """The slot's current ams_filament_setting fields, as the printer reports them.

    None until a report carried all five; a write that changes one field keeps
    the other four exactly, so the slot's identity and assignment stand.
    """
    try:
        low, high = int(tray["nozzle_temp_min"]), int(tray["nozzle_temp_max"])
        color = str(tray["tray_color"]).strip().upper()
        material = str(tray["tray_type"]).strip().upper()
        sku = str(tray.get("tray_info_idx") or "")
    except (KeyError, TypeError, ValueError):
        return None
    if not material or not re.fullmatch(r"[0-9A-F]{8}", color):
        return None
    return {
        "tray_type": material,
        "tray_info_idx": sku,
        "tray_color": color,
        "nozzle_temp_min": low,
        "nozzle_temp_max": high,
    }


def source_choices(service: Any, *, stale_ok: bool = False) -> list[dict[str, Any]]:
    """The printer's slots as assignment sources.

    Only a fresh frame lists slots unless `stale_ok`: then the last known
    slots are listed too, marked stale, and never assignable. Callers that
    act on a slot (assigning, unseating) leave it off.
    """
    frame = service.material_inventory.snapshot()
    view = inventory_view(frame, stale_ok=stale_ok)
    now = time.time()
    native = service.native_snapshot()
    units = (frame.get("ams") or {}).get("ams", [])
    modules = (native.get("info") or {}).get("module", [])
    if not isinstance(modules, list):
        modules = []
    modules = [module for module in modules if isinstance(module, dict)]
    choices: list[dict[str, Any]] = []
    hardware: str | None
    for slot in view["slots"]:
        wire = slot["wire_id"]
        if wire == 254:
            tray = frame.get("external") or {}
            hardware = "external"
            source = "external"
        else:
            aid, tid = divmod(wire, 4)
            unit: dict[str, Any] = next((u for u in units if str(u.get("id")) == str(aid)), {})
            module: dict[str, Any] = next((m for m in modules if m.get("name") == f"ams/{aid}"), {})
            hardware = unit.get("sn") or module.get("sn")
            # Unit numbers alone do not prove the same AMS is still attached.
            digest = hashlib.sha256(str(hardware).encode()).hexdigest()[:24] if hardware else None
            source = f"ams:{digest}:tray:{tid}" if digest else f"unresolved:{wire}"
            tray = next((t for t in unit.get("tray", []) if str(t.get("id")) == str(tid)), {})
        fresh = view["external_fresh" if wire == 254 else "ams_fresh"]
        stamp = frame.get("external_at" if wire == 254 else "ams_at")
        issues = []
        if not fresh:
            issues.append("ams_stale")
        if not service.connected:
            issues.append("printer_disconnected")
        if not slot["present"]:
            issues.append("source_empty")
        if not hardware:
            issues.append("ams_hardware_identity_unavailable")
        tagged = bool(str(tray.get("tag_uid") or "").strip("0"))
        choices.append(
            {
                "printer_id": service.serial,
                "source": source,
                "wire_slot": wire,
                "label": slot["label"],
                "material": slot["material"],
                "color": slot["color"],
                "observed_identity": declared_identity(tray),
                "spool_key": spool_key(tray),
                # An RFID spool: the printer read its material from the tag.
                "tagged": tagged,
                "tag": tag_details(tray),
                # Reading the tag now: what the row says may change in a moment.
                "tag_reading": tag_reading(frame.get("ams") or {}, wire, slot["present"]),
                "tag_read_done": tag_read_done(frame.get("ams") or {}, wire),
                "setting": tray_setting(tray),
                "observed_at": stamp,
                "stale": not fresh,
                "age_seconds": round(now - stamp, 1) if stamp is not None else None,
                "assignable": not issues,
                "issues": issues,
            }
        )
    return choices
