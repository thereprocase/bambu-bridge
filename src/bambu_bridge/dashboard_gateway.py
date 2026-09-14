"""Opt-in loopback-only Tailscale Serve identity gateway for the web dashboard.

Serve strips client-supplied identity headers. This listener MUST keep proxy
header processing disabled so the socket peer can be checked before trusting
those headers. Normal API listeners never use this authentication path.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class DashboardGateway:
    def __init__(self, app: ASGIApp, origin: str, login: str, owner_key: str) -> None:
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".ts.net")
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or not login
            or not owner_key
        ):
            raise ValueError("Dashboard requires an HTTPS Tailscale origin, login and owner key")
        self.app = app
        self.origin = origin.rstrip("/")
        self.host = parsed.netloc
        self.login = login
        self.owner_key = owner_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v.decode("latin-1") for k, v in scope["headers"]}
        peer = scope.get("client")
        origin = headers.get(b"origin")
        trusted = (
            peer is not None
            and peer[0] in ("127.0.0.1", "::1")
            and headers.get(b"host") == self.host
            and headers.get(b"tailscale-user-login") == self.login
            and headers.get(b"x-forwarded-proto") == "https"
            and (origin is None or origin == self.origin)
            and headers.get(b"sec-fetch-site") != "cross-site"
        )
        if scope["type"] == "websocket" or scope.get("method") not in ("GET", "HEAD", "OPTIONS"):
            trusted = trusted and origin == self.origin
        if not trusted:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse(
                    {"detail": "Use the private HTTPS dashboard with your Tailscale account"},
                    status_code=403,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
            return
        if scope["type"] == "http" and scope["path"] == "/app/session":
            await JSONResponse(
                {"connected": True, "authentication": "tailscale"},
                headers={"Cache-Control": "no-store", "Vary": "Tailscale-User-Login"},
            )(scope, receive, send)
            return
        # Copy scope: never put the real owner credential into a URL, response,
        # browser storage, or the original scope used by access logging.
        child = dict(scope)
        child["bambu.dashboard_authenticated"] = True
        child["scheme"] = "wss" if scope["type"] == "websocket" else "https"
        child["headers"] = [(k, v) for k, v in scope["headers"] if k.lower() != b"authorization"]
        child["headers"].append((b"authorization", ("Bearer " + self.owner_key).encode()))
        child["query_string"] = urlencode(
            [
                (k, v)
                for k, v in parse_qsl(
                    scope.get("query_string", b"").decode(), keep_blank_values=True
                )
                if k != "token"
            ]
        ).encode()
        await self.app(child, receive, send)
