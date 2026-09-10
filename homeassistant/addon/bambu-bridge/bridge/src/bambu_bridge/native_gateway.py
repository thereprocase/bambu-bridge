"""Native P1S LAN gateway on an explicitly configured private IPv4 address.

Orca keeps its P1S Device and print dialogs. This gateway authenticates a
separate LAN code and multiplexes the bridge's existing MQTT/camera sessions.
No vendor networking code or signing credentials are bundled.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import ssl
import string
import struct
import time
from collections import defaultdict, deque
from typing import Any

from bambu_bridge.pairing import PairingStore, identity


def field(value: bytes) -> bytes:
    return struct.pack("!H", len(value)) + value


def take(data: bytes, pos: int) -> tuple[bytes, int]:
    size = struct.unpack_from("!H", data, pos)[0]
    pos += 2
    if pos + size > len(data):
        raise ValueError("Truncated MQTT field")
    return data[pos : pos + size], pos + size


def packet(kind: int, body: bytes = b"") -> bytes:
    size = len(body)
    result = bytearray([kind])
    while True:
        digit = size % 128
        size //= 128
        result.append(digit | (128 if size else 0))
        if not size:
            return bytes(result) + body


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    head = (await reader.readexactly(1))[0]
    size = 0
    for i in range(4):
        digit = (await reader.readexactly(1))[0]
        size += (digit & 127) * 128**i
        if size > 1024 * 1024:
            raise ValueError("MQTT packet too large")
        if not digit & 128:
            return head, await reader.readexactly(size)
    raise ValueError("Invalid MQTT length")


def rewrite_address(value: Any, old: str, new: str) -> Any:
    if isinstance(value, dict):
        return {k: rewrite_address(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [rewrite_address(v, old, new) for v in value]
    if isinstance(value, str):
        return value.replace(old, new)
    return value


class NativeGateway:
    def __init__(
        self,
        app: Any,
        store: PairingStore,
        host: str,
        *,
        ports: tuple[int, int, int] = (8883, 990, 6000),
        detect_port: int = 3000,
    ):
        address = ipaddress.ip_address(host)
        if address.version != 4 or address.is_unspecified or address.is_multicast:
            raise ValueError("Native gateway needs one explicit private IPv4 address")
        if not (address.is_private or address in ipaddress.ip_network("100.64.0.0/10")):
            raise ValueError("Use a LAN, loopback or Tailscale address for the native gateway")
        self.app, self.store, self.host, self.ports = app, store, host, ports
        self.detect_port = detect_port
        self.servers: list[asyncio.Server] = []
        self.tasks: set[asyncio.Task[None]] = set()
        self.writers: set[asyncio.StreamWriter] = set()
        self.failed: dict[str, deque[float]] = defaultdict(deque)
        self.transfer_lock = asyncio.Semaphore(2)
        self.change_lock = asyncio.Lock()
        self.config: dict[str, str] | None = None
        self.context: ssl.SSLContext | None = None
        self.last_error: str | None = None
        self.diagnostics: dict[str, dict[str, Any]] = {}
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS native_gateway (
                id INTEGER PRIMARY KEY CHECK(id=1), printer_id TEXT NOT NULL,
                salt TEXT NOT NULL, hash TEXT NOT NULL, enabled INTEGER NOT NULL)""")
            row = db.execute("SELECT * FROM native_gateway WHERE id=1 AND enabled=1").fetchone()
        if row:
            self.config = dict(row)

    def service(self) -> Any:
        if self.config is None:
            raise ValueError("Native gateway disabled")
        service = self.app.state.registry.get(self.config["printer_id"])
        if getattr(service, "cert_status", "unknown") == "changed":
            raise ValueError("Printer certificate needs review")
        return service

    def status(self) -> dict[str, Any]:
        return {
            "configured": True,
            "enabled": bool(self.servers),
            "host": self.host,
            "printer_id": self.config["printer_id"] if self.config else None,
            "model": "P1S",
            "error": self.last_error,
            "connections": self.diagnostics,
            "ports": {
                "mqtt": self.ports[0],
                "ftps": self.ports[1],
                "camera": self.ports[2],
                "detect": self.detect_port,
            },
        }

    async def start(self) -> None:
        if not self.config:
            return
        service = self.service()
        # Native TLS clients may offer RSA-authenticated cipher suites only.
        # Keep this identity separate from pinned phones and the old EC leaf.
        directory = self.store.directory / "native-rsa"
        key, cert, _ = identity(
            directory, common_name=service.serial, key_kind="rsa", host=self.host
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(cert), str(key))
        self.context = context
        from bambu_bridge.native_ftps import serve_ftps

        try:
            for port, handler in zip(
                (*self.ports, self.detect_port),
                (self.mqtt, serve_ftps, self.camera, self.detect),
                strict=True,
            ):

                def connected(
                    reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter,
                    handler: Any = handler,
                ) -> None:
                    task = asyncio.create_task(self.client(handler, reader, writer))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)

                options: dict[str, Any] = {}
                if handler != self.detect:
                    options = {"ssl": context, "ssl_handshake_timeout": 10}
                server = await asyncio.start_server(
                    connected, self.host, port, limit=8192, **options
                )
                self.servers.append(server)
            self.last_error = None
        except Exception:
            await self.close()
            self.last_error = "Could not start native listeners; check the bind address and ports"
            raise

    async def close(self) -> None:
        for server in self.servers:
            server.close()
        for writer in list(self.writers):
            writer.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.gather(*(s.wait_closed() for s in self.servers))
        self.servers.clear()

    async def announce(self, target: str) -> None:
        """Unicast discovery to the authenticated owner's current computer.

        Multicast discovery does not cross a Tailscale link. The target comes
        from the trusted HTTP client address, never from a request body.
        """
        address = ipaddress.ip_address(target)
        if (
            address.version != 4
            or not (address.is_private or address in ipaddress.ip_network("100.64.0.0/10"))
            or address.is_unspecified
            or address.is_multicast
        ):
            raise ValueError("Discovery requires a private IPv4 client address")
        if not self.servers:
            raise ValueError("Enable native access first")
        serial = self.service().serial
        message = (
            "NOTIFY * HTTP/1.1\r\n"
            "HOST: 239.255.255.250:1900\r\nServer: UPnP/1.0\r\n"
            f"Location: {self.host}\r\nNT: urn:bambulab-com:device:3dprinter:1\r\n"
            f"USN: {serial}\r\nCache-Control: max-age=1800\r\n"
            "DevModel.bambu.com: C12\r\nDevName.bambu.com: Bridge P1S\r\n"
            "DevSignal.bambu.com: -50\r\nDevConnect.bambu.com: lan\r\n"
            "DevBind.bambu.com: free\r\nDevseclink.bambu.com: secure\r\n"
            "DevCap.bambu.com: 1\r\n\r\n"
        ).encode()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind((self.host, 0))
            sock.settimeout(2)
            for port in (1990, 2021):
                await asyncio.to_thread(sock.sendto, message, (target, port))

    async def enable(self, printer_id: str) -> dict[str, Any]:
        async with self.change_lock:
            await self.close()
            code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
            salt = secrets.token_hex(16)
            hashed = hashlib.scrypt(
                code.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1
            ).hex()
            self.config = {"printer_id": printer_id, "salt": salt, "hash": hashed}
            try:
                await self.start()
            except Exception:
                self.config = None
                with self.store.connect() as db:
                    db.execute("UPDATE native_gateway SET enabled=0 WHERE id=1")
                raise
            with self.store.connect() as db:
                db.execute(
                    "INSERT OR REPLACE INTO native_gateway VALUES (1, ?, ?, ?, 1)",
                    (printer_id, salt, hashed),
                )
            return {**self.status(), "access_code": code}

    async def disable(self) -> None:
        async with self.change_lock:
            await self.close()
            self.config = None
            with self.store.connect() as db:
                db.execute("UPDATE native_gateway SET enabled=0 WHERE id=1")

    async def authenticate(self, username: str, code: str, peer: str) -> bool:
        attempts = self.failed[peer]
        while attempts and attempts[0] < time.monotonic() - 60:
            attempts.popleft()
        config = self.config
        if config is None or len(attempts) >= 5:
            return False
        attempts.append(time.monotonic())
        if username != "bblp" or len(code) != 8:
            return False
        hashed = await asyncio.to_thread(
            hashlib.scrypt, code.encode(), salt=bytes.fromhex(config["salt"]), n=16384, r=8, p=1
        )
        ok = self.config is config and hmac.compare_digest(hashed.hex(), config["hash"])
        if ok:
            attempts.clear()
        return ok

    async def client(
        self, handler: Any, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if len(self.writers) >= 16:
            writer.close()
            return
        self.writers.add(writer)
        protocol = (
            "detect"
            if handler == self.detect
            else "mqtt"
            if handler == self.mqtt
            else "camera"
            if handler == self.camera
            else "ftps"
        )
        self.note(protocol, "tcp_connected" if handler == self.detect else "tls_connected")
        try:
            if handler in (self.mqtt, self.camera, self.detect):
                await handler(reader, writer)
            else:
                await handler(self, reader, writer)
        except asyncio.CancelledError:
            pass
        except (asyncio.IncompleteReadError, ConnectionError):
            self.note(protocol, "disconnected")
        except TimeoutError:
            self.note(protocol, "timeout")
        except Exception:
            # Do not log protocol payloads, codes, file names or camera bytes.
            self.note(protocol, "protocol_error")
        finally:
            self.writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2)

    def note(self, protocol: str, phase: str) -> None:
        previous = self.diagnostics.get(protocol, {})
        self.diagnostics[protocol] = {
            "phase": phase,
            "updated": int(time.time()),
            "tls_connections": previous.get("tls_connections", 0) + (phase == "tls_connected"),
            "tcp_connections": previous.get("tcp_connections", 0) + (phase == "tcp_connected"),
            "auth_failures": previous.get("auth_failures", 0) + (phase == "access_code_rejected"),
        }

    async def detect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Answer Orca's initial IP lookup; never proxy login or control commands.

        The native identity probe uses plaintext framed JSON on private TCP
        port 3000, before MQTT authentication. It discloses only the identity
        already advertised by discovery, without any access code or key.
        """
        async with asyncio.timeout(5):
            header = await reader.readexactly(4)
            size = struct.unpack_from("<H", header, 2)[0]
            if header[:2] != b"\xa5\xa5" or not 8 <= size <= 4096:
                raise ValueError("Invalid identity frame")
            body = await reader.readexactly(size - 4)
            if body[-2:] != b"\xa7\xa7":
                raise ValueError("Invalid identity trailer")
            request = json.loads(body[:-2])
            login = request.get("login") if isinstance(request, dict) else None
            if not isinstance(login, dict) or login.get("command") != "detect":
                raise ValueError("Only identity detection is supported")
            sequence = login.get("sequence_id", "0")
            if type(sequence) not in (str, int) or len(str(sequence)) > 64:
                raise ValueError("Invalid identity sequence")
            service = self.service()
            modules = service.native_snapshot().get("info", {}).get("module", [])
            version = next((m.get("sw_ver", "") for m in modules if m.get("name") == "ota"), "")
            response = json.dumps(
                {
                    "login": {
                        "command": "detect",
                        "sequence_id": sequence,
                        "id": service.serial,
                        "model": "C12",
                        "name": "Bridge P1S",
                        "version": version,
                        "bind": "free",
                        "connect": "lan",
                    }
                },
                separators=(",", ":"),
            ).encode()
            writer.write(
                b"\xa5\xa5" + struct.pack("<H", len(response) + 6) + response + b"\xa7\xa7"
            )
            await writer.drain()
            self.note("detect", "identity_sent")

    async def camera(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        auth = await asyncio.wait_for(reader.readexactly(80), 10)
        if struct.unpack_from("<II", auth) != (0x40, 0x3000):
            return
        user = auth[16:48].split(b"\0", 1)[0].decode("ascii")
        code = auth[48:80].split(b"\0", 1)[0].decode("ascii")
        if not await self.authenticate(user, code, writer.get_extra_info("peername")[0]):
            self.note("camera", "access_code_rejected")
            return
        self.note("camera", "authenticated")
        async with self.service().camera.subscribe() as queue:
            while True:
                self.service()  # fence changed printer certificate / deleted printer
                jpeg = await asyncio.wait_for(queue.get(), 45)
                self.note("camera", "streaming")
                writer.write(struct.pack("<IIII", len(jpeg), 0, 0, 0) + jpeg)
                await asyncio.wait_for(writer.drain(), 10)

    async def mqtt(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head, data = await asyncio.wait_for(read_packet(reader), 10)
        protocol, pos = take(data, 0)
        if head != 0x10 or protocol != b"MQTT" or data[pos] != 4:
            self.note("mqtt", "unsupported_mqtt_version")
            writer.write(packet(0x20, b"\x00\x01"))
            await writer.drain()
            return
        flags = data[pos + 1]
        _, pos = take(data, pos + 4)  # client identifier
        if flags & 4:
            _, pos = take(data, pos)
            _, pos = take(data, pos)
        if flags & 0xC0 != 0xC0:
            return
        user, pos = take(data, pos)
        code, pos = take(data, pos)
        if not await self.authenticate(
            user.decode(), code.decode(), writer.get_extra_info("peername")[0]
        ):
            self.note("mqtt", "access_code_rejected")
            writer.write(packet(0x20, b"\x00\x05"))
            await writer.drain()
            return
        service = self.service()
        self.note("mqtt", "authenticated")
        serial = service.serial
        report_topic = f"device/{serial}/report".encode()
        request_topic = f"device/{serial}/request".encode()
        subscribed = False
        lock = asyncio.Lock()

        async def send(kind: int, body: bytes = b"") -> None:
            async with lock:
                writer.write(packet(kind, body))
                await asyncio.wait_for(writer.drain(), 10)

        async def report(payload: dict[str, Any]) -> None:
            value = rewrite_address(payload, service.ip, self.host)
            await send(
                0x30, field(report_topic) + json.dumps(value, separators=(",", ":")).encode()
            )

        async with service.raw_bus.subscribe() as reports:

            async def forward() -> None:
                async for event in reports:
                    self.service()
                    if subscribed:
                        await report(event.data)

            forwarding = asyncio.create_task(forward())
            try:
                await send(0x20, b"\x00\x00")
                while True:
                    head, data = await asyncio.wait_for(read_packet(reader), 120)
                    self.service()
                    kind = head >> 4
                    if kind == 8:  # subscribe
                        pos, grants = 2, bytearray()
                        while pos < len(data):
                            topic, pos = take(data, pos)
                            allowed = topic == report_topic and data[pos] <= 1
                            pos += 1
                            grants.append(0 if allowed else 128)
                            subscribed |= allowed
                        await send(0x90, data[:2] + grants)
                        if subscribed:
                            await report(service.native_snapshot())
                    elif kind == 3:  # publish
                        topic, pos = take(data, 0)
                        qos = (head >> 1) & 3
                        mid = data[pos : pos + 2] if qos else b""
                        pos += 2 if qos else 0
                        if topic != request_topic or qos > 1 or head & 1:
                            return
                        payload = json.loads(data[pos:])
                        if not isinstance(payload, dict):
                            return
                        await service.send_raw(payload)
                        if qos:
                            await send(0x40, mid)
                    elif kind == 10:
                        subscribed = False
                        await send(0xB0, data[:2])
                    elif kind == 12:
                        await send(0xD0)
                    elif kind == 14:
                        return
                    else:
                        return
            finally:
                forwarding.cancel()
                await asyncio.gather(forwarding, return_exceptions=True)
