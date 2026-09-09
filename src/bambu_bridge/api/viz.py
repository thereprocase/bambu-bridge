"""Mesh visualisation endpoints (spec G4).

Three routes, all under ``/api/v1/printers/{printer_id}``:

* ``GET /{printer_id}/viz/mesh`` — downloads the printer's current 3MF job
  file, parses it, and returns mesh + metadata as JSON.  Auth: master key OR
  viz token via ``Authorization: Bearer`` header or ``?token=`` query param.

* ``GET /{printer_id}/viz/toolpath`` — downloads the printer's current
  ``.gcode.3mf`` file, extracts ``Metadata/plate_1.gcode``, parses the
  extrusion moves, and returns a GL_LINES-ready float32 buffer.

  Two formats, selected by ``?fmt=`` query parameter:

  * ``fmt=json`` (default, HA iframe compat) — same JSON shape as before,
    positions as base64.
  * ``fmt=bin`` — ``application/octet-stream``:

      - 4 bytes : uint32-LE header length (H)
      - H bytes : UTF-8 JSON header: ``{segment_count, bbox, layer_table,
                  job, total_layers, layer_height_mm, decimated,
                  source_file, cached}``
      - remainder : raw float32-LE positions (segment_count × 2 × 3 × 4 bytes)

    Gzip is applied to the bin response only when compression is ≥ 15%
    (floats compress poorly; the threshold avoids wasting CPU for no gain).
    Same ETag/304 semantics as the JSON path, but the ETag is
    **representation-specific**: it folds the wire format into the tag
    (``…:bin`` vs ``…:json``) so a client that cached one format never
    receives a 304 for the other and reuses the wrong bytes (RFC 7232 §2.3).

  Auth: same ``require_read_or_viz`` dependency.

* ``GET /{printer_id}/viz`` — serves ``src/bambu_bridge/static/viewer.html``
  (authored separately).  Auth: master key OR viz token via ``?token=`` or
  bearer header (browsers in an iframe can't set Authorization headers).
  Returns HTTP 501 if the static file is not yet present.

Caching
-------
Parsed meshes and parsed toolpaths are cached in the shared
:class:`~bambu_bridge.service.viz_cache.VizCache` object stored on
``app.state.viz_cache_obj``.  Both caches use a simple LRU-ish eviction
(≤ 3 entries each).  The toolpath cache stores the PARSED float array, not
the raw gcode bytes.  The cache key includes the file size so a re-sliced
file with the same name is never stale.

The same VizCache instance is held by the JobManager so the pre-warm path
(triggered on ``print_started``) can fill both caches from the service layer
without importing any FastAPI symbols.

HTTP caching (ETag / compression)
----------------------------------
Both ``/viz/mesh`` and ``/viz/toolpath`` carry:

* **ETag**: ``"<filename>:<size_bytes>:<fmt>"`` (strong, double-quoted per
  RFC 7232).  The trailing ``<fmt>`` (``json`` or ``bin``) makes the tag
  **representation-specific**: ``/viz/mesh`` always uses ``json``;
  ``/viz/toolpath`` uses the requested ``fmt`` so its two formats never share
  a tag.  An ETag can only be emitted after the file size is known (i.e. after
  the first download).  Subsequent requests whose cached key AND format match
  the ``If-None-Match`` header receive ``304 Not Modified`` *without*
  re-parsing or re-serialising the payload.

* **Cache-Control**: ``private, max-age=0, must-revalidate`` — clients always
  revalidate, but reuse the stored body on ``304`` (safe for per-user viz
  tokens; ``private`` prevents shared-proxy caching).

* **Gzip compression**: JSON responses are compressed when the client sends
  ``Accept-Encoding: gzip`` and the body is > 1 KB.  Binary (``fmt=bin``)
  responses are compressed only when gzip reduces size by ≥ 15% (float32
  data compresses poorly; skipping saves CPU for negligible benefit).
  Compression is applied in-response (not via middleware) to avoid touching
  the MJPEG ``StreamingResponse`` in camera.py, which is incompatible with
  buffering middleware.  GZipMiddleware is therefore **not** added to the app.
"""

from __future__ import annotations

