"""Job endpoints (spec 6 "Jobs" + M4).

``POST /printers/{id}/jobs`` takes the 3MF as multipart and drives the whole
machine in one call — but FTPS upload and ``print.project_file`` remain
*distinct* transitions internally (spec 5.2). ``GET /jobs/{id}`` returns the
job plus its full event log.
"""

from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)

from bambu_bridge.api import errors as api_errors
from bambu_bridge.api.auth import require_auth
from bambu_bridge.api.printers import get_registry
from bambu_bridge.db.jobs import JobState
from bambu_bridge.service.jobs import JobManager, JobNotFoundError
from bambu_bridge.service.registry import PrinterNotFoundError, Registry
from bambu_bridge.slicedoc import validate as slice_validate

router = APIRouter(tags=["jobs"], dependencies=[Depends(require_auth)])


def get_jobs(request: Request) -> JobManager:
    return request.app.state.jobs  # type: ignore[no-any-return]


def _parse_ams(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    try:
        return [int(x) for x in raw.split(",") if x.strip() != ""]
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ams_mapping must be comma-separated integers",
        ) from exc


_GATE_CATEGORY = {
    "G1": "container",   # zip / file format
    "G2": "members",     # required entries present
    "G3": "checksum",    # md5 integrity
    "G4": "thermal",     # safe temperature range
    "G5": "ams",         # filament / AMS mapping coherence
}


def _structure_issues(raw_issues: list[str]) -> list[dict[str, str]]:
    """Parse the validator's ``"G5 ams_mapping arity 2 != 1 …"`` strings into
    the contract §7.2 ``{code, category, message}`` shape so the APK can
    render an "issue inspector" without re-parsing prose."""
    out: list[dict[str, str]] = []
    for line in raw_issues:
        head, _, rest = line.partition(" ")
        if head in _GATE_CATEGORY:
            out.append(
                {"code": head, "category": _GATE_CATEGORY[head], "message": rest}
            )
        else:
            out.append({"code": "unknown", "category": "validate", "message": line})
    return out


@router.post("/printers/{printer_id}/jobs", status_code=status.HTTP_201_CREATED)
async def submit_job(
    printer_id: str,
    file: UploadFile,
    request: Request,
    ams_mapping: str | None = Form(default=None),
    registry: Registry = Depends(get_registry),
    jobs: JobManager = Depends(get_jobs),
) -> dict[str, Any]:
    """Upload a 3MF and start the print (queued -> ... -> completed).

    Contract §7.2: the .gcode.3mf is **validated synchronously** before the
    queued job is created. A malformed container returns ``422 invalid_3mf``
    with structured issues — APK shows the issue list without waiting on a
    WS event. (service/jobs._lifecycle keeps its own validate() as a
    belt-and-suspenders against any future async re-entry.)
    """
    name = file.filename or "upload.3mf"
    data = await file.read()
    ams = _parse_ams(ams_mapping)
    report = slice_validate(data, expected_ams_mapping=ams)
    if not report.ok:
        return api_errors.envelope(  # type: ignore[return-value]
            error=api_errors.ERR_INVALID_3MF,
            message="The .gcode.3mf failed validation; the print was not started.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            context={"file_name": name, "printer_id": printer_id},
            extra={"issues": _structure_issues(report.issues)},
        )
    try:
        job = await jobs.submit(printer_id, data, name, ams_mapping=ams)
    except PrinterNotFoundError:
        return api_errors.not_found("printer", printer_id)  # type: ignore[return-value]
    return job.model_dump(mode="json")


@router.get("/jobs")
async def list_jobs(
    jobs: JobManager = Depends(get_jobs),
    printer_id: str | None = None,
    state: JobState | None = None,
    since: int | None = None,
    until: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Paginated history (filters: printer_id, state, queued-at range)."""
    history = await jobs.history(
        printer_id=printer_id,
        state=state,
        since=since,
        until=until,
        limit=min(limit, 200),
        offset=offset,
    )
    return [j.model_dump(mode="json") for j in history]


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str, jobs: JobManager = Depends(get_jobs)
) -> dict[str, Any]:
    """Job detail with its full event log."""
    job = await jobs.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"job {job_id} not found"
        )
    return {
        "job": job.model_dump(mode="json"),
        "events": await jobs.events_for(job_id),
    }


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str, jobs: JobManager = Depends(get_jobs)
) -> dict[str, Any]:
    try:
        job = await jobs.cancel(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"job {job_id} not found"
        ) from exc
    return job.model_dump(mode="json")
