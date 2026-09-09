"""Implicit-TLS FTPS client for the Bambu P1S (spec 5.2).

Pure protocol: no FastAPI, no service imports.

The P1S FTP server speaks **implicit** TLS on :990 — the socket is TLS-wrapped
before the FTP ``220`` welcome banner, unlike explicit FTPS (``AUTH TLS``).

**Why stdlib ftplib, not aioftp** (2026-05-19 hardware post-mortem): aioftp
0.23 uploaded the bytes fine but then hung on the post-``STOR`` ``226`` and
raised a bare ``TimeoutError``. The P1S withholds ``226`` on the control
channel until the *data* connection is TLS-shut-down with ``close_notify``,
and it also requires the data connection to resume the control connection's
TLS session. aioftp does neither; ``curl`` and stdlib ``ftplib.FTP_TLS`` do
both (``storbinary``/``retrbinary`` call ``conn.unwrap()``; ``ntransfercmd``
passes ``session=self.sock.session``). ftplib is blocking, so every call
runs in a thread executor — the public API stays ``async``.

Bambu quirks pinned here:
  * port 990, implicit TLS, self-signed rotating cert -> CERT_NONE (gotcha #7)
  * TLS 1.2 only (gotcha #8) — cap+floor the context
  * PASV only (no EPSV); IPv4 ftplib already uses PASV
  * data connection must reuse the control TLS session + close_notify (above)
  * upload targets ``/model/`` (printer storage UI) or ``/cache/`` (transient)

**File listing timestamp enrichment** (newest-first sort support):

``list_dir_with_timestamps()`` returns :class:`FileEntry` objects, each with
optional ISO-8601 UTC timestamps derived from a three-tier fallback ladder:

1. **MLSD** (RFC 3659): ``modify`` and ``create`` facts when the server
   supports them.  aioftp's test server and any RFC-3659-compliant daemon will
   take this path.  P1S firmware *may* not support MLSD — see below.
2. **LIST** (Unix-format): falls back when ``MLSD`` raises ``error_perm``.
   Parses both the ``HH:MM`` (recent, current year) and ``YYYY`` (old) forms of
   the date field.  Only ``modified_at`` is available (no create time).
3. **MDTM** (per-file): issues one ``MDTM <filename>`` command per file as a
   last resort.  Capped at :data:`_MDTM_MAX_FILES` round-trips to avoid
   hammering the printer for large listings.  Files beyond the cap get
   ``modified_at = None``.

The P1S firmware FTPS daemon is an embedded server; MLSD is not guaranteed.
The ``LIST`` path is the most likely happy-path on real hardware.  MDTM is the
last resort when the LIST date field is absent or unparseable.

``list_dir()`` preserves the original ``list[str]`` return type for all callers
that don't need timestamps.
"""

from __future__ import annotations

import asyncio
import ftplib
import io
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bambu_bridge.db.jobs import SlicedDateRepo

import structlog

from bambu_bridge.config import Settings
from bambu_bridge.protocol.tls import insecure_tls_context

log = structlog.get_logger(__name__)


class TransferTooLarge(ValueError):
    """A buffered transfer exceeded the configured memory budget."""

# Maximum number of per-file MDTM round-trips issued as a fallback.
# Beyond this cap the files receive modified_at = None rather than hanging
# the listing with O(N) network calls.
_MDTM_MAX_FILES = 30

# Month abbreviation table for LIST date parsing.
_MONTHS: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Regex for MDTM responses: "213 YYYYMMDDHHmmss"
_MDTM_RE = re.compile(r"^213\s+(\d{14})$")


@dataclass
class FileEntry:
    """A single file returned by :meth:`FtpsTransfer.list_dir_with_timestamps`.

    All timestamp fields are UTC datetimes or ``None`` when the server did not
    supply the information.  ``sort_basis`` records which tier provided the
    timestamp that was actually used for sorting at call-site.
    """

    name: str
    modified_at: datetime | None = None
    created_at: datetime | None = None
    # sort_basis is filled in by the caller (sort_files_newest_first) after
    # sliced_at is overlaid from the memo; not set by FtpsTransfer itself.
    sort_basis: str = "none"


