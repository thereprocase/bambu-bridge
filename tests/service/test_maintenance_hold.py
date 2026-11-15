"""Execution fences, not acknowledgements, bound the held-jog command queue."""

import asyncio
import re

import pytest

from bambu_bridge.service.maintenance_hold import HoldStopped, MaintenanceHold
from bambu_bridge.service.maintenance_motion import MaintenanceMotion


class Rig:
    def __init__(self):
        self.motion = MaintenanceMotion()
        self.motion.requested({"X": 128, "Y": 128, "Z": 125})
        self.raw = {"mc_percent": 100, "mc_remaining_time": 0}
        self.ready = True
        self.commands = []
        self.auto = False
        self.engine = MaintenanceHold(
            self.motion,
            lambda _: self.ready,
            lambda: self.raw,
            self.publish,
            self.move,
            lambda: None,
            asyncio.Lock(),
            fence_timeout=0.06,
        )
        self.engine.LEASE_S = 0.06

    async def publish(self, envelope):
        self.commands.append(envelope)
        if self.auto:
            asyncio.get_running_loop().call_soon(self.report, self.marker(envelope))

    async def move(self, envelope, targets):
        self.motion.requested(targets)
        await self.publish(envelope)

    def marker(self, envelope):
        match = re.search(r"M73 P(\d+) R(\d+)", envelope["print"]["param"])
        return (int(match.group(1)), int(match.group(2))) if match else None

    def report(self, value):
        if value is not None:
            self.raw["mc_percent"], self.raw["mc_remaining_time"] = value
        self.engine.observe(
            {
                "mc_percent": self.raw["mc_percent"],
                "mc_remaining_time": self.raw["mc_remaining_time"],
            }
        )

    def moves(self):
        return [c for c in self.commands if "G1 " in c["print"]["param"]]


@pytest.mark.asyncio
async def test_qualification_uses_nonmotion_markers_and_restores_baseline():
    rig = Rig()
    rig.auto = True
    result = await rig.engine.qualify()
    await asyncio.sleep(0)
    assert result["qualified"]
    assert not rig.moves()
    assert rig.raw["mc_percent"] == 100 and rig.raw["mc_remaining_time"] == 0
    assert [rig.marker(c) for c in rig.commands] == [(1, 1), (100, 0)]
    await rig.engine.close()


@pytest.mark.asyncio
async def test_ack_alone_cannot_qualify_or_claim_completion():
    rig = Rig()
    task = asyncio.create_task(rig.engine.qualify())
    await asyncio.sleep(0)
    rig.engine.observe({"command": "gcode_line", "result": "success"})
    assert not task.done()
    assert len(rig.commands) == 1
    with pytest.raises(HoldStopped, match="did not report"):
        await task
    assert not rig.engine.qualified
    assert not rig.moves()
    await rig.engine.close()


@pytest.mark.asyncio
async def test_old_baseline_is_not_a_fresh_execution_marker():
    rig = Rig()
    task = asyncio.create_task(rig.engine.qualify())
    await asyncio.sleep(0)
    rig.report((100, 0))
    assert not task.done()
    rig.report((1, 1))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(rig.commands) == 2
    await task
    assert rig.marker(rig.commands[-1]) == (100, 0)
    await rig.engine.close()


@pytest.mark.asyncio
async def test_anchor_completion_is_fenced_before_first_segment():
    rig = Rig()
    rig.engine.qualified = True
    lease = rig.engine.start("Z", dz=1)
    await asyncio.sleep(0)
    assert not rig.moves()
    assert rig.engine.phase == "arming"
    rig.engine.observe({"command": "gcode_line", "result": "success"})
    assert not rig.moves()
    rig.report(rig.marker(rig.commands[-1]))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(rig.moves()) == 1
    assert "G1 Z126 F600" in rig.moves()[0]["print"]["param"]
    assert "M211 X1 Y1 Z1" in rig.moves()[0]["print"]["param"]
    # Arbitrarily many command ACKs cannot enqueue a second segment.
    for _ in range(20):
        rig.engine.observe({"command": "gcode_line", "result": "success"})
    await asyncio.sleep(0.005)
    assert len(rig.moves()) == 1
    rig.engine.stop(lease)
    assert rig.engine.lock.locked()
    rig.report(rig.marker(rig.moves()[-1]))
    await rig.engine._task
    assert len(rig.moves()) == 1
    await rig.engine.close()


