"""What a print used, per slot, counted layer by layer.

The bridge sends the exact G-code and the filament-to-slot mapping, so the
dispatch manifest holds the grams each layer used per filament (from
gcode_usage) and which slot feeds each filament. The printer reports the
layer it is on, and an AMS auto-refill moves a filament to another slot at a
known layer. A completed print counts every layer; a stopped print counts the
layers up to the one it reached, that one by half. Nothing else is guessed:
a manifest without a layer plan counts nothing.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def slot_usage(
    manifest: dict[str, Any],
    layer_reached: int | None,
    switches: list[tuple[int, int, int]] | None = None,
) -> dict[int, float]:
    """Grams per slot (wire). layer_reached None means the print completed.

    `switches` are AMS auto-refills: (from wire, to wire, layer).
    """
    plan = manifest.get("layer_grams") or []
    wires = {str(k): v for k, v in (manifest.get("tool_wires") or {}).items()}
    if not plan or not wires:
        return {}
    last = len(plan) if layer_reached is None else min(max(layer_reached, 0), len(plan))
    moves = sorted(switches or [], key=lambda s: s[2])
    used: dict[int, float] = defaultdict(float)
    for layer in range(1, last + 1):
        part = 0.5 if layer_reached is not None and layer == last else 1.0
        for tool, grams in plan[layer - 1].items():
            wire = wires.get(tool)
            if wire is None:
                continue
            share = part  # per filament: a switch on one never shrinks another
            for from_wire, to_wire, at in moves:
                if wire == from_wire and at <= layer:
                    if at == layer:  # the switch layer is split between both slots
                        used[wire] += grams * share / 2
                        share /= 2
                    wire = to_wire
            used[wire] += grams * share
    return {wire: round(grams, 2) for wire, grams in used.items() if round(grams, 2) > 0}


def slot_layers(
    manifest: dict[str, Any],
    layer_reached: int | None,
    switches: list[tuple[int, int, int]] | None = None,
) -> dict[int, tuple[int, int]]:
    """(first, last) layer each slot (wire) fed, with the same refill handling as slot_usage.

    The switch layer belongs to both slots. layer_reached None means completed.
    """
    plan = manifest.get("layer_grams") or []
    wires = {str(k): v for k, v in (manifest.get("tool_wires") or {}).items()}
    if not plan or not wires:
        return {}
    last = len(plan) if layer_reached is None else min(max(layer_reached, 0), len(plan))
    moves = sorted(switches or [], key=lambda s: s[2])
    ranges: dict[int, tuple[int, int]] = {}

    def touch(wire: int, layer: int) -> None:
        first, final = ranges.get(wire, (layer, layer))
        ranges[wire] = (min(first, layer), max(final, layer))

    for layer in range(1, last + 1):
        for tool, grams in plan[layer - 1].items():
            wire = wires.get(tool)
            if wire is None or not grams:
                continue
            for from_wire, to_wire, at in moves:
                if wire == from_wire and at <= layer:
                    if at == layer:
                        touch(wire, layer)
                    wire = to_wire
            touch(wire, layer)
    return ranges
