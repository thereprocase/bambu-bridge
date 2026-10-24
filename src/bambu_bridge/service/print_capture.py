"""Keep a copy of every print the printer runs and count the ones the bridge did not send.

The printer reports the file it is running and its MD5. If the library has no
copy, the file is fetched from the printer's storage (or its cache), checked
against that MD5 and stored. A print the bridge did not send is then adopted
into the inbox with the same per-layer plan as a bridge print, so it is
debited the same way when it ends.

Prints made while the bridge was down are caught up from the printer's file
listing, newest first, each dated by the printer's own file time so the
library keeps its order. Those are copied, not debited.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import posixpath
import re
import time
import zipfile
from collections import Counter
from collections.abc import AsyncIterator, Callable
from typing import Any

import structlog

from bambu_bridge.library import CHUNK_LIMIT, Artifact, Capture, LibraryError, LibraryStore
from bambu_bridge.protocol.ftps import FtpsTransfer
from bambu_bridge.service.library_history import identity
from bambu_bridge.service.shadow_accounting import (
    plan_manifest,
    read_plan,
    slot_identities,
    tray_materials,
)

log = structlog.get_logger(__name__)
OWNER = "printer-storage"
CAPTURE_INTERVAL_S = 15.0
CATCH_UP_INTERVAL_S = 3600.0
# With no record of earlier catch-ups, look back this far rather than copy the whole card.
FIRST_LOOK_BACK_S = 7 * 86400.0
ACTIVE = {"PREPARE", "RUNNING", "PAUSE"}
PRINTABLE = (".gcode.3mf", ".gcode")
PLATE = re.compile(r"Metadata/plate_([1-9][0-9]*)\.gcode")


def running_file(raw: dict[str, Any]) -> tuple[str, str] | None:
    """(file name, MD5) of the print running now, if the printer reports both."""
    if raw.get("gcode_state") not in ACTIVE:
        return None
    md5 = str(raw.get("md5") or "").upper()
    name = posixpath.basename(str(raw.get("file") or raw.get("gcode_file") or ""))
    if not re.fullmatch(r"[0-9A-F]{32}", md5) or not name:
        return None
    return name, md5


def _plates(data: bytes) -> list[int]:
    """Sliced plates in a .gcode.3mf; none for plain G-code."""
    if data[:2] != b"PK":
        return []
    with contextlib.suppress(zipfile.BadZipFile), zipfile.ZipFile(io.BytesIO(data)) as bundle:
        return [int(found[1]) for name in bundle.namelist() if (found := PLATE.fullmatch(name))]
    return []


def _plate(raw: dict[str, Any], data: bytes, fresh: set[str]) -> int:
    """The plate the printer runs: its reported `param` if current and in the file."""
    plates = _plates(data)
    found = PLATE.fullmatch(str(raw.get("param") or ""))
    if "param" in fresh and found and (not plates or int(found[1]) in plates):
        return int(found[1])
    return min(plates, default=1)


def store_copy(
    store: LibraryStore,
    cid: str,
    title: str,
    plate: int,
    name: str,
    data: bytes,
    created: float | None = None,
) -> None:
    capture = Capture(
        id=cid,
        title=title[:160] or name[:160],
        slicer_version="printer storage",
        plate=plate,
        artifacts=(
            Artifact(
                name=name[:160],
                role="slice",
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
            ),
        ),
    )
    store.create(OWNER, capture, created=created)
    for offset in range(0, len(data), CHUNK_LIMIT):
        store.append(
            cid, capture.artifacts[0].name, offset, data[offset : offset + CHUNK_LIMIT], OWNER
        )
    store.finalize(cid, OWNER)


def stored_copy(store: LibraryStore, cid: str) -> bytes | None:
    """The archived copy, if it is finalized and intact."""
    with contextlib.suppress(LibraryError):
        return store.read(cid, store.get(cid)["artifacts"][0]["name"])
    return None


# (printer, MD5) -> (fetches tried, when the last was): a running file is
# fetched once per process; a failed fetch is retried a few times, spaced out.
_FETCHES: dict[tuple[str, str], tuple[int, float]] = {}
# Files whose copy the library refused: each new print of one is fetched once
# more for adoption, since there is no stored copy to read it from.
_REFUSED: set[tuple[str, str]] = set()
FETCH_TRIES = 3
FETCH_RETRY_S = 60.0


def _may_fetch(key: tuple[str, str]) -> bool:
    if key not in _FETCHES:
        return True
    tries, last = _FETCHES[key]
    return tries < FETCH_TRIES and time.monotonic() - last >= FETCH_RETRY_S


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest().upper()


def _ftps(printer: Any, port: int) -> FtpsTransfer:
    return FtpsTransfer(printer.ip, printer.access_code, port=port)


async def fetch(ftps: FtpsTransfer, name: str, md5: str) -> tuple[str, bytes] | None:
    """(path, bytes) of the printed file in the printer's storage, if it matches the MD5."""
    for directory in ("", "cache"):
        with contextlib.suppress(Exception):
            data = await ftps.download_bytes(name, remote_dir=directory)
            if await asyncio.to_thread(_md5, data) == md5:
                return posixpath.join("/", directory, name), data
    return None


