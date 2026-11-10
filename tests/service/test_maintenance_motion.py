"""Last commands are references for bounded targets, not position telemetry."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from bambu_bridge.protocol import commands
from bambu_bridge.protocol.models import ReportMessage
from bambu_bridge.service.maintenance_motion import MaintenanceMotion
from tests.service.test_printer import _service


def test_unknown_reference_is_not_seeded_and_bounds_reject_overrun():
    motion = MaintenanceMotion()
    with pytest.raises(LookupError):
        motion.step_target("Z", 1, 10)
    motion.requested({"Z": 250, "X": 0, "Y": 128})
    with pytest.raises(ValueError):
        motion.step_target("Z", 1, 1)
    with pytest.raises(ValueError):
        motion.step_target("X", -1, 50)
    assert motion.step_target("Z", -1, 50) == 200
    assert motion.snapshot()["position_verified"] is False
    assert motion.snapshot()["last_requested"] == {"X": 0, "Y": 128, "Z": 250}


@pytest.mark.parametrize("axis,direction,step", [("Z", 0, 10), ("Z", 1, 20), ("A", 1, 10)])
def test_invalid_step_never_changes_target(axis, direction, step):
    motion = MaintenanceMotion()
    motion.requested({"Z": 125})
    with pytest.raises(ValueError):
        motion.step_target(axis, direction, step)
    assert motion.targets["Z"] == 125


@pytest.mark.parametrize(
    "envelope",
    [
        commands.home(),
        commands.move_axis("X", 1),
        {"print": {"command": "project_file"}},
        {"print": {"command": "gcode_line", "param": "M400\nG92 X0"}},
        {"print": {"command": "gcode_line", "param": "G380 S2 Z10"}},
    ],
)
def test_other_motion_invalidates_last_targets(envelope):
    motion = MaintenanceMotion()
    motion.requested({"Z": 125})
    assert motion.observe_command(envelope)
    assert motion.targets["Z"] is None


def test_climate_commands_and_own_ack_do_not_claim_or_erase_position():
    motion = MaintenanceMotion()
    envelope = commands.move_bed_absolute(83)
    motion.register(envelope)
    motion.requested({"Z": 83})
    assert not motion.observe_command(commands.set_fan("part", 30))
    assert not motion.observe_report(
        {
            "command": "gcode_line",
            "sequence_id": envelope["print"]["sequence_id"],
            "result": "success",
        },
        {"home_flag": 7},
    )
    assert motion.targets["Z"] == 83
    assert not motion.snapshot()["position_verified"]


@pytest.mark.parametrize(
    "report",
    [
        {"command": "gcode_line", "sequence_id": "foreign"},
        {"command": "axis_control"},
        {"home_flag": 3},
        {"home_flag": None},
    ],
)
def test_observed_external_motion_or_homing_invalidates(report):
    motion = MaintenanceMotion()
    motion.requested({"Z": 83})
    assert motion.observe_report(report, {"home_flag": 7})
    assert motion.targets["Z"] is None


@pytest.mark.asyncio
async def test_service_targets_preserve_other_axes_without_seeding_verified_position():
    service = _service(1883)
    service._publish_raw = AsyncMock()
    await service.send_maintenance(commands.move_xy_absolute(128, 128), {"X": 128, "Y": 128})
    await service.send_maintenance(commands.move_bed_absolute(83), {"Z": 83})
    assert service.snapshot()["maintenance"]["last_requested"] == {"X": 128, "Y": 128, "Z": 83}
    assert all(service.tracked_position(axis) is None for axis in "XYZ")
    await service.send_raw(commands.home())
    assert service.snapshot()["maintenance"]["last_requested"] == dict.fromkeys("XYZ")


@pytest.mark.asyncio
async def test_lifecycle_reset_during_publish_cannot_resurrect_stale_reference():
    service = _service(1883)
    started, release = asyncio.Event(), asyncio.Event()

    async def publish(_):
        started.set()
        await release.wait()

    service._publish_raw = publish
    pending = asyncio.create_task(
        service.send_maintenance(commands.move_bed_absolute(83), {"Z": 83})
    )
    await started.wait()
    service.reset_motion_state("connection_lost")
    release.set()
    await pending
    assert service.maintenance_motion.targets == dict.fromkeys("XYZ")


@pytest.mark.asyncio
async def test_failed_send_and_print_start_clear_reference():
    service = _service(1883)
    await service._handle_report(
        ReportMessage.model_validate({"print": {"gcode_state": "FINISH", "home_flag": 7}})
    )
    service._publish_raw = AsyncMock()
    await service.send_maintenance(commands.move_bed_absolute(83), {"Z": 83})
    await service._handle_report(
        ReportMessage.model_validate({"print": {"gcode_state": "RUNNING"}})
    )
    assert service.maintenance_motion.targets["Z"] is None
    service._publish_raw = AsyncMock(side_effect=ConnectionError("fixture disconnected"))
    with pytest.raises(ConnectionError):
        await service.send_maintenance(commands.move_bed_absolute(83), {"Z": 83})
    assert service.maintenance_motion.targets["Z"] is None
