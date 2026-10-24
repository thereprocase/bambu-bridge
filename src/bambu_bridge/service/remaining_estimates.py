"""Receipt provenance for firmware percentages, never a claim of calculation freshness."""

from __future__ import annotations

import asyncio
import copy
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bambu_bridge.service.printer import PrinterService


class RemainingEstimates:
    def __init__(self) -> None:
        self.slots: dict[int, dict[str, Any]] = {}

    def clear(self) -> None:
        self.slots.clear()

    def requested(self, wire: int) -> None:
        entry = self.slots.setdefault(wire, {})
        entry["refresh_requested_at"] = time.time()
        entry["post_request_report_at"] = None

    def observe(self, report: dict[str, Any]) -> None:
        ams = report.get("ams")
        if not isinstance(ams, dict) or not isinstance(ams.get("ams"), list):
            return
        for unit in ams["ams"]:
            if not isinstance(unit, dict) or str(unit.get("id")) not in {"0", "1", "2", "3"}:
                continue
            trays = unit.get("tray")
            if not isinstance(trays, list):
                continue
            for tray in trays:
                if not isinstance(tray, dict) or str(tray.get("id")) not in {"0", "1", "2", "3"}:
                    continue
                wire = int(unit["id"]) * 4 + int(tray["id"])
                entry = self.slots.setdefault(wire, {})
                # A declaration change invalidates provenance; partial frames cannot
                # establish that omitted identity fields still describe this spool.
                identity = {
                    k: tray[k]
                    for k in ("tray_uuid", "tag_uid", "tray_type", "tray_info_idx", "tray_color")
                    if k in tray
                }
                previous = entry.setdefault("identity", {})
                if any(k in previous and previous[k] != v for k, v in identity.items()):
                    entry.clear()
                    previous = entry.setdefault("identity", {})
                previous.update(identity)
                if "remain" not in tray:
                    continue
                now = time.time()
                value = tray["remain"]
                if entry.get("raw_value") != value:
                    entry["value_changed_at"] = now
                entry.update(raw_value=value, reported_at=now)
                if entry.get("refresh_requested_at") is not None:
                    entry["post_request_report_at"] = now

    def view(self, wire: int) -> dict[str, Any]:
        entry = copy.deepcopy(self.slots.get(wire, {}))
        entry.pop("identity", None)
        return {
            **entry,
            "basis": "firmware_estimate",
            "calculated_at": None,
            "calculation_freshness": "unknown",
            "refresh_confirmed": False,
            "sufficient_for_mass_check": False,
        }


async def request_remaining_report(
    service: PrinterService, envelope: dict[str, Any], wire: int, timeout: float
) -> dict[str, Any]:
    """Send one scan and optionally wait for an explicit target-slot percentage.

    A report after the request is not a firmware scan-completion acknowledgement.
    Callers must inspect refresh_status and must not use it as a weighed mass.
    """
    tracker = service.remaining_estimates
    async with service.raw_bus.subscribe() as subscription:
        tracker.requested(wire)
        requested_at = tracker.slots[wire]["refresh_requested_at"]
        try:
            await service.send_raw(envelope)
        except BaseException:
            if tracker.slots.get(wire, {}).get("refresh_requested_at") == requested_at:
                tracker.slots[wire].pop("refresh_requested_at", None)
            raise
        result = "requested"
        if timeout > 0:
            try:
                async with asyncio.timeout(timeout):
                    while True:
                        entry = tracker.slots.get(wire, {})
                        if entry.get("refresh_requested_at") != requested_at:
                            result = "invalidated_or_superseded"
                            break
                        if entry.get("post_request_report_at") is not None:
                            result = "report_received_recalculation_unverified"
                            break
                        await subscription.get()
            except TimeoutError:
                result = "timed_out"
    return {
        "sent": envelope,
        "confirmation": "pending",
        "refresh_status": result,
        "remaining_estimate": tracker.view(wire),
    }