async def capture_once(
    printer: Any, store: LibraryStore, inbox: Any, inventory: Any, ftps_port: int
) -> str | None:
    """Make sure the running print is copied and, if foreign, tracked. Returns its capture id."""
    if not printer.print_md5_fresh:
        return None  # the reported MD5 may still be the previous print's
    raw = printer.native_snapshot().get("print") or {}
    found = running_file(raw)
    if found is None:
        return None
    name, md5 = found
    cid = identity("printer-file", md5)
    # Count only with auto-deduct on and no start the bridge owns, the same
    # test adoption makes, so a print adoption would refuse is never re-read.
    adopt = (
        inventory is not None
        and inbox is not None
        and not await asyncio.to_thread(inbox.has_active_print, printer.serial)
    )
    stored = await asyncio.to_thread(store.capture_state, cid) == "stored"
    sent = inbox is not None and await asyncio.to_thread(inbox.sent_file, name, printer.serial)
    if not adopt and (stored or sent):
        return cid  # the usual tick: copied, and counted or not to be counted here
    fresh = printer.fresh_print_fields()
    data = await asyncio.to_thread(stored_copy, store, cid) if stored else None
    fetched = None
    # A print no bridge start owns is foreign even under a bridge-sent name
    # (a reprint from the printer's storage), so adoption fetches its bytes.
    if data is None and (adopt or not sent):
        key = (printer.serial, md5)
        if not _may_fetch(key) and not (adopt and key in _REFUSED):
            return None  # fetched already this run (its copy may be deleted), or failing
        fetched = await fetch(_ftps(printer, ftps_port), name, md5)
        tries = _FETCHES.get(key, (0, 0.0))[0]
        if fetched is None:
            _FETCHES[key] = (tries + 1, time.monotonic())
            log.info("library.print_copy_unavailable", file=name, tries=tries + 1)
            return None
        path, data = fetched
        # Catch-up need not download it again; its listing time is not known here.
        await asyncio.to_thread(store.saw_printer_file, printer.serial, path, time.time())
    if data is None:
        return cid  # the bridge's own upload, not archived: nothing to read
    plate = _plate(raw, data, fresh)
    if fetched:
        title = str(raw.get("subtask_name") or "") if "subtask_name" in fresh else ""
        try:
            await asyncio.to_thread(store_copy, store, cid, title, plate, name, data)
            log.info("library.print_copied", file=name, bytes=len(data))
        except (LibraryError, ValueError) as exc:
            # Refused for good (full, too old, or a plate or name the library
            # cannot hold). Counting still uses the bytes.
            log.warning("library.print_copy_not_stored", file=name, error=type(exc).__name__)
            _REFUSED.add(key)
        # Not reached when storing failed otherwise (disk): the next tick retries.
        _FETCHES[key] = (FETCH_TRIES, time.monotonic())
    if not adopt:
        return cid
    sha256, plan, header = await asyncio.to_thread(read_plan, data, plate)
    # Only what was reported during this print; a stale mapping would debit
    # the wrong spool, a missing one leaves that filament uncharged with a note.
    command = {
        key: raw.get(key) if key in fresh else None
        for key in ("subtask_name", "ams_mapping", "use_ams", "param")
    }
    command["md5"] = md5
    snapshot = await inventory.assignments.snapshot()
    trays, slots = tray_materials(printer), slot_identities(printer)
    manifest = plan_manifest(
        sha256, plate, plan, header, command, snapshot, printer.serial, trays=trays, slots=slots
    )
    current = printer.native_snapshot().get("print") or {}
    if not printer.print_md5_fresh or running_file(current) != found:
        return cid  # the print ended or changed while its file was read
    adopted = await asyncio.to_thread(
        inbox.adopt_running_print,
        printer.serial,
        "/" + name,
        command,
        manifest,
        sha256=sha256,
        started_at=printer.print_started_at,
    )
    if adopted:
        log.info("inventory.print_adopted", file=name, attempt=adopted)
    return cid


# (printer, path, modified) -> failed catch-up downloads this run. At FETCH_TRIES
# the file is marked seen, so the give-up outlasts a restart; a re-upload of
# that path has a new file time and is tried again.
_UNREADABLE: Counter[tuple[str, str, float]] = Counter()


def _idle(printer: Any) -> bool:
    """Connected and reporting no print, so its storage is free to read."""
    state = (printer.native_snapshot().get("print") or {}).get("gcode_state")
    return printer.connected and state is not None and state not in ACTIVE