import base64
import gzip as _gzip
import json as _json
import struct
from pathlib import Path
from typing import Any
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from bambu_bridge.api.auth import require_read_or_viz
from bambu_bridge.api.printers import get_registry
from bambu_bridge.protocol.ftps import FtpsTransfer
from bambu_bridge.protocol.gcode_path import GcodeToolpath
from bambu_bridge.protocol.threemf import Mesh3MF
from bambu_bridge.service.registry import PrinterNotFoundError, Registry
from bambu_bridge.service.viz_cache import (
    VizCache,
    VizFillError,
)
from bambu_bridge.service.viz_cache import (
    find_3mf as _find_3mf_impl,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/printers", tags=["viz"])


# ------------------------------------------------------------------ #
# ETag / caching helpers
# ------------------------------------------------------------------ #

_CACHE_CONTROL_VIZ = "private, max-age=0, must-revalidate"
_GZIP_MIN_BYTES = 1024
_GZIP_LEVEL = 6
# Binary format: skip gzip when savings are below this fraction of original.
# Float32 data typically compresses 5-10%; requiring 15% avoids wasting CPU.
_BIN_GZIP_MIN_SAVINGS = 0.15


def _viz_etag(
    filename: str, size: int, repr_fmt: str = "json", revision: str = ""
) -> str:
    """Return a strong ETag for ONE representation of a viz resource.

    A strong ETag must identify a single representation (RFC 7232 §2.3): a
    client that cached the JSON body under an ETag and then requests the binary
    body must NOT be told ``304`` and reuse the wrong bytes.  The wire format
    (*repr_fmt*, ``"json"`` or ``"bin"``) is therefore folded into the tag, so
    ``fmt=json`` and ``fmt=bin`` carry distinct ETags for the same source file.

    The value is double-quoted as required by RFC 7232 §2.3.
    """
    return f'"{quote(filename, safe="")}:{size}:{revision}:{repr_fmt}"'


def _wants_gzip(request: Request) -> bool:
    """Return True when the client advertises ``gzip`` in Accept-Encoding."""
    return "gzip" in request.headers.get("Accept-Encoding", "")


def _json_response_with_headers(
    content: dict[str, Any],
    request: Request,
    *,
    extra_headers: dict[str, str],
) -> Response:
    """Serialise *content* to JSON; gzip if the client accepts it and body is
    large enough.  Always adds *extra_headers* (ETag, Cache-Control, …).

    Always returns a plain :class:`~starlette.responses.Response` so that
    ``Content-Encoding`` can be set directly without middleware involvement.
    """
    body = _json.dumps(content).encode()
    headers = {
        "Content-Type": "application/json",
        **extra_headers,
    }
    if _wants_gzip(request) and len(body) >= _GZIP_MIN_BYTES:
        compressed = _gzip.compress(body, compresslevel=_GZIP_LEVEL)
        headers["Content-Encoding"] = "gzip"
        headers["Content-Length"] = str(len(compressed))
        return Response(content=compressed, status_code=200, headers=headers)

    headers["Content-Length"] = str(len(body))
    return Response(content=body, status_code=200, headers=headers)


# ------------------------------------------------------------------ #
# In-process mesh cache
# ------------------------------------------------------------------ #

# _CacheKey is imported from service.viz_cache (single definition).
# _CACHE_MAX and the put helpers live in VizCache; the API layer accesses
# the shared VizCache object from app.state.viz_cache_obj.


def _get_viz_cache(request: Request) -> VizCache:
    """Return the shared VizCache from app.state; create a fallback if absent.

    The fallback path exists for tests that build an app without wiring a
    VizCache in the lifespan (all pre-existing viz tests).  In production
    ``main.py`` always sets ``app.state.viz_cache_obj`` during lifespan.
    """
    obj = getattr(request.app.state, "viz_cache_obj", None)
    if obj is None:
        # Lazy fallback: create an unshared instance so the endpoint works
        # even if main.py has not wired the cache yet (test / legacy path).
        obj = VizCache(ftps_port=getattr(request.app.state, "ftps_port", 990))
        request.app.state.viz_cache_obj = obj
    return obj


# ------------------------------------------------------------------ #
# FTPS helpers
# ------------------------------------------------------------------ #


def _ftps_for(request: Request, registry: Registry, printer_id: str) -> FtpsTransfer:
    """Build an FtpsTransfer for ``printer_id``; raises 404 if not found."""
    try:
        service = registry.get(printer_id)
    except PrinterNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"printer {printer_id!r} not found",
        ) from exc
    return FtpsTransfer(
        service.ip,
        service.access_code,
        port=request.app.state.ftps_port,
    )


async def _find_3mf(
    ftps: FtpsTransfer, job_name: str
) -> tuple[str, str] | None:
    """Locate the .3mf (or .gcode.3mf) file matching ``job_name``.

    Thin wrapper around the canonical ``find_3mf`` in service/viz_cache.py so
    internal call sites in this module are unchanged.
    """
    return await _find_3mf_impl(ftps, job_name)


