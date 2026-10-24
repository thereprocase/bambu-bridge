"""Freeze what a print will use, per layer and per slot, before it is sent."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import zipfile
from typing import Any, BinaryIO

from bambu_bridge.gcode_usage import PARSER_VERSION, layer_plan
from bambu_bridge.inventory_sources import (
    declared_identity,
    same_spool,
    spool_key,
    tag_read_done,
    tag_reading,
)
from bambu_bridge.native_inbox import NativeInbox
from bambu_bridge.service.inventory import InventoryService
from bambu_bridge.service.material_inventory import inventory_view


async def reserve_shadow(
    inbox: NativeInbox,
    identifier: str,
    inventory: InventoryService,
    printer_id: str,
    printer: Any,
    *,
    artifact: bytes | None = None,
    command: dict[str, Any] | None = None,
) -> None:
    """Freeze accounting context for one attempt before its start is sent.

    Native uploads are read from the inbox's verified payload. A bridge-managed
    job (app queue or HTTP submission) passes the exact bytes it uploaded and
    the start command it is about to send. Anything else is review-only.
    """
    row = await asyncio.to_thread(inbox.get, identifier)
    if row["kind"] == "upload":
        command = json.loads(row["command"])["print"]
    elif artifact and isinstance(command, dict) and isinstance(command.get("print"), dict):
        command = command["print"]
    else:
        await asyncio.to_thread(
            inbox.review_accounting, identifier, "submitted_artifact_unavailable"
        )
        return
    assert command is not None
    selected = re.fullmatch(r"Metadata/plate_([1-9][0-9]*)\.gcode", str(command.get("param", "")))
    if selected is None:
        await asyncio.to_thread(inbox.review_accounting, identifier, "selected_plate_unverified")
        return

    plate = int(selected[1])

    def analyze() -> tuple[str, list[dict[str, float]], dict[str, list[float]]]:
        try:
            if row["kind"] != "upload":
                assert artifact is not None
                return read_plan(artifact, plate)
            with inbox.open_verified(identifier) as archive:
                return read_plan(archive, plate)
        except (zipfile.BadZipFile, KeyError, OSError) as exc:
            raise ValueError("ACCOUNTING_ARCHIVE_UNREADABLE") from exc

    sha256, plan, header = await asyncio.to_thread(analyze)
    snapshot = await inventory.assignments.snapshot()
    await asyncio.to_thread(
        inbox.freeze_accounting,
        identifier,
        plan_manifest(
            sha256,
            plate,
            plan,
            header,
            command,
            snapshot,
            printer_id,
            trays=tray_materials(printer),
            slots=slot_identities(printer),
        ),
    )


def _present_slots(printer: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(the printer's last AMS frame, its present slots), empty when it has none."""
    try:
        frame = printer.material_inventory.snapshot()
        stamps = [t for t in (frame.get("ams_at"), frame.get("external_at")) if t is not None]
        if not stamps:
            return {}, []
        slots = inventory_view(frame, now=max(stamps))["slots"]
    except Exception:  # noqa: BLE001 — a missing frame only loses the slot's name
        return {}, []
    return frame, [slot for slot in slots if slot["present"]]


def tray_materials(printer: Any) -> dict[str, dict[str, str | None]]:
    """Material and colour the printer last reported per slot, by wire ("0".."15", "254").

    Frozen with the manifest so a slot used without a spool can still be
    named at the end. Empty when the printer has reported nothing usable.
    """
    _, slots = _present_slots(printer)
    return {
        str(slot["wire_id"]): {"material": slot["material"] or None, "color": slot["color"] or None}
        for slot in slots
    }


def _raw_tray(frame: dict[str, Any], wire: int) -> dict[str, Any]:
    if wire == 254:
        return frame.get("external") or {}
    aid, tid = divmod(wire, 4)
    units = (frame.get("ams") or {}).get("ams") or []
    unit = next((u for u in units if isinstance(u, dict) and str(u.get("id")) == str(aid)), {})
    trays = unit.get("tray") or []
    return next((t for t in trays if isinstance(t, dict) and str(t.get("id")) == str(tid)), {})


