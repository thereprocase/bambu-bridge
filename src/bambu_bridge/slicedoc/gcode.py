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
MAX_NOZZLE_C = 300  # P1S manufacturer nozzle limit; owner approved 2026-10-17.
MAX_BED_C = 100  # P1S manufacturer limit; other adapters require separate policy.

_FILAMENTS = 64  # Orca's project filament limit; larger indices are sentinels
_SELECT_RE = re.compile(rb"^[ \t]*(M620|M621)[ \t]+S(\d+)A?\b", re.MULTILINE)
_TOOL_RE = re.compile(rb"^[ \t]*T(\d+)(?=[ \t;\r\n]|$)", re.MULTILINE)
# Heater lines: optional ``N<line number>``, the command, then any words up to a
# ``;`` comment. Firmware reads every S word on the line, so the scan does too:
# any letter case, other words first (``T0``, ``P0``), space after the letter,
# an explicit ``+``/``-`` sign and exponent notation (``S3.5e2`` is 350).
_HEATER_RE = re.compile(
    rb"^[ \t]*(?:N[ \t]*\d+[ \t]*)?M(104|109|140|190)(?!\d)([^;\r\n]*)",
    re.MULTILINE | re.IGNORECASE,
)
_S_WORD_RE = re.compile(
    rb"(?<![A-Za-z])S[ \t]*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)", re.IGNORECASE
)
_NOZZLE_CMDS = frozenset({b"104", b"109"})


@dataclass(frozen=True, slots=True)
class GcodeScan:
    loads: frozenset[int]  # M620 S<n>A, 0-based project filament indices
    finishes: frozenset[int]  # M621 S<n>A
    tools: frozenset[int]  # T<n>
    max_nozzle_c: float | None
    max_bed_c: float | None

    @property
    def used(self) -> frozenset[int]:
        """Every project filament the handshake references."""
        return self.loads | self.finishes | self.tools


def scan_gcode(gcode: bytes) -> GcodeScan:
    selects = [(cmd, int(n)) for cmd, n in _SELECT_RE.findall(gcode)]
    nozzles: list[float] = []
    beds: list[float] = []
    for cmd, args in _HEATER_RE.findall(gcode):
        target = nozzles if cmd in _NOZZLE_CMDS else beds
        target.extend(float(m) for m in _S_WORD_RE.findall(args))
    return GcodeScan(
        loads=frozenset(n for cmd, n in selects if cmd == b"M620" and n < _FILAMENTS),
        finishes=frozenset(n for cmd, n in selects if cmd == b"M621" and n < _FILAMENTS),
        tools=frozenset(n for n in map(int, _TOOL_RE.findall(gcode)) if n < _FILAMENTS),
        max_nozzle_c=max(nozzles) if nozzles else None,
        max_bed_c=max(beds) if beds else None,
    )
