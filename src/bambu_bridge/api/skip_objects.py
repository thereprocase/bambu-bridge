"""Skip Objects endpoints — OrcaSlicer's PartSkipDialog over HTTP.

* ``GET  /printers/{id}/skip_objects`` — the running plate's objects (id,
  name, skipped), the pick map as run-length rows for hit-testing a tap, and
  whether a skip would be accepted now (``available`` + ``reason``). The map
  is Orca's pick image, or for a slice without one (the CLI renders none)
  the objects' printed footprint from the plate G-code (``map_source``).
* ``GET  /printers/{id}/skip_objects/map.png?checked=1,2`` — the plate drawn
  in Orca's skip-canvas colours with those ids selected.
* ``POST /printers/{id}/skip_objects`` ``{obj_list: [...]}`` — validated like
  the dialog, then ``print.skip_objects``; a selection that leaves no object
  stops the print, as Orca does. Behind the control capability gate:
  withheld ("under review") unless the bridge runs with
  ``BRIDGE_ENABLE_SKIP_OBJECTS=1``.

The job's project is read from printer storage by its ``subtask_name``
through the viewer's source cache (one download per print, revalidated by
SIZE/MDTM), and parsed once per job for the map route.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from bambu_bridge import skip_objects as skip
from bambu_bridge.api.auth import require_auth, require_media_auth
from bambu_bridge.api.capabilities import (
    control_capability_gate,
    require_p1s,
    require_trusted_cert,
    unavailable,
)
from bambu_bridge.api.control import _online, _send
from bambu_bridge.api.printers import get_registry
from bambu_bridge.protocol import commands
from bambu_bridge.protocol.ftps import FtpsTransfer
from bambu_bridge.service.registry import PrinterNotFoundError, Registry
from bambu_bridge.service.viz_cache import VizFillError

router = APIRouter(prefix="/printers", tags=["control"])

_JOBS_KEY = "skip_jobs"


def _raw(service: Any) -> dict[str, Any]:
    return service.snapshot().get("_raw") or {}


async def _load(request: Request, printer_id: str, service: Any) -> skip.SkipJob:
    """The running job's SkipJob; refreshed from printer storage."""
    from bambu_bridge.api.viz import _get_viz_cache

    raw = _raw(service)
    job_name = raw.get("subtask_name")
    if not job_name:
        raise HTTPException(404, "No current job on this printer.")
    plate = skip.plate_index(raw)
    ftps = FtpsTransfer(service.ip, service.access_code, port=request.app.state.ftps_port)
    try:
        data = await _get_viz_cache(request).acquire_source(printer_id, ftps, job_name)
    except VizFillError as exc:
        raise HTTPException(404, exc.detail) from exc
    except Exception as exc:  # noqa: BLE001 — FTPS failure
        raise HTTPException(502, f"Could not read the print file: {exc}") from exc
    jobs = getattr(request.app.state, _JOBS_KEY, None)
    if jobs is None:
        jobs = {}
        setattr(request.app.state, _JOBS_KEY, jobs)
    # A G-code footprint takes seconds on a 20 MB plate: parse each file once.
    digest = await asyncio.to_thread(lambda: hashlib.sha256(data).hexdigest())
    hit = jobs.get(printer_id)
    if hit and hit[:3] == (job_name, plate, digest):
        return hit[3]  # type: ignore[no-any-return]
    try:
        job = await asyncio.to_thread(skip.read_job, data, plate)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    jobs[printer_id] = (job_name, plate, digest, job)
    return job


async def _cached(request: Request, printer_id: str, service: Any) -> skip.SkipJob:
    raw = _raw(service)
    hit = (getattr(request.app.state, _JOBS_KEY, None) or {}).get(printer_id)
    if hit and hit[0] == raw.get("subtask_name") and hit[1] == skip.plate_index(raw):
        return hit[3]  # type: ignore[no-any-return]
    return await _load(request, printer_id, service)