@pytest.mark.asyncio
async def test_one_outstanding_segment_even_when_lease_is_renewed():
    rig = Rig()
    rig.engine.qualified = True
    lease = rig.engine.start("XY", dx=1, dy=1)
    await asyncio.sleep(0)
    rig.report(rig.marker(rig.commands[-1]))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    for _ in range(3):
        rig.engine.renew(lease)
        await asyncio.sleep(0.005)
    assert len(rig.moves()) == 1
    assert "G1 X129 Y129 F3000" in rig.moves()[0]["print"]["param"]
    rig.report(rig.marker(rig.moves()[0]))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(rig.moves()) == 2
    rig.engine.stop(lease)
    rig.report(rig.marker(rig.moves()[-1]))
    await rig.engine._task
    assert len(rig.moves()) == 2
    await rig.engine.close()


@pytest.mark.asyncio
async def test_lost_lease_or_release_never_queues_further_motion():
    rig = Rig()
    rig.engine.fence_timeout = 0.2
    rig.engine.qualified = True
    rig.engine.start("Z", dz=-1)
    await asyncio.sleep(0)
    rig.report(rig.marker(rig.commands[-1]))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    rig.engine.fence_timeout = 0.2
    await asyncio.sleep(0.07)
    assert len(rig.moves()) == 1
    rig.report(rig.marker(rig.moves()[-1]))
    await rig.engine._task
    assert len(rig.moves()) == 1
    assert not rig.engine.active
    assert "lease expired" in rig.engine.reason
    await rig.engine.close()


@pytest.mark.asyncio
async def test_stale_busy_or_reconnect_invalidation_clears_qualification_and_targets():
    rig = Rig()
    rig.engine.qualified = True
    lease = rig.engine.start("Z", dz=1)
    await asyncio.sleep(0)
    rig.ready = False
    rig.engine.observe({"mc_percent": 1})
    await rig.engine._task
    assert not rig.engine.qualified
    assert all(v is None for v in rig.motion.targets.values())
    assert not rig.moves()
    with pytest.raises(HoldStopped):
        rig.engine.renew(lease)
    await rig.engine.close()


@pytest.mark.asyncio
async def test_travel_limit_and_unknown_anchor_never_publish_motion():
    rig = Rig()
    rig.engine.qualified = True
    rig.motion.targets["Z"] = None
    with pytest.raises(HoldStopped, match="absolute"):
        rig.engine.start("Z", dz=1)
    rig.motion.targets["Z"] = 250
    rig.auto = True
    rig.engine.start("Z", dz=1)
    await rig.engine._task
    assert not rig.moves()
    assert "Travel limit" in rig.engine.reason
    await rig.engine.close()


@pytest.mark.asyncio
async def test_old_released_segment_marker_cannot_release_a_new_anchor():
    rig = Rig()
    rig.engine.qualified = True
    first = rig.engine.start("Z", dz=1)
    await asyncio.sleep(0)
    rig.report(rig.marker(rig.commands[-1]))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    old_marker = rig.marker(rig.moves()[-1])
    rig.engine.stop(first)
    await asyncio.sleep(0)
    assert rig.engine.lock.locked() and rig.engine.active
    with pytest.raises(HoldStopped):
        rig.engine.start("Z", dz=1)
    rig.report(old_marker)
    await rig.engine._task
    # A separately requested long absolute anchor precedes the next arming fence.
    rig.motion.requested({"Z": 200})
    second = rig.engine.start("Z", dz=1)
    await asyncio.sleep(0)
    anchor_marker = rig.marker(rig.commands[-1])
    assert anchor_marker != old_marker
    before = len(rig.moves())
    rig.report(old_marker)
    await asyncio.sleep(0)
    assert len(rig.moves()) == before, "late prior marker cannot release the new anchor"
    rig.report(anchor_marker)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert "G1 Z201 " in rig.moves()[-1]["print"]["param"]
    rig.engine.stop(second)
    rig.report(rig.marker(rig.moves()[-1]))
    await rig.engine._task
    await rig.engine.close()


