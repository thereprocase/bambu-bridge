"""Implicit FTPS front end for native Orca uploads and SD file operations.

The existing P1S transfer client handles upstream TLS session reuse and close
quirks. A successful STOR reply is sent only after the printer received the
file, so the following native project_file command cannot outrun its upload.
"""

from __future__ import annotations

import asyncio
import contextlib
import ftplib
import io
import ssl
import time
from typing import TYPE_CHECKING

import structlog

from bambu_bridge.native_inbox import InboxError
from bambu_bridge.protocol.ftps import FtpsTransfer, _ImplicitFTP_TLS

if TYPE_CHECKING:
    from bambu_bridge.native_gateway import NativeGateway


class UploadReader(asyncio.StreamReader):
    """Preserve EOF already delivered by TLS if its reply later hits a reset.

    StreamReader normally lets a later connection_lost exception mask buffered
    bytes AND a previously delivered EOF. A slow disk consumer must not change
    the result compared with a consumer that drained those same bytes promptly.
    Errors before protocol EOF remain fatal; no private stream buffers are read.
    """

    protocol_eof = False

    def feed_eof(self) -> None:
        self.protocol_eof = True
        super().feed_eof()

    def set_exception(self, exc) -> None:
        if self.protocol_eof and isinstance(exc, ConnectionResetError | BrokenPipeError):
            return
        super().set_exception(exc)


