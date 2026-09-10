"""M1 acceptance (part 1): parse a recorded push_status without losing fields."""

from __future__ import annotations

from bambu_bridge.protocol.models import (
    GcodeState,
    ReportMessage,
    build_command,
    new_sequence_id,
    pushall_request,
)
from tests.conftest import SAMPLE_PUSH_STATUS


def test_push_status_typed_fields() -> None:
    msg = ReportMessage.parse(SAMPLE_PUSH_STATUS)
    assert msg.print is not None
    p = msg.print
    assert p.gcode_state is GcodeState.RUNNING
    assert p.mc_percent == 42
    assert p.layer_num == 120
    assert p.total_layer_num == 285
    assert p.nozzle_temper == 219.8
    assert p.bed_target_temper == 60.0
    assert p.ams is not None
    assert p.ams.ams[0].tray[1].tray_type == "PETG"
    assert p.ams.tray_now == "0"


def test_unmodelled_fields_survive() -> None:
    """extra="allow": firmware fields we don't type must still round-trip."""
    p = ReportMessage.parse(SAMPLE_PUSH_STATUS).print
    assert p is not None
    extra = p.model_extra or {}
    assert extra["wifi_signal"] == "-54dBm"
    assert extra["spd_lvl"] == 2
    assert extra["vt_tray"] == {"id": "254", "tray_type": "ABS"}


def test_no_field_loss_on_redump() -> None:
    """Every key in the source print object is recoverable from the model."""
    src = SAMPLE_PUSH_STATUS["print"]
    p = ReportMessage.parse(SAMPLE_PUSH_STATUS).print
    assert p is not None
    dumped = p.model_dump(exclude_none=True)
    for key in src:
        assert key in dumped, f"lost field: {key}"


def test_gcode_state_unknown_degrades() -> None:
    p = ReportMessage.parse({"print": {"gcode_state": "SOME_NEW_FW_STATE"}}).print
    assert p is not None
    assert p.gcode_state is GcodeState.UNKNOWN


def test_command_envelope_shape() -> None:
    cmd = build_command("print", "gcode_line", param="G1 Z10\n")
    assert set(cmd) == {"print"}
    body = cmd["print"]
    assert body["command"] == "gcode_line"
    assert body["param"] == "G1 Z10\n"
    assert len(body["sequence_id"]) == 32  # uuid4 hex
    assert build_command("print", "pause")["print"]["sequence_id"] != (
        build_command("print", "pause")["print"]["sequence_id"]
    )


def test_sequence_id_unique() -> None:
    assert new_sequence_id() != new_sequence_id()


def test_pushall_payload_is_exact() -> None:
    # Spec 5.1: documented without a sequence_id.
    assert pushall_request() == {
        "pushing": {"command": "pushall", "version": 1, "push_target": 1}
    }
