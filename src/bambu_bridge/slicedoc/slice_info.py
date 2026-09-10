"""Render ``Metadata/slice_info.config``.

Schema is reproduced from the real artifact
(`probes/3DBenchy_PETG_slot2.gcode.3mf`). Everything that encodes the AMS
binding — ``filament_maps``, ``limit_filament_maps``, the ``<filament>``
records, ``layer_filament_list`` — is *derived from the* :class:`FeedPlan`,
never passed in independently. That is the §6.3 fix: there is no way to render
a ``slice_info.config`` whose filament arity disagrees with the plan, because
the renderer never sees an arity that isn't the plan's.
"""

from __future__ import annotations

from dataclasses import dataclass

from bambu_bridge.slicedoc.errors import SliceConsistencyError
from bambu_bridge.slicedoc.feed import FeedPlan


@dataclass(frozen=True, slots=True)
class PlateInfo:
    """Plate-level metadata. Defaults match an observed P1S 0.4 mm slice."""

    printer_model_id: str  # "C12" for P1S
    total_layers: int
    prediction_s: int  # estimated print seconds
    weight_g: float
    first_layer_time_s: float
    object_id: int
    object_name: str  # e.g. "3DBenchy.drc"
    index: int = 1
    nozzle_diameters: str = "0.4"
    extruder_type: str = "0"
    nozzle_volume_type: str = "0"
    timelapse_type: str = "0"
    outside: bool = False
    support_used: bool = False
    label_object_enabled: bool = True

    def __post_init__(self) -> None:
        if self.total_layers < 1:
            raise SliceConsistencyError(
                f"total_layers must be >= 1, got {self.total_layers}"
            )


def _esc(value: str) -> str:
    """Escape for an XML double-quoted attribute."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _b(value: bool) -> str:
    return "true" if value else "false"


def _num(value: float) -> str:
    """Drop the trailing ``.0`` on integral floats; otherwise plain str."""
    return str(int(value)) if value == int(value) else str(value)


def render_slice_info(
    plate: PlateInfo,
    feed: FeedPlan,
    *,
    layer_lists: list[tuple[int, str]] | None = None,
) -> str:
    """Build the ``slice_info.config`` XML string.

    ``layer_lists`` is ``[(filament_list_index, "<start> <end>"), …]`` — the
    0-based filament index used over each (inclusive) layer range. Defaults
    for a single-filament print to the whole range on filament 0 (the
    print-blocker case). Multi-filament requires it explicitly.
    """
    filaments = feed.slice_filaments()
    if layer_lists is None:
        if len(filaments) != 1:
            raise SliceConsistencyError(
                "multi-filament slice_info needs explicit layer_lists "
                f"({len(filaments)} filaments)"
            )
        layer_lists = [(0, f"0 {plate.total_layers - 1}")]
    for fl_idx, _ in layer_lists:
        if not (0 <= fl_idx < len(filaments)):
            raise SliceConsistencyError(
                f"layer_filament_list index {fl_idx} outside "
                f"0..{len(filaments) - 1} (filament arity)"
            )

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<config>",
        "  <header>",
        '    <header_item key="X-BBL-Client-Type" value="slicer"/>',
        '    <header_item key="X-BBL-Client-Version" value=""/>',
        "  </header>",
        "  <plate>",
        f'    <metadata key="index" value="{plate.index}"/>',
        f'    <metadata key="extruder_type" value="{plate.extruder_type}"/>',
        '    <metadata key="nozzle_volume_type" '
        f'value="{plate.nozzle_volume_type}"/>',
        '    <metadata key="printer_model_id" '
        f'value="{_esc(plate.printer_model_id)}"/>',
        '    <metadata key="nozzle_diameters" '
        f'value="{_esc(plate.nozzle_diameters)}"/>',
        f'    <metadata key="timelapse_type" value="{plate.timelapse_type}"/>',
        f'    <metadata key="prediction" value="{plate.prediction_s}"/>',
        f'    <metadata key="weight" value="{_num(plate.weight_g)}"/>',
        '    <metadata key="first_layer_time" '
        f'value="{_num(plate.first_layer_time_s)}"/>',
        f'    <metadata key="outside" value="{_b(plate.outside)}"/>',
        f'    <metadata key="support_used" value="{_b(plate.support_used)}"/>',
        '    <metadata key="label_object_enabled" '
        f'value="{_b(plate.label_object_enabled)}"/>',
        '    <metadata key="filament_maps" '
        f'value="{feed.filament_maps_value()}"/>',
        '    <metadata key="limit_filament_maps" '
        f'value="{feed.limit_filament_maps_value()}"/>',
        f'    <object identify_id="{plate.object_id}" '
        f'name="{_esc(plate.object_name)}" skipped="false" />',
    ]
    for fid, fil in filaments:
        lines.append(
            f'    <filament id="{fid}" '
            f'tray_info_idx="{_esc(fil.tray_info_idx)}" '
            f'type="{_esc(fil.material)}" color="{_esc(fil.color)}" '
            f'used_m="{_num(fil.used_m)}" used_g="{_num(fil.used_g)}" />'
        )
    lines.append("    <layer_filament_lists>")
    for fl_idx, ranges in layer_lists:
        lines.append(
            f'      <layer_filament_list filament_list="{fl_idx}" '
            f'layer_ranges="{_esc(ranges)}" />'
        )
    lines.append("    </layer_filament_lists>")
    lines.append("  </plate>")
    lines.append("</config>")
    return "\n".join(lines) + "\n"
