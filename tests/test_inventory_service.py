from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from bambu_bridge.inventory_journal import InventoryConflict
from bambu_bridge.service.inventory import ArchiveSpool, InventoryService, NewSpool, SpoolWeight
from bambu_bridge.spool_assignments import AssignmentConflict, SpoolAssignment
from bambu_bridge.spoolman import ProviderWriteError

PRODUCT = {"id": 2, "name": "ASA Black", "material": "ASA", "weight": 1000, "spool_weight": 150}
SPOOL = {"id": 43, "filament": PRODUCT, "remaining_weight": 436, "spool_weight": 150}


class Provider:
    def __init__(self) -> None:
        self.writes: list[httpx.Request] = []
        self.spool = dict(SPOOL)
        self.failure: str | int | None = None
        self.accepted = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/spool"):
            return httpx.Response(
                200, json=[self.spool] if int(request.url.params.get("offset", "0")) == 0 else []
            )
        if request.method == "GET":
            return httpx.Response(
                200, json=PRODUCT if "/filament/" in request.url.path else self.spool
            )
        self.writes.append(request)
        if self.failure == "connect":
            raise httpx.ConnectError("offline", request=request)
        if self.failure == "timeout":
            raise httpx.ReadTimeout("response lost", request=request)
        if self.failure == "cancel":
            self.accepted.set()
            await self.release.wait()
        if isinstance(self.failure, int):
            return httpx.Response(self.failure, json={"message": "provider private details"})
        body = json.loads(request.content)
        record = {
            **self.spool,
            "comment": body.get("comment", self.spool.get("comment")),
            "remaining_weight": body.get("remaining_weight", self.spool["remaining_weight"]),
            "archived": body.get("archived", False),
        }
        self.spool = record
        return httpx.Response(200, json=record)


async def open_service(path: Path, provider: Provider) -> InventoryService:
    return await InventoryService.open(
        path,
        "https://inventory.invalid/",
        transport=httpx.MockTransport(provider),
    )


async def test_a_record_change_is_forwarded_once_and_spoolman_answers(tmp_path: Path) -> None:
    provider = Provider()
    service = await open_service(tmp_path, provider)
    try:
        record = await service.record("create_spool", NewSpool(filament_id=2, remaining_weight=400))
        assert record["id"] == 43
        assert len(provider.writes) == 1
        assert json.loads(provider.writes[0].content) == {"filament_id": 2, "remaining_weight": 400}
    finally:
        await service.close()


@pytest.mark.parametrize(
    "failure, outcome",
    [("connect", "not_sent"), ("timeout", "uncertain"), (500, "uncertain"), (422, "rejected")],
)
async def test_a_failed_write_is_reported_and_never_retried(
    tmp_path: Path, failure: str | int, outcome: str
) -> None:
    provider = Provider()
    provider.failure = failure
    service = await open_service(tmp_path, provider)
    try:
        with pytest.raises(ProviderWriteError) as raised:
            await service.record("create_spool", NewSpool(filament_id=2))
        assert raised.value.outcome == outcome
        assert len(provider.writes) == 1
    finally:
        await service.close()


async def test_gross_weight_uses_net_correction_preserving_package_capacity(tmp_path: Path) -> None:
    provider = Provider()
    service = await open_service(tmp_path, provider)
    try:
        record = await service.record("weigh", SpoolWeight(spool_id=43, gross_weight=600))
        assert record["remaining_weight"] == 450
        request = provider.writes[0]
        assert request.method == "PATCH"
        assert json.loads(request.content) == {"remaining_weight": 450}
        with pytest.raises(ValueError, match="smaller"):
            await service.record("weigh", SpoolWeight(spool_id=43, gross_weight=100))
        assert len(provider.writes) == 1
    finally:
        await service.close()


async def test_assigned_spool_cannot_be_archived(tmp_path: Path) -> None:
    provider = Provider()
    service = await open_service(tmp_path, provider)
    try:
        snapshot = await service.assignments.snapshot()
        await service.assignments.replace(
            (SpoolAssignment("p1", "external", 43),),
            provider_id=snapshot.provider_id,
            expected_revision=snapshot.revision,
        )
        with pytest.raises(InventoryConflict, match="Remove #43 from the external holder first"):
            await service.record("archive", ArchiveSpool(spool_id=43))
        assert not provider.writes
    finally:
        await service.close()


async def test_second_writer_and_provider_replacement_rejected(tmp_path: Path) -> None:
    provider = Provider()
    service = await open_service(tmp_path, provider)
    try:
        with pytest.raises(BlockingIOError):
            await open_service(tmp_path, provider)
    finally:
        await service.close()
    with pytest.raises(AssignmentConflict):
        await InventoryService.open(tmp_path, "https://replacement.invalid/")


async def test_assignment_move_and_stale_phone(tmp_path: Path) -> None:
    provider = Provider()
    service = await open_service(tmp_path, provider)
    choice = {
        "source": "ams:hardware:tray:0",
        "label": "AMS A · slot 1",
        "assignable": True,
        "issues": [],
        "observed_identity": "a" * 64,
        "spool_key": "untagged",
        "wire_slot": 0,
        "material": "ASA",
    }
    try:
        initial = await service.assignments.snapshot()
        await service.assign(
            provider_id=initial.provider_id,
            expected_revision=0,
            spool_id=43,
            printer_id="p1",
            source=choice["source"],
            observed_identity="a" * 64,
            choices=lambda: [choice],
        )
        with pytest.raises(AssignmentConflict):
            await service.assign(
                provider_id=initial.provider_id,
                expected_revision=0,
                spool_id=None,
                printer_id="p1",
                source=choice["source"],
                observed_identity=None,
                choices=lambda: [],
            )
        moved = await service.assign(
            provider_id=initial.provider_id,
            expected_revision=1,
            spool_id=43,
            printer_id="p2",
            source=choice["source"],
            observed_identity="a" * 64,
            choices=lambda: [choice],
        )
        assert len(moved["assignments"]) == 1
        assert moved["assignments"][0]["printer_id"] == "p2"
        assert moved["assignments"][0]["assigned_at"] > 0
        assert not provider.writes  # assigning inventory never edits printer or provider
        # The identity remembers its last spool even after the slot is cleared.
        await service.assign(
            spool_id=None,
            printer_id="p2",
            source=choice["source"],
            observed_identity=None,
            choices=lambda: [],
        )
        history = await service.assignments.last_spools()
        assert history[("a" * 64, choice["source"])][0] == 43
    finally:
        await service.close()


async def test_assignment_rechecks_source_after_provider_read(tmp_path: Path) -> None:
    provider = Provider()
    service = await open_service(tmp_path, provider)
    try:
        initial = await service.assignments.snapshot()
        with pytest.raises(InventoryConflict, match="not reporting this slot"):
            await service.assign(
                provider_id=initial.provider_id,
                expected_revision=0,
                spool_id=43,
                printer_id="p1",
                source="external",
                observed_identity="a" * 64,
                choices=lambda: [],
            )
        assert (await service.assignments.snapshot()).revision == 0
    finally:
        await service.close()
