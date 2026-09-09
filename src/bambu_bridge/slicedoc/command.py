"""Build the ``project_file`` MQTT command from the same :class:`FeedPlan`.

The §6.3 failure was four numbers that should have agreed and didn't. Three of
them live in the container (`slice_info`); the fourth, ``ams_mapping``, lives
in *this* command. Deriving it from the same plan closes the last gap — the
command physically cannot disagree with the container it launches.

The accepted form is `REPORT.md` §6.2 verbatim. Two corrections vs. the old
`service/jobs.py`:

* ``param = "Metadata/plate_1.gcode"`` (not the gcode filename)
* ``url  = "file:///sdcard/<name>.gcode.3mf"`` — the **only confirmed**
  scheme. The old code used ``ftp://`` which `REPORT.md` §9 explicitly flags
  as *untested*.
"""

from __future__ import annotations

from typing import Any

from bambu_bridge.slicedoc.container import GCODE_MEMBER
from bambu_bridge.slicedoc.feed import FeedPlan


def sd_filename(name: str) -> str:
    """The on-SD-card name. Stored at the card root, not ``model/``."""
    stem = name
    for suffix in (".gcode.3mf", ".3mf", ".gcode"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return f"{stem}.gcode.3mf"


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
    protocol layer. ``use_ams``/``ams_mapping`` should come from the same
    :class:`FeedPlan` that produced the container (or, on the upload path,
    from the validated container's own arity)."""
    stem = sd_filename(name)[: -len(".gcode.3mf")]
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


def build_project_file_command(
    feed: FeedPlan, name: str, **flags: Any
) -> dict[str, Any]:
    """Same, derived from a :class:`FeedPlan` (the single-source path)."""
    return project_file_command(
        name, use_ams=feed.use_ams, ams_mapping=feed.ams_mapping, **flags
    )