def slot_identities(printer: Any) -> dict[str, dict[str, Any]]:
    """What is in each present slot now, as the assignment flow fingerprints it.

    By wire: "identity" (declared_identity, frozen into manifests to notice a
    tag swap), "tagged", and the fields same_spool reads, so an assignment is
    judged here exactly as the seating pass judges it.
    """
    frame, slots = _present_slots(printer)
    found = {}
    for slot in slots:
        tray = _raw_tray(frame, slot["wire_id"])
        identity = declared_identity(tray)
        found[str(slot["wire_id"])] = {
            "identity": identity,
            "tagged": bool(str(tray.get("tag_uid") or "").strip("0")),
            "observed_identity": identity,
            "spool_key": spool_key(tray),
            "material": slot["material"],
            "tag_reading": tag_reading(frame.get("ams") or {}, slot["wire_id"], True),
            "tag_read_done": tag_read_done(frame.get("ams") or {}, slot["wire_id"]),
        }
    return found


def assignment_stands(assignment: Any, slot: dict[str, Any] | None) -> bool:
    """False when the slot now proves it holds another spool (same_spool)."""
    if slot is None:
        return True
    if assignment.spool_key is None:
        # Not yet stamped by the seating pass: only a tag can prove the spool left.
        return not slot["tagged"] or slot["identity"] == assignment.observed_identity
    return same_spool(assignment, slot) is not False


def read_plan(
    data: bytes | BinaryIO, plate: int
) -> tuple[str, list[dict[str, float]], dict[str, list[float]]]:
    """(sha256, layer plan, slicer header) for a .gcode.3mf archive or plain G-code."""
    raw = data if isinstance(data, bytes) else data.read()
    sha256 = hashlib.sha256(raw).hexdigest()
    if raw[:2] == b"PK":
        with (
            zipfile.ZipFile(io.BytesIO(raw)) as bundle,
            io.TextIOWrapper(
                bundle.open(f"Metadata/plate_{plate}.gcode"), "utf-8", "replace"
            ) as lines,
        ):
            plan, header = layer_plan(lines)
    else:
        plan, header = layer_plan(io.StringIO(raw.decode("utf-8", "replace")))
    return sha256, plan, header


def plan_manifest(
    sha256: str,
    plate: int,
    plan: list[dict[str, float]],
    header: dict[str, list[float]],
    command: dict[str, Any],
    snapshot: Any,
    printer_id: str,
    trays: dict[str, dict[str, str | None]] | None = None,
    slots: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The frozen record a print is counted against at its end.

    `slots` is what each slot holds now (slot_identities): an assignment for a
    tagged slot that holds another spool is not frozen, so that slot is asked
    about at the end instead of charged to a spool that left.
    """
    mapping = command.get("ams_mapping")
    tool_wires: dict[str, int] = {}
    for tool in sorted({int(t) for row in plan for t in row}):
        wire = mapping[tool] if isinstance(mapping, list) and tool < len(mapping) else None
        if command.get("use_ams") is False or wire == 254:
            tool_wires[str(tool)] = 254
        elif type(wire) is int and 0 <= wire < 16:
            tool_wires[str(tool)] = wire
    slots = slots or {}
    wire_spools = {
        str(254 if a.source == "external" else a.wire_slot): a.spool_id
        for a in snapshot.assignments
        if a.printer_id == printer_id
        and (a.source == "external" or a.wire_slot is not None)
        and assignment_stands(a, slots.get(str(254 if a.source == "external" else a.wire_slot)))
    }
    return {
        "artifact_sha256": sha256,
        "plate": plate,
        "parser_version": PARSER_VERSION,
        "provider_id": snapshot.provider_id,
        "assignment_revision": snapshot.revision,
        "sliced_settings": {
            "slicer_length_mm": header.get("filament used [mm]"),
            "slicer_mass_g": header.get("filament used [g]"),
        },
        "planned_grams": round(sum(g for row in plan for g in row.values()), 2),
        "analysis_issues": [
            f"unweighed_filament_{int(t)}" for t in header.get("unweighed_filaments") or []
        ],
        # Counted at the end: grams per layer per filament, filament -> slot,
        # and the spool in each slot when the print started.
        "layer_grams": plan,
        "tool_wires": tool_wires,
        "wire_spools": wire_spools,
        # What the printer said was in each slot, for a slot used without a spool.
        "tray_materials": trays or {},
        # Each slot's spool fingerprint at dispatch, to notice a swap by the end.
        "slot_identities": {wire: slot["identity"] for wire, slot in slots.items()},
    }
