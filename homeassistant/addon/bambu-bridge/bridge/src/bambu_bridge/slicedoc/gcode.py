"""Read — and normalize — the AMS binding in sliced gcode.

The firmware binds an AMS tray from a *handshake* in the gcode:
``M620 S<n>A`` (load) … ``M621 S<n>A`` (finish) plus the initial ``T<n>``
tool select. **All of these must reference the same physical tray.** The
real-hardware §6.3 recurrence (2026-05-19) was caused by a hand-patched slice
whose ``M620 S1A`` (load tray 1) disagreed with ``M621 S0A`` (finish tray 0):
the AMS never engaged, the printer extruded air for 43 layers,
``print_error:50348044``.

So this module does two jobs:

* :func:`scan_gcode` — extract loads / finishes / real tool selects (and the
  temperature envelope). ``M620.1``/``M620.11`` (calibration), ``M620 M``,
  flush pseudo-tools ``T1000``/``T1100`` and the ``255`` external sentinel are
  *not* tray binds and are excluded.
* :func:`normalize_ams_selectors` — rewrite every executable real-tray
  ``M620 S<n>A`` / ``M621 S<n>A`` / ``T<n>`` to a single bound tray, so the
  handshake is internally coherent. Comment/metadata lines, the ``255``
  sentinels, calibration and flush tools are left untouched. Self-consistency
  becomes a *post-rewrite* invariant.

Pure: bytes in, bytes/dataclass out. No printer, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from bambu_bridge.slicedoc.errors import (
    SliceConsistencyError,
    TemperatureEnvelopeError,
)

# Physical-safety envelope (Aragorn §5 gates 4/5).
MAX_NOZZLE_C = 280
MAX_BED_C = 120

EXTERNAL_SENTINEL = 255  # M620 S255 / T255 = external/virtual spool, not a bind
_MAX_REAL_TRAY = 15  # 4 AMS units × 4 trays; >this = flush/external pseudo-tool

# Anchored at real line start (re.MULTILINE) — comment lines (";…") and the
# single-line embedded machine_start_gcode template never match.
_LOAD_RE = re.compile(rb"^M620[ \t]+S(\d+)A?\b", re.MULTILINE)
_FINISH_RE = re.compile(rb"^M621[ \t]+S(\d+)A?\b", re.MULTILINE)
_TOOL_RE = re.compile(rb"^T(\d+)(?=[ \t;\r\n]|$)", re.MULTILINE)
_NOZZLE_RE = re.compile(rb"^M10[49][ \t]+S(\d+)", re.MULTILINE)
_BED_RE = re.compile(rb"^M1[49]0[ \t]+S(\d+)", re.MULTILINE)

# Rewrite forms: capture the lead, the digits, the tail so only digits change.
_LOAD_SUB = re.compile(rb"(?m)^(M620[ \t]+S)(\d+)(A)")
_FINISH_SUB = re.compile(rb"(?m)^(M621[ \t]+S)(\d+)(A)")
_TOOL_SUB = re.compile(rb"(?m)^(T)(\d+)(?=[ \t;\r\n]|$)")


def _real(values: list[int]) -> frozenset[int]:
    """Keep only real AMS tray indices (drop 255 / flush pseudo-tools)."""
    return frozenset(v for v in values if 0 <= v <= _MAX_REAL_TRAY)


@dataclass(frozen=True, slots=True)
class GcodeScan:
    load_trays: frozenset[int]  # real M620 S<n>A loads (255 excluded)
    finish_trays: frozenset[int]  # real M621 S<n>A finishes
    tool_trays: frozenset[int]  # real ``T<n>`` selects (flush/255 excluded)
    has_external: bool  # any S255/T255 seen (end-gcode unload, expected)
    max_nozzle_c: int | None
    max_bed_c: int | None

    @property
    def bound_trays(self) -> frozenset[int]:
        """Every real tray the handshake actually references."""
        return self.load_trays | self.finish_trays | self.tool_trays


def scan_gcode(gcode: bytes) -> GcodeScan:
    loads = [int(m) for m in _LOAD_RE.findall(gcode)]
    finishes = [int(m) for m in _FINISH_RE.findall(gcode)]
    tools = [int(m) for m in _TOOL_RE.findall(gcode)]
    nozzles = [int(m) for m in _NOZZLE_RE.findall(gcode)]
    beds = [int(m) for m in _BED_RE.findall(gcode)]
    return GcodeScan(
        load_trays=_real(loads),
        finish_trays=_real(finishes),
        tool_trays=_real(tools),
        has_external=any(
            v == EXTERNAL_SENTINEL for v in loads + finishes + tools
        ),
        max_nozzle_c=max(nozzles) if nozzles else None,
        max_bed_c=max(beds) if beds else None,
    )


def assert_temperature_envelope(scan: GcodeScan) -> None:
    """Refuse a slice that commands a physically unsafe temperature."""
    if scan.max_nozzle_c is not None and scan.max_nozzle_c > MAX_NOZZLE_C:
        raise TemperatureEnvelopeError(
            f"nozzle {scan.max_nozzle_c} °C exceeds the {MAX_NOZZLE_C} °C "
            "safety envelope"
        )
    if scan.max_bed_c is not None and scan.max_bed_c > MAX_BED_C:
        raise TemperatureEnvelopeError(
            f"bed {scan.max_bed_c} °C exceeds the {MAX_BED_C} °C safety "
            "envelope"
        )


def normalize_ams_selectors(
    gcode: bytes, tray: int
) -> tuple[bytes, list[tuple[int, str, str]]]:
    """Rewrite every executable real-tray selector to ``tray``.

    Returns ``(new_gcode, changes)`` where ``changes`` is
    ``[(line_no, old, new), …]`` for the forensic audit trail. Single-tray
    only — for one physical filament every load/finish/tool must point at the
    one bound tray (the §6.3 contract). ``255`` sentinels (end-gcode unload),
    ``M620.1``/``M620 M`` and flush ``T1000``/``T1100`` are preserved.
    """
    if not (0 <= tray <= _MAX_REAL_TRAY):
        raise SliceConsistencyError(
            f"bound tray {tray} out of range 0..{_MAX_REAL_TRAY}"
        )
    want = str(tray).encode("ascii")
    changes: list[tuple[int, str, str]] = []

    def repl(m: re.Match[bytes]) -> bytes:
        whole = m.group(0)
        n = int(m.group(2))  # group 2 is always the digits
        if not (0 <= n <= _MAX_REAL_TRAY) or n == tray:
            return whole  # 255 / flush / already-correct: untouched
        tail = m.group(3) if m.re.groups >= 3 else b""
        new = m.group(1) + want + tail
        line_no = gcode.count(b"\n", 0, m.start()) + 1
        changes.append(
            (
                line_no,
                whole.decode("ascii", "replace"),
                new.decode("ascii", "replace"),
            )
        )
        return new

    repl_fn: Callable[[re.Match[bytes]], bytes] = repl
    out = _LOAD_SUB.sub(repl_fn, gcode)
    out = _FINISH_SUB.sub(repl_fn, out)
    out = _TOOL_SUB.sub(repl_fn, out)

    after = scan_gcode(out)
    stray = after.bound_trays - {tray}
    if stray:
        raise SliceConsistencyError(
            f"normalize left stray tray refs {sorted(stray)} (expected only "
            f"{tray}) — gcode shape unsupported, not normalizing blindly"
        )
    return out, changes
