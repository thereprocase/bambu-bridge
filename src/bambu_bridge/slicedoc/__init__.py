"""slicedoc — validate a sliced Bambu ``.gcode.3mf`` and build its start command.

``validate`` gates the bytes before upload (see validate.py for the gates and
the §6.3 "printed air" history); ``project_file_command`` builds the MQTT
start for the validated file.

Pure: no FastAPI, no MQTT, no I/O. ``service/jobs.py`` orchestrates; this
decides what is correct.
"""

from __future__ import annotations

from bambu_bridge.slicedoc.command import project_file_command, sd_filename, sd_url, subtask_name
from bambu_bridge.slicedoc.gcode import GcodeScan, scan_gcode
from bambu_bridge.slicedoc.validate import ValidationReport, gcode_md5, validate

__all__ = [
    "GcodeScan",
    "ValidationReport",
    "gcode_md5",
    "project_file_command",
    "scan_gcode",
    "sd_filename",
    "sd_url",
    "subtask_name",
    "validate",
]