@pytest.mark.asyncio
async def test_genuine_execution_feedback_during_publish_is_accepted():
    rig = Rig()

    async def publish(envelope):
        rig.commands.append(envelope)
        rig.report(rig.marker(envelope))
        await asyncio.sleep(0.001)  # execution report can precede MQTT publish return

    rig.engine.publish = publish
    result = await rig.engine.qualify()
    assert result["qualified"]
    await rig.engine.close()


@pytest.mark.asyncio
async def test_missing_remaining_time_cannot_qualify():
    rig = Rig()
    task = asyncio.create_task(rig.engine.qualify())
    await asyncio.sleep(0)
    rig.engine.observe({"mc_percent": rig.marker(rig.commands[-1])[0]})
    with pytest.raises(HoldStopped):
        await task
    assert not rig.engine.qualified and not rig.engine.active
    await rig.engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [OSError("fixture"), TimeoutError("fixture")])
async def test_unexpected_publication_errors_never_leave_qualification_active(failure):
    rig = Rig()

    async def publish(_):
        raise failure

    rig.engine.publish = publish
    with pytest.raises(HoldStopped):
        await rig.engine.qualify()
    assert not rig.engine.active and not rig.engine.qualified
    await rig.engine.close()


def test_marker_pair_is_durable_and_never_reused_after_restart(tmp_path):
    from bambu_bridge.service.maintenance_hold import FenceSequence

    path = tmp_path / "fences.sqlite"
    first = FenceSequence(path).next(100)
    second = FenceSequence(path).next(first[0])
    assert first != second and second[0] != first[0]
    assert all(0 <= p <= 100 and 1 <= r <= 9999 for p, r in [first, second])


@pytest.mark.asyncio
async def test_real_service_own_fence_and_restore_ack_preserve_references():
    from bambu_bridge.protocol.models import ReportMessage
    from tests.service.test_printer import _service

    service = _service(1883)
    service._connected = True
    await service._handle_report(
        ReportMessage.model_validate(
            {
                "print": {
                    "gcode_state": "FINISH",
                    "home_flag": 7,
                    "mc_percent": 100,
                    "mc_remaining_time": 0,
                }
            }
        )
    )
    service.maintenance_motion.requested({"X": 128, "Y": 128, "Z": 125})

    async def publish(envelope):
        body = envelope["print"]
        await service._handle_report(
            ReportMessage.model_validate(
                {
                    "print": {
                        "command": "gcode_line",
                        "sequence_id": body["sequence_id"],
                        "result": "success",
                    }
                }
            )
        )
        assert service.maintenance_hold.active
        marker = re.search(r"M73 P(\d+) R(\d+)", body.get("param", ""))
        if marker:
            await service._handle_report(
                ReportMessage.model_validate(
                    {
                        "print": {
                            "command": "push_status",
                            "mc_percent": int(marker.group(1)),
                            "mc_remaining_time": int(marker.group(2)),
                        }
                    }
                )
            )

    service._mqtt.publish = publish
    result = await service.maintenance_hold.qualify()
    assert result["qualified"]
    assert service.maintenance_motion.targets["Z"] == 125
    assert service._state["mc_percent"] == 100 and service._state["mc_remaining_time"] == 0
    assert all(service.tracked_position(axis) is None for axis in "XYZ")
    await service.maintenance_hold.close()


def test_marker_changes_both_percent_and_remaining_time():
    from bambu_bridge.service.maintenance_hold import FenceSequence

    sequence = FenceSequence(None)
    p, r = sequence.next(100, 1)
    assert p != 100 and r != 1


@pytest.mark.asyncio
async def test_reservation_disk_stall_cannot_publish_after_lease_expiry():
    import time

    rig = Rig()
    rig.engine.qualified = True
    original = rig.engine._sequence.next

    def stalled(*args):
        marker = original(*args)
        time.sleep(0.08)
        return marker

    rig.engine._sequence.next = stalled
    rig.engine.start("Z", dz=1)
    await rig.engine._task
    assert not rig.moves()
    assert not any("G1" in c["print"]["param"] for c in rig.commands)
    await rig.engine.close()
