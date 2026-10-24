"""Filament use per layer and per filament, read from the sliced G-code.

The bridge holds the exact file it sends, so the G-code says which filament
each layer uses. This sums net extrusion (E) by the active tool (T<n>) and the
printer's own layer marker (M73 L<n>, reported back as layer_num). Retractions
cancel their primes; material pulled back before a filament change stays on
its spool. Extrusion before the first layer marker (purge lines) counts
towards layer 1. Each filament's counts are then scaled to the slicer's own
"filament used [g]" figure, so a completed print adds up to exactly what the
slicer reported.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable

# Stamped on every frozen manifest. The value predates this reader and stays so
# manifests frozen by earlier releases keep matching.
PARSER_VERSION = "bridge-e-v1"
_WORDS = re.compile(r"([A-Z])([-+]?\d*\.?\d+)")
_HEADER = re.compile(r"^;\s*([a-z_ ]+?)\s*(?:\[(g|mm)\])?\s*[=:]\s*(.+?)\s*$", re.I)


def _numbers(text: str) -> list[float]:
    values = []
    for part in re.split(r"\s*[,;]\s*", text):
        try:
            value = float(part)
        except ValueError:
            return []
        values.append(value if math.isfinite(value) else 0.0)
    return values


def count_extrusion(
    lines: Iterable[str],
) -> tuple[dict[int, dict[int, float]], dict[str, list[float]]]:
    """({layer: {tool: net mm}}, header figures) for one plate's G-code."""
    counts: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    header: dict[str, list[float]] = {}
    relative = True  # Bambu start G-code extrudes before it states M83.
    position = 0.0
    tool = 0
    layer = 1
    for raw in lines:
        if raw[:3] in ("G1 ", "G0 ", "G2 ", "G3 "):
            # Fast path: nine lines in ten are moves; only their E word matters.
            code = raw.split(";", 1)[0]
            at = code.find("E")
            if at < 0:
                continue
            end = code.find(" ", at)
            value = float(code[at + 1 : end if end > 0 else None])
            delta = value if relative else value - position
            position = position + value if relative else value
            counts[layer][tool] += delta
            continue
        code, _, comment = raw.partition(";")
        if not code.strip():
            match = _HEADER.match(raw.strip())
            if match:
                key = match[1].strip().lower() + (f" [{match[2].lower()}]" if match[2] else "")
                if key in ("filament used [g]", "filament used [mm]", "filament_density"):
                    header.setdefault(key, _numbers(match[3]))
            continue
        word = code.split(None, 1)[0].upper()
        if word == "M83":
            relative = True
        elif word == "M82":
            relative = False
        elif word == "M73":
            params = dict(_WORDS.findall(code.upper()[3:]))
            if "L" in params:
                layer = max(1, int(float(params["L"])))
        elif word[0] == "T" and word[1:].isdigit():
            number = int(word[1:])
            if number < 255:  # T255 unloads; T1000/T1100 are Bambu pseudo-tools
                tool = number
        elif word == "G92":
            params = dict(_WORDS.findall(code.upper()[3:]))
            if "E" in params:
                position = float(params["E"])
        elif word in ("G0", "G1", "G2", "G3"):
            params = dict(_WORDS.findall(code.upper()[len(word) :]))
            if "E" in params:
                value = float(params["E"])
                delta = value if relative else value - position
                position = position + value if relative else value
                counts[layer][tool] += delta
    return {k: dict(v) for k, v in counts.items()}, header


def layer_grams(lines: Iterable[str]) -> list[dict[str, float]]:
    """Grams per layer per filament: index 0 is layer 1; keys are filament numbers."""
    return layer_plan(lines)[0]


def layer_plan(lines: Iterable[str]) -> tuple[list[dict[str, float]], dict[str, list[float]]]:
    """(grams per layer per filament, slicer header figures).

    Scaled to the slicer's per-filament grams when the header has them.
    """
    counts, header = count_extrusion(lines)
    if not counts:
        return [], header
    totals: dict[int, float] = defaultdict(float)
    for by_tool in counts.values():
        for tool, mm in by_tool.items():
            totals[tool] += mm
    slicer = header.get("filament used [g]") or []
    density = header.get("filament_density") or []
    scale: dict[int, float] = {}
    for tool, mm in totals.items():
        if mm <= 0:
            continue
        if tool < len(slicer) and slicer[tool] > 0:
            scale[tool] = slicer[tool] / mm
        elif tool < len(density) and density[tool] > 0:
            # 1.75 mm filament: cross-section 2.405 mm², grams = mm³ · g/cm³ / 1000.
            scale[tool] = math.pi * 0.875**2 * density[tool] / 1000
    # A filament with neither figure cannot be weighed; it is named so the
    # print is noted at the end instead of silently counted short.
    header["unweighed_filaments"] = [
        float(t) for t in sorted(totals) if totals[t] > 0 and t not in scale
    ]
    last = max(counts)
    plan = []
    for layer in range(1, last + 1):
        row = {
            str(tool): round(mm * scale[tool], 4)
            for tool, mm in (counts.get(layer) or {}).items()
            if tool in scale and round(mm * scale[tool], 4)
        }
        plan.append(row)
    return plan, header
