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
from dataclasses import dataclass

# Physical-safety envelope (design review §5 gates 4/5).
MAX_NOZZLE_C = 280
MAX_BED_C = 100  # P1S manufacturer limit; other adapters require separate policy.

EXTERNAL_SENTINEL = 255  # M620 S255 / T255 = external/virtual spool, not a bind
_MAX_REAL_TRAY = 15  # 4 AMS units × 4 trays; >this = flush/external pseudo-tool

# Anchored at real line start (re.MULTILINE) — comment lines (";…") and the
# single-line embedded machine_start_gcode template never match.
_LOAD_RE = re.compile(rb"^M620[ \t]+S(\d+)A?\b", re.MULTILINE)
_FINISH_RE = re.compile(rb"^M621[ \t]+S(\d+)A?\b", re.MULTILINE)
_TOOL_RE = re.compile(rb"^T(\d+)(?=[ \t;\r\n]|$)", re.MULTILINE)
_NOZZLE_RE = re.compile(rb"^M10[49][ \t]+S(\d+)", re.MULTILINE)
_BED_RE = re.compile(rb"^M1[49]0[ \t]+S(\d+)", re.MULTILINE)


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