def _copy_file(store: LibraryStore, name: str, data: bytes, modified: float) -> int:
    """Store a caught-up file unless the library has it; 1 if stored."""
    cid = identity("printer-file", _md5(data))
    if store.capture_state(cid) != "missing":
        return 0
    try:
        # A printer clock ahead of the bridge never dates a copy into the future.
        store_copy(
            store, cid, "", min(_plates(data), default=1), name, data, min(modified, time.time())
        )
    except (LibraryError, ValueError) as exc:
        # Full of newer prints, older than the age limit, or a plate or name
        # the library cannot hold: skip this file, keep the rest.
        log.info("library.catch_up_not_stored", file=name, error=type(exc).__name__)
        return 0
    return 1


async def catch_up(printer: Any, store: LibraryStore, inbox: Any, ftps_port: int) -> int:
    """Copy files that reached the printer's storage while the bridge was away.

    Each file is looked at once, newest first, and dated by its printer time.
    Files the bridge itself uploaded are already in the library. Returns the
    number copied.
    """
    ftps = _ftps(printer, ftps_port)
    seen = await asyncio.to_thread(store.printer_files, printer.serial)
    since = max(seen.values(), default=time.time()) - FIRST_LOOK_BACK_S
    found: list[tuple[float, str, str]] = []
    for directory in ("", "cache"):
        try:
            entries = await ftps.list_dir_with_timestamps(directory)
        except Exception:
            log.info("library.catch_up_unlisted", directory=directory or "/")
            continue
        for entry in entries:
            when = entry.modified_at or entry.created_at
            path = posixpath.join("/", directory, entry.name)
            if (
                when is not None
                and entry.name.lower().endswith(PRINTABLE)
                and when.timestamp() >= since
                and when.timestamp() > seen.get(path, 0.0)
            ):
                found.append((when.timestamp(), directory, entry.name))
    copied = 0
    for modified, directory, name in sorted(found, reverse=True):
        path = posixpath.join("/", directory, name)
        if inbox is None or not await asyncio.to_thread(
            inbox.sent_file, name, printer.serial, modified
        ):
            if not _idle(printer):
                break  # a print started: the rest waits for the next round
            try:
                data = await ftps.download_bytes(name, remote_dir=directory)
            except Exception:
                _UNREADABLE[printer.serial, path, modified] += 1
                tries = _UNREADABLE[printer.serial, path, modified]
                log.info("library.catch_up_unreadable", file=name, tries=tries)
                if tries < FETCH_TRIES:
                    continue  # tried again next round
            else:
                copied += await asyncio.to_thread(_copy_file, store, name, data, modified)
        await asyncio.to_thread(store.saw_printer_file, printer.serial, path, modified)
    if copied:
        log.info("library.caught_up", printer=printer.serial, copied=copied)
    return copied


async def capture_loop(
    printers: Callable[[], list[Any]],
    store: LibraryStore,
    inbox_for: Callable[[], Any],
    inventory: Any,
    ftps_port: int,
) -> None:
    while True:
        for printer in printers():
            if not printer.connected:
                continue  # a stale report names nothing reliably
            try:
                await capture_once(printer, store, inbox_for(), inventory, ftps_port)
            except Exception:
                # One printer's storage must never stop the others.
                log.warning("library.print_capture_failed", exc_info=True)
        await asyncio.sleep(CAPTURE_INTERVAL_S)


async def catch_up_loop(
    printers: Callable[[], list[Any]],
    store: LibraryStore,
    inbox_for: Callable[[], Any],
    ftps_port: int,
) -> None:
    """Hourly per printer, and only while it is idle; separate so a long first
    catch-up never delays copying a print that starts meanwhile."""
    last: dict[str, float] = {}
    while True:
        for printer in printers():
            done = last.get(printer.serial)
            if done is not None and time.monotonic() - done < CATCH_UP_INTERVAL_S:
                continue
            if not _idle(printer):
                continue  # leave the printer's storage alone while it prints
            last[printer.serial] = time.monotonic()
            try:
                await catch_up(printer, store, inbox_for(), ftps_port)
            except Exception:
                log.warning("library.catch_up_failed", exc_info=True)
        await asyncio.sleep(CAPTURE_INTERVAL_S)


@contextlib.asynccontextmanager
async def capture_worker(
    printers: Callable[[], list[Any]],
    store: LibraryStore,
    inbox_for: Callable[[], Any],
    inventory: Any,
    ftps_port: int,
) -> AsyncIterator[None]:
    tasks = [
        asyncio.create_task(capture_loop(printers, store, inbox_for, inventory, ftps_port)),
        asyncio.create_task(catch_up_loop(printers, store, inbox_for, ftps_port)),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