def _parse_rfc3659_time(value: str | None) -> datetime | None:
    """Parse an RFC 3659 time value (``YYYYMMDDHHmmss[.frac]``) to UTC datetime.

    Returns ``None`` when the value is absent or malformed.
    """
    if not value:
        return None
    # Strip any fractional-seconds suffix.
    s = value.split(".")[0]
    if len(s) < 14:
        return None
    try:
        return datetime(
            int(s[0:4]), int(s[4:6]), int(s[6:8]),
            int(s[8:10]), int(s[10:12]), int(s[12:14]),
            tzinfo=UTC,
        )
    except (ValueError, IndexError):
        return None

FTPS_PORT = 990
_USER = "bblp"
# P1S firmware stores .gcode.3mf files in the FTPS root ("/"), not in a
# named sub-directory. Empirically confirmed on hardware 2026-05-19: the
# printer only pre-creates "/" and "/cache"; "/model" does not exist and
# any reference to it returns 550. Use the empty string as the sentinel
# so that PurePosixPath("/") / "" / name == "/name" (root-placed file).
UPLOAD_DIR_PERSISTENT = ""
UPLOAD_DIR_CACHE = "cache"
_TIMEOUT_S = 30


def _close_notify_unidirectional(conn: ssl.SSLSocket) -> None:
    """Send our TLS ``close_notify`` on the data socket; do NOT wait for the
    peer's.

    Stock ``FTP_TLS.storbinary``/``retrbinary`` call ``conn.unwrap()`` — a
    *bidirectional* shutdown that blocks until the peer's ``close_notify``
    arrives. The P1S never sends one (embedded server: it just wants ours,
    then it emits ``226`` and drops the data socket). The blocking unwrap
    therefore hangs to the socket timeout — the entire FTPS defect. curl /
    OpenSSL do a one-shot ``SSL_shutdown``; mirror that. A non-blocking
    ``unwrap()`` emits our ``close_notify`` and then raises
    ``SSLWantRead`` ("sent; not waiting") — exactly the curl behaviour.
    Verified on hardware 2026-05-19: ``226`` returns in <0.1 s.
    """
    try:
        conn.setblocking(False)
        conn.unwrap()
    except (ssl.SSLError, OSError):
        pass


class _ImplicitFTP_TLS(ftplib.FTP_TLS):
    """``ftplib.FTP_TLS`` adapted to the P1S's implicit-TLS quirks.

    Two overrides vs stock:

    * **implicit TLS** — stock ``FTP_TLS`` is explicit (plaintext until
      ``AUTH TLS``); the P1S is TLS from byte zero. Wrapping the socket the
      moment ftplib assigns it makes it implicit while keeping ftplib's
      data-channel session reuse (which the P1S also requires).
    * **unidirectional data shutdown** — see
      :func:`_close_notify_unidirectional`. Without this every transfer
      hangs to the socket timeout.
    """

    @property
    def sock(self) -> socket.socket | None:
        return self._sock

    @sock.setter
    def sock(self, value: socket.socket | None) -> None:
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def storbinary(
        self,
        cmd: str,
        fp: Any,
        blocksize: int = 8192,
        callback: Any = None,
        rest: Any = None,
    ) -> str:
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd, rest) as conn:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback is not None:
                    callback(buf)
            if isinstance(conn, ssl.SSLSocket):
                _close_notify_unidirectional(conn)
        return self.voidresp()

    def retrbinary(
        self,
        cmd: str,
        callback: Any,
        blocksize: int = 8192,
        rest: Any = None,
    ) -> str:
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd, rest) as conn:
            while True:
                try:
                    data = conn.recv(blocksize)
                except ssl.SSLEOFError:
                    break  # embedded server FIN'd without close_notify == EOF
                except ssl.SSLError as exc:
                    if "UNEXPECTED_EOF" in str(exc):
                        break
                    raise
                if not data:
                    break
                callback(data)
            if isinstance(conn, ssl.SSLSocket):
                _close_notify_unidirectional(conn)
        return self.voidresp()


