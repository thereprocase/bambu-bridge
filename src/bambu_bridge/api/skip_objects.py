"""Skip Objects endpoints — OrcaSlicer's PartSkipDialog over HTTP.

* ``GET  /printers/{id}/skip_objects`` — the running plate's objects (id,
  name, skipped), the map as run-length rows for hit-testing a tap, whether
  a skip would be accepted now (``available`` + ``reason``), and the job
  identity the sheet was built from (``job``, ``gcode_file``, ``plate``,
  ``digest``). The map is Orca's pick image, or for a slice without one (the
  CLI renders none) the objects' printed footprint from the plate G-code
  (``map_source``).
* ``GET  /printers/{id}/skip_objects/map.png?checked=1,2&digest=…`` — the
  plate drawn in Orca's skip-canvas colours with those ids selected.
* ``POST /printers/{id}/skip_objects`` — ``obj_list``, the identity from the
  GET and the ``action`` the user confirmed (``skip``, or ``stop`` when the
  selection leaves no object, as Orca does). Behind the control capability
  gate: withheld ("under review") unless the bridge runs with
  ``BRIDGE_ENABLE_SKIP_OBJECTS=1``.

The running file is found the way the viewer and turntable find it: the
native inbox's verified copy, else ``preview_source.source_name`` (the
reported ``gcode_file`` when it names an archive), else the subtask name,
looked up exactly and strictly on printer storage.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from bambu_bridge import skip_objects as skip
from bambu_bridge.api.auth import require_auth, require_media_auth
from bambu_bridge.api.capabilities import (
    control_capability_gate,
    fresh_state,
    require_p1s,
    require_trusted_cert,
    unavailable,
)
from bambu_bridge.api.control import _online, _send
from bambu_bridge.api.printers import get_registry
from bambu_bridge.preview_source import source_name
from bambu_bridge.protocol import commands
from bambu_bridge.protocol.ftps import FtpsTransfer
from bambu_bridge.service.registry import PrinterNotFoundError, Registry
from bambu_bridge.service.viz_cache import VizFillError, locate_exact

router = APIRouter(prefix="/printers", tags=["control"])

CHANGED = "The print changed; reopen Skip Objects"
# Ids sent but not yet echoed in s_obj count as skipped for this long, like
# Orca's set_part_skipped_dirty filter after an apply.
PENDING_S = 30.0


@dataclass(frozen=True)
class Loaded:
    """A parsed job and what identifies it."""

    job_name: str | None
    gcode_file: str | None
    archive: str
    plates: tuple[int, ...]
    digest: str
    job: skip.SkipJob

    def identity(self) -> tuple[str | None, str | None, int, str]:
        return self.job_name, self.gcode_file, self.job.plate, self.digest


@dataclass
class _PrinterState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    loaded: Loaded | None = None
    pending_for: tuple[Any, ...] = ()
    pending: dict[int, float] = field(default_factory=dict)


def _states(request: Request) -> dict[str, _PrinterState]:
    states = getattr(request.app.state, "skip_objects", None)
    if states is None:
        states = defaultdict(_PrinterState)
        request.app.state.skip_objects = states
    return states


def _raw(service: Any) -> dict[str, Any]:
    return service.snapshot().get("_raw") or {}


def _candidates(raw: dict[str, Any]) -> tuple[list[str], bool]:
    """Exact archive names to look for, and whether only /cache may hold it.

    preview_source.source_name gives the reported gcode_file when it names an
    archive; when it is the inner plate member, the subtask name, tried as
    ``{subtask}.gcode.3mf`` then ``{subtask}.3mf`` (never stem-truncated).
    """
    name = source_name(raw)
    if not name:
        return [], False
    path = PurePosixPath(name)
    if name != str(raw.get("gcode_file") or ""):
        names = [name] if name.endswith(".3mf") else [f"{name}.gcode.3mf", f"{name}.3mf"]
        return names, False
    return [path.name], "cache" in path.parts


async def _source(request: Request, printer_id: str, service: Any) -> tuple[str, bytes]:
    """(archive name, bytes) of the running print, or an HTTP refusal."""
    snapshot = service.snapshot()
    raw = snapshot.get("_raw") or {}
    gateway = getattr(request.app.state, "native_gateway", None)
    if gateway is not None:
        local = await asyncio.to_thread(gateway.local_camera_source, snapshot)
        if local is not None:
            archive = str(raw.get("gcode_file") or "").rsplit("/", 1)[-1]
            if not archive.endswith(".3mf"):
                archive = f"{raw.get('subtask_name') or ''}.gcode.3mf"
            return archive, local
    names, cache_only = _candidates(raw)
    if not names:
        raise HTTPException(404, "No current job on this printer.")
    from bambu_bridge.api.viz import _get_viz_cache

    viz = _get_viz_cache(request)
    ftps = FtpsTransfer(service.ip, service.access_code, port=request.app.state.ftps_port)
    try:
        remote_dir, filename = await locate_exact(ftps, names, cache_only)
        await viz.validate_revision(printer_id, ftps, remote_dir, filename)
        data = await viz.source_bytes(printer_id, ftps, remote_dir, filename)
    except VizFillError as exc:
        status = {"not_found": 404, "ambiguous": 409}.get(exc.kind, 502)
        raise HTTPException(status, exc.detail) from exc
    except Exception as exc:  # noqa: BLE001 — FTPS failure
        raise HTTPException(502, f"Could not read the print file: {exc}") from exc
    return filename, data


async def _load(request: Request, printer_id: str, service: Any) -> Loaded:
    """The running job, re-read from storage; parsed once per file and plate."""
    raw = _raw(service)
    job_name, gcode_file = raw.get("subtask_name"), raw.get("gcode_file")
    archive, data = await _source(request, printer_id, service)
    digest = await asyncio.to_thread(lambda: hashlib.sha256(data).hexdigest())
    try:
        plates = tuple(await asyncio.to_thread(skip.archive_plates, data))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    plate = skip.resolve_plate(raw, list(plates), archive) or 0
    state = _states(request)[printer_id]
    hit = state.loaded
    if hit and (hit.digest, hit.job.plate) == (digest, plate):
        job = hit.job
    elif plate:
        try:
            job = await asyncio.to_thread(skip.read_job, data, plate)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    else:
        job = skip.SkipJob(0, False, ())
    state.loaded = Loaded(job_name, gcode_file, archive, plates, digest, job)
    return state.loaded


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
    loaded = await _load(request, printer_id, service)
    job = loaded.job
    skipped = set(skip.skipped_ids(_raw(service)))
    reason = _availability(service, job)
    pick = job.pick
    return {
        "job": loaded.job_name,
        "gcode_file": loaded.gcode_file,
        "plate": job.plate,
        "digest": loaded.digest,
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
    """identify_ids from ``1,2,3``; anything that is not a uint32 is a 422."""
    try:
        ids = frozenset(int(t) for t in text.split(",") if t.strip())
    except ValueError:
        ids = frozenset({-1})
    if any(not 0 <= i < 2**32 for i in ids):
        raise HTTPException(422, "checked must be comma-separated object ids")
    return ids


@router.get("/{printer_id}/skip_objects/map.png", dependencies=[Depends(require_media_auth)])
async def get_skip_map(
    printer_id: str,
    request: Request,
    checked: str = "",
    digest: str = "",
    registry: Registry = Depends(get_registry),
) -> Response:
    service = _service(registry, printer_id)
    selected = _ids(checked)
    raw = _raw(service)
    hit = _states(request)[printer_id].loaded
    if not (
        hit
        and (hit.job_name, hit.gcode_file) == (raw.get("subtask_name"), raw.get("gcode_file"))
        and (not digest or hit.digest == digest)
    ):
        hit = await _load(request, printer_id, service)
    if digest and hit.digest != digest:
        raise HTTPException(409, CHANGED)
    job = hit.job
    if job.pick is None:
        raise HTTPException(404, "This print file has no object map.")
    skipped = frozenset(skip.skipped_ids(raw)) & job.ids
    png = await asyncio.to_thread(
        skip.render_map, job.pick, (selected & job.ids) - skipped, skipped
    )
    # The URL can carry the owner key (?token=): never keep it in a cache.
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


class SkipObjectsBody(BaseModel):
    """The objects to skip, the job they were picked from, and the confirmed action."""

    model_config = ConfigDict(extra="forbid")

    obj_list: list[int] = Field(min_length=1, max_length=skip.MAX_OBJECTS)
    job: str | None
    gcode_file: str | None
    plate: int
    digest: str = Field(min_length=1)
    action: Literal["skip", "stop"]


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
    fresh RUNNING/PAUSE status. Under a per-printer lock the job is re-read,
    then everything is checked again against the printer's latest report:
    the state, part-skip support, the job identity the user saw, and, like
    PartSkipDialog, a labelled plate of at most 64 objects whose listed,
    not-yet-skipped ids were chosen. Ids sent moments ago count as skipped
    until the printer echoes them, so the request that empties the plate
    stops the print, and the client must have confirmed that same action.
    """
    state = _states(request)[printer_id]
    async with state.lock:
        service = _online(registry, printer_id)
        reason = skip.unavailable_reason(_raw(service))
        if reason:
            unavailable(reason)
        loaded = await _load(request, printer_id, service)
        # The load can take seconds of FTPS: judge the printer as it is now.
        if fresh_state(service) not in skip.SKIP_STATES:
            unavailable(f"Printer state: {_raw(service).get('gcode_state')}")
        raw = _raw(service)
        reason = skip.unavailable_reason(raw)
        if reason:
            unavailable(reason)
        now_plate = skip.resolve_plate(raw, list(loaded.plates), loaded.archive) or 0
        seen = (body.job, body.gcode_file, body.plate, body.digest)
        current = (raw.get("subtask_name"), raw.get("gcode_file"), now_plate, loaded.digest)
        if seen != loaded.identity() or current != loaded.identity():
            unavailable(CHANGED)
        reason = skip.job_reason(loaded.job)
        if reason:
            unavailable(reason)
        reported = skip.skipped_ids(raw)
        clock = time.monotonic()
        if state.pending_for != loaded.identity():
            state.pending_for, state.pending = loaded.identity(), {}
        state.pending = {
            i: t for i, t in state.pending.items() if i not in reported and t > clock
        }
        try:
            action, ids = skip.plan(loaded.job, [*reported, *state.pending], body.obj_list)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if action != body.action:
            unavailable(
                "Skipping these objects would now stop the print; reopen Skip Objects"
                if action == "stop"
                else "Other objects remain now; reopen Skip Objects"
            )
        envelope = commands.skip_objects(ids) if action == "skip" else commands.print_stop()
        result = await _send(service, envelope)
        state.pending.update({i: clock + PENDING_S for i in ids})
        return {**result, "action": action}
