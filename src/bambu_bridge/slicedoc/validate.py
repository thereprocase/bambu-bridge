"""Gate a finished ``.gcode.3mf`` before it is uploaded and started.

Accept what OrcaSlicer 2.4.2 / Bambu Studio produce, and check what Orca checks
before it sends a job (``SelectMachineDialog::update_show_status`` in
``src/slic3r/GUI/SelectMachine.cpp``), plus the bridge's temperature envelope:

G1 archive: bounded, no duplicate or encrypted members (CRC checked on read)
G2 the three members read here exist
G3 ``plate_1.gcode.md5`` is the gcode's UPPERCASE md5, no newline
G4 temperature envelope (physical safety; bridge policy, not Orca's)
G5 AMS: the gcode handshake is coherent, selects exactly the filaments
   slice_info declares, and every one of them has an AMS tray in
   ``ams_mapping`` (or a single filament on the external spool)
G6 printer: a P1S/P1P slice (``SelectMachineDialog::is_same_printer_model``)
   for the reported nozzle (``_is_same_nozzle_diameters``)

Index spaces, as Orca writes them (see tests/fixtures/orca): ``<filament id>``
is the 1-based *project* filament and only used filaments are listed;
``M620 S<n>A`` / ``M621 S<n>A`` / ``T<n>`` carry the 0-based project index.
``ams_mapping`` is Orca's v0 wire list
(``SelectMachineDialog::get_ams_mapping_result``): indexed by project filament,
value = AMS tray 0..15, -1 = not mapped. ``None`` = no start (upload only), so
no mapping check; ``[]`` or all -1 = external spool.

§6.3 ("printed air", 2026-05-19): a hand-patched file loaded ``M620 S1A`` but
finished ``M621 S0A`` while slice_info declared only filament 5 and
``ams_mapping`` was ``[1]``. G5's handshake, declared-set and mapping rules
each reject it. The printer itself started that file, so these are not
redundant with firmware checks.

Collects every issue (does not raise) so the caller can report all at once.
"""

from __future__ import annotations

import hashlib
import io
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass, field

from bambu_bridge.slicedoc.gcode import MAX_BED_C, MAX_NOZZLE_C, scan_gcode

GCODE_MEMBER = "Metadata/plate_1.gcode"
MD5_MEMBER = "Metadata/plate_1.gcode.md5"
SLICE_INFO_MEMBER = "Metadata/slice_info.config"

_MAX_MEMBERS = 4096
_MAX_EXPANDED = 512 * 1024 * 1024
_FILAMENT_ID = re.compile(r"[1-9][0-9]?")
# The CLI leaves slice_info printer_model_id empty (it reads it from
# resources/profiles/BBL/machine_full/, which Orca does not ship); the gcode
# config block always names the printer and nozzle.
_SETTING = re.compile(rb"^; (printer_model|nozzle_diameter) = ([^\r\n]*)", re.MULTILINE)
# is_same_printer_model treats P1P (C11) and P1S (C12) slices as interchangeable.
_P1 = frozenset({"C12", "C11", "Bambu Lab P1S", "Bambu Lab P1P"})


def gcode_md5(gcode: bytes) -> str:
    """The printer's md5 member format: UPPERCASE hex, no filename, no newline."""
    return hashlib.md5(gcode, usedforsecurity=False).hexdigest().upper()


@dataclass(slots=True)
class ValidationReport:
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def _read(container: bytes, r: ValidationReport) -> tuple[bytes, bytes, bytes] | None:
    try:
        with zipfile.ZipFile(io.BytesIO(container)) as zf:
            infos = zf.infolist()
            names = [i.filename for i in infos]
            if len(infos) > _MAX_MEMBERS or sum(i.file_size for i in infos) > _MAX_EXPANDED:
                r.issues.append("G1 archive exceeds the 4096-member / 512 MiB expanded limit")
            elif len(set(names)) != len(names) or any(i.flag_bits & 1 for i in infos):
                r.issues.append("G1 duplicate or encrypted archive members")
            elif missing := {GCODE_MEMBER, MD5_MEMBER, SLICE_INFO_MEMBER} - set(names):
                r.issues.append(f"G2 missing members: {sorted(missing)}")
            else:
                # read() verifies each CRC; the declared sizes are bounded above.
                return zf.read(GCODE_MEMBER), zf.read(MD5_MEMBER), zf.read(SLICE_INFO_MEMBER)
    except (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, EOFError) as exc:
        r.issues.append(f"G1 not a valid zip: {exc}")
    return None


