"""Physical source choices for inventory assignment, without synthetic data."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import time
from typing import Any

from bambu_bridge.service.material_inventory import inventory_view

# Complete-frame age accepted without asking for a new one; below the 30 s
# limit in inventory_view so the choices outlive the request that used them.
FRESH_FRAME_S = 20.0
FULL_REPORT_WAIT_S = 5.0


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
        # The material frame is updated before the raw report is published.
        async with service.raw_bus.subscribe() as reports:
            await request()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(wait_s):
                    async for _ in reports:
                        if _frame_fresh(service):
                            break


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
