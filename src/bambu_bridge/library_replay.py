"""Bounded, readonly analysis of archived slices and proposed filament mappings.

This module deliberately has no transport dependency. A successful review is
not a printer-start authorization or a qualified physical replay.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from typing import Any

from bambu_bridge.library import LibraryError, LibraryStore
from bambu_bridge.service.material_inventory import inventory_view
from bambu_bridge.slicedoc.gcode import MAX_BED_C, MAX_NOZZLE_C, scan_gcode

_CONFIG_LIMIT = 2 * 1024**2
_GCODE_LIMIT = 128 * 1024**2
_EXPANDED_LIMIT = 512 * 1024**2
_SELECT = re.compile(rb"^[ \t]*(M620|M621)[ \t]+S(\d+)(A?)\b", re.MULTILINE)
_TOOL = re.compile(rb"^[ \t]*T(\d+)(?=[ \t;\r\n]|$)", re.MULTILINE)


def _text(value: Any, maximum: int = 160) -> str:
    return str(value or "")[:maximum]


def _number(value: Any) -> float | None:
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else None
    except (TypeError, ValueError):
        return None


def _color(value: str) -> str:
    color = value.lstrip("#").upper()
    return color[:6] if re.fullmatch(r"[0-9A-F]{6}([0-9A-F]{2})?", color) else ""


def _metadata(element: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for child in element.findall("metadata"):
        key, value = child.get("key", ""), child.get("value", "")
        if key in result:
            raise ValueError("Duplicate metadata key")
        result[key] = value
    return result


def requirements(data: bytes, plate: int) -> dict[str, Any]:
    """Read the selected plate only; never extract paths or rewrite archive bytes."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(entries) > 2048 or len(names) != len(set(names)):
                raise ValueError("Archive has too many or duplicate members")
            if sum(entry.file_size for entry in entries) > _EXPANDED_LIMIT:
                raise ValueError("Archive exceeds the analysis expansion limit")
            if any(entry.flag_bits & 1 for entry in entries):
                raise ValueError("Encrypted members are unsupported")

            def read(name: str, limit: int) -> bytes:
                if archive.getinfo(name).file_size > limit:
                    raise ValueError("Archive member exceeds the analysis limit")
                with archive.open(name) as source:
                    value = source.read(limit + 1)
                if len(value) > limit:
                    raise ValueError("Archive member exceeds the analysis limit")
                return value

            gcode = read(f"Metadata/plate_{plate}.gcode", _GCODE_LIMIT)
            md5 = read(f"Metadata/plate_{plate}.gcode.md5", 64).decode("ascii").strip()
            if not re.fullmatch(r"[0-9a-fA-F]{32}", md5) or (
                hashlib.md5(gcode, usedforsecurity=False).hexdigest() != md5.lower()
            ):
                raise ValueError("Selected plate G-code checksum does not match")
            xml = read("Metadata/slice_info.config", _CONFIG_LIMIT).decode("utf-8-sig")
            if "\0" in xml or re.search(r"<!\s*(DOCTYPE|ENTITY)", xml, re.IGNORECASE):
                raise ValueError("XML declarations/entities are unsupported")
            root = ET.fromstring(xml)  # noqa: S314 — bounded UTF-8, no DTD/entities
            if sum(1 for _ in root.iter()) > 20000:
                raise ValueError("Slice metadata is too complex")
            plates = [(element, _metadata(element)) for element in root.findall("plate")]
            selected = [
                (element, meta) for element, meta in plates if meta.get("index") == str(plate)
            ]
            if len(selected) != 1:
                raise ValueError("Slice metadata does not identify the selected plate uniquely")
            element, meta = selected[0]
            settings = {}
            if "Metadata/project_settings.config" in names:
                settings = json.loads(read("Metadata/project_settings.config", _CONFIG_LIMIT))
                if not isinstance(settings, dict):
                    raise ValueError("Project settings must be an object")

        filaments: list[dict[str, Any]] = []
        indices: set[int] = set()
        profiles = settings.get("filament_settings_id", [])
        for filament in element.findall("filament"):
            fid = filament.get("id", "")
            if not re.fullmatch(r"[1-9][0-9]?", fid) or not 1 <= int(fid) <= 64:
                raise ValueError("Invalid logical filament ID")
            index = int(fid) - 1
            if index in indices:
                raise ValueError("Duplicate logical filament ID")
            indices.add(index)
            filaments.append(
                {
                    "index": index,
                    "label": f"Filament {index + 1}",
                    "material": _text(filament.get("type"), 64),
                    "color": _text(filament.get("color"), 16),
                    "profile_id": _text(filament.get("tray_info_idx"), 128),
                    "profile_name": _text(profiles[index])
                    if isinstance(profiles, list) and index < len(profiles)
                    else "",
                    "used_g": _number(filament.get("used_g")),
                }
            )
        if not filaments:
            raise ValueError("Slice has no logical filament records")

        # P1S uses logical indices with the A mapping flag. Physical pre-binds
        # outside the metadata's logical index space must never be remapped by
        # silently patching the stored G-code.
        binds = _SELECT.findall(gcode)
        tools = {int(v) for v in _TOOL.findall(gcode) if int(v) < 64}
        loads = {
            int(value) for command, value, _ in binds if command == b"M620" and int(value) < 64
        }
        finishes = {
            int(value) for command, value, _ in binds if command == b"M621" and int(value) < 64
        }
        used = loads | finishes | tools
        issues: list[str] = []
        if (
            not used
            or loads != used
            or finishes != used
            or (tools and tools != used)
            or not used <= indices
        ):
            issues.append("Logical filament handshakes are ambiguous; reslice this file in Orca.")
        if any(not flag for _, value, flag in binds if int(value) < 64):
            issues.append("The toolpath does not explicitly enable logical AMS remapping.")
        if any(int(v) not in set(range(64)) | {254, 255, 1000, 1100} for v in _TOOL.findall(gcode)):
            issues.append("The toolpath uses an unsupported tool selector.")
        for item in filaments:
            item["used"] = item["index"] in used
            if item["used_g"] and not item["used"]:
                issues.append("Filament usage disagrees with the toolpath; reslice for review.")
        nozzle = _number(meta.get("nozzle_diameters"))
        if nozzle not in (0.2, 0.4, 0.6, 0.8):
            issues.append("A single supported nozzle diameter is not recorded.")
        model = meta.get("printer_model_id", "")
        if model != "C12":
            issues.append("Replay review currently supports P1S slice metadata only.")
        bed = settings.get("curr_bed_type", "")
        if not isinstance(bed, str) or not bed:
            issues.append("The sliced plate type is not recorded; reopen the project in Orca.")
        scan = scan_gcode(gcode)
        if scan.max_nozzle_c is None or scan.max_nozzle_c > MAX_NOZZLE_C:
            issues.append(
                "Nozzle temperatures are absent or exceed the bridge's validated envelope."
            )
        if scan.max_bed_c is None or scan.max_bed_c > MAX_BED_C:
            issues.append("Bed temperatures are absent or exceed the bridge's validated envelope.")
        return {
            "plate": plate,
            "slice_sha256": hashlib.sha256(data).hexdigest(),
            "printer_model": model,
            "nozzle_diameter": nozzle,
            "bed_type": _text(bed),
            "filaments": sorted(filaments, key=lambda f: f["index"]),
            "max_nozzle_c": scan.max_nozzle_c,
            "max_bed_c": scan.max_bed_c,
            "issues": list(dict.fromkeys(issues)),
        }
    except (
        ValueError,
        KeyError,
        TypeError,
        ET.ParseError,
        zipfile.BadZipFile,
        NotImplementedError,
        RuntimeError,
        OverflowError,
        zlib.error,
    ) as exc:
        raise LibraryError(
            422, "Slice metadata is damaged, incomplete or exceeds analysis limits"
        ) from exc


