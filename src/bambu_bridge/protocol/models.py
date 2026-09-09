"""Pydantic models for the Bambu P1S MQTT JSON wire format.

Strategy (spec 5.1): firmware adds fields constantly. Every model allows extra
fields (``extra="allow"``) and only the fields we actually consume are strictly
typed. We never break when Bambu ships a new firmware field, and the raw payload
is preserved so the API can forward unmodelled data if needed.

Field set referenced from ha-bambulab's push_status layout — reimplemented here,
not imported.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Base
# --------------------------------------------------------------------------- #


class WireModel(BaseModel):
    """Base for everything parsed off the wire: tolerate unknown fields."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


# --------------------------------------------------------------------------- #
# gcode_state (spec gotcha #8)
# --------------------------------------------------------------------------- #


class GcodeState(StrEnum):
    """Printer ``gcode_state`` values.

    ``PREPARE`` is "uploading and warming up" — it belongs to the job-machine's
    ``started`` phase, not ``printing`` (spec gotcha #8). Mapping lives in the
    job state machine, not here.
    """

    IDLE = "IDLE"
    PREPARE = "PREPARE"
    RUNNING = "RUNNING"
    PAUSE = "PAUSE"
    FINISH = "FINISH"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def _missing_(cls, value: object) -> GcodeState:
        # Firmware could introduce a new state; degrade gracefully.
        return cls.UNKNOWN


# --------------------------------------------------------------------------- #
# AMS
# --------------------------------------------------------------------------- #


class AmsTray(WireModel):
    """One AMS slot.

    NB (spec gotcha #9): ``id`` here is the protocol index (0-based). The
    physical AMS unit labels slots 1–4. Convert at the API boundary, not here.
    """

    id: str | None = None
    tray_type: str | None = None
    tray_color: str | None = None
    tray_sub_brands: str | None = None
    remain: int | None = None


class AmsUnit(WireModel):
    id: str | None = None
    humidity: str | None = None
    temp: str | None = None
    tray: list[AmsTray] = Field(default_factory=list)


class AmsState(WireModel):
    ams: list[AmsUnit] = Field(default_factory=list)
    tray_now: str | None = None
    tray_pre: str | None = None
    tray_tar: str | None = None


# --------------------------------------------------------------------------- #
# print.push_status
# --------------------------------------------------------------------------- #


class PrintReport(WireModel):
    """The ``print`` object of a ``push_status`` / ``pushall`` report.

    Only consumed fields are typed; everything else is retained via
    ``extra="allow"`` and reachable through ``model_extra``.
    """

    command: str | None = None
    sequence_id: str | None = None
    msg: int | None = None

    # Job / progress
    gcode_state: GcodeState | None = None
    gcode_file: str | None = None
    subtask_name: str | None = None
    mc_percent: int | None = None
    mc_remaining_time: int | None = None  # minutes
    layer_num: int | None = None
    total_layer_num: int | None = None

    # Temperatures (°C)
    nozzle_temper: float | None = None
    nozzle_target_temper: float | None = None
    bed_temper: float | None = None
    bed_target_temper: float | None = None
    chamber_temper: float | None = None

    # Fans (raw printer scale, sent as strings)
    cooling_fan_speed: str | None = None
    big_fan1_speed: str | None = None
    big_fan2_speed: str | None = None
    heatbreak_fan_speed: str | None = None

    # Errors
    print_error: int | None = None
    mc_print_error_code: str | None = None

    # AMS
    ams: AmsState | None = None


class ReportMessage(WireModel):
    """Top-level message on ``device/<serial>/report``.

    Bambu wraps payloads by category. We only model ``print``; other categories
    (``system``, ``mc_print``, …) are preserved as raw extra.
    """

    print: PrintReport | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> ReportMessage:
        return cls.model_validate(raw)


# --------------------------------------------------------------------------- #
# Outbound commands  (spec 5.1 "Command envelope")
# --------------------------------------------------------------------------- #


def new_sequence_id() -> str:
    """Unique per-command id. Printer echoes it in the ack so we can correlate."""
    return uuid.uuid4().hex


def build_command(
    category: str,
    command: str,
    *,
    sequence_id: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Build a ``{category: {command, sequence_id, ...}}`` envelope.

    Example::

        build_command("print", "gcode_line", param="G1 Z10\\n")
        # -> {"print": {"command": "gcode_line",
        #               "sequence_id": "ab12…", "param": "G1 Z10\\n"}}
    """
    body: dict[str, Any] = {
        "command": command,
        "sequence_id": sequence_id or new_sequence_id(),
        **fields,
    }
    return {category: body}


def pushall_request() -> dict[str, Any]:
    """Seed-state request (spec 5.1).

    Sent immediately after subscribing so we get full state without waiting
    ~30 s for the next periodic push. Payload is exactly as specified — no
    ``sequence_id`` (pushall is documented without one).
    """
    return {"pushing": {"command": "pushall", "version": 1, "push_target": 1}}
