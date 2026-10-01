"""Build the ``project_file`` MQTT command that starts an uploaded ``.gcode.3mf``.

The accepted form is `REPORT.md` §6.2 verbatim. Two corrections vs. the old
`service/jobs.py`:

* ``param = "Metadata/plate_1.gcode"`` (not the gcode filename)
* ``url  = "file:///sdcard/<name>.gcode.3mf"`` — the **only confirmed**
  scheme. The old code used ``ftp://`` which `REPORT.md` §9 explicitly flags
  as *untested*.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from bambu_bridge.slicedoc.validate import GCODE_MEMBER


def sd_filename(name: str) -> str:
    """The on-SD-card name. Stored at the card root, not ``model/``.

    Reduced to its last path component, as FtpsTransfer stores it, so the
    project_file url and subtask name address the file that was uploaded.
    """
    stem = PurePosixPath(name.replace("\\", "/")).name or "upload"
    for suffix in (".gcode.3mf", ".3mf", ".gcode"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return f"{stem}.gcode.3mf"


def subtask_name(name: str) -> str:
    """The subtask_name the printer reports while printing ``name``.

    Bridge rows store the uploaded file name and external rows the reported
    subtask itself; both reduce to the same stem.
    """
    return sd_filename(name)[: -len(".gcode.3mf")]


def sd_url(name: str) -> str:
    """The confirmed ``project_file`` url scheme (REPORT §6.2)."""
    return f"file:///sdcard/{sd_filename(name)}"


def project_file_command(
    name: str,
    *,
    use_ams: bool,
    ams_mapping: list[int],
    bed_type: str = "textured_plate",
    bed_leveling: bool = True,
    flow_cali: bool = False,
    vibration_cali: bool = True,
    layer_inspect: bool = False,
    timelapse: bool = False,
) -> dict[str, Any]:
    """The ``param``/fields for ``service.send_command("print",
    "project_file", **fields)`` — ``command``/``sequence_id`` are added by the
    protocol layer. ``ams_mapping`` is the list the container was validated
    against."""
    stem = subtask_name(name)
    return {
        "param": GCODE_MEMBER,
        "url": sd_url(name),
        "subtask_name": stem,
        "use_ams": use_ams,
        "ams_mapping": ams_mapping,
        "timelapse": timelapse,
        "bed_leveling": bed_leveling,
        "flow_cali": flow_cali,
        "vibration_cali": vibration_cali,
        "layer_inspect": layer_inspect,
        "bed_type": bed_type,
        "project_id": "0",
        "profile_id": "0",
        "task_id": "0",
        "subtask_id": "0",
    }
