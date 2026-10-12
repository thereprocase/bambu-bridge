"""Scan sliced G-code for the two things the bridge gates on.

* The temperature envelope (bridge safety policy).
* The AMS handshake. OrcaSlicer / Bambu Studio write each filament change as
  ``M620 S<n>A`` (load) … ``T<n>`` … ``M621 S<n>A`` (finish), where ``n`` is the
  0-based *project* filament index and ``A`` tells the firmware to remap it
  through the start command's ``ams_mapping``. The real-hardware §6.3 failure
  (2026-05-19) was a hand-patched file whose ``M620 S1A`` disagreed with
  ``M621 S0A``: the AMS never engaged and the printer extruded air.

Orca indents the first load inside the start G-code (``    T[initial_extruder]``,
``    M109 S…``), so every pattern allows leading whitespace. Indices of 64 and
above are not filament binds: ``255`` is the external-spool unload sentinel and
``T1000``/``T1100`` are flush pseudo-tools. ``M620.1``/``M620.11``/``M620 M`` do
not match.

Pure: bytes in, dataclass out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Physical-safety envelope.
MAX_NOZZLE_C = 280
MAX_BED_C = 100  # P1S manufacturer limit; other adapters require separate policy.

_FILAMENTS = 64  # Orca's project filament limit; larger indices are sentinels
_SELECT_RE = re.compile(rb"^[ \t]*(M620|M621)[ \t]+S(\d+)A?\b", re.MULTILINE)
_TOOL_RE = re.compile(rb"^[ \t]*T(\d+)(?=[ \t;\r\n]|$)", re.MULTILINE)
_NOZZLE_RE = re.compile(rb"^[ \t]*M10[49][ \t]+S(\d+)", re.MULTILINE)
_BED_RE = re.compile(rb"^[ \t]*M1[49]0[ \t]+S(\d+)", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class GcodeScan:
    loads: frozenset[int]  # M620 S<n>A, 0-based project filament indices
    finishes: frozenset[int]  # M621 S<n>A
    tools: frozenset[int]  # T<n>
    max_nozzle_c: int | None
    max_bed_c: int | None

    @property
    def used(self) -> frozenset[int]:
        """Every project filament the handshake references."""
        return self.loads | self.finishes | self.tools


def scan_gcode(gcode: bytes) -> GcodeScan:
    selects = [(cmd, int(n)) for cmd, n in _SELECT_RE.findall(gcode)]
    nozzles = [int(m) for m in _NOZZLE_RE.findall(gcode)]
    beds = [int(m) for m in _BED_RE.findall(gcode)]
    return GcodeScan(
        loads=frozenset(n for cmd, n in selects if cmd == b"M620" and n < _FILAMENTS),
        finishes=frozenset(n for cmd, n in selects if cmd == b"M621" and n < _FILAMENTS),
        tools=frozenset(n for n in map(int, _TOOL_RE.findall(gcode)) if n < _FILAMENTS),
        max_nozzle_c=max(nozzles) if nozzles else None,
        max_bed_c=max(beds) if beds else None,
    )
