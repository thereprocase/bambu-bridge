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
from typing import TYPE_CHECKING

from bambu_bridge.protocol.ftps import FtpsTransfer, _ImplicitFTP_TLS

if TYPE_CHECKING:
    from bambu_bridge.native_gateway import NativeGateway


async def serve_ftps(
    gateway: NativeGateway, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    backend: _ImplicitFTP_TLS | None = None
    passive: asyncio.Server | None = None
    channel: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] | None = None
    user = ""
    private = True
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
                if backend:
                    return
                user = argument
                await reply("331 Password required")
                continue
            if verb == "PASS" and backend is None:
                if not await gateway.authenticate(user, argument, peer):
                    await reply("530 Login incorrect")
                    return
                service = gateway.service()
                if not service.connected:
                    await reply("421 Printer offline")
                    return
                backend = await asyncio.to_thread(
                    FtpsTransfer(
                        service.ip, service.access_code, port=gateway.app.state.ftps_port
                    )._connect
                )
                await reply("230 Login successful")
                continue
            if backend is None:
                await reply("530 Login first")
                continue
            gateway.service()
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

                passive = await asyncio.start_server(
                    accept,
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
                await reply("150 Opening data connection")
                try:
                    async with gateway.transfer_lock:
                        data_reader, data_writer = await asyncio.wait_for(channel, 20)
                        if verb == "STOR":
                            chunks: list[bytes] = []
                            total = 0
                            while chunk := await asyncio.wait_for(data_reader.read(65536), 60):
                                total += len(chunk)
                                if total > limit:
                                    raise ValueError("Transfer limit exceeded")
                                chunks.append(chunk)
                            await clear_passive()
                            gateway.service()
                            await asyncio.to_thread(
                                backend.storbinary, "STOR " + argument, io.BytesIO(b"".join(chunks))
                            )
                        else:
                            result: list[bytes] = []
                            total = 0

                            def receive(chunk: bytes, result: list[bytes] = result) -> None:
                                nonlocal total
                                total += len(chunk)
                                if total > limit:
                                    raise ValueError("Transfer limit exceeded")
                                result.append(chunk)

                            await asyncio.to_thread(backend.retrbinary, command, receive)
                            assert data_writer is not None
                            for chunk in result:
                                data_writer.write(chunk)
                                await asyncio.wait_for(data_writer.drain(), 30)
                            await clear_passive()
                    await reply("226 Transfer complete")
                except Exception:
                    await clear_passive()
                    await reply("451 Printer transfer failed; file is not confirmed")
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
                    await reply(await asyncio.to_thread(backend.sendcmd, command))
                except ftplib.Error:
                    await reply("550 Printer rejected the file operation")
            else:
                await reply("502 Command not supported; use passive FTPS")
    finally:
        await clear_passive()
        if backend:
            await asyncio.to_thread(backend.close)