# ------------------------------------------------------------------ #
# Mesh endpoint
# ------------------------------------------------------------------ #


@router.get("/{printer_id}/viz/mesh")
async def get_viz_mesh(
    printer_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
    _auth: None = Depends(require_read_or_viz),
) -> Response:
    """Download, parse, and return the printer's current 3MF job as mesh JSON.

    Response shape::

        {
          "job": str | null,
          "total_layers": int | null,
          "layer_height_mm": float | null,
          "geometry_available": bool,  # false for Bambu .gcode.3mf (no mesh)
          "vertex_count": int,
          "triangle_count": int,
          "vertices_b64": str,    # base64 of float32 LE byte array
          "indices_b64": str,     # base64 of uint32 LE byte array
          "bbox": {"min": [x,y,z], "max": [x,y,z]},
          "filaments": [{"slot": int, "type": str|null, "color": "#RRGGBB"|null}],
          "source_file": str,
          "cached": bool
        }

    When ``geometry_available`` is ``false`` the archive is a Bambu sliced
    output (``.gcode.3mf``).  ``vertex_count`` and ``triangle_count`` are
    both 0 and the arrays are empty.  ``bbox`` is populated from
    ``Metadata/plate_1.json`` (2D footprint; z is 0.0) when available.

    Includes ``ETag`` and ``Cache-Control`` headers for HTTP-level caching
    (see module docstring).
    """
    # ---- Resolve current job ------------------------------------------- #
    try:
        service = registry.get(printer_id)
    except PrinterNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"printer {printer_id!r} not found",
        ) from exc

    snap = service.snapshot()
    job_block: dict[str, Any] = snap.get("job") or {}
    job_name: str | None = job_block.get("subtask_name") or None
    total_layers: int | None = job_block.get("total_layer_num")

    if not job_name:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No current job on this printer.",
        )

    # ---- Build FTPS client --------------------------------------------- #
    ftps = _ftps_for(request, registry, printer_id)

    # ---- Locate the 3MF on the printer ---------------------------------- #
    try:
        location = await _find_3mf(ftps, job_name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"FTPS list failed: {exc!s}",
        ) from exc

    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"3MF file for job {job_name!r} not found on printer storage.",
        )

    remote_dir, filename = location
    try:
        await _get_viz_cache(request).validate_revision(printer_id, ftps, remote_dir, filename)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"FTPS metadata failed: {exc}") from exc

    # ---- Try cache ----------------------------------------------------- #
    viz_cache = _get_viz_cache(request)
    # list_dir returns only filenames, not sizes — the file size is only
    # known after download.  The optimistic pre-check below matches on
    # (printer_id, filename); when the cached size matches the ETag we can
    # short-circuit the whole download+parse+serialise pipeline.
    mesh_hit = viz_cache.lookup_mesh(printer_id, filename)

    if mesh_hit is not None:
        cache_key_hit, cached_mesh = mesh_hit
        # We know the file size from the cache key — derive the ETag and
        # check If-None-Match *before* serialising the payload.
        cached_size = cache_key_hit[2]
        etag = _viz_etag(filename, cached_size, revision=viz_cache.content_id(printer_id, filename))
        if request.headers.get("If-None-Match") == etag:
            return Response(
                status_code=304,
                headers={
                    "ETag": etag,
                    "Cache-Control": _CACHE_CONTROL_VIZ,
                },
            )
        log.info(
            "viz.cache_hit",
            printer_id=printer_id,
            filename=filename,
        )
        return _mesh_response(
            mesh=cached_mesh,
            job_name=job_name,
            total_layers=total_layers,
            source_file=filename,
            cached=True,
            etag=etag,
            request=request,
        )

    # ---- Cold load: delegate to VizCache (fills memo + cache) ----------- #
    # VizCache.fill_mesh_for_request is the single authoritative pipeline:
    # download → sliced-date memo → parse → cache-insert.  Using it here
    # (instead of inline download+parse) ensures the sliced-date memo is
    # always populated on the first endpoint-triggered load, not only on
    # pre-warm.
    try:
        filename, mesh = await viz_cache.fill_mesh_for_request(
            printer_id, service.ip, service.access_code, job_name
        )
    except VizFillError as exc:
        _HTTP_STATUS = {
            "not_found": status.HTTP_404_NOT_FOUND,
            "download":  status.HTTP_502_BAD_GATEWAY,
            "parse":     status.HTTP_422_UNPROCESSABLE_ENTITY,
        }
        raise HTTPException(
            status_code=_HTTP_STATUS.get(exc.kind, status.HTTP_502_BAD_GATEWAY),
            detail=exc.detail,
        ) from exc

    # The cache key was written by fill_mesh_for_request; look up the size.
    mesh_hit = viz_cache.lookup_mesh(printer_id, filename)
    if mesh_hit is not None:
        file_size = mesh_hit[0][2]
    else:
        # Should not happen: fill_mesh_for_request just inserted this key.
        # Falling back to size 0 yields a stable-but-wrong ETag; log loudly
        # rather than silently disabling 304s for this resource.
        file_size = 0
        log.warning(
            "viz.etag_size_fallback",
            printer_id=printer_id,
            filename=filename,
            kind="mesh",
        )
    etag = _viz_etag(filename, file_size, revision=viz_cache.content_id(printer_id, filename))
    return _mesh_response(
        mesh=mesh,
        job_name=job_name,
        total_layers=total_layers,
        source_file=filename,
        cached=False,
        etag=etag,
        request=request,
    )


