from bambu_bridge.service.remaining_estimates import RemainingEstimates
from bambu_bridge.translate import _remaining_pct


def report(value=84, *, slot=0, **extra):
    return {"ams": {"ams": [{"id": "0", "tray": [{"id": str(slot), "remain": value, **extra}]}]}}


def test_receipt_is_not_calculation_freshness(monkeypatch):
    clock = iter([100.0, 200.0, 300.0, 400.0])
    monkeypatch.setattr("bambu_bridge.service.remaining_estimates.time.time", lambda: next(clock))
    tracker = RemainingEstimates()
    tracker.observe(report())
    tracker.observe({"nozzle_temper": 200})
    assert tracker.view(0)["reported_at"] == 100
    tracker.observe(report())
    assert tracker.view(0)["reported_at"] == 200
    assert tracker.view(0)["value_changed_at"] == 100
    tracker.requested(0)
    tracker.observe(report(57))
    view = tracker.view(0)
    assert view["raw_value"] == 57
    assert view["post_request_report_at"] == 400
    assert view["calculated_at"] is None
    assert not view["refresh_confirmed"]
    assert not view["sufficient_for_mass_check"]


def test_unrelated_or_omitted_field_does_not_complete_request():
    tracker = RemainingEstimates()
    tracker.observe(report())
    tracker.requested(0)
    tracker.observe(report(57, slot=1))
    tracker.observe({"ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PETG"}]}]}})
    assert tracker.view(0)["post_request_report_at"] is None


def test_six_polls_after_84_to_57_do_not_verify_recalculation():
    tracker = RemainingEstimates()
    tracker.observe(report(84))
    tracker.requested(0)
    tracker.observe(report(57))
    changed_at = tracker.view(0)["value_changed_at"]
    for _ in range(6):
        tracker.observe(report(57))
        view = tracker.view(0)
        assert view["raw_value"] == 57
        assert view["value_changed_at"] == changed_at
        assert view["calculation_freshness"] == "unknown"
        assert not view["refresh_confirmed"]
        assert not view["sufficient_for_mass_check"]


def test_identity_change_and_disconnect_forget_provenance():
    tracker = RemainingEstimates()
    tracker.observe(report(tray_uuid="first"))
    tracker.requested(0)
    tracker.observe(report(57, tray_uuid="second"))
    assert "refresh_requested_at" not in tracker.view(0)
    tracker.clear()
    assert "reported_at" not in tracker.view(0)


def test_unknown_percent_is_not_empty_or_guessed_mass():
    for value in (-1, 101, 570, None):
        assert _remaining_pct({"remain": value}, True) is None
    assert _remaining_pct({"remain": 0}, True) == 0
    assert _remaining_pct({"remain": 57}, True) == 57


async def test_refresh_waits_for_target_not_unrelated_telemetry():
    from bambu_bridge.protocol.models import ReportMessage
    from bambu_bridge.service.remaining_estimates import request_remaining_report
    from tests.service.test_printer import _service

    service = _service(1883)

    async def send(envelope):
        await service._handle_report(
            ReportMessage.model_validate({"print": {"nozzle_temper": 220}})
        )
        await service._handle_report(ReportMessage.model_validate({"print": report(90, slot=1)}))
        assert service.remaining_estimates.view(0)["post_request_report_at"] is None
        await service._handle_report(ReportMessage.model_validate({"print": report(57)}))

    service.send_raw = send
    result = await request_remaining_report(service, {"print": {"command": "ams_get_rfid"}}, 0, 0.1)
    assert result["refresh_status"] == "report_received_recalculation_unverified"
    assert result["remaining_estimate"]["raw_value"] == 57
    assert not result["remaining_estimate"]["refresh_confirmed"]


async def test_refresh_timeout_and_failed_send_do_not_confirm():
    from bambu_bridge.service.remaining_estimates import request_remaining_report
    from tests.service.test_printer import _service

    service = _service(1883)
    service.remaining_estimates.observe(report())

    async def send(envelope):
        pass

    service.send_raw = send
    result = await request_remaining_report(service, {}, 0, 0.001)
    assert result["refresh_status"] == "timed_out"
    assert result["remaining_estimate"]["raw_value"] == 84
    assert not result["remaining_estimate"]["refresh_confirmed"]

    async def fail(envelope):
        raise ConnectionError("offline")

    service.send_raw = fail
    import pytest

    with pytest.raises(ConnectionError):
        await request_remaining_report(service, {}, 0, 0.001)
    assert "refresh_requested_at" not in service.remaining_estimates.view(0)
