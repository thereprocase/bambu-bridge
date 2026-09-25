"""The documented OctoPrint upload subset, backed by local archive custody."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from library_plugin import FILE_LIMIT
from starlette.datastructures import UploadFile
from store import Custody

BODY_LIMIT = FILE_LIMIT + 64 * 1024


def tailnet_origin(value: str) -> str:
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or not url.hostname.endswith(".ts.net")
        or url.username
        or url.password
        or url.port not in (None, 443)
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        raise ValueError("Use an HTTPS Tailscale DNS address without a path")
    return "https://" + url.hostname


class UploadLimit:
    """Bound multipart bytes, including chunked bodies, before parsing/spooling."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        count = 0

        async def bounded_receive() -> Any:
            nonlocal count
            message = await receive()
            if message["type"] == "http.request":
                count += len(message.get("body", b""))
                if count > BODY_LIMIT:
                    raise HTTPException(413, "Upload exceeds the 512 MiB file limit")
            return message

        await self.app(scope, bounded_receive, send)


def create_app(custody: Custody, public_origin: str, token: str) -> FastAPI:
    origin = tailnet_origin(public_origin)
    if not token.startswith("bcs_") or len(token) < 40:
        raise ValueError("Invalid companion upload credential")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(UploadLimit)
    upload_lock = asyncio.Lock()

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        from starlette.responses import JSONResponse

        # Uvicorn binds only loopback. Tailscale Serve terminates TLS and forwards
        # the original Host/proto; forwarding from any other peer is forbidden.
        direct_tls = request.url.scheme == "https"
        local_proxy = (
            request.client is not None
            and request.client.host in {"127.0.0.1", "::1"}
            and request.headers.get("x-forwarded-proto") == "https"
        )
        if request.headers.get("host", "").lower() not in {
            urlsplit(origin).netloc,
            urlsplit(origin).netloc + ":443",
        } or not (direct_tls or local_proxy):
            return JSONResponse(
                {"error": "Connect through the configured HTTPS Tailscale address"}, status_code=403
            )
        if request.headers.get("origin") not in (None, origin):
            return JSONResponse(
                {"error": "Cross-origin requests are not accepted"}, status_code=403
            )
        if request.query_params or not secrets.compare_digest(
            request.headers.get("x-api-key", ""), token
        ):
            return JSONResponse({"error": "A companion upload key is required"}, status_code=401)
        try:
            size = int(request.headers.get("content-length", "0"))
        except ValueError:
            size = -1
        if size < 0 or size > BODY_LIMIT:
            return JSONResponse({"error": "Upload exceeds the size limit"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/version")
    async def version() -> dict[str, str]:
        return {
            "api": "0.1",
            "server": "0.1.0",
            "text": "OctoPrint compatible Bridge Library Companion",
        }

    @app.post("/api/files/local", status_code=201)
    async def upload(request: Request) -> dict[str, Any]:
        # One in-flight body prevents parallel requests filling the OS spool.
        if upload_lock.locked():
            raise HTTPException(409, "An upload is being received; retry after it finishes")
        async with upload_lock, request.form(max_files=1, max_fields=8) as form:
            if any(len(form.getlist(key)) != 1 for key in form):
                raise HTTPException(422, "Duplicate upload fields are not supported")
            if any(key not in {"file", "print", "select", "path", "plateindex"} for key in form):
                raise HTTPException(422, "Unsupported upload field")
            requested_print = form.get("print", "false")
            if not isinstance(requested_print, str) or requested_print.lower() not in {
                "false",
                "0",
            }:
                raise HTTPException(
                    409,
                    "This companion receives archives only. Choose Upload, then review "
                    "the saved job in the bridge. No print was started.",
                )
            if form.get("path", "") not in ("", "/"):
                raise HTTPException(422, "Leave the upload folder empty")
            file = form.get("file")
            if not isinstance(file, UploadFile):
                raise HTTPException(422, "Supply one sliced .gcode.3mf file")
            try:
                plate = int(str(form.get("plateindex", "1")))
                row = await asyncio.to_thread(
                    custody.receive, file.filename or "", plate, file.file
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            except OSError as exc:
                raise HTTPException(
                    507, "Local storage is unavailable; this upload was not accepted"
                ) from exc
        return {
            "files": {
                "local": {
                    "name": row["name"],
                    "path": row["id"] + "/" + row["name"],
                    "origin": "local",
                }
            },
            "done": True,
            "effectivePrint": False,
            "companion": {
                "receipt": row["id"],
                "sha256": row["sha256"],
                "state": "awaiting_review",
            },
        }

    return app