def _mesh_response(
    *,
    mesh: Mesh3MF,
    job_name: str | None,
    total_layers: int | None,
    source_file: str,
    cached: bool,
    etag: str,
    request: Request,
) -> Response:
    """Serialise a :class:`Mesh3MF` to the wire JSON shape.

    Applies gzip compression when the client accepts it and body is large
    enough (see :func:`_json_response_with_headers`).
    """
    # Convert array('f') to little-endian float32 bytes.
    # array.tobytes() returns native byte order; we must ensure LE.
    if struct.pack("=f", 1.0) == struct.pack("<f", 1.0):
        # Native is already LE (x86, ARM LE) — zero-copy path.
        verts_bytes = mesh.vertices.tobytes()
        idx_bytes = mesh.indices.tobytes()
    else:
        # Big-endian host: convert element by element.
        verts_bytes = struct.pack(f"<{len(mesh.vertices)}f", *mesh.vertices)
        idx_bytes = struct.pack(f"<{len(mesh.indices)}I", *mesh.indices)

    return _json_response_with_headers(
        {
            "job": job_name,
            "total_layers": total_layers,
            "layer_height_mm": mesh.layer_height_mm,
            "geometry_available": mesh.geometry_available,
            "vertex_count": mesh.vertex_count,
            "triangle_count": mesh.triangle_count,
            "vertices_b64": base64.b64encode(verts_bytes).decode(),
            "indices_b64": base64.b64encode(idx_bytes).decode(),
            "bbox": {
                "min": mesh.bbox_min,
                "max": mesh.bbox_max,
            },
            "filaments": [
                {"slot": f.slot, "type": f.type, "color": f.color}
                for f in mesh.filaments
            ],
            "source_file": source_file,
            "cached": cached,
        },
        request,
        extra_headers={
            "ETag": etag,
            "Cache-Control": _CACHE_CONTROL_VIZ,
        },
    )


# ------------------------------------------------------------------ #
# Binary toolpath helpers
# ------------------------------------------------------------------ #