async def serve_ftps(
    gateway: NativeGateway, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    backend: _ImplicitFTP_TLS | None = None
    authenticated = False
    passive: asyncio.Server | None = None
    channel: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] | None = None
    user = ""
    private = True
    upstream_directory = "/"
    restart_offset: str | None = None
    data_tls_version: str | None = None
    peer = writer.get_extra_info("peername")[0]
    data_writer: asyncio.StreamWriter | None = None
    limit = gateway.app.state.settings.bridge_max_transfer_bytes

    async def reply(value: str) -> None:
        writer.write((value.replace("\r\n", "\n").replace("\n", "\r\n") + "\r\n").encode())
        await asyncio.wait_for(writer.drain(), 10)

    async def clear_passive() -> None:
        nonlocal passive, channel, data_writer
        if passive:
            passive.close()
        if data_writer:
            data_writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(data_writer.wait_closed(), 2)
            data_writer = None
        if channel and channel.done() and not channel.cancelled():
            channel.result()[1].close()
        elif channel:
            channel.cancel()
        channel = None
        if passive:
            await passive.wait_closed()
            passive = None

    try:
        await reply("220 Bambu Bridge native FTPS")
        while True:
            line = await asyncio.wait_for(reader.readline(), 120)
            if not line:
                return
            if len(line) > 4096 or b"\0" in line:
                return
            command = line.decode("utf-8").rstrip("\r\n")
            verb, _, argument = command.partition(" ")
            verb = verb.upper()
            if verb == "QUIT":
                await reply("221 Goodbye")
                return
            if verb == "USER":
                if authenticated:
                    return
                user = argument
                await reply("331 Password required")
                continue
            if verb == "PASS" and not authenticated:
                if not await gateway.authenticate(user, argument, peer):
                    gateway.note("ftps", "access_code_rejected")
                    await reply("530 Login incorrect")
                    return
                if gateway.inbox is None:
                    service = gateway.service()
                    if not service.connected:
                        await reply("421 Printer offline")
                        return
                    backend = await asyncio.to_thread(
                        FtpsTransfer(
                            service.ip, service.access_code, port=gateway.app.state.ftps_port
                        )._connect
                    )
                    upstream_directory = await asyncio.to_thread(backend.pwd)
                authenticated = True
                await reply("230 Login successful")
                gateway.note("ftps", "authenticated")
                continue
            if not authenticated:
                await reply("530 Login first")
                continue
            gateway.service()
            if gateway.inbox is not None:
                from bambu_bridge.native_inbox import logical_path

                if verb in ("TYPE", "NOOP", "OPTS"):
                    await reply("200 OK")
                    continue
                if verb in ("PWD", "XPWD"):
                    await reply('257 "' + upstream_directory.replace('"', '""') + '"')
                    continue
                if verb in ("CWD", "CDUP"):
                    upstream_directory = logical_path(
                        ".." if verb == "CDUP" else argument, upstream_directory
                    )
                    await reply("250 Directory selected")
                    continue
                if verb == "SYST":
                    await reply("215 UNIX Type: L8")
                    continue
                if verb == "FEAT":
                    await reply("211-Features\n PBSZ\n PROT\n EPSV\n211 End")
                    continue
                if verb == "REST":
                    await reply("502 Resumed uploads are not supported by durable custody")
                    continue
                if verb in (
                    "RETR",
                    "LIST",
                    "NLST",
                    "MLSD",
                    "SIZE",
                    "MDTM",
                    "DELE",
                    "RNFR",
                    "RNTO",
                    "MKD",
                    "RMD",
                ):
                    if backend is None:
                        service = gateway.service()
                        backend = await asyncio.to_thread(
                            FtpsTransfer(
                                service.ip, service.access_code, port=gateway.app.state.ftps_port
                            )._connect
                        )
                    await asyncio.to_thread(backend.cwd, upstream_directory)
            if verb == "PBSZ":
                await reply("200 PBSZ=0")
            elif verb == "PROT":
                if argument not in ("P", "C"):
                    await reply("504 Use PROT P or C")
                else:
                    private = argument == "P"
                    await reply("200 Data protection set")
            elif verb in ("PASV", "EPSV"):
                await clear_passive()
                data_tls_version = None
                channel = asyncio.get_running_loop().create_future()
                target = channel

                def accept(
                    r: asyncio.StreamReader,
                    w: asyncio.StreamWriter,
                    target: asyncio.Future[
                        tuple[asyncio.StreamReader, asyncio.StreamWriter]
                    ] = target,
                ) -> None:
                    if w.get_extra_info("peername")[0] != peer or target.done():
                        w.close()
                    else:
                        target.set_result((r, w))

                loop = asyncio.get_running_loop()

                def data_protocol(accept=accept, loop=loop):
                    return asyncio.StreamReaderProtocol(
                        UploadReader(limit=256 * 1024), accept, loop=loop
                    )

                passive = await loop.create_server(
                    data_protocol,
                    gateway.host,
                    0,
                    ssl=gateway.context if private else None,
                    **({"ssl_handshake_timeout": 15} if private else {}),
                )
                port = passive.sockets[0].getsockname()[1]
                if verb == "EPSV":
                    await reply(f"229 Entering Extended Passive Mode (|||{port}|)")
                else:
                    await reply(
                        "227 Entering Passive Mode ("
                        + gateway.host.replace(".", ",")
                        + f",{port // 256},{port % 256})"
                    )
            elif verb in ("STOR", "RETR", "LIST", "NLST", "MLSD"):
                if channel is None:
                    await reply("425 Use PASV or EPSV first")
                    continue
                if gateway.inbox is not None and gateway.transfer_lock.locked():
                    await reply("452 BBFTP_BUSY; upload not accepted")
                    await clear_passive()
                    continue
                await reply("150 Opening data connection")
                phase = "waiting_for_transfer_slot"
                total = 0
                started = time.monotonic()
                try:
                    async with gateway.transfer_lock:
                        phase = "waiting_for_client_data"
                        data_reader, data_writer = await asyncio.wait_for(channel, 20)
                        tls = data_writer.get_extra_info("ssl_object")
                        data_tls_version = tls.version() if tls else None
                        if verb == "STOR":
                            phase = "receiving_client_data"
                            if gateway.inbox is not None:
                                phase = "reserving_server_storage"
                                row = await asyncio.to_thread(
                                    gateway.inbox.reserve,
                                    gateway.config["printer_id"],
                                    logical_path(argument, upstream_directory),
                                    limit,
                                )
                                phase = "receiving_client_data"
                                stored = await gateway.inbox.receive(data_reader, row, limit)
                                total = stored["bytes"]
                                gateway.inbox_wake.set()
                                # The receipt identifies durable SERVER custody,
                                # never printer delivery or a physical start.
                                await reply(f"226 BBFTP_STORED id={stored['id']} bytes={total}")
                                await clear_passive()
                                continue
                            chunks: list[bytes] = []
                            total = 0
                            while chunk := await asyncio.wait_for(data_reader.read(65536), 60):
                                total += len(chunk)
                                if total > limit:
                                    phase = "transfer_size_limit"
                                    raise ValueError("Transfer limit exceeded")
                                chunks.append(chunk)
                            await clear_passive()
                            service = gateway.service()
                            phase = "connecting_for_upload"
                            # The laptop transfer can take minutes. Do not
                            # reuse a printer control session that sat idle
                            # throughout it. Refresh BEFORE any STOR, never
                            # replay a write whose outcome is uncertain.
                            await asyncio.to_thread(backend.close)
                            backend = await asyncio.to_thread(
                                FtpsTransfer(
                                    service.ip,
                                    service.access_code,
                                    port=gateway.app.state.ftps_port,
                                )._connect
                            )
                            phase = "restoring_upload_directory"
                            await asyncio.to_thread(backend.cwd, upstream_directory)
                            phase = "uploading_to_printer"
                            await asyncio.to_thread(
                                backend.storbinary,
                                "STOR " + argument,
                                io.BytesIO(b"".join(chunks)),
                                rest=restart_offset,
                            )
                            restart_offset = None
                        else:
                            phase = "reading_from_printer"
                            result: list[bytes] = []
                            total = 0

                            def receive(chunk: bytes, result: list[bytes] = result) -> None:
                                nonlocal total, phase
                                total += len(chunk)
                                if total > limit:
                                    phase = "transfer_size_limit"
                                    raise ValueError("Transfer limit exceeded")
                                result.append(chunk)

                            await asyncio.to_thread(backend.retrbinary, command, receive)
                            phase = "sending_client_data"
                            assert data_writer is not None
                            for chunk in result:
                                data_writer.write(chunk)
                                await asyncio.wait_for(data_writer.drain(), 30)
                            await clear_passive()
                    await reply("226 Transfer complete")
                except Exception as exc:
                    # Log structure, never exception text: FTP replies may
                    # contain filenames, addresses, or credentials.
                    ftp_code = None
                    if isinstance(exc, ftplib.Error):
                        prefix = str(exc)[:3]
                        if len(prefix) == 3 and prefix.isascii() and prefix.isdigit():
                            ftp_code = int(prefix)
                    diagnostic = {
                        "code": transfer_failure_code(phase, exc),
                        "phase": phase,
                        "tls_version": data_tls_version,
                        "operation": verb,
                        "bytes": total,
                        "limit_bytes": limit,
                        "exception_type": type(exc).__name__,
                        "ftp_reply_code": ftp_code,
                        "errno": exc.errno if isinstance(exc, OSError) else None,
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                    }
                    gateway.note("ftps", "transfer_failed")
                    gateway.diagnostics["ftps"]["last_transfer_failure"] = diagnostic
                    structlog.get_logger(__name__).warning(
                        "native.ftps.transfer_failed", **diagnostic
                    )
                    await clear_passive()
                    await reply(f"451 {diagnostic['code']} at {phase}; file is not confirmed")
                    return  # failed transfers leave the upstream FTP stream ambiguous
            elif verb in {
                "TYPE",
                "CWD",
                "CDUP",
                "PWD",
                "XPWD",
                "SYST",
                "FEAT",
                "OPTS",
                "SIZE",
                "MDTM",
                "NOOP",
                "DELE",
                "RNFR",
                "RNTO",
                "MKD",
                "RMD",
                "REST",
            }:
                try:
                    response = await asyncio.to_thread(backend.sendcmd, command)
                    if verb in ("CWD", "CDUP"):
                        upstream_directory = await asyncio.to_thread(backend.pwd)
                    elif verb == "REST":
                        restart_offset = argument
                    await reply(response)
                except ftplib.Error:
                    await reply("550 Printer rejected the file operation")
            else:
                await reply("502 Command not supported; use passive FTPS")
    finally:
        await clear_passive()
        if backend:
            await asyncio.to_thread(backend.close)


def transfer_failure_code(phase: str, error: Exception) -> str:
    """Stable application codes carried inside standards-compliant FTP replies."""
    if isinstance(error, InboxError):
        return error.code
    if phase == "transfer_size_limit":
        return "BBFTP_SIZE_LIMIT"
    if phase == "reserving_server_storage":
        return "BBFTP_STORAGE_UNAVAILABLE"
    client = phase in ("waiting_for_client_data", "receiving_client_data", "sending_client_data")
    side = "CLIENT" if client else "PRINTER"
    if isinstance(error, TimeoutError):
        if phase == "waiting_for_client_data":
            return "BBFTP_DATA_CONNECT_TIMEOUT"
        if phase == "waiting_for_transfer_slot":
            return "BBFTP_BUSY_TIMEOUT"
        return f"BBFTP_{side}_TIMEOUT"
    if isinstance(error, ConnectionResetError):
        return f"BBFTP_{side}_RESET"
    if isinstance(error, ssl.SSLError):
        return f"BBFTP_{side}_TLS"
    if isinstance(error, ftplib.Error):
        return "BBFTP_PRINTER_REJECTED"
    return f"BBFTP_{side}_TRANSFER_ERROR"
