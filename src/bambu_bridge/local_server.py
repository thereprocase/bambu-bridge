"""One bridge lifespan, encrypted local listener, optional legacy HTTP listener."""

from __future__ import annotations

import argparse
import asyncio
import html
import io
import os
import signal
import socket
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import qrcode
import qrcode.image.svg
import uvicorn

from bambu_bridge.config import Settings
from bambu_bridge.pairing import PairingStore, identity, invitation_payload


def local_url(settings: Settings) -> str:
    # UDP route lookup sends no packets. --url supports multihomed hosts.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("192.0.2.1", 9))
        host = sock.getsockname()[0]
    return f"https://{host}:{settings.bridge_https_port}/api/v1"


def pairing_page(store: PairingStore, base: str, output: Path) -> str:
    payload = invitation_payload(store, base)
    qr = qrcode.make(payload, image_factory=qrcode.image.svg.SvgPathImage)
    svg = io.BytesIO()
    qr.save(svg)
    page = (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<meta name=referrer content=no-referrer><title>Pair Bambu Bridge</title>"
        "<style>body{font:18px system-ui;background:#e9f2eb;color:#17271c;"
        "max-width:640px;margin:40px auto;padding:24px}svg{width:100%;height:auto;"
        "background:white}details{overflow-wrap:anywhere}h1{font-size:2rem}</style>"
        "<h1>Connect your phone</h1><p>In Bambu Bridge for Android, tap "
        "<b>Pair with QR code</b> and scan below.</p><p>This code expires in "
        "10 minutes and works once. Keep this page private.</p>"
        + svg.getvalue().decode().split("?>")[-1]
        + "<details><summary>Use a pairing code instead</summary><pre style='white-space:"
        "pre-wrap'>" + html.escape(payload) + "</pre></details></html>"
    )
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(page)
    return payload


class Listener(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield  # supervisor owns shutdown for BOTH listeners


async def serve(settings: Settings) -> None:
    from bambu_bridge.main import create_app

    assert settings.bridge_pairing_dir
    key, cert, _ = identity(Path(settings.bridge_pairing_dir))
    app = create_app(settings)
    configs: list[dict[str, Any]] = [
        {"port": settings.bridge_https_port, "ssl_keyfile": str(key), "ssl_certfile": str(cert)}
    ]
    if settings.bridge_http_enabled:
        if settings.bridge_port == settings.bridge_https_port:
            raise ValueError("HTTP and HTTPS ports must differ")
        configs.append({"port": settings.bridge_port})
    servers = [
        Listener(
            uvicorn.Config(
                app,
                host=settings.bridge_host,
                lifespan="off",
                proxy_headers=True,
                forwarded_allow_ips=settings.bridge_trusted_proxies,
                **config,
            )
        )
        for config in configs
    ]
    loop = asyncio.get_running_loop()

    def stop() -> None:
        for server in servers:
            server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop))
    async with app.router.lifespan_context(app):
        tasks = [asyncio.create_task(server.serve()) for server in servers]
        renewal = asyncio.create_task(
            renew_certificate(servers[0], Path(settings.bridge_pairing_dir))
        )
        try:
            done, _ = await asyncio.wait([*tasks, renewal], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
            stop()
            await asyncio.gather(*tasks, return_exceptions=True)


async def renew_certificate(server: Listener, directory: Path) -> None:
    # Uvicorn keeps its SSLContext. Reload the renewed leaf into that same
    # context so long-running bridges need no restart and paired keys persist.
    while True:
        await asyncio.sleep(6 * 3600)
        key, cert, _ = await asyncio.to_thread(identity, directory)
        if server.config.ssl:
            server.config.ssl.load_cert_chain(str(cert), str(key))


def cli() -> None:
    parser = argparse.ArgumentParser(description="Bambu Bridge with secure local pairing")
    parser.add_argument(
        "--env-file", type=Path, help="Read the same environment file as the service"
    )
    sub = parser.add_subparsers(dest="command")
    pair = sub.add_parser("pair", help="Create a private, single-use pairing QR page")
    pair.add_argument("--url", help="HTTPS URL ending in /api/v1; default: local route IP")
    pair.add_argument("--output", required=True, type=Path, help="New private HTML file")
    pair.add_argument(
        "--terminal", action="store_true", help="Also show the QR in an interactive terminal"
    )
    sub.add_parser("devices", help="List paired devices, without credentials")
    revoke = sub.add_parser("revoke", help="Revoke one paired device")
    revoke.add_argument("device_id")
    args = parser.parse_args()
    # pydantic-settings supports _env_file; its generated mypy signature omits it.
    settings = (
        Settings(_env_file=args.env_file) if args.env_file else Settings()  # type: ignore[call-arg]
    )
    if not settings.bridge_pairing_dir:
        settings.bridge_pairing_dir = str(Path(settings.bridge_db_path).parent / "pairing")
    if args.command:
        store = PairingStore(settings.bridge_pairing_dir)
        if args.command == "pair":
            if args.terminal and not sys.stdout.isatty():
                parser.error(
                    "--terminal requires an interactive terminal; use the private HTML file"
                )
            payload = pairing_page(store, args.url or local_url(settings), args.output)
            if args.terminal:
                code = qrcode.QRCode()
                code.add_data(payload)
                code.print_ascii(invert=True)
            print(f"Open this private file on the bridge host: {args.output.resolve()}")
        elif args.command == "devices":
            for device in store.devices():
                print(device["id"], device["name"], "revoked" if device["revoked"] else "active")
        elif args.command == "revoke":
            if not store.revoke(args.device_id):
                parser.error("Active device not found")
            print("Device revoked")
    else:
        asyncio.run(serve(settings))