def _build_bin_toolpath(
    *,
    tp: GcodeToolpath,
    job_name: str | None,
    total_layers: int | None,
    layer_height: float | None,
    source_file: str,
    cached: bool,
) -> bytes:
    """Serialise a :class:`GcodeToolpath` to the binary wire format.

    Layout::

        [uint32-LE header_len][<header_len> bytes of UTF-8 JSON][float32-LE positions]

    The JSON header contains all scalar metadata so the client can render the
    HUD and scrub UI before it has parsed a single float.  The positions block
    that follows is a contiguous float32-LE array: xyzxyz per segment vertex.

    The ``layer_table`` field in the header is a compact form of the same layer
    index that the viewer's ``parseToolpath`` builds client-side from a slow
    float32 scan.  Each entry is ``[v0, v1, z]`` (int start vertex, int end
    vertex, float Z in mm).  Shipping this pre-computed eliminates the
    O(segment_count) scan from the client hot path.
    """
    # ---- Build layer table from positions --------------------------------- #
    # Mirror the viewer's layer-splitting logic (half-layer threshold) so the
    # server and client always agree on layer boundaries.  We do this once at
    # pre-warm / first-request time; the client reuses it directly.
    layer_height_val: float
    if layer_height is not None and layer_height > 0:
        layer_height_val = layer_height
    else:
        # Derive from bbox when contract omits it.
        z_span = tp.bbox_max[2] - tp.bbox_min[2] if tp.segment_count else 0.0
        layer_height_val = (z_span / 200.0) if z_span > 0 else 0.2

    layer_table: list[list[float]] = []
    pos = tp.positions
    vertex_count = len(pos) // 3
    if vertex_count > 0:
        thr = layer_height_val * 0.5
        v0 = 0
        z_ref = pos[2]
        for v in range(1, vertex_count):
            z = pos[v * 3 + 2]
            if z - z_ref >= thr:
                layer_table.append([v0, v - 1, pos[v0 * 3 + 2]])
                v0 = v
                z_ref = z
            elif z > z_ref:
                z_ref = z
        layer_table.append([v0, vertex_count - 1, pos[v0 * 3 + 2]])

    # ---- JSON header ------------------------------------------------------ #
    header_obj = {
        "segment_count": tp.segment_count,
        "bbox": {"min": tp.bbox_min, "max": tp.bbox_max},
        "layer_table": layer_table,
        "job": job_name,
        "total_layers": total_layers,
        "layer_height_mm": layer_height,
        "decimated": tp.decimated,
        "source_file": source_file,
        "cached": cached,
    }
    header_bytes = _json.dumps(header_obj).encode("utf-8")

    # ---- Pad header to a 4-byte boundary ---------------------------------- #
    # new Float32Array(arrayBuffer, byteOffset, …) in the browser requires
    # byteOffset to be a multiple of 4 (Float32 element size).  The positions
    # block starts at offset (4 + len(header_bytes)), so we must ensure
    # len(header_bytes) % 4 == 0.  JSON.parse tolerates trailing ASCII spaces,
    # so we pad with spaces — safe, standard, zero-cost to parse.
    pad = (4 - len(header_bytes) % 4) % 4
    if pad:
        header_bytes += b" " * pad

    # ---- Float32-LE positions block --------------------------------------- #
    if struct.pack("=f", 1.0) == struct.pack("<f", 1.0):
        pos_bytes = tp.positions.tobytes()
    else:
        pos_bytes = struct.pack(f"<{len(tp.positions)}f", *tp.positions)

    # ---- Assemble: uint32-LE header length + header + positions ----------- #
    # header_len is the PADDED length so the client computes the correct
    # posOffset = 4 + header_len, which is always a multiple of 4.
    header_len = len(header_bytes)
    result: bytes = struct.pack("<I", header_len) + header_bytes + pos_bytes
    return result


def _bin_response_with_headers(
    body: bytes,
    request: Request,
    *,
    extra_headers: dict[str, str],
) -> Response:
    """Return a binary (application/octet-stream) response, gzip only when
    it saves ≥ ``_BIN_GZIP_MIN_SAVINGS`` of the original size.

    Float32 buffers compress much less than JSON text; the threshold avoids
    wasting CPU and inflating Content-Encoding when the gain is negligible.
    """
    headers = {
        "Content-Type": "application/octet-stream",
        **extra_headers,
    }
    if _wants_gzip(request) and len(body) >= _GZIP_MIN_BYTES:
        compressed = _gzip.compress(body, compresslevel=_GZIP_LEVEL)
        if len(compressed) <= len(body) * (1.0 - _BIN_GZIP_MIN_SAVINGS):
            headers["Content-Encoding"] = "gzip"
            headers["Content-Length"] = str(len(compressed))
            return Response(content=compressed, status_code=200, headers=headers)

    headers["Content-Length"] = str(len(body))
    return Response(content=body, status_code=200, headers=headers)


# ------------------------------------------------------------------ #
# Toolpath endpoint
# ------------------------------------------------------------------ #