def review(
    store: LibraryStore,
    cid: str,
    *,
    printer: dict[str, Any],
    frame: dict[str, Any],
    choices: dict[int, int] | None = None,
    expected_inventory: str | None = None,
) -> dict[str, Any]:
    capture = store.get(cid)
    artifact = next((a for a in capture["artifacts"] if a["role"] == "slice"), None)
    if artifact is None:
        raise LibraryError(409, "This capture has no sliced print file")
    path, _ = store.download(cid, artifact["name"])
    spec = requirements(path.read_bytes(), capture["plate"])
    if spec["slice_sha256"] != artifact["sha256"]:
        raise LibraryError(409, "Archived slice changed during analysis; verify library integrity")
    inventory = inventory_view(frame)
    issues = list(spec["issues"])
    if not printer.get("connected"):
        issues.append("Printer is disconnected.")
    if printer.get("cert_status") == "changed":
        issues.append("The printer certificate changed; verify the printer connection first.")
    if not printer.get("model"):
        issues.append("The bridge has not recorded this printer's model; verify it is a P1S.")
    elif printer.get("model") != "P1S":
        issues.append("The selected printer must be a P1S.")
    reported_nozzle = _number(printer.get("nozzle_diameter"))
    if reported_nozzle is not None and reported_nozzle != spec["nozzle_diameter"]:
        issues.append(
            "The reported nozzle diameter differs from this slice; reslice for this nozzle."
        )
    if expected_inventory and expected_inventory != inventory["fingerprint"]:
        issues.append("Material inventory changed; review the new tray choices.")
    selected = choices or {}
    used_indices = {f["index"] for f in spec["filaments"] if f["used"]}
    if selected.keys() - used_indices:
        issues.append("Mapping contains an unused or unknown logical filament.")
    rows = []
    for filament in spec["filaments"]:
        if not filament["used"]:
            continue
        candidates = [
            slot
            for slot in inventory["slots"]
            if slot["present"]
            and slot["material"].upper() == filament["material"].upper()
            and bool(slot["material"])
        ]
        exact = [
            s
            for s in candidates
            if _color(s["color"])
            and _color(s["color"]) == _color(filament["color"])
            and s["profile_id"] == filament["profile_id"]
        ]
        choice = selected.get(filament["index"])
        if choice is None:
            issues.append(f"Choose a current spool for {filament['label'].lower()}.")
        elif not any(slot["wire_id"] == choice for slot in candidates):
            issues.append(
                f"{filament['label']}: chosen tray is absent, stale or a different material."
            )
        rows.append(
            {
                **filament,
                "candidates": candidates,
                "choice": choice,
                "suggestion": exact[0]["wire_id"] if len(exact) == 1 else None,
            }
        )
    if 254 in selected.values() and (
        len(used_indices) != 1 or any(v != 254 for v in selected.values())
    ):
        issues.append("External spool replay requires exactly one used logical filament.")
    if any(value != 254 for value in selected.values()):
        issues.extend(inventory["issues"])
    notes = [
        "Confirm the installed nozzle and plate match the saved settings.",
        "Confirm each spool suits the saved filament profile and has enough material; "
        "color is a suggestion.",
        "Inventory must be checked again immediately before a future Start print action.",
    ]
    return {
        "capture_id": cid,
        "printer_id": printer.get("printer_id"),
        "requirements": spec,
        "inventory": inventory,
        "mapping": rows,
        "mapping_complete": not issues,
        "issues": list(dict.fromkeys(issues)),
        "review_notes": notes,
        "dispatch_available": False,
        "dispatch_note": "Read-only replay review. Starting archived prints is not yet enabled.",
    }
