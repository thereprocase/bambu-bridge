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

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from bambu_bridge.api import errors
from bambu_bridge.api.auth import require_auth
from bambu_bridge.api.printers import get_registry
from bambu_bridge.db.jobs import QueueRepo
from bambu_bridge.service.jobs import JobManager
from bambu_bridge.service.registry import PrinterNotFoundError, Registry

router = APIRouter(tags=["queue"], dependencies=[Depends(require_auth)])


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


@router.post(
    "/printers/{printer_id}/queue", status_code=status.HTTP_201_CREATED
)
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
async def reorder_queue_item(
    item_id: str, body: ReorderQueueItem, request: Request
) -> Any:
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


@router.post(
    "/queue/{item_id}/start", status_code=status.HTTP_201_CREATED
)
async def start_queue_item(
    item_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
) -> Any:
    """Pop the queue item, submit it as a job, and return the job."""
    repo = _queue_repo(request)
    item = await repo.get(item_id)
    if item is None:
        return errors.not_found("queue item", item_id)
    try:
        service = registry.get(item.printer_id)
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
    # Pull bytes from FTPS so JobManager.submit() can re-validate the 3MF.
    # The .gcode.3mf was already uploaded; we just round-trip to validate.
    from bambu_bridge.protocol.ftps import FtpsTransfer

    ftps = FtpsTransfer(
        service.ip,
        service.access_code,
        port=request.app.state.ftps_port,
    )
    try:
        data = await ftps.download_bytes(item.file_name, remote_dir="")
    except Exception as exc:  # noqa: BLE001 — FTPS failure → 502
        return errors.envelope(
            error=errors.ERR_FTPS_FAILED,
            message=f"Couldn't retrieve staged file: {type(exc).__name__}: {exc}",
            status_code=status.HTTP_502_BAD_GATEWAY,
            context={"printer_id": item.printer_id, "file_name": item.file_name},
            raw={"exc_type": type(exc).__name__, "exc_str": str(exc) or repr(exc)},
        )
    job = await _job_manager(request).submit(
        item.printer_id,
        data,
        item.file_name,
        ams_mapping=item.ams_mapping,
    )
    # Remove from queue once accepted.
    await repo.delete(item_id)
    return job.model_dump(mode="json")
