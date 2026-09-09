"""Gate a finished ``.gcode.3mf`` before it is ever uploaded.

Defense in depth: :func:`synthesize` already makes an inconsistent container
unconstructable, but a job may also be handed a `.gcode.3mf` built elsewhere
(a real slicer, a user upload). This re-derives the §6.3 invariant *from the
bytes*, **source-agnostically**, so the "printed air" config is caught no
matter how the file was produced — and a genuinely self-consistent slicer
container (no donor thumbnails, no Bambu ``filament_maps``) is *not* rejected.

Aragorn §5 gates 1–5:

1. zip integrity
2. the three members the gate reads are present (NOT the donor's full
   15-member layout — a real OrcaSlicer container legitimately differs)
3. md5 contract (UPPERCASE, no newline, matches the gcode)
4. temperature envelope (≤ 280 °C nozzle / ≤ 120 °C bed) — physical safety
5. AMS consistency in slice-filament-index space: <filament> arity ≡
   len(ams_mapping); gcode handshake coherent (M620≡M621≡T) and only
   referencing filaments the slice defines. ams_mapping *values* are the
   physical remap and are deliberately not constrained here.

Collects every issue (does not raise) so the caller can report all at once.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field

from bambu_bridge.slicedoc.container import (
    GATE_REQUIRED_MEMBERS,
    GCODE_MEMBER,
    MD5_MEMBER,
    SLICE_INFO_MEMBER,
    gcode_md5,
)
from bambu_bridge.slicedoc.errors import ContainerError
from bambu_bridge.slicedoc.gcode import MAX_BED_C, MAX_NOZZLE_C, scan_gcode


@dataclass(slots=True)
class ValidationReport:
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def raise_for_issues(self) -> None:
        if self.issues:
            raise ContainerError("; ".join(self.issues))


def _slice_info_arity(xml_bytes: bytes) -> tuple[int, int, set[int]]:
    """``(filament_maps token count, <filament> count, filament_list idxs)``."""
    root = ET.fromstring(xml_bytes)  # noqa: S314 — our own generated XML
    maps_tokens = 0
    for md in root.iter("metadata"):
        if md.get("key") == "filament_maps":
            maps_tokens = len((md.get("value") or "").split())
    filament_count = sum(1 for _ in root.iter("filament"))
    fl_idxs = {int(lfl.get("filament_list") or -1) for lfl in root.iter("layer_filament_list")}
    return maps_tokens, filament_count, fl_idxs


def validate(
    container: bytes, *, expected_ams_mapping: list[int] | None = None
) -> ValidationReport:
    r = ValidationReport()

    # G1 — zip integrity
    try:
        zf = zipfile.ZipFile(io.BytesIO(container))
    except zipfile.BadZipFile as exc:
        r.issues.append(f"G1 not a valid zip: {exc}")
        return r
    with zf:
        bad = zf.testzip()
        if bad is not None:
            r.issues.append(f"G1 corrupt member: {bad}")
        names = set(zf.namelist())

        # G2 — required members. Only the three the gate actually reads; a
        # real slicer container legitimately omits the donor's thumbnails.
        missing = GATE_REQUIRED_MEMBERS - names
        if missing:
            r.issues.append(f"G2 missing members: {sorted(missing)}")

        gcode = zf.read(GCODE_MEMBER) if GCODE_MEMBER in names else b""
        md5_raw = zf.read(MD5_MEMBER) if MD5_MEMBER in names else b""
        slice_xml = zf.read(SLICE_INFO_MEMBER) if SLICE_INFO_MEMBER in names else b""

    # G3 — md5 contract
    if gcode and md5_raw:
        want = gcode_md5(gcode)
        got = md5_raw.decode("ascii", "replace")
        if got != got.strip():
            r.issues.append("G3 md5 has surrounding whitespace/newline")
        if got.strip() != got.strip().upper():
            r.issues.append("G3 md5 is not UPPERCASE")
        if got.strip() != want:
            r.issues.append(f"G3 md5 mismatch: file={got.strip()!r} expected={want!r}")
    elif GCODE_MEMBER in GATE_REQUIRED_MEMBERS and not r.issues:
        r.issues.append("G3 cannot verify md5 (gcode or md5 member absent)")

    # G4 — temperature envelope
    if gcode:
        scan = scan_gcode(gcode)
        if scan.max_nozzle_c is not None and scan.max_nozzle_c > MAX_NOZZLE_C:
            r.issues.append(f"G4 nozzle {scan.max_nozzle_c} °C > {MAX_NOZZLE_C} °C")
        if scan.max_bed_c is not None and scan.max_bed_c > MAX_BED_C:
            r.issues.append(f"G4 bed {scan.max_bed_c} °C > {MAX_BED_C} °C")

    # G5 — AMS consistency (the §6.3 invariant, re-derived from bytes).
    #
    # Reasoned in *slice-filament-index* space — the space the printer
    # actually uses. gcode M620/M621/T carry slice-filament indices (0-based);
    # ``ams_mapping`` is what remaps each to a *physical* AMS tray. The old
    # gate compared gcode S-numbers directly to ams_mapping values, conflating
    # the two — which rejected the hardware-correct print #4 slice (gcode S0,
    # ams_mapping [1]). The real invariants are arity + range + handshake
    # coherence, none of which constrain the physical ams_mapping *values*.
    fil_n: int | None = None
    if slice_xml:
        try:
            maps_n, fil_n, fl_idxs = _slice_info_arity(slice_xml)
        except ET.ParseError as exc:
            r.issues.append(f"G5 slice_info.config not parseable: {exc}")
            fil_n = None
        else:
            # ``filament_maps`` is a Bambu-Studio key; OrcaSlicer omits it.
            # It is a *cross-check when present* (a donor whose maps list
            # disagreed with its <filament> records is the original §6.3
            # trap) — its absence is not a defect.
            if maps_n and maps_n != fil_n:
                r.issues.append(
                    f"G5 filament arity mismatch: filament_maps has "
                    f"{maps_n} slots but {fil_n} <filament> record(s) "
                    "(the §6.3 trap)"
                )
            for idx in fl_idxs:
                if not (0 <= idx < max(fil_n, 1)):
                    r.issues.append(
                        f"G5 layer_filament_list filament_list={idx} " f"outside 0..{fil_n - 1}"
                    )
            if expected_ams_mapping is not None and len(expected_ams_mapping) != fil_n:
                r.issues.append(
                    f"G5 ams_mapping arity {len(expected_ams_mapping)} != "
                    f"slice_info <filament> count {fil_n}"
                )

    if gcode:
        gs = scan_gcode(gcode)
        # (a) Internal handshake coherence — M620(load) ≡ M621(finish) ≡
        # T(tool). THIS is the actual 2026-05-19 recurrence (M620 S1A vs
        # M621 S0A). Source-agnostic, independent of ams_mapping.
        present = [s for s in (gs.load_trays, gs.finish_trays, gs.tool_trays) if s]
        if len({frozenset(s) for s in present}) > 1:
            r.issues.append(
                f"G5 gcode handshake incoherent: "
                f"loads={sorted(gs.load_trays)} "
                f"finishes={sorted(gs.finish_trays)} "
                f"tools={sorted(gs.tool_trays)} — M620≡M621≡T violated "
                f"(§6.3/2026-05-19)"
            )
        # (b) Exactly one coherent bind per slice filament. Source-agnostic
        # by *count*, not value: a real slicer leaves slice-filament indices
        # (the printer remaps them physically via ams_mapping); the donor
        # synthesize path pre-bakes the physical tray. Either way a
        # self-consistent N-filament slice binds N distinct trays — the
        # §6.3 trap binds a different number (and (a) already flags the
        # split handshake). We deliberately do not constrain *which*
        # numbers, only that the arity matches.
        if fil_n is not None and gs.bound_trays and len(gs.bound_trays) != fil_n:
            r.issues.append(
                f"G5 gcode binds {len(gs.bound_trays)} distinct tray(s) "
                f"{sorted(gs.bound_trays)} but slice defines {fil_n} "
                f"filament(s) (§6.3: one coherent bind per filament)"
            )
    return r
