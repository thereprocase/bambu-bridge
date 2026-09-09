"""slicedoc — synthesize and validate a self-consistent Bambu ``.gcode.3mf``.

The product's actual moat (SYNTHESIS §2.4): no public tool generates a
*self-consistent* ``slice_info.config``; everyone else consumes a slicer's
output and hopes. This package makes the §6.3 "printed air" failure —
``slice_info`` filament arity vs ``ams_mapping`` vs gcode ``M620`` selectors
disagreeing — a *construction-time impossibility*, then double-checks the
finished bytes (Aragorn gates 1–5), then builds the launching ``project_file``
command from the very same plan so it cannot drift.

Pure: no FastAPI, no MQTT, no I/O. ``service/jobs.py`` orchestrates; this
decides what is correct.
"""

from __future__ import annotations

from bambu_bridge.slicedoc.command import (
    build_project_file_command,
    project_file_command,
    sd_filename,
    sd_url,
)
from bambu_bridge.slicedoc.container import (
    StaticMembers,
    gcode_md5,
    read_member,
    synthesize,
)
from bambu_bridge.slicedoc.errors import (
    ContainerError,
    SliceConsistencyError,
    SlicedocError,
    TemperatureEnvelopeError,
)
from bambu_bridge.slicedoc.feed import (
    AmsFeed,
    AmsFilament,
    ExternalSpoolFeed,
    FeedPlan,
    Filament,
)
from bambu_bridge.slicedoc.gcode import (
    GcodeScan,
    normalize_ams_selectors,
    scan_gcode,
)
from bambu_bridge.slicedoc.slice_info import PlateInfo, render_slice_info
from bambu_bridge.slicedoc.validate import ValidationReport, validate

__all__ = [
    "AmsFeed",
    "AmsFilament",
    "ContainerError",
    "ExternalSpoolFeed",
    "FeedPlan",
    "Filament",
    "GcodeScan",
    "PlateInfo",
    "SliceConsistencyError",
    "SlicedocError",
    "StaticMembers",
    "TemperatureEnvelopeError",
    "ValidationReport",
    "build_project_file_command",
    "gcode_md5",
    "normalize_ams_selectors",
    "project_file_command",
    "read_member",
    "render_slice_info",
    "scan_gcode",
    "sd_filename",
    "sd_url",
    "synthesize",
    "validate",
]