@router.get("/{printer_id}/viz/toolpath")
async def get_viz_toolpath(
    printer_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
    _auth: None = Depends(require_read_or_viz),
    fmt: str = Query(default="json", pattern="^(json|bin)$"),
) -> Response:
    """Download, parse, and return the printer's current gcode toolpath.

    The gcode is extracted from ``Metadata/plate_1.gcode`` inside the
    ``.gcode.3mf`` archive on the printer's FTPS storage.

    ``?fmt=json`` (default) — JSON response::

        {
          "job": str | null,
          "total_layers": int | null,
          "layer_height_mm": float | null,
          "segment_count": int,
          "positions_b64": str,   # base64 of float32 LE, xyzxyz per segment
          "bbox": {"min": [x, y, z], "max": [x, y, z]},
          "decimated": bool,      # true if budget forced dropping detail
          "source_file": str,
          "cached": bool
        }

    ``?fmt=bin`` — ``application/octet-stream``::

        [4 bytes uint32-LE header_len]
        [header_len bytes UTF-8 JSON: {segment_count, bbox, layer_table,
                                       job, total_layers, layer_height_mm,
                                       decimated, source_file, cached}]
        [segment_count * 2 * 3 * 4 bytes: float32-LE positions, xyzxyz]

    The ``layer_table`` in the binary header is a list of ``[v0, v1, z]``
    entries (int start vertex, int end vertex, float Z) — the same layer
    index the viewer computes client-side from the positions scan, shipped
    pre-built to eliminate that O(N) scan from the critical path.

    Error codes mirror ``/viz/mesh``:
    * 404 — no current job, printer not found, or file not on storage.
    * 422 — gcode parse failure.
    * 502 — FTPS download/list failure.

    Includes ``ETag`` and ``Cache-Control`` headers for HTTP-level caching
    (see module docstring).  The ETag is **representation-specific**: it folds
    the requested ``fmt`` into the tag (``…:json`` vs ``…:bin``) so a client
    that cached one format is never told ``304`` for the other (RFC 7232 §2.3).
    """
    # ---- Resolve current job ------------------------------------------- #
    try:
        service = registry.get(printer_id)
    except PrinterNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"printer {printer_id!r} not found",
        ) from exc

    snap = service.snapshot()
    job_block: dict[str, Any] = snap.get("job") or {}
    job_name: str | None = job_block.get("subtask_name") or None
    total_layers: int | None = job_block.get("total_layer_num")
    layer_height: float | None = job_block.get("layer_height_mm")

    if not job_name:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No current job on this printer.",
        )

    # ---- Build FTPS client --------------------------------------------- #
    ftps = _ftps_for(request, registry, printer_id)

    # ---- Locate the .gcode.3mf on the printer --------------------------- #
    try:
        location = await _find_3mf(ftps, job_name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"FTPS list failed: {exc!s}",
        ) from exc

    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"3MF file for job {job_name!r} not found on printer storage.",
        )

    remote_dir, filename = location
    try:
        await _get_viz_cache(request).validate_revision(printer_id, ftps, remote_dir, filename)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"FTPS metadata failed: {exc}") from exc

    # ---- Try toolpath cache -------------------------------------------- #
    viz_cache = _get_viz_cache(request)
    tp_hit = viz_cache.lookup_toolpath(printer_id, filename)

    if tp_hit is not None:
        tp_cache_key_hit, cached_tp = tp_hit
        # We know the file size from the cache key — derive the ETag and
        # check If-None-Match *before* serialising the payload.  The ETag is
        # representation-specific (folds in fmt) so a json-cached client never
        # gets a 304 for the bin body and vice versa.
        cached_size = tp_cache_key_hit[2]
        etag = _viz_etag(filename, cached_size, fmt, viz_cache.content_id(printer_id, filename))
        if request.headers.get("If-None-Match") == etag:
            return Response(
                status_code=304,
                headers={
                    "ETag": etag,
                    "Cache-Control": _CACHE_CONTROL_VIZ,
                },
            )
        log.info(
            "viz.toolpath_cache_hit",
            printer_id=printer_id,
            filename=filename,
        )
        return _toolpath_response(
            tp=cached_tp,
            job_name=job_name,
            total_layers=total_layers,
            layer_height=layer_height,
            source_file=filename,
            cached=True,
            etag=etag,
            request=request,
            fmt=fmt,
        )

    # ---- Cold load: delegate to VizCache (fills memo + cache) ----------- #
    # VizCache.fill_toolpath_for_request is the single authoritative pipeline:
    # download → sliced-date memo → parse → cache-insert.  Using it here
    # (instead of inline download+parse) ensures the sliced-date memo is
    # always populated on the first endpoint-triggered load, not only on
    # pre-warm.  Sliced .gcode.3mf files have no mesh geometry; fill_mesh is
    # never called for them, so the toolpath path is the only chance to fill
    # the memo.
    _HTTP_STATUS = {
        "not_found": status.HTTP_404_NOT_FOUND,
        "download":  status.HTTP_502_BAD_GATEWAY,
        "parse":     status.HTTP_422_UNPROCESSABLE_ENTITY,
    }
    try:
        filename, tp = await viz_cache.fill_toolpath_for_request(
            printer_id, service.ip, service.access_code, job_name
        )
    except VizFillError as exc:
        raise HTTPException(
            status_code=_HTTP_STATUS.get(exc.kind, status.HTTP_502_BAD_GATEWAY),
            detail=exc.detail,
        ) from exc

    # The cache key was written by fill_toolpath_for_request; look up the size.
    tp_hit2 = viz_cache.lookup_toolpath(printer_id, filename)
    if tp_hit2 is not None:
        file_size = tp_hit2[0][2]
    else:
        # Should not happen: fill_toolpath_for_request just inserted this key.
        # Falling back to size 0 yields a stable-but-wrong ETag; log loudly
        # rather than silently disabling 304s for this resource.
        file_size = 0
        log.warning(
            "viz.etag_size_fallback",
            printer_id=printer_id,
            filename=filename,
            kind="toolpath",
        )
    etag = _viz_etag(filename, file_size, fmt, viz_cache.content_id(printer_id, filename))
    return _toolpath_response(
        tp=tp,
        job_name=job_name,
        total_layers=total_layers,
        layer_height=layer_height,
        source_file=filename,
        cached=False,
        etag=etag,
        request=request,
        fmt=fmt,
    )


