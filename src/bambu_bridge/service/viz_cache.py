"""Shared viz cache + background pre-warm logic (spec G4 / pre-warm PR).

This module owns:
- The two in-process caches (mesh and toolpath) that were previously created
  lazily on ``app.state`` inside ``api/viz.py``.
- The shared ``find_3mf`` file-locator used by both the HTTP endpoints and the
  pre-warm path (single authoritative copy; previously duplicated).
- The pre-warm coordinator: on ``print_started`` the watcher spawns a
  background asyncio task that sleeps 15 s (printer preheat / file-settling
  window) then fetches and fills both caches.

Layering contract
-----------------
This module is pure service-layer: no FastAPI imports, no ``Request`` objects,
no ``HTTPException``.  The HTTP layer (``api/viz.py``) holds a reference to
the single :class:`VizCache` instance created in ``main.py`` and delegates
cache lookups + inserts to it.  Pre-warm uses the same VizCache instance so
both paths share one set of dicts.

Cache semantics (identical to the prior ``app.state``-keyed behaviour)
-----------------------------------------------------------------------
Both caches are ``dict[(printer_id, filename, size_bytes), T]`` with a cap of
3 entries, oldest-evicted (insertion-ordered Python dict).  The cache key
includes the file size so a re-sliced file with the same name is never stale.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import PurePosixPath

import structlog

from bambu_bridge.protocol.ftps import (
    UPLOAD_DIR_CACHE,
    UPLOAD_DIR_PERSISTENT,
    FtpsTransfer,
    SlicedDateMemo,
    extract_sliced_at_from_bytes,
)
from bambu_bridge.protocol.gcode_path import (
    GcodeParseError,
    GcodeToolpath,
    parse_gcode_from_archive,
)
from bambu_bridge.protocol.threemf import Mesh3MF, ParseError, parse_3mf

log = structlog.get_logger(__name__)

# Cache key type: (printer_id, filename, size_bytes).  Exported so api/viz.py
# can reference the same type alias without re-defining it.
_CacheKey = tuple[str, str, int]


# ------------------------------------------------------------------ #
# Typed exception for request-path fill errors
# ------------------------------------------------------------------ #


class VizFillError(Exception):
    """Raised by fill_*_for_request on a recoverable failure.

    ``kind`` maps to an HTTP status code in api/viz.py:
      - ``"not_found"``  → 404
      - ``"download"``   → 502
      - ``"parse"``      → 422

    This exception is deliberately thin: no FastAPI imports, no Request
    objects.  The HTTP layer (api/viz.py) owns the mapping.
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail

_CACHE_MAX = 3

# Delay after print_started before the pre-warm download begins (seconds).
# The printer is busy preheating / handshaking early in this window; waiting
# avoids hammering it and lets cloud-relayed SD-card writes settle.
_PREWARM_DELAY_S = 15.0

# After a first failure, wait this long and retry once before giving up.
_PREWARM_RETRY_S = 30.0


# ------------------------------------------------------------------ #
# Module-level cache-put helpers (used by VizCache and api/viz.py)
# ------------------------------------------------------------------ #


def _cache_put_mesh(
    cache: dict[_CacheKey, Mesh3MF],
    key: _CacheKey,
    mesh: Mesh3MF,
) -> None:
    """Store *mesh* under *key*; evict oldest entry when at cap."""
    if key in cache:
        del cache[key]
    elif len(cache) >= _CACHE_MAX:
        oldest = next(iter(cache))
        del cache[oldest]
    cache[key] = mesh


def _cache_put_toolpath(
    cache: dict[_CacheKey, GcodeToolpath],
    key: _CacheKey,
    tp: GcodeToolpath,
) -> None:
    """Store *tp* under *key*; evict oldest entry when at cap."""
    if key in cache:
        del cache[key]
    elif len(cache) >= _CACHE_MAX:
        oldest = next(iter(cache))
        del cache[oldest]
    cache[key] = tp


# ------------------------------------------------------------------ #
# Module-level FTPS file-locator (single authoritative copy)
# ------------------------------------------------------------------ #


