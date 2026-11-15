"""Idempotency keys for command requests.

A phone that loses the response to a command cannot tell whether the bridge
received it. Without a key, sending the command again could run it twice, so
clients stop and report an unknown outcome. With an ``Idempotency-Key`` header
the bridge runs the command once and answers a repeat with the stored response,
which makes a retry over the same or another network path safe.

Scope and limits:

* Applies to POST, PUT, PATCH and DELETE that carry the header. Requests
  without it behave exactly as before.
* A key is bound to the caller's credential, the method and the full request
  target. The request body is not compared: a client generates one random key
  per command and reuses it only to repeat that command.
* A repeat that arrives while the first request is still running waits for it
  and receives the same response.
* Stored responses are kept in memory for ``TTL_S`` seconds, at most
  ``MAX_ENTRIES`` of them, and only up to ``MAX_BODY`` bytes each. A larger
  response is not stored; its repeat receives a 409 that says the command was
  already received. The same applies to a command whose handler raised.
* Every HTTP response carries ``X-Bridge-Features: idempotency-key`` so a
  client knows a retry is safe before it needs one.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from bambu_bridge.api import errors as api_errors

TTL_S = 300.0
MAX_ENTRIES = 256
MAX_BODY = 64 * 1024
FEATURE_HEADER = (b"x-bridge-features", b"idempotency-key")
REPLAY_HEADER = (b"idempotency-replayed", b"true")
_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_KEY = re.compile(rb"[A-Za-z0-9_-]{16,64}")


@dataclass
class _Entry:
    done: asyncio.Event = field(default_factory=asyncio.Event)
    finished_at: float | None = None
    # None after completion means the response was too large to keep.
    start: dict[str, Any] | None = None
    body: bytes = b""


class IdempotencyMiddleware:
    """Pure ASGI middleware; see the module docstring for the contract."""

    def __init__(self, app: Any, *, clock: Any = time.monotonic) -> None:
        self.app = app
        self._clock = clock
        self._entries: OrderedDict[bytes, _Entry] = OrderedDict()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def tagged(message: Any) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), FEATURE_HEADER]}
            await send(message)

        headers = dict(scope.get("headers") or [])
        key = headers.get(b"idempotency-key")
        if key is None or scope["method"] not in _METHODS:
            await self.app(scope, receive, tagged)
            return
        if not _KEY.fullmatch(key):
            response = api_errors.invalid_input(
                "Idempotency-Key must be 16 to 64 letters, digits, '-' or '_'."
            )
            await response(scope, receive, tagged)
            return

        digest = hashlib.sha256()
        for part in (
            headers.get(b"authorization", b""),
            scope["method"].encode(),
            scope.get("raw_path") or scope["path"].encode(),
            scope.get("query_string", b""),
            key,
        ):
            digest.update(len(part).to_bytes(4, "big"))
            digest.update(part)
        slot = digest.digest()

        self._prune()
        entry = self._entries.get(slot)
        if entry is not None:
            await entry.done.wait()
            await self._replay(entry, scope, receive, tagged)
            return

        entry = self._entries[slot] = _Entry()
        start: dict[str, Any] | None = None
        chunks: list[bytes] = []
        size = 0
        keep = True

        async def capture(message: Any) -> None:
            nonlocal start, size, keep
            if message["type"] == "http.response.start":
                start = {"status": message["status"], "headers": list(message.get("headers", []))}
            elif message["type"] == "http.response.body" and keep:
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > MAX_BODY:
                    keep = False
                    chunks.clear()
                else:
                    chunks.append(chunk)
            await tagged(message)

        try:
            await self.app(scope, receive, capture)
        except BaseException:
            # The handler failed part-way and may already have sent something
            # to the printer. Keep the key with nothing stored, so a repeat is
            # answered with a 409 and the command is never run a second time.
            entry.finished_at = self._clock()
            entry.done.set()
            raise
        if start is not None and keep:
            entry.start = start
            entry.body = b"".join(chunks)
        entry.finished_at = self._clock()
        entry.done.set()

    async def _replay(self, entry: _Entry, scope: Any, receive: Any, send: Any) -> None:
        if entry.start is None:
            response = api_errors.conflict(
                "This command was already received. "
                "Check the printer status before sending it again."
            )
            await response(scope, receive, send)
            return
        await send({
            "type": "http.response.start",
            "status": entry.start["status"],
            "headers": [*entry.start["headers"], REPLAY_HEADER],
        })
        await send({"type": "http.response.body", "body": entry.body})

    def _prune(self) -> None:
        now = self._clock()
        for slot, entry in list(self._entries.items()):
            if entry.finished_at is not None and now - entry.finished_at > TTL_S:
                del self._entries[slot]
        # Oldest finished entries go first; a running command is never dropped.
        for slot, entry in list(self._entries.items()):
            if len(self._entries) < MAX_ENTRIES:
                break
            if entry.finished_at is not None:
                del self._entries[slot]