def _toolpath_response(
    *,
    tp: GcodeToolpath,
    job_name: str | None,
    total_layers: int | None,
    layer_height: float | None,
    source_file: str,
    cached: bool,
    etag: str,
    request: Request,
    fmt: str = "json",
) -> Response:
    """Serialise a :class:`GcodeToolpath` to the wire shape.

    ``fmt="json"`` (default): JSON with base64 positions.
    ``fmt="bin"``: binary format (uint32-LE header length + JSON header +
    float32-LE positions).  See :func:`_build_bin_toolpath` for layout.
    """
    extra_headers: dict[str, str] = {
        "ETag": etag,
        "Cache-Control": _CACHE_CONTROL_VIZ,
    }

    if fmt == "bin":
        body = _build_bin_toolpath(
            tp=tp,
            job_name=job_name,
            total_layers=total_layers,
            layer_height=layer_height,
            source_file=source_file,
            cached=cached,
        )
        return _bin_response_with_headers(body, request, extra_headers=extra_headers)

    # Default: JSON with base64-encoded positions.
    # Ensure little-endian float32 byte order.
    if struct.pack("=f", 1.0) == struct.pack("<f", 1.0):
        pos_bytes = tp.positions.tobytes()
    else:
        pos_bytes = struct.pack(f"<{len(tp.positions)}f", *tp.positions)

    return _json_response_with_headers(
        {
            "job": job_name,
            "total_layers": total_layers,
            "layer_height_mm": layer_height,
            "segment_count": tp.segment_count,
            "positions_b64": base64.b64encode(pos_bytes).decode(),
            "bbox": {
                "min": tp.bbox_min,
                "max": tp.bbox_max,
            },
            "decimated": tp.decimated,
            "source_file": source_file,
            "cached": cached,
        },
        request,
        extra_headers=extra_headers,
    )


# ------------------------------------------------------------------ #
# Viewer HTML endpoint
# ------------------------------------------------------------------ #

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_VIEWER_HTML = _STATIC_DIR / "viewer.html"


@router.get("/{printer_id}/viz", response_class=HTMLResponse)
async def get_viz_viewer(
    printer_id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
    _auth: None = Depends(require_read_or_viz),
) -> HTMLResponse:
    """Serve the mesh viewer HTML page.

    Auth: master key OR viz token via ``Authorization: Bearer`` header or
    ``?token=`` query param (browsers in an iframe/WebView can't set headers).
    Returns 401 if the token is absent or invalid.
    Returns 501 if the static viewer HTML file has not been built yet.
    """
    # 404 if printer doesn't exist (don't reveal the viewer to unknown IDs).
    try:
        registry.get(printer_id)
    except PrinterNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"printer {printer_id!r} not found",
        ) from exc

    if not _VIEWER_HTML.is_file():
        return HTMLResponse(content="viewer not built yet", status_code=501)

    content = _VIEWER_HTML.read_text(encoding="utf-8")
    return HTMLResponse(content=content, status_code=200)