def _check_mapping(mapping: list[int], need: list[int], r: ValidationReport) -> None:
    if len(mapping) > 16 or any(not -1 <= v <= 15 for v in mapping):
        r.issues.append(f"G5 ams_mapping {mapping} must be at most 16 trays in -1..15")
    elif all(v == -1 for v in mapping):
        if len(need) > 1:
            r.issues.append(
                f"G5 the external spool feeds one filament; slice uses {[n + 1 for n in need]}"
            )
    elif unmapped := [n + 1 for n in need if n >= len(mapping) or mapping[n] < 0]:
        r.issues.append(
            f"G5 project filament(s) {unmapped} have no AMS tray in ams_mapping {mapping}"
        )


def validate(
    container: bytes,
    *,
    expected_ams_mapping: list[int] | None = None,
    expected_nozzle: float | None = None,
) -> ValidationReport:
    r = ValidationReport()
    members = _read(container, r)
    if members is None:
        return r
    gcode, md5, slice_xml = members

    if md5 != gcode_md5(gcode).encode("ascii"):
        r.issues.append("G3 md5 member is not the gcode's UPPERCASE md5 (no newline)")

    scan = scan_gcode(gcode)
    if scan.max_nozzle_c is not None and scan.max_nozzle_c > MAX_NOZZLE_C:
        r.issues.append(f"G4 nozzle {scan.max_nozzle_c} °C > {MAX_NOZZLE_C} °C")
    if scan.max_bed_c is not None and scan.max_bed_c > MAX_BED_C:
        r.issues.append(f"G4 bed {scan.max_bed_c} °C > {MAX_BED_C} °C")

    try:
        root = ET.fromstring(slice_xml)  # noqa: S314 — expat resolves no external entities
    except ET.ParseError as exc:
        r.issues.append(f"G5 slice_info.config not parseable: {exc}")
        return r
    meta = {m.get("key"): m.get("value") or "" for m in root.iter("metadata")}
    ids = [f.get("id", "") for f in root.iter("filament")]
    if not ids or not all(_FILAMENT_ID.fullmatch(i) for i in ids):
        r.issues.append(f"G5 slice_info filament ids {ids} are not project filament numbers")
        return r
    declared = {int(i) - 1 for i in ids}

    # G5 — all in 0-based project-filament space.
    if scan.loads != scan.finishes or (scan.tools and scan.tools != scan.loads):
        r.issues.append(
            f"G5 gcode handshake incoherent: loads={sorted(scan.loads)} "
            f"finishes={sorted(scan.finishes)} tools={sorted(scan.tools)} "
            "— M620≡M621≡T violated (§6.3)"
        )
    if scan.used and scan.used != declared:
        r.issues.append(
            f"G5 gcode selects project filament(s) {sorted(n + 1 for n in scan.used)} "
            f"but slice_info declares {sorted(n + 1 for n in declared)}"
        )
    if expected_ams_mapping is not None:
        _check_mapping(expected_ams_mapping, sorted(scan.used or declared), r)

    # G6 — the slice must be for this printer and nozzle.
    settings = {k.decode(): v.decode(errors="replace").strip() for k, v in _SETTING.findall(gcode)}
    model = meta.get("printer_model_id") or settings.get("printer_model", "")
    if model not in _P1:
        r.issues.append(f"G6 slice is for {model or 'an unknown printer'!r}, not a P1S")
    if expected_nozzle is not None:
        sliced = meta.get("nozzle_diameters") or settings.get("nozzle_diameter", "")
        try:
            same = float(sliced) == float(expected_nozzle)
        except ValueError:
            same = False
        if not same:
            r.issues.append(
                f"G6 sliced nozzle {sliced or '?'} mm differs from the printer's "
                f"{expected_nozzle} mm"
            )
    return r