async def find_3mf(
    ftps: FtpsTransfer, job_name: str
) -> tuple[str, str] | None:
    """Locate the .3mf (or .gcode.3mf) file matching *job_name* on the printer.

    Searches the printer root (persistent) then /cache, trying both
    ``<job_name>.3mf`` and ``<job_name>.gcode.3mf``.  Returns
    ``(remote_dir, filename)`` or ``None`` when not found.

    This is the single canonical implementation; ``api/viz.py`` imports and
    calls this function rather than maintaining a duplicate.
    """
    stem = PurePosixPath(job_name).stem
    candidates = [job_name, f"{stem}.3mf", f"{stem}.gcode.3mf"]
    for remote_dir in (UPLOAD_DIR_PERSISTENT, UPLOAD_DIR_CACHE):
        try:
            listing = await ftps.list_dir(remote_dir)
        except Exception:  # noqa: BLE001 — FTPS list failure treated as empty
            listing = []
        for fname in candidates:
            if fname in listing:
                return remote_dir, fname
    return None


# ------------------------------------------------------------------ #
# VizCache
# ------------------------------------------------------------------ #


class VizCache:
    """Container for both viz caches plus the pre-warm logic.

    One instance is created in ``main.py`` during lifespan and placed on
    ``app.state.viz_cache_obj``.  The ``JobManager`` receives the same
    instance via its constructor so the watcher can fill the caches without
    importing anything from the FastAPI layer.
    """

    def __init__(
        self,
        ftps_port: int = 990,
        sliced_date_memo: SlicedDateMemo | None = None,
    ) -> None:
        # Exposed as plain dicts so api/viz.py can read/write them directly.
        self.mesh_cache: dict[_CacheKey, Mesh3MF] = {}
        self.toolpath_cache: dict[_CacheKey, GcodeToolpath] = {}
        self._revisions: dict[tuple[str, str], tuple[str, tuple[int, str]]] = {}
        self._digests: dict[tuple[str, str], str] = {}
        self._fill_lock = asyncio.Lock()
        self._ftps_port = ftps_port
        # Optional sliced-date memo: filled whenever a .gcode.3mf is downloaded
        # for pre-warm.  When None the feature is disabled (tests that don't
        # need the memo pass None).
        self._sliced_memo: SlicedDateMemo | None = sliced_date_memo
        # Keys currently being pre-warmed — guards against duplicate tasks.
        self._inflight: set[tuple[str, str]] = set()  # (printer_id, job_name)

    # ------------------------------------------------------------------ #
    # Cache access helpers
    # ------------------------------------------------------------------ #

    def invalidate(self, printer_id: str, filename: str | None = None) -> None:
        """Forget a replaced file or all files when a new print starts."""
        for cache in (self.mesh_cache, self.toolpath_cache):
            for key in list(cache):
                if key[0] == printer_id and (filename is None or key[1] == filename):
                    del cache[key]
        for metadata in (self._revisions, self._digests):
            for file_key in list(metadata):
                if file_key[0] == printer_id and (filename is None or file_key[1] == filename):
                    del metadata[file_key]

    async def validate_revision(
        self, printer_id: str, ftps: FtpsTransfer, remote_dir: str, filename: str
    ) -> None:
        """Revalidate before both API conditional responses and pre-warm reuse."""
        key = (printer_id, filename)
        try:
            revision = await ftps.file_revision(filename, remote_dir=remote_dir)
        except Exception:
            self.invalidate(printer_id, filename)
            raise
        current = (remote_dir, revision) if revision is not None else None
        if current is None or self._revisions.get(key) != current:
            self.invalidate(printer_id, filename)
        if current is not None:
            self._revisions[key] = current

    def content_id(self, printer_id: str, filename: str) -> str:
        return self._digests.get((printer_id, filename), "")

    def _record_content(self, printer_id: str, filename: str, data: bytes) -> None:
        self._digests[(printer_id, filename)] = hashlib.sha256(data).hexdigest()
        live = {(k[0], k[1]) for k in (*self.mesh_cache, *self.toolpath_cache)}
        for metadata in (self._revisions, self._digests):
            for key in list(metadata):
                if key not in live:
                    del metadata[key]

    def put_mesh(self, key: _CacheKey, mesh: Mesh3MF) -> None:
        _cache_put_mesh(self.mesh_cache, key, mesh)

    def put_toolpath(self, key: _CacheKey, tp: GcodeToolpath) -> None:
        _cache_put_toolpath(self.toolpath_cache, key, tp)

    def lookup_mesh(
        self, printer_id: str, filename: str
    ) -> tuple[_CacheKey, Mesh3MF] | None:
        """Return ``(key, mesh)`` for a cached entry matching printer+filename, or None."""
        for k, v in self.mesh_cache.items():
            if k[0] == printer_id and k[1] == filename:
                return k, v
        return None

    def lookup_toolpath(
        self, printer_id: str, filename: str
    ) -> tuple[_CacheKey, GcodeToolpath] | None:
        """Return ``(key, tp)`` for a cached entry matching printer+filename, or None."""
        for k, v in self.toolpath_cache.items():
            if k[0] == printer_id and k[1] == filename:
                return k, v
        return None

    # ------------------------------------------------------------------ #
    # Fill helpers (download + parse + cache-insert, used by pre-warm)
    # ------------------------------------------------------------------ #

    def _make_ftps(self, ip: str, access_code: str) -> FtpsTransfer:
        return FtpsTransfer(ip, access_code, port=self._ftps_port)

    # ---- Internal raising pipeline (single source of truth) ----------- #

    async def _run_fill_mesh(
        self, printer_id: str, ftps: FtpsTransfer, job_name: str
    ) -> tuple[str, Mesh3MF]:
        async with self._fill_lock:
            return await self._fill_mesh(printer_id, ftps, job_name)

    async def _fill_mesh(
        self, printer_id: str, ftps: FtpsTransfer, job_name: str
    ) -> tuple[str, Mesh3MF]:
        """Download, memo-fill, parse, and cache the mesh.

        Returns ``(filename, mesh)`` on success.
        Raises :class:`VizFillError` on any error.

        This is the authoritative pipeline for mesh cold-loads.  Both the
        pre-warm path (via :meth:`fill_mesh`) and the request path (via
        :meth:`fill_mesh_for_request`) call this method — there is exactly
        one download-parse-memo-cache code path.
        """
        try:
            location = await find_3mf(ftps, job_name)
        except Exception as exc:  # noqa: BLE001
            raise VizFillError("download", f"FTPS list failed: {exc}") from exc
        if location is None:
            raise VizFillError(
                "not_found", f"3MF file for job {job_name!r} not found on printer storage."
            )
        remote_dir, filename = location

        try:
            await self.validate_revision(printer_id, ftps, remote_dir, filename)
        except Exception as exc:
            raise VizFillError("download", f"FTPS metadata failed: {exc}") from exc

        # Reuse only after checking the remote file's revision and directory.
        if self.lookup_mesh(printer_id, filename) is not None:
            hit = self.lookup_mesh(printer_id, filename)
            assert hit is not None  # narrowing — checked above
            return filename, hit[1]

        try:
            data = await ftps.download_bytes(filename, remote_dir=remote_dir)
        except Exception as exc:  # noqa: BLE001
            raise VizFillError("download", f"FTPS download failed: {exc}") from exc

        # Opportunistically fill the sliced-date memo before parsing.
        if self._sliced_memo is not None:
            sliced_at = extract_sliced_at_from_bytes(data)
            if sliced_at is not None:
                await self._sliced_memo.aput(filename, len(data), sliced_at)
                log.debug(
                    "viz.sliced_memo.filled",
                    filename=filename,
                    sliced_at=sliced_at.isoformat(),
                )

        try:
            mesh = await asyncio.to_thread(parse_3mf, data)
        except (ParseError, Exception) as exc:  # noqa: BLE001
            raise VizFillError("parse", f"3MF parse error: {exc}") from exc

        key: _CacheKey = (printer_id, filename, len(data))
        self.put_mesh(key, mesh)
        self._record_content(printer_id, filename, data)
        log.info(
            "viz.parsed",
            printer_id=printer_id,
            filename=filename,
            triangles=mesh.triangle_count,
            vertices=mesh.vertex_count,
        )
        return filename, mesh

    async def _run_fill_toolpath(
        self, printer_id: str, ftps: FtpsTransfer, job_name: str
    ) -> tuple[str, GcodeToolpath]:
        async with self._fill_lock:
            return await self._fill_toolpath(printer_id, ftps, job_name)

    async def _fill_toolpath(
        self, printer_id: str, ftps: FtpsTransfer, job_name: str
    ) -> tuple[str, GcodeToolpath]:
        """Download, memo-fill, parse, and cache the toolpath.

        Returns ``(filename, toolpath)`` on success.
        Raises :class:`VizFillError` on any error.

        This is the authoritative pipeline for toolpath cold-loads.  Both
        the pre-warm path (via :meth:`fill_toolpath`) and the request path
        (via :meth:`fill_toolpath_for_request`) call this method.
        """
        try:
            location = await find_3mf(ftps, job_name)
        except Exception as exc:  # noqa: BLE001
            raise VizFillError("download", f"FTPS list failed: {exc}") from exc
        if location is None:
            raise VizFillError(
                "not_found", f"3MF file for job {job_name!r} not found on printer storage."
            )
        remote_dir, filename = location

        try:
            await self.validate_revision(printer_id, ftps, remote_dir, filename)
        except Exception as exc:
            raise VizFillError("download", f"FTPS metadata failed: {exc}") from exc

        # Reuse only after checking the remote file's revision and directory.
        if self.lookup_toolpath(printer_id, filename) is not None:
            hit = self.lookup_toolpath(printer_id, filename)
            assert hit is not None  # narrowing — checked above
            return filename, hit[1]

        try:
            data = await ftps.download_bytes(filename, remote_dir=remote_dir)
        except Exception as exc:  # noqa: BLE001
            raise VizFillError("download", f"FTPS download failed: {exc}") from exc

        # Opportunistically fill the sliced-date memo before parsing.
        # Sliced Bambu .gcode.3mf files carry no mesh, so fill_mesh is never
        # called for them — the memo must be filled here in the toolpath path.
        if self._sliced_memo is not None:
            sliced_at = extract_sliced_at_from_bytes(data)
            if sliced_at is not None:
                await self._sliced_memo.aput(filename, len(data), sliced_at)
                log.debug(
                    "viz.sliced_memo.filled_from_toolpath",
                    filename=filename,
                    sliced_at=sliced_at.isoformat(),
                )

        try:
            tp = await asyncio.to_thread(parse_gcode_from_archive, data)
        except (GcodeParseError, Exception) as exc:  # noqa: BLE001
            raise VizFillError("parse", f"Gcode parse error: {exc}") from exc

        key: _CacheKey = (printer_id, filename, len(data))
        self.put_toolpath(key, tp)
        self._record_content(printer_id, filename, data)
        log.info(
            "viz.toolpath_parsed",
            printer_id=printer_id,
            filename=filename,
            segments=tp.segment_count,
            decimated=tp.decimated,
        )
        return filename, tp

    # ---- Pre-warm path (fire-and-forget; returns bool, never raises) --- #

    async def fill_mesh(
        self, printer_id: str, ip: str, access_code: str, job_name: str
    ) -> bool:
        """Download, parse, and cache the mesh for *job_name*.

        Returns ``True`` when the cache was filled (or was already populated
        for this filename), ``False`` on any error.  Does NOT raise.
        """
        ftps = self._make_ftps(ip, access_code)
        try:
            await self._run_fill_mesh(printer_id, ftps, job_name)
            return True
        except VizFillError as exc:
            if exc.kind == "not_found":
                log.debug(
                    "viz.prewarm.file_not_found", printer_id=printer_id, job=job_name
                )
            else:
                log.warning(
                    "viz.prewarm.fill_mesh_failed",
                    printer_id=printer_id,
                    job=job_name,
                    kind=exc.kind,
                    detail=exc.detail,
                )
            return False

    async def fill_toolpath(
        self, printer_id: str, ip: str, access_code: str, job_name: str
    ) -> bool:
        """Download, parse, and cache the toolpath for *job_name*.

        Returns ``True`` on success (or already cached), ``False`` on error.
        """
        ftps = self._make_ftps(ip, access_code)
        try:
            await self._run_fill_toolpath(printer_id, ftps, job_name)
            return True
        except VizFillError as exc:
            if exc.kind == "not_found":
                log.debug(
                    "viz.prewarm.file_not_found", printer_id=printer_id, job=job_name
                )
            else:
                log.warning(
                    "viz.prewarm.fill_toolpath_failed",
                    printer_id=printer_id,
                    job=job_name,
                    kind=exc.kind,
                    detail=exc.detail,
                )
            return False

    # ---- Request path (raises VizFillError so caller can map to HTTP) -- #

    async def fill_mesh_for_request(
        self, printer_id: str, ip: str, access_code: str, job_name: str
    ) -> tuple[str, Mesh3MF]:
        """Download, memo-fill, parse, and cache the mesh; raise on error.

        Called from ``api/viz.py`` on a cold endpoint request so that:
        1. The sliced-date memo is always filled (single pipeline).
        2. The caller gets typed errors to map to HTTP status codes.

        Raises :class:`VizFillError` with ``kind`` in
        ``{"not_found", "download", "parse"}``.
        """
        ftps = self._make_ftps(ip, access_code)
        return await self._run_fill_mesh(printer_id, ftps, job_name)

    async def fill_toolpath_for_request(
        self, printer_id: str, ip: str, access_code: str, job_name: str
    ) -> tuple[str, GcodeToolpath]:
        """Download, memo-fill, parse, and cache the toolpath; raise on error.

        Called from ``api/viz.py`` on a cold endpoint request so that:
        1. The sliced-date memo is always filled (single pipeline).
        2. The caller gets typed errors to map to HTTP status codes.

        Raises :class:`VizFillError` with ``kind`` in
        ``{"not_found", "download", "parse"}``.
        """
        ftps = self._make_ftps(ip, access_code)
        return await self._run_fill_toolpath(printer_id, ftps, job_name)

    # ------------------------------------------------------------------ #
    # Pre-warm entry point (called by JobManager._watch_printer)
    # ------------------------------------------------------------------ #

    def schedule_prewarm(
        self,
        printer_id: str,
        ip: str,
        access_code: str,
        job_name: str,
    ) -> None:
        """Spawn a best-effort background warm task for *printer_id* / *job_name*.

        Safe to call multiple times: if a task for this (printer_id, job_name)
        pair is already in flight the call is silently ignored (debounce).
        The task is also skipped implicitly when both caches already have an
        entry for the filename (the fill_* helpers return early on cache hit).
        """
        guard_key = (printer_id, job_name)
        if guard_key in self._inflight:
            log.debug(
                "viz.prewarm.skipped_inflight",
                printer_id=printer_id,
                job=job_name,
            )
            return

        self.invalidate(printer_id)
        self._inflight.add(guard_key)
        asyncio.create_task(
            self._prewarm_task(printer_id, ip, access_code, job_name, guard_key),
            name=f"viz:prewarm:{printer_id}:{job_name}",
        )

    async def _prewarm_task(
        self,
        printer_id: str,
        ip: str,
        access_code: str,
        job_name: str,
        guard_key: tuple[str, str],
    ) -> None:
        """Best-effort warm task; logs failures and is never fatal."""
        try:
            await asyncio.sleep(_PREWARM_DELAY_S)

            ok_mesh = await self.fill_mesh(printer_id, ip, access_code, job_name)
            ok_tp = await self.fill_toolpath(printer_id, ip, access_code, job_name)

            if not ok_mesh or not ok_tp:
                # One retry after a short pause, then give up.
                log.info(
                    "viz.prewarm.retry_scheduled",
                    printer_id=printer_id,
                    job=job_name,
                    ok_mesh=ok_mesh,
                    ok_tp=ok_tp,
                )
                await asyncio.sleep(_PREWARM_RETRY_S)
                if not ok_mesh:
                    await self.fill_mesh(printer_id, ip, access_code, job_name)
                if not ok_tp:
                    await self.fill_toolpath(printer_id, ip, access_code, job_name)

        except asyncio.CancelledError:
            raise  # propagate cancellation cleanly
        except Exception:  # noqa: BLE001
            log.warning(
                "viz.prewarm_failed",
                printer_id=printer_id,
                job=job_name,
                exc_info=True,
            )
        finally:
            self._inflight.discard(guard_key)

    # ------------------------------------------------------------------ #
    # Introspection (tests / diagnostics)
    # ------------------------------------------------------------------ #

    @property
    def inflight_keys(self) -> frozenset[tuple[str, str]]:
        """Snapshot of currently in-flight warm tasks."""
        return frozenset(self._inflight)