# ------------------------------------------------------------------ #
# SPA web-app shell (unauthenticated; serves static/app/**)
# ------------------------------------------------------------------ #
#
# These routes serve the vanilla-JS single-page app that ships as package
# data under ``static/app/`` (hatchling packages all of ``static/`` into the
# wheel exactly as it does ``viewer.html`` — no pyproject change needed).
#
# They are mounted at the application ROOT (``/``, ``/app``, ``/app/{path}``),
# NOT under ``/api/v1``: the shell is the bridge's own front-end, not a
# versioned API surface.  They are therefore carried on a SEPARATE router
# (:data:`app_shell_router`) with NO prefix and NO auth dependency — the shell
# is pure HTML/CSS/JS and exposes no printer data; every ``/api/v1/*`` request
# the SPA makes still carries a Bearer token (or ``?token=``) and is gated by
# the existing per-route auth dependencies.  Because FastAPI matches routes in
# registration order and this router is included AFTER the ``/api/v1`` router
# (see main.py wiring note below), the literal ``/app`` prefix and bare ``/``
# can never shadow an ``/api/v1/...`` path or the per-printer ``/viz`` route.
#
# main.py wiring (coordinator must add ONE line, at the app level, after the
# v1 router is included so /api/v1 wins on any overlap):
#
#     app.include_router(viz.app_shell_router)
#
# (Do NOT add it to the ``v1`` router — that would prefix it with /api/v1.)

_APP_DIR = _STATIC_DIR / "app"
_APP_INDEX = _APP_DIR / "index.html"

# Suffix -> media type.  ``.js`` MUST be ``text/javascript`` (not
# ``application/octet-stream``) or the browser refuses to evaluate it as an
# ES module.  ``.webmanifest`` is ``application/manifest+json`` per the W3C
# Web App Manifest spec.
_APP_MEDIA_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".map": "application/json; charset=utf-8",
}

# No-build means no content-hashed filenames, so a stale browser cache would
# defeat the wheel-upgrade -> updated-SPA flow.  ``no-cache`` forces a
# revalidation on every load (cheap on a LAN / Tailscale link); the bridge's
# updated assets are then always picked up after a restart.
_APP_CACHE_CONTROL = "no-cache"

app_shell_router = APIRouter(tags=["webapp"])


def _serve_app_index() -> Response:
    """Return the SPA shell HTML, or 501 if it was not built into the wheel.

    Mirrors :func:`get_viz_viewer`'s defensive posture for a missing
    ``viewer.html``: an old wheel (or a dev checkout where Stage-1 has not yet
    produced the SPA) returns a clear 501 rather than a 404 or a stack trace.
    """
    if not _APP_INDEX.is_file():
        return HTMLResponse(content="web app not built into this wheel", status_code=501)
    return FileResponse(
        _APP_INDEX,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": _APP_CACHE_CONTROL},
    )


@app_shell_router.get("/")
async def get_root() -> RedirectResponse:
    """Redirect the bare host to the SPA shell.

    A 307 (temporary, method-preserving) keeps the bridge free to repurpose
    ``/`` later without baking a permanent redirect into client caches.  This
    is the only behavior change to ``/`` (previously unrouted -> 404).
    """
    return RedirectResponse(url="/app/", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@app_shell_router.get("/app")
@app_shell_router.get("/app/")
async def get_app_index() -> Response:
    """Serve the SPA shell HTML at ``/app`` and ``/app/`` (no auth).

    Routing is hash-based client-side, so there is no SPA path-fallback to
    implement here: every real URL path maps to a real file under
    ``static/app/`` and unknown hashes never reach the server.
    """
    return _serve_app_index()


@app_shell_router.get("/app/{path:path}")
async def get_app_asset(path: str) -> Response:
    """Serve a static asset under ``static/app/`` with the correct media type.

    Path-traversal hardening: the requested path is resolved against the
    canonical app directory and rejected (404) if it escapes that directory or
    is not a regular file.  An explicit ``..`` check short-circuits the obvious
    attack before any filesystem resolution.  404 (not 403) is used uniformly
    so the endpoint never reveals whether a path outside the tree exists.
    """
    # Empty path means a request for "/app/" with a trailing slash captured by
    # this catch-all on some clients; serve the index for parity.
    if path in ("", "/"):
        return _serve_app_index()

    # Cheap, unambiguous reject of traversal segments before touching the FS.
    if ".." in path.split("/"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    candidate = (_APP_DIR / path).resolve()
    app_root = _APP_DIR.resolve()

    # Confirm the resolved path is still inside the app directory.  Path.relative_to
    # raises ValueError when ``candidate`` is not under ``app_root`` — that is the
    # authoritative containment check (covers symlinks and ``..`` that survived).
    try:
        candidate.relative_to(app_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="not found"
        ) from exc

    if not candidate.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    media_type = _APP_MEDIA_TYPES.get(
        candidate.suffix.lower(), "application/octet-stream"
    )
    return FileResponse(
        candidate,
        media_type=media_type,
        headers={"Cache-Control": _APP_CACHE_CONTROL},
    )