class FtpsTransfer:
    """High-level upload/list/delete/download against one printer's FTPS.

    Each call opens a fresh connection: the P1S is flaky with long-lived FTP
    sessions and transfers are infrequent (job start), so per-op connect is
    the robust choice. ftplib is blocking; calls run via ``asyncio.to_thread``.
    """

    def __init__(
        self,
        ip: str,
        access_code: str,
        *,
        port: int = FTPS_PORT,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.ip = ip
        self.port = port
        self._access_code = access_code
        self._ssl_context = ssl_context
        self._max_bytes = Settings().bridge_max_transfer_bytes
        self._log = log.bind(ip=ip)

    # ----------------------------------------------------------------- #
    # Sync core (runs in a worker thread)
    # ----------------------------------------------------------------- #

    def _connect(self) -> _ImplicitFTP_TLS:
        ctx = self._ssl_context or insecure_tls_context()
        ftp = _ImplicitFTP_TLS(context=ctx, timeout=_TIMEOUT_S)
        ftp.connect(self.ip, self.port)
        ftp.login(_USER, self._access_code)
        ftp.prot_p()  # encrypt the data channel (server requires PROT P)
        ftp.set_pasv(True)  # P1S has no EPSV; IPv4 ftplib uses PASV anyway
        return ftp

    @staticmethod
    def _close(ftp: _ImplicitFTP_TLS) -> None:
        try:
            ftp.quit()
        except (OSError, ftplib.Error):  # best-effort; force the socket shut
            ftp.close()

    @staticmethod
    def _remote_path(remote_dir: str, name: str) -> str:
        # Bambu wants forward-slash paths rooted at the storage volume.
        return str(PurePosixPath("/") / remote_dir / PurePosixPath(name).name)

    def _upload(self, data: bytes, remote_path: str) -> None:
        ftp = self._connect()
        try:
            # our storbinary override sends a one-shot close_notify so the
            # P1S returns 226 instead of hanging (see _close_notify_*).
            ftp.storbinary(f"STOR {remote_path}", io.BytesIO(data))
        finally:
            self._close(ftp)

    def _download(self, remote_path: str) -> bytes:
        ftp = self._connect()
        chunks: list[bytes] = []
        total = 0

        def receive(chunk: bytes) -> None:
            nonlocal total
            total += len(chunk)
            if total > self._max_bytes:
                raise TransferTooLarge(f"File exceeds {self._max_bytes} byte transfer limit")
            chunks.append(chunk)

        try:
            ftp.retrbinary(f"RETR {remote_path}", receive)
        finally:
            self._close(ftp)
        return b"".join(chunks)

    # ----------------------------------------------------------------- #
    # LIST date-field parsers (Unix ls-style)
    # ----------------------------------------------------------------- #

    @staticmethod
    def _parse_list_date(
        month_str: str,
        day_str: str,
        year_or_time_str: str,
    ) -> datetime | None:
        """Parse the three-token date field from a Unix-format LIST line.

        Two forms emitted by FTP daemons:
        * Recent file (within ~6 months):  ``Jan 15 14:23``  — no year,
          time known; year is inferred as the current year, adjusting back
          one year when the resulting date would be in the future.
        * Old file:  ``Jan 15 2024``  — year known, time is midnight UTC.

        Returns ``None`` on any parse failure.
        """
        m = _MONTHS.get(month_str.lower())
        if m is None:
            return None
        try:
            d = int(day_str)
        except ValueError:
            return None

        if ":" in year_or_time_str:
            # HH:MM form — infer year.
            try:
                hhmm = year_or_time_str.split(":")
                hour = int(hhmm[0])
                minute = int(hhmm[1])
            except (IndexError, ValueError):
                return None
            now = datetime.now(tz=UTC)
            year = now.year
            try:
                dt = datetime(year, m, d, hour, minute, tzinfo=UTC)
            except ValueError:
                return None
            # If the resulting timestamp is in the future (by more than a few
            # minutes), the file was created last year — step back one year.
            if dt > now:
                try:
                    dt = datetime(year - 1, m, d, hour, minute, tzinfo=UTC)
                except ValueError:
                    return None
            return dt
        else:
            # YYYY form.
            try:
                year = int(year_or_time_str)
            except ValueError:
                return None
            try:
                return datetime(year, m, d, tzinfo=UTC)
            except ValueError:
                return None

    @staticmethod
    def _parse_list_line(line: str) -> tuple[str, datetime | None]:
        """Parse one Unix-format LIST line into (name, modified_at | None).

        Unix ls format (simplified):
          ``<perms> <links> <user> <group> <size> <Mon> <DD> <HH:MM|YYYY> <name>``

        We split into at most 9 tokens: columns 0-7 are fixed, token 8 is the
        filename (which may contain spaces; splitting stops at 9 tokens keeps
        the rest of the name together).
        """
        parts = line.split(maxsplit=8)
        if len(parts) < 9:
            # Short line — still extract the name from the last token.
            name = parts[-1] if parts else ""
            return name, None

        # Month is column 5, day is column 6, year-or-time is column 7,
        # filename is column 8.
        dt = FtpsTransfer._parse_list_date(parts[5], parts[6], parts[7])
        return parts[8], dt

    # ----------------------------------------------------------------- #
    # MDTM helper
    # ----------------------------------------------------------------- #

    @staticmethod
    def _mdtm_datetime(ftp: _ImplicitFTP_TLS, remote_path: str) -> datetime | None:
        """Issue ``MDTM <remote_path>`` and parse the response.

        Returns ``None`` on any failure (unsupported command, missing file, …).
        """
        try:
            resp = ftp.sendcmd(f"MDTM {remote_path}")
            m = _MDTM_RE.match(resp)
            if m is None:
                return None
            ts = m.group(1)
            return datetime(
                int(ts[0:4]), int(ts[4:6]), int(ts[6:8]),
                int(ts[8:10]), int(ts[10:12]), int(ts[12:14]),
                tzinfo=UTC,
            )
        except (ftplib.Error, ValueError):
            return None

    # ----------------------------------------------------------------- #
    # Core listing (returns FileEntry objects with timestamps)
    # ----------------------------------------------------------------- #

    def _list_with_facts(self, base: str) -> list[FileEntry]:
        """Return file entries with timestamps from the three-tier fallback ladder.

        Tier 1 — MLSD: ``modify`` and ``create`` facts (RFC 3659).
        Tier 2 — LIST: Unix-style date field (``modified_at`` only).
        Tier 3 — MDTM: per-file timestamp (capped at _MDTM_MAX_FILES).

        ``"."`` and ``".."`` are always stripped.
        """
        ftp = self._connect()
        try:
            # --- Tier 1: MLSD -------------------------------------------- #
            try:
                entries: list[FileEntry] = []
                for name, facts in ftp.mlsd(base):
                    if name in (".", ".."):
                        continue
                    # RFC 3659 §7.5: fact names are case-insensitive.
                    lf = {k.lower(): v for k, v in facts.items()}
                    modified_at = _parse_rfc3659_time(lf.get("modify"))
                    created_at = _parse_rfc3659_time(lf.get("create"))
                    entries.append(FileEntry(
                        name=name,
                        modified_at=modified_at,
                        created_at=created_at,
                    ))
                return entries
            except (ftplib.error_perm, ftplib.error_proto):
                pass  # fall through to LIST

            # --- Tier 2: LIST -------------------------------------------- #
            try:
                lines: list[str] = []
                ftp.retrlines(f"LIST {base}", lines.append)
                entries = []
                missing_dt: list[int] = []  # indices where modified_at is None
                for ln in lines:
                    name, dt = self._parse_list_line(ln)
                    if not name:
                        continue
                    if dt is None:
                        missing_dt.append(len(entries))
                    entries.append(FileEntry(name=name, modified_at=dt))

                # --- Tier 3: MDTM (capped) -------------------------------- #
                mdtm_budget = _MDTM_MAX_FILES
                for idx in missing_dt:
                    if mdtm_budget <= 0:
                        break
                    e = entries[idx]
                    remote_path = str(PurePosixPath(base) / e.name)
                    dt = self._mdtm_datetime(ftp, remote_path)
                    if dt is not None:
                        entries[idx] = FileEntry(
                            name=e.name,
                            modified_at=dt,
                            created_at=e.created_at,
                        )
                    mdtm_budget -= 1

                return entries

            except ftplib.error_perm as exc:
                # 550 = path not found / no access — degrade to empty.
                self._log.warning(
                    "ftps.list_empty_on_perm_error",
                    path=base,
                    error=str(exc),
                )
                return []

        finally:
            self._close(ftp)

    def _list(self, base: str) -> list[str]:
        """Legacy helper — names only, no timestamps.  Delegates to _list_with_facts."""
        return [e.name for e in self._list_with_facts(base)]

    def _delete(self, remote_path: str) -> None:
        ftp = self._connect()
        try:
            ftp.delete(remote_path)
        finally:
            self._close(ftp)

    # ----------------------------------------------------------------- #
    # Async public API (unchanged surface)
    # ----------------------------------------------------------------- #

    async def upload_bytes(
        self,
        data: bytes,
        name: str,
        *,
        remote_dir: str = UPLOAD_DIR_PERSISTENT,
    ) -> str:
        """Upload ``data`` as ``/<remote_dir>/<name>``; return the remote path."""
        if len(data) > self._max_bytes:
            raise TransferTooLarge(f"File exceeds {self._max_bytes} byte transfer limit")
        remote_path = self._remote_path(remote_dir, name)
        await asyncio.to_thread(self._upload, data, remote_path)
        self._log.info("ftps.uploaded", path=remote_path, size=len(data))
        return remote_path

    async def upload_file(
        self,
        local_path: str,
        *,
        name: str | None = None,
        remote_dir: str = UPLOAD_DIR_PERSISTENT,
    ) -> str:
        """Upload a local file; return the remote path."""
        fname = name or PurePosixPath(local_path).name
        def read() -> bytes:
            with Path(local_path).open("rb") as stream:
                return stream.read(self._max_bytes + 1)

        data = await asyncio.to_thread(read)
        return await self.upload_bytes(data, fname, remote_dir=remote_dir)

    def _file_revision(self, remote_path: str) -> tuple[int, str] | None:
        ftp = self._connect()
        try:
            ftp.voidcmd("TYPE I")
            size = ftp.size(remote_path)
            modified = ftp.sendcmd(f"MDTM {remote_path}")
            if size is None or not modified.startswith("213 "):
                return None
            return size, modified[4:].strip()
        except ftplib.error_perm:
            # Firmware without SIZE/MDTM must re-fetch, not trust an old name.
            return None
        finally:
            self._close(ftp)

    async def file_revision(self, name: str, *, remote_dir: str = "") -> tuple[int, str] | None:
        return await asyncio.to_thread(self._file_revision, self._remote_path(remote_dir, name))

    async def download_bytes(
        self, name: str, *, remote_dir: str = UPLOAD_DIR_PERSISTENT
    ) -> bytes:
        """Fetch ``/<remote_dir>/<name>`` and return its bytes.

        Buffered whole (matches the buffered upload path; LAN box, infrequent).
        """
        remote_path = self._remote_path(remote_dir, name)
        data = await asyncio.to_thread(self._download, remote_path)
        self._log.info("ftps.downloaded", path=remote_path, size=len(data))
        return data

    async def list_dir(self, remote_dir: str = UPLOAD_DIR_PERSISTENT) -> list[str]:
        """List filenames under ``/<remote_dir>/``."""
        base = str(PurePosixPath("/") / remote_dir)
        names = await asyncio.to_thread(self._list, base)
        return [PurePosixPath(n).name for n in names]

    async def list_dir_with_timestamps(
        self, remote_dir: str = UPLOAD_DIR_PERSISTENT
    ) -> list[FileEntry]:
        """List files under ``/<remote_dir>/`` with timestamp metadata.

        Uses the three-tier fallback ladder (MLSD → LIST → MDTM) described in
        this module's docstring.  Returns :class:`FileEntry` objects with
        ``modified_at`` and ``created_at`` populated where available.

        ``sort_basis`` on returned entries is left as ``"none"``; call
        :func:`sort_files_newest_first` to overlay the sliced-date memo and
        sort.
        """
        base = str(PurePosixPath("/") / remote_dir)
        entries = await asyncio.to_thread(self._list_with_facts, base)
        # Normalise names (strip any leading path components the server returns).
        return [
            FileEntry(
                name=PurePosixPath(e.name).name,
                modified_at=e.modified_at,
                created_at=e.created_at,
            )
            for e in entries
        ]

    async def delete(self, remote_path: str) -> None:
        """Delete a file by its remote path."""
        await asyncio.to_thread(self._delete, remote_path)
        self._log.info("ftps.deleted", path=remote_path)


# Backwards-compat alias: callers/tests that referenced the old aioftp
# subclass name still import something sane (it's just the client now).
BambuFTP: Any = _ImplicitFTP_TLS


# ------------------------------------------------------------------ #
# Sliced-date memo (opportunistic, populated by download paths)
# ------------------------------------------------------------------ #


class SlicedDateMemo:
    """Sliced-date cache mapping (filename, size_bytes) → sliced_at datetime.

    The "sliced date" is the authoritative user-facing timestamp — the moment
    Bambu Studio / OrcaSlicer finished slicing the file.  It lives inside the
    ``.gcode.3mf`` archive (``Metadata/slice_info.config`` ZIP entry
    ``date_time``, or the ``; generated by … on YYYY-MM-DD at HH:MM:SS``
    comment at the top of ``Metadata/plate_1.gcode``), but downloading the
    entire archive just to sort a file listing is unacceptable.

    Instead, this memo is filled **opportunistically** whenever the bridge
    already downloads a file for another reason (viz pre-warm, file download
    endpoint).  The key includes the file size so a re-sliced file with the
    same name is never stale.

    **Persistence:** when a :class:`~bambu_bridge.db.jobs.SlicedDateRepo`
    instance is supplied at construction the memo writes through to SQLite on
    every ``put``, and loads from the DB on ``get`` (no full in-memory mirror
    required).  Without a repo the memo falls back to a small in-process LRU
    dict (useful for tests and for the in-process viz-prewarm path where the
    DB write-through already happened).

    In-memory LRU: :data:`_MEMO_MAX` entries, oldest-evicted (kept for the
    no-repo fallback path and as a read-side cache to avoid repeated DB hits
    within a single listing call).
    """

    _MEMO_MAX = 64

    def __init__(self, repo: SlicedDateRepo | None = None) -> None:
        # Optional write-through / load-from DB backend.
        self._repo: SlicedDateRepo | None = repo
        # Small in-process LRU for the no-repo path and as a read cache.
        self._data: dict[tuple[str, int], datetime] = {}

    def put(self, name: str, size: int, sliced_at: datetime) -> None:
        """Store a sliced-date for ``(name, size)`` in the in-memory LRU.

        Always synchronous — only updates the in-memory LRU.  To also persist
        to the DB repo (when one is attached), call the async counterpart
        :meth:`aput` from an ``async`` context.

        This is the right call site for callers that cannot ``await`` (e.g.
        direct test setups, legacy in-process paths); the DB write can be
        deferred or done separately via ``aput``.
        """
        key = (name, size)
        if key in self._data:
            del self._data[key]
        elif len(self._data) >= self._MEMO_MAX:
            oldest = next(iter(self._data))
            del self._data[oldest]
        self._data[key] = sliced_at

    async def aput(self, name: str, size: int, sliced_at: datetime) -> None:
        """Store a sliced-date for ``(name, size)`` in both the LRU and the DB.

        Updates the in-memory LRU synchronously then awaits the DB write (when
        a :class:`~bambu_bridge.db.jobs.SlicedDateRepo` is attached).  Always
        safe to call from ``async`` contexts; the DB write is a no-op when no
        repo is attached.

        This is the preferred call site for async callers (viz_cache.fill_mesh,
        etc.) because it guarantees the DB row is committed before the caller
        returns.
        """
        self.put(name, size, sliced_at)  # LRU update (sync)
        if self._repo is not None:
            await self._repo.put(name, size, sliced_at)

    def get(self, name: str, size: int) -> datetime | None:
        """Return the cached sliced-date for ``(name, size)``, or ``None``.

        Checks the in-memory LRU first; falls back to the DB repo only if the
        key is absent locally.  The DB path is intentionally synchronous
        (blocking) — listing calls are infrequent and the existing pattern in
        this codebase is to run blocking work via ``asyncio.to_thread``.
        However, ``get`` is almost always called from async contexts where the
        LRU will already have been primed.  The DB fallback runs only for cold
        starts where the LRU is empty but the DB has rows.

        Because ``get`` may be called from a sync (to_thread) context the DB
        query cannot use ``await``.  We therefore rely on the LRU being
        populated by ``put`` calls (which always update it) and the startup
        load in :func:`bambu_bridge.main.lifespan`.  A DB round-trip on
        ``get`` is explicitly *not* implemented here — the listing code path
        uses :meth:`latest_by_names` for batch async DB reads instead.
        """
        return self._data.get((name, size))

    def latest_by_names(self, names: list[str]) -> dict[str, datetime]:
        """Return the most-recently-learned sliced_at per filename from the
        in-memory LRU only (synchronous, no DB I/O).

        For each filename, returns the entry with the maximum sliced_at across
        all stored size variants.  This is the sync fallback for contexts where
        ``await latest_by_names_async`` cannot be called.

        The async counterpart :meth:`latest_by_names_async` queries the DB repo
        and should be preferred from async call sites.
        """
        result: dict[str, datetime] = {}
        for (fname, _size), sliced_at in self._data.items():
            if fname in names:
                existing = result.get(fname)
                if existing is None or sliced_at > existing:
                    result[fname] = sliced_at
        return result

    async def latest_by_names_async(
        self, names: list[str]
    ) -> dict[str, datetime]:
        """Return the most-recently-learned sliced_at per filename for *names*.

        Queries the DB repo when available (single SQL query with MAX
        aggregation — no full table scan in Python), then overlays the
        in-memory LRU on top.  The overlay handles two cases:

        1. Entries written via ``put`` whose async DB task has not yet
           committed (the LRU is always updated synchronously by ``put``).
        2. The no-repo fallback for tests and unit paths.

        The LRU entry wins over the DB entry only when it is strictly newer,
        so a stale LRU cannot hide a more-recent DB row.

        Returns a dict mapping filename → sliced_at for every name that has at
        least one stored entry (in either the DB or the LRU).  Names with no
        entry are omitted.
        """
        if self._repo is not None:
            db_result = await self._repo.latest_by_names(names)
        else:
            db_result = {}
        # Overlay in-memory LRU: prefer whichever is newer.
        lru_result = self.latest_by_names(names)
        result = dict(db_result)
        for name, lru_dt in lru_result.items():
            existing = result.get(name)
            if existing is None or lru_dt > existing:
                result[name] = lru_dt
        return result

    def __len__(self) -> int:
        return len(self._data)


def extract_sliced_at_from_bytes(data: bytes) -> datetime | None:
    """Extract the sliced timestamp from a ``.gcode.3mf`` archive's bytes.

    Two extraction strategies (tried in order):

    1. **ZIP entry date_time** of ``Metadata/slice_info.config`` or
       ``Metadata/plate_1.gcode`` — no decompression needed, just reads the
       ZIP central directory.  This is the most reliable source because Bambu
       Studio and OrcaSlicer always touch these members at slice time.

    2. **Gcode header comment** — ``; generated by … on YYYY-MM-DD at HH:MM:SS``
       in the first 4 kB of ``Metadata/plate_1.gcode``.  Fallback for archives
       where the ZIP timestamps were zeroed by a transfer tool.

    Returns ``None`` on any failure (not a ZIP, missing members, malformed).
    """
    import zipfile  # local import to avoid circular imports

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())

            # Strategy 1: ZIP entry date_time.
            for candidate in ("Metadata/slice_info.config", "Metadata/plate_1.gcode"):
                if candidate in names:
                    info = zf.getinfo(candidate)
                    dt_tuple = info.date_time  # (year, month, day, hour, min, sec)
                    if dt_tuple[0] >= 2020:  # sanity: after epoch-zero 1980
                        try:
                            return datetime(
                                dt_tuple[0], dt_tuple[1], dt_tuple[2],
                                dt_tuple[3], dt_tuple[4], dt_tuple[5],
                                tzinfo=UTC,
                            )
                        except ValueError:
                            pass

            # Strategy 2: gcode header comment.
            if "Metadata/plate_1.gcode" in names:
                try:
                    header = zf.read("Metadata/plate_1.gcode")[:4096]
                    text = header.decode("utf-8", errors="replace")
                    m = re.search(
                        r";\s*generated by .+ on (\d{4}-\d{2}-\d{2}) at (\d{2}:\d{2}:\d{2})",
                        text,
                        re.IGNORECASE,
                    )
                    if m:
                        date_str = m.group(1)
                        time_str = m.group(2)
                        parts_d = date_str.split("-")
                        parts_t = time_str.split(":")
                        return datetime(
                            int(parts_d[0]), int(parts_d[1]), int(parts_d[2]),
                            int(parts_t[0]), int(parts_t[1]), int(parts_t[2]),
                            tzinfo=UTC,
                        )
                except Exception:  # noqa: BLE001
                    pass

    except Exception:  # noqa: BLE001
        pass
    return None


