"""Opt-in packaged-app smoke test. Only a synthetic loopback upload is sent."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from library_plugin import atomic_json


def run(desktop: Any, report: Path) -> None:
    deadline = time.monotonic() + 10
    while not desktop.server.started and time.monotonic() < deadline:
        desktop.root.update()
        time.sleep(0.025)
    if not desktop.server.started:
        raise RuntimeError("Packaged receiver failed to start")
    port = desktop.server.servers[0].sockets[0].getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    headers = {
        "Host": "smoke.example.ts.net",
        "X-Forwarded-Proto": "https",
        "X-Api-Key": desktop.receiver_key,
    }
    opener = build_opener(ProxyHandler({}))
    with opener.open(Request(origin + "/api/version", headers=headers), timeout=5) as response:
        version = json.load(response)
    if not version["text"].startswith("OctoPrint"):
        raise RuntimeError("Unexpected OctoPrint version response")
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(zipfile.ZipInfo("Metadata/plate_1.gcode"), "G1 X1 Y1 E0.1\n")
    payload = data.getvalue()
    boundary = "companion-diagnostic-boundary"

    def upload(print_now: bool) -> None:
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="print"\r\n\r\n'
                f"{str(print_now).lower()}\r\n--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; filename="synthetic.gcode.3mf"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + payload
            + f"\r\n--{boundary}--\r\n".encode()
        )
        request = Request(
            origin + "/api/files/local",
            data=body,
            headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with opener.open(request, timeout=5) as response:
            receipt = json.load(response)
        if receipt["effectivePrint"] is not False:
            raise RuntimeError("An archive response incorrectly asserted printing")

    upload(False)
    try:
        upload(True)
    except HTTPError as exc:
        if exc.code != 409:
            raise
    else:
        raise RuntimeError("Upload and print was not refused")
    desktop.refresh()
    desktop.root.update_idletasks()
    rows = desktop.custody.rows("inbox")
    if len(rows) != 1 or rows[0]["sha256"] != hashlib.sha256(payload).hexdigest():
        raise RuntimeError("Packaged receiver did not preserve exact bytes")
    if len(desktop.uploads.get_children()) != 1:
        raise RuntimeError("Received upload did not appear in the desktop interface")
    cid = desktop.custody.freeze(rows[0]["id"], "Packaged diagnostic", "synthetic", [])
    # Check queue/client imports inside the frozen build, without any remote call.
    if desktop.custody.outbox.pending() != [cid]:
        raise RuntimeError("Packaged archive queue failed")
    atomic_json(
        report,
        {
            "passed": True,
            "frozen_executable": bool(getattr(sys, "frozen", False)),
            "desktop_created": True,
            "receiver": "loopback",
            "stock_orca_request_shape": True,
            "slice_sha256_verified": True,
            "upload_and_print_refused": True,
            "durable_outbox": True,
            "remote_requests": 0,
            "printer_commands": 0,
            "requested_window": [desktop.root.winfo_reqwidth(), desktop.root.winfo_reqheight()],
        },
    )
