"""A confirmed archived print enters the existing native inbox exactly once."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from bambu_bridge.library import LibraryError, LibraryStore
from bambu_bridge.library_replay import ReplayStart, review
from bambu_bridge.slicedoc.command import project_file_command

if TYPE_CHECKING:
    from bambu_bridge.native_gateway import NativeGateway
    from bambu_bridge.service.printer import PrinterService

_BEDS = {
    "Textured PEI Plate": "textured_plate",
    "Smooth PEI Plate": "hot_plate",
    "High Temp Plate": "hot_plate",
    "Cool Plate": "cool_plate",
    "Engineering Plate": "eng_plate",
}


async def refresh_materials(service: PrinterService, timeout: float = 5) -> None:
    revision = service.material_inventory.revision
    async with service.raw_bus.subscribe() as subscription:
        async with asyncio.timeout(timeout):
            await service.send_command("pushing", "pushall", version=1, push_target=1)
            while service.material_inventory.revision == revision:
                await subscription.get()


class LibraryReplay:
    def __init__(
        self,
        store: LibraryStore,
        gateway: Callable[[], NativeGateway | None],
        *,
        enabled: bool,
    ):
        self.store, self.gateway, self.enabled = store, gateway, enabled
        self.pending: set[str] = set()
        self.claim_lock = asyncio.Lock()

    def _gateway(self) -> NativeGateway:
        gateway = self.gateway()
        if not self.enabled or gateway is None or gateway.inbox is None or not gateway.config:
            raise LibraryError(503, "Archived print starts are not enabled on the native gateway")
        return gateway

    def status(self, identifier: str) -> dict[str, Any]:
        request = self.store.replay_request(identifier)
        if request is None:
            raise LibraryError(404, "Replay request not found")
        result = {
            "id": identifier,
            "capture_id": request["capture_id"],
            "state": "preparing_transfer" if identifier in self.pending else "not_started",
            "code": "BBREPLAY_NO_DISPATCH_RECEIPT",
        }
        if request.get("receipt_created") and identifier not in self.pending:
            result.update(state="receipt_unavailable", code="BBREPLAY_RECEIPT_UNAVAILABLE")
            try:
                capture = self.store.get(request["capture_id"])
            except LibraryError:
                pass
            else:
                for attempt in capture["attempts"]:
                    if attempt["source"] == "replay" and attempt["source_id"] == identifier:
                        result.update(state=attempt["state"], code=attempt["error_code"])
        gateway = self.gateway()
        if gateway and gateway.inbox:
            try:
                row = gateway.inbox.get(identifier)
            except ValueError:
                pass
            else:
                if row.get("replay_request_id") == identifier:
                    result.update(state=row["start_state"] or row["state"], code=row["code"])
        return result

    async def checked(self, cid: str, approval: ReplayStart) -> dict[str, Any]:
        gateway = self._gateway()
        assert gateway.config is not None
        if gateway.config["printer_id"] != approval.printer_id:
            raise LibraryError(409, "This printer is not served by the native delivery queue")
        service = gateway.service()
        try:
            await refresh_materials(service)
        except Exception as exc:
            raise LibraryError(409, "Fresh material inventory could not be obtained") from exc
        native = service.native_snapshot().get("print", {})
        printer = {
            **service.summary(),
            "cert_status": service.cert_status,
            "nozzle_diameter": native.get("nozzle_diameter"),
        }
        # The explicit Start confirmation includes the P1S hardware. It can
        # supply a missing registration model, never override a conflicting one.
        printer["model"] = printer.get("model") or "P1S"
        result = await asyncio.to_thread(
            review,
            self.store,
            cid,
            printer=printer,
            frame=service.material_inventory.snapshot(),
            choices=approval.choices,
            expected_inventory=approval.inventory_fingerprint,
        )
        spec = result["requirements"]
        if (
            approval.nozzle_diameter != spec["nozzle_diameter"]
            or approval.bed_type != spec["bed_type"]
        ):
            raise LibraryError(409, "Confirmed nozzle or plate differs from the saved slice")
        if approval.bed_type not in _BEDS:
            raise LibraryError(409, "This plate type has not been qualified for archived starts")
        if not result["mapping_complete"]:
            raise LibraryError(409, "; ".join(result["issues"]))
        return result

    @staticmethod
    def command(approval: ReplayStart, report: dict[str, Any]) -> dict[str, Any]:
        spec = report["requirements"]
        use_ams = 254 not in approval.choices.values()
        count = spec["logical_arity"]
        mapping = (
            [approval.choices.get(index, -1) for index in range(count)] if use_ams else [-1] * count
        )
        fields = project_file_command(
            f"replay-{approval.id}",
            use_ams=use_ams,
            ams_mapping=mapping,
            bed_type=_BEDS[approval.bed_type],
            **approval.options.model_dump(),
        )
        fields["param"] = f"Metadata/plate_{spec['plate']}.gcode"
        return {"print": {"command": "project_file", "sequence_id": approval.id, **fields}}

    async def submit(self, cid: str, approval: ReplayStart) -> dict[str, Any]:
        body = {"capture_id": cid, "approval": approval.model_dump(mode="json")}
        signature = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        # Record the ID before any printer round trips. Even while preflight is
        # waiting, a lost-response retry can only inspect this same request.
        async with self.claim_lock:
            existing = await asyncio.to_thread(self.store.replay_request, approval.id)
            if existing:
                if existing["signature"] != signature:
                    raise LibraryError(409, "This replay request ID has different print choices")
                return await asyncio.to_thread(self.status, approval.id)
            capture = await asyncio.to_thread(self.store.get, cid)
            artifact = next((a for a in capture["artifacts"] if a["role"] == "slice"), None)
            if artifact is None:
                raise LibraryError(409, "This capture has no saved slice")
            document = {
                **body,
                "signature": signature,
                "slice_sha256": artifact["sha256"],
            }
            self.pending.add(approval.id)
            try:
                claimed = await asyncio.to_thread(
                    self.store.claim_replay, approval.id, cid, document
                )
            except BaseException:
                self.pending.discard(approval.id)
                raise
            if not claimed:
                self.pending.discard(approval.id)
                return await asyncio.to_thread(self.status, approval.id)
        try:
            gateway = self._gateway()
            inbox = gateway.inbox
            assert inbox is not None
            async with gateway.inbox_dispatch_lock:
                settings = gateway.app.state.settings
                if artifact["size"] > settings.bridge_max_transfer_bytes:
                    raise LibraryError(413, "Saved slice exceeds the printer transfer limit")
                await gateway.ensure_idle(force_refresh=True)
                if await asyncio.to_thread(inbox.unresolved, approval.printer_id):
                    raise LibraryError(409, "A previous printer start still requires resolution")
                report = await self.checked(cid, approval)
                command = self.command(approval, report)
                await asyncio.to_thread(
                    self.store.prepare_replay, approval.id, command, report["inventory"]
                )
                await self._stage(
                    gateway, cid, approval, artifact["name"], command, document["slice_sha256"]
                )
                gateway.inbox_wake.set()
        finally:
            self.pending.discard(approval.id)
        return await asyncio.to_thread(self.status, approval.id)

    async def _stage(
        self,
        gateway: NativeGateway,
        cid: str,
        approval: ReplayStart,
        name: str,
        command: dict[str, Any],
        digest: str,
    ) -> None:
        """Stage and release failed reservations while holding the dispatch lock."""
        inbox = gateway.inbox
        assert inbox is not None
        path, _ = await asyncio.to_thread(self.store.download, cid, name)
        data = await asyncio.to_thread(path.read_bytes)
        if hashlib.sha256(data).hexdigest() != digest:
            raise LibraryError(409, "Slice changed before staging")
        reserve_task = asyncio.create_task(
            asyncio.to_thread(
                inbox.reserve,
                approval.printer_id,
                f"/replay-{approval.id}.gcode.3mf",
                len(data),
                replay_request_id=approval.id,
                replay_command=command,
            )
        )
        try:
            row = await asyncio.shield(reserve_task)
        except asyncio.CancelledError:
            # A canceled to_thread call may still commit a reservation.
            await asyncio.gather(reserve_task, return_exceptions=True)
            if not reserve_task.cancelled() and reserve_task.exception() is None:
                await asyncio.to_thread(inbox.abandon_replay, approval.id)
            raise
        try:
            await asyncio.to_thread(self.store.replay_receipt_created, approval.id)
            source = asyncio.StreamReader()
            source.feed_data(data)
            source.feed_eof()
            receipt = await inbox.receive(source, row, len(data))
            if receipt["sha256"] != digest:
                raise LibraryError(409, "Staged slice checksum differs from the archive")
            await asyncio.to_thread(inbox.queue_replay, approval.id)
        except BaseException:
            await asyncio.to_thread(inbox.abandon_replay, approval.id)
            raise

    async def before_dispatch(self, row: dict[str, Any]) -> None:
        """Runs inside the native dispatch lock immediately before claiming Start."""
        try:
            request = await asyncio.to_thread(self.store.replay_request, row["replay_request_id"])
            if request is None or request["slice_sha256"] != row["sha256"]:
                raise LibraryError(409, "Replay request does not match this staged file")
            if json.loads(row["command"]) != request["command"]:
                raise LibraryError(409, "Replay start options changed after confirmation")
            approval = ReplayStart.model_validate(request["approval"])
            await self.checked(request["capture_id"], approval)
        except Exception as exc:
            # The native delivery worker persists a blocked receipt, rather
            # than dying or automatically remapping/retrying the print.
            raise ValueError("BBREPLAY_REVIEW_REQUIRED") from exc