def _availability(service: Any, job: skip.SkipJob) -> str | None:
    """Why a POST would be refused right now, or None."""
    try:
        require_trusted_cert(service)
        require_p1s(service)
    except HTTPException as exc:
        detail: Any = exc.detail
        return str(detail.get("message") if isinstance(detail, dict) else detail)
    if not skip.enabled():
        return "Control support under review"
    return skip.unavailable_reason(_raw(service)) or skip.job_reason(job)


def _service(registry: Registry, printer_id: str) -> Any:
    try:
        return registry.get(printer_id)
    except PrinterNotFoundError as exc:
        raise HTTPException(404, f"printer {printer_id} not found") from exc


@router.get("/{printer_id}/skip_objects", dependencies=[Depends(require_auth)])
async def get_skip_objects(
    printer_id: str, request: Request, registry: Registry = Depends(get_registry)
) -> dict[str, Any]:
    service = _service(registry, printer_id)
    job = await _load(request, printer_id, service)
    raw = _raw(service)
    skipped = set(skip.skipped_ids(raw))
    reason = _availability(service, job)
    pick = job.pick
    return {
        "job": raw.get("subtask_name"),
        "plate": job.plate,
        "label_object_enabled": job.label_object_enabled,
        "map_source": job.map_source,
        "max_objects": skip.MAX_OBJECTS,
        "objects": [{"id": o.id, "name": o.name, "skipped": o.id in skipped} for o in job.objects],
        "map": None
        if pick is None
        else {
            "width": int(pick.shape[1]),
            "height": int(pick.shape[0]),
            "rows": await asyncio.to_thread(skip.run_rows, pick),
        },
        "available": reason is None,
        "reason": reason,
    }


def _ids(text: str) -> frozenset[int]:
    try:
        return frozenset(int(t) for t in text.split(",") if t.strip())
    except ValueError as exc:
        raise HTTPException(422, "checked must be comma-separated integers") from exc


@router.get("/{printer_id}/skip_objects/map.png", dependencies=[Depends(require_media_auth)])
async def get_skip_map(
    printer_id: str,
    request: Request,
    checked: str = "",
    registry: Registry = Depends(get_registry),
) -> Response:
    service = _service(registry, printer_id)
    job = await _cached(request, printer_id, service)
    if job.pick is None:
        raise HTTPException(404, "This print file has no object map.")
    skipped = frozenset(skip.skipped_ids(_raw(service)))
    png = await asyncio.to_thread(skip.render_map, job.pick, _ids(checked) - skipped, skipped)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, no-cache"})


class SkipObjectsBody(BaseModel):
    """The objects to skip: identify_ids from the GET's ``objects``."""

    model_config = ConfigDict(extra="forbid")

    obj_list: list[int] = Field(min_length=1, max_length=skip.MAX_OBJECTS)


@router.post(
    "/{printer_id}/skip_objects",
    dependencies=[Depends(require_auth), Depends(control_capability_gate)],
)
async def post_skip_objects(
    printer_id: str,
    body: SkipObjectsBody,
    request: Request,
    registry: Registry = Depends(get_registry),
) -> dict[str, Any]:
    """Skip objects in the running print (irreversible).

    The capability gate has already required the opt-in, a trusted P1S and
    fresh RUNNING/PAUSE status. Then, like PartSkipDialog: the printer must
    report part-skip support, the job must be labelled and within 64 objects,
    and every id must be a not-yet-skipped object of this plate.
    """
    service = _online(registry, printer_id)
    raw = _raw(service)
    reason = skip.unavailable_reason(raw)
    if reason:
        unavailable(reason)
    job = await _load(request, printer_id, service)
    reason = skip.job_reason(job)
    if reason:
        unavailable(reason)
    try:
        action, ids = skip.plan(job, skip.skipped_ids(raw), body.obj_list)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    envelope = commands.skip_objects(ids) if action == "skip" else commands.print_stop()
    return {**await _send(service, envelope), "action": action}