def sort_files_newest_first(
    entries: list[FileEntry],
    *,
    memo: SlicedDateMemo | None = None,
    sizes: dict[str, int] | None = None,
    name_map: dict[str, datetime] | None = None,
) -> list[FileEntry]:
    """Sort ``entries`` newest-first using the sliced → modified → created chain.

    Priority:
    1. ``sliced_at`` from *name_map* (a pre-built name → datetime dict, e.g.
       from :meth:`SlicedDateMemo.latest_by_names_async`).  Takes precedence
       over the *memo* + *sizes* path when both are supplied.
    2. ``sliced_at`` from *memo* (keyed by ``(name, size_bytes)``).
    3. ``modified_at`` from the file entry.
    4. ``created_at`` from the file entry.
    5. No timestamp — sort last, stable relative order.

    ``sort_basis`` is set on each returned entry to one of
    ``"sliced"|"modified"|"created"|"none"``.

    *memo* and *sizes* are both optional; when absent the size-keyed sliced
    tier is simply skipped.  *sizes* maps filename → file size in bytes (used
    to key the memo lookup).  *name_map* is a pre-built name-keyed dict that
    takes priority over the memo lookup — the listing endpoint uses this path
    to avoid a protected-attribute scan of the memo's internal dict.
    """
    # Single pass: determine sort_basis and sort_dt for each entry.
    dated: list[tuple[FileEntry, datetime | None]] = []
    for e in entries:
        sliced_at: datetime | None = None

        # Tier 1: pre-built name_map (highest priority for the name-keyed path).
        if name_map is not None:
            sliced_at = name_map.get(e.name)

        # Tier 2: size-keyed memo lookup (used when sizes are known).
        if sliced_at is None and memo is not None and sizes is not None:
            sz = sizes.get(e.name)
            if sz is not None:
                sliced_at = memo.get(e.name, sz)

        if sliced_at is not None:
            basis = "sliced"
            sort_dt: datetime | None = sliced_at
        elif e.modified_at is not None:
            basis = "modified"
            sort_dt = e.modified_at
        elif e.created_at is not None:
            basis = "created"
            sort_dt = e.created_at
        else:
            basis = "none"
            sort_dt = None

        dated.append((
            FileEntry(
                name=e.name,
                modified_at=e.modified_at,
                created_at=e.created_at,
                sort_basis=basis,
            ),
            sort_dt,
        ))

    # Sort: dated entries newest-first (tier 0, descending ts), undated last (tier 1).
    def _sort_key(pair: tuple[FileEntry, datetime | None]) -> tuple[int, float]:
        _, dt = pair
        if dt is None:
            return (1, 0.0)
        return (0, -dt.timestamp())

    dated.sort(key=_sort_key)
    return [e for e, _ in dated]
