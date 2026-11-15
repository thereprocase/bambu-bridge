"""An idle P1S goes quiet for about 27 s; silence alone is not motion."""

import asyncio
import re

import pytest

from bambu_bridge.protocol import commands
from bambu_bridge.protocol.models import ReportMessage
from bambu_bridge.service.maintenance_hold import HoldStopped, MaintenanceHold
from bambu_bridge.service.maintenance_motion import MaintenanceMotion
from tests.service.test_printer import _service

TARGETS = {"X": 128.0, "Y": 128.0, "Z": 125.0}


def _report(fields: dict) -> ReportMessage:
    return ReportMessage.model_validate({"print": fields})


async def _qualified():
    """A real service, connected, idle and homed, with held jogging enabled."""
    service = _service(1883)
    sent: list[dict] = []

    async def publish(envelope):
        sent.append(envelope)
        marker = re.search(r"M73 P(\d+) R(\d+)", str((envelope.get("print") or {}).get("param")))
        if marker:  # the printer echoes the execution marker
            await service._handle_report(
                _report(
                    {
                        "command": "push_status",
                        "mc_percent": int(marker.group(1)),
                        "mc_remaining_time": int(marker.group(2)),
                    }
                )
            )

    async def no_probe():
        return None

    service._mqtt.publish = publish  # type: ignore[method-assign]
    service._tofu_compare = no_probe  # type: ignore[method-assign]
    await service._handle_connected()
    await service._handle_report(
        _report({"gcode_state": "IDLE", "home_flag": 7, "mc_percent": 100, "mc_remaining_time": 0})
    )
    service.maintenance_motion.requested(dict(TARGETS))
    assert (await service.maintenance_hold.qualify())["qualified"]
    sent.clear()
    return service, sent


def _full_report_requests(sent):
    return [e for e in sent if (e.get("pushing") or {}).get("command") == "pushall"]


async def test_a_27_s_idle_gap_keeps_qualification_and_targets():
    service, sent = await _qualified()
    service._last_telemetry_at -= 27  # connected and idle, between report bursts
    await asyncio.sleep(0.8)  # three watchdog ticks
    hold = service.maintenance_hold
    assert hold.qualified and hold.phase == "ready"
    assert service.maintenance_motion.targets == TARGETS
    # One full-report request; the service's 10 s spacing holds back the rest.
    assert len(_full_report_requests(sent)) == 1

    await service._handle_report(_report({"command": "push_status", "gcode_state": "IDLE"}))
    await asyncio.sleep(0.3)
    assert hold.qualified
    assert service.maintenance_motion.step_target("Z", 1, 10) == 135
    await hold.close()


async def test_silence_past_the_quiet_limit_drops_qualification_but_keeps_targets():
    service, _ = await _qualified()
    hold = service.maintenance_hold
    service._last_telemetry_at -= 61  # past the documented 60 s bound
    await asyncio.sleep(0.4)
    assert not hold.qualified and hold.phase == "unqualified"
    assert service.maintenance_motion.targets == TARGETS
    assert "stopped reporting" in hold.reason
    assert hold.QUIET_LIMIT_S == 60
    await hold.close()


async def test_disconnect_still_clears_qualification_and_targets():
    service, _ = await _qualified()
    await service._handle_lost()
    await asyncio.sleep(0.3)
    assert not service.maintenance_hold.qualified
    assert all(v is None for v in service.maintenance_motion.targets.values())
    await service.maintenance_hold.close()


async def test_leaving_idle_during_a_quiet_gap_clears_qualification_and_targets():
    service, _ = await _qualified()
    await service._handle_report(_report({"command": "push_status", "gcode_state": "PREPARE"}))
    service._last_telemetry_at -= 27
    await asyncio.sleep(0.4)
    assert not service.maintenance_hold.qualified
    assert all(v is None for v in service.maintenance_motion.targets.values())
    await service.maintenance_hold.close()


async def test_stale_status_with_a_live_lease_stops_strictly_without_refreshing():
    motion = MaintenanceMotion()
    motion.requested(dict(TARGETS))
    refreshes = []

    async def refresh():
        refreshes.append(1)

    async def unused(*_):
        raise AssertionError("no publication expected")

    hold = MaintenanceHold(
        motion,
        lambda _: False,
        lambda: {},
        unused,
        unused,
        lambda: None,
        asyncio.Lock(),
        settled=lambda _: True,
        telemetry_age=lambda: 20.0,
        refresh=refresh,
    )
    hold.qualified, hold.phase, hold._token = True, "holding", "lease"
    await asyncio.wait_for(hold._watch_status(), 2)
    assert not hold.qualified
    assert "stale during held jogging" in hold.reason
    assert not refreshes
    assert motion.targets == TARGETS


async def test_failed_verification_keeps_the_click_step_reference():
    motion = MaintenanceMotion()
    motion.requested(dict(TARGETS))
    published = []

    async def publish(envelope):
        published.append(envelope)  # never echoed

    async def unused(*_):
        raise AssertionError("verification sends no axis move")

    hold = MaintenanceHold(
        motion,
        lambda _: True,
        lambda: {"mc_percent": 100, "mc_remaining_time": 0},
        publish,
        unused,
        lambda: None,
        asyncio.Lock(),
        fence_timeout=0.05,
    )
    with pytest.raises(HoldStopped, match="did not report"):
        await hold.qualify()
    assert not hold.qualified
    assert motion.targets == TARGETS
    assert not any("G1" in e["print"]["param"] for e in published)


@pytest.mark.parametrize("envelope", [commands.ams_change_filament(1), commands.unload_filament()])
def test_ams_load_and_unload_commands_invalidate_targets(envelope):
    motion = MaintenanceMotion()
    motion.requested(dict(TARGETS))
    assert motion.observe_command(envelope)
    assert all(v is None for v in motion.targets.values())


def test_foreign_ams_change_report_invalidates_targets():
    motion = MaintenanceMotion()
    motion.requested(dict(TARGETS))
    report = {"command": "ams_change_filament", "sequence_id": "9999", "result": "success"}
    assert motion.observe_report(report, {"home_flag": 7})
    assert all(v is None for v in motion.targets.values())


async def test_ams_unload_drops_a_qualified_hold():
    service, _ = await _qualified()
    await service.send_raw(commands.unload_filament())
    assert not service.maintenance_hold.qualified
    assert all(v is None for v in service.maintenance_motion.targets.values())
    await service.maintenance_hold.close()
