"""Per-printer print queue endpoints (contract §16 v0-promoted).

Queue items are 3MFs already staged on the printer's storage (uploaded via
``POST /printers/{id}/files`` or sent through ``POST /jobs``). Each item
references that ``file_path`` plus an optional AMS mapping. Starting an
item submits it via :class:`JobManager.submit` and removes it from the queue.

The queue is user-orderable: ``PATCH /queue/{id} {position}`` shifts the
remaining items to keep positions dense; ``DELETE /queue/{id}`` likewise.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from bambu_bridge.api import errors
from bambu_bridge.api.auth import require_auth, require_owner
from bambu_bridge.api.printers import get_registry
from bambu_bridge.db.jobs import QueueRepo
from bambu_bridge.service.jobs import JobManager
from bambu_bridge.service.registry import PrinterNotFoundError, Registry

router = APIRouter(tags=["queue"], dependencies=[Depends(require_auth)])


class StartStoredFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    file_name: str = Field(min_length=1, max_length=255)
    ams_mapping: list[int] | None = None


def operation_view(operation: dict[str, Any]) -> dict[str, Any]:
    return {
        key: operation[key]
        for key in (
            "id",
            "printer_id",
            "job_id",
            "state",
            "holds_printer",
            "revision",
            "created_at",
            "updated_at",
            "reason",
        )
    }


@router.post("/printers/{printer_id}/start-operations", status_code=202)
async def start_stored_file(printer_id: str, body: StartStoredFile, request: Request) -> Any:
    try:
        _validate_ams(body.ams_mapping)
    except _SlotError:
        raise HTTPException(422, "Physical AMS slots must be 1–4") from None
    operation = await _job_manager(request).start_stored(
        body.operation_id,
        printer_id,
        body.file_name,
        "/" + body.file_name,
        body.ams_mapping,
    )
    return operation_view(operation)


@router.get("/start-operations/{operation_id}")
async def get_start_operation(operation_id: str, request: Request) -> Any:
    operation = await _job_manager(request).starts.get(operation_id)
    if operation is None:
        raise HTTPException(404, "Start operation not found")
    return operation_view(operation)


@router.get("/printers/{printer_id}/start-operation")
async def active_start_operation(printer_id: str, request: Request) -> Any:
    operation = await _job_manager(request).starts.active(printer_id)
    return operation_view(operation) if operation else None


class ResolveStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    confirm_printer_idle: bool


@router.post("/start-operations/{operation_id}/resolve", dependencies=[Depends(require_owner)])
async def resolve_start(operation_id: str, body: ResolveStart, request: Request) -> Any:
    if not body.confirm_printer_idle:
        raise HTTPException(422, "Inspect the printer and explicitly confirm it is idle")
    manager = _job_manager(request)
    operation = await manager.starts.get(operation_id)
    if operation is None:
        raise HTTPException(404, "Start operation not found")
    if operation["state"] != "outcome_unknown" or operation["revision"] != body.revision:
        raise HTTPException(409, "Operation changed; refresh before resolving")
    # Stop only the local worker, never the physical printer. No dispatch task
    # may remain runnable after its reservation is released.
    await manager.quiesce_start(operation["job_id"])
    manager.require_idle(manager._registry.get(operation["printer_id"]))
    await manager.starts.resolve_unknown(operation_id, body.revision)
    resolved = await manager.starts.get(operation_id)
    assert resolved is not None
    return operation_view(resolved)


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #


class AddQueueItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(
        min_length=1, description="Path on printer storage (returned by POST /files)"
    )
    file_name: str = Field(min_length=1, description="Display name shown in the UI")
    ams_mapping: list[int] | None = Field(
        default=None,
        description="Physical slot numbers (1-4). NOT 0-based protocol indices.",
    )
    notes: str | None = Field(default=None, max_length=500)


class ReorderQueueItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: int = Field(ge=0, description="0-based target position; clamped to queue length")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _queue_repo(request: Request) -> QueueRepo:
    return QueueRepo(request.app.state.db)


def _job_manager(request: Request) -> JobManager:
    return request.app.state.jobs  # type: ignore[no-any-return]


def _validate_ams(slots: list[int] | None) -> None:
    if slots is None:
        return
    for s in slots:
        if not (1 <= s <= 4):
            # Surface as a structured 422 so the APK can render the bad slot.
            raise _slot_error(s)


def _slot_error(value: int) -> _SlotError:
    return _SlotError(value)


class _SlotError(Exception):
    def __init__(self, value: int) -> None:
        self.value = value
        super().__init__(f"physical_slot must be 1-4, got {value}")


# --------------------------------------------------------------------------- #
# Endpoints — per-printer queue
# --------------------------------------------------------------------------- #


@router.get("/printers/{printer_id}/queue")
async def list_queue(
    printer_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
) -> Any:
    try:
        registry.get(printer_id)
    except PrinterNotFoundError:
        return errors.not_found("printer", printer_id)
    items = await _queue_repo(request).list_for(printer_id)
    return [i.model_dump(mode="json") for i in items]


@router.post("/printers/{printer_id}/queue", status_code=status.HTTP_201_CREATED)
async def add_to_queue(
    printer_id: str,
    body: AddQueueItem,
    request: Request,
    registry: Registry = Depends(get_registry),
) -> Any:
    try:
        registry.get(printer_id)
    except PrinterNotFoundError:
        return errors.not_found("printer", printer_id)
    try:
        _validate_ams(body.ams_mapping)
    except _SlotError as exc:
        return errors.invalid_input(
            f"physical_slot must be 1-4, got {exc.value}",
            issues=[
                {
                    "code": "ams_slot_invalid",
                    "category": "ams",
                    "message": f"physical_slot must be 1-4, got {exc.value}",
                }
            ],
        )
    item = await _queue_repo(request).add(
        item_id=uuid.uuid4().hex,
        printer_id=printer_id,
        file_path=body.file_path,
        file_name=body.file_name,
        ams_mapping=body.ams_mapping,
        added_at=int(time.time()),
        notes=body.notes,
    )
    return item.model_dump(mode="json")


@router.get("/queue/{item_id}")
async def get_queue_item(item_id: str, request: Request) -> Any:
    item = await _queue_repo(request).get(item_id)
    if item is None:
        return errors.not_found("queue item", item_id)
    return item.model_dump(mode="json")


@router.patch("/queue/{item_id}")
async def reorder_queue_item(item_id: str, body: ReorderQueueItem, request: Request) -> Any:
    updated = await _queue_repo(request).reorder(item_id, body.position)
    if updated is None:
        return errors.not_found("queue item", item_id)
    return updated.model_dump(mode="json")


@router.delete("/queue/{item_id}")
async def delete_queue_item(item_id: str, request: Request) -> Response:
    removed = await _queue_repo(request).delete(item_id)
    if removed is None:
        return errors.not_found("queue item", item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/queue/{item_id}/start", status_code=status.HTTP_201_CREATED)
async def start_queue_item(
    item_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
) -> Any:
    """Pop the queue item, submit it as a job, and return the job."""
    repo = _queue_repo(request)
    manager = _job_manager(request)
    prior = await manager.starts.get("queue-" + item_id)
    if prior:
        job = await manager.get(prior["job_id"])
        assert job is not None
        return job.model_dump(mode="json")
    item = await repo.get(item_id)
    if item is None:
        return errors.not_found("queue item", item_id)
    try:
        registry.get(item.printer_id)
    except PrinterNotFoundError:
        # Printer was deleted while item was queued — drop the orphan.
        await repo.delete(item_id)
        return errors.not_found("printer", item.printer_id)
    # Re-validate ams_mapping at start time in case slot range tightened.
    try:
        _validate_ams(item.ams_mapping)
    except _SlotError as exc:
        return errors.invalid_input(
            f"physical_slot must be 1-4, got {exc.value}",
            issues=[
                {
                    "code": "ams_slot_invalid",
                    "category": "ams",
                    "message": f"physical_slot must be 1-4, got {exc.value}",
                }
            ],
        )
    operation = await manager.start_stored(
        "queue-" + item_id,
        item.printer_id,
        item.file_name,
        item.file_path,
        item.ams_mapping,
        queue_id=item_id,
    )
    job = await manager.get(operation["job_id"])
    assert job is not None
    return job.model_dump(mode="json")
