# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Bridge Library"
# description = "Browse archived print artifacts and deliver queued captures."
# author = "Bambu Bridge"
# version = "0.1.0"
# ///
"""Stock-Orca page adapter and restart-safe archive queue.

This client is shared with the desktop companion. It never calls a printer
command. The queue accepts only explicit artifact paths supplied by a capture
adapter; it never scans user directories or Orca backups.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

CHUNK = 4 * 1024 * 1024
FILE_LIMIT = 512 * 1024 * 1024
CAPTURE_LIMIT = 2 * 1024**3


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as output:
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    if os.name == "posix":
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        # In particular, never forward a slicer credential to another host.
        return None


class Bridge:
    def __init__(self, origin: str, printer: str, token: str):
        url = urlsplit(origin)
        if (
            url.scheme != "https"
            or not url.hostname
            or not url.hostname.endswith(".ts.net")
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
            or url.port not in (None, 443)
        ):
            raise ValueError("Use the bridge's HTTPS Tailscale DNS address")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", printer):
            raise ValueError("Invalid printer identifier")
        if not token.startswith("bbs_") or not 20 <= len(token) <= 128:
            raise ValueError("Use a scoped Orca upload key")
        self.base = origin.rstrip("/") + "/orca/" + quote(printer, safe="") + "/library"
        self.token = token
        self.opener = build_opener(NoRedirect())

    def request(self, method: str, path: str, body: Any = None) -> Any:
        if not path.startswith("/captures") or ".." in path:
            raise ValueError("Invalid library resource")
        payload = (
            body
            if isinstance(body, bytes)
            else json.dumps(body).encode()
            if body is not None
            else None
        )
        headers = {
            "X-Api-Key": self.token,
            "Content-Type": "application/octet-stream"
            if isinstance(body, bytes)
            else "application/json",
        }
        request = Request(self.base + path, data=payload, headers=headers, method=method)
        with self.opener.open(request, timeout=20) as response:
            data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("Library response exceeds limit")
            return json.loads(data)


class Outbox:
    def __init__(self, root: Path, quota: int = 4 * 1024**3):
        self.root, self.quota = root, quota
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.Lock()

    def enqueue(
        self,
        title: str,
        slicer: str,
        plate: int,
        sources: list[tuple[str, Path]],
        originals: str = "disabled",
        capture_id: str | None = None,
    ) -> str:
        """Freeze explicit files now; future retries need none of the source paths."""
        with self.lock:
            if not sources or len(sources) > 64:
                raise ValueError("Declare between one and 64 artifacts")
            sizes = [path.stat().st_size for _, path in sources]
            used = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
            if (
                any(n < 1 or n > FILE_LIMIT for n in sizes)
                or sum(sizes) > CAPTURE_LIMIT
                or used + sum(sizes) > self.quota
            ):
                raise ValueError("Local archive queue quota exceeded")
            cid = capture_id or uuid.uuid4().hex
            if not re.fullmatch(r"[a-f0-9]{32}", cid):
                raise ValueError("Invalid capture ID")
            if (self.root / cid).exists():
                raise FileExistsError("Capture already exists")
            stage = self.root / (cid + ".pending")
            stage.mkdir(mode=0o700)
            artifacts = []
            names: set[str] = set()
            try:
                for index, (role, source) in enumerate(sources):
                    if role not in {"slice", "project", "original", "preview"}:
                        raise ValueError("Unknown artifact role")
                    name = source.name
                    while name in names:
                        name = str(index) + "-" + name
                    names.add(name)
                    before = source.stat()
                    destination = stage / str(index)
                    digest = hashlib.sha256()
                    count = 0
                    with source.open("rb") as src, destination.open("xb") as dst:
                        while data := src.read(CHUNK):
                            count += len(data)
                            if count > sizes[index]:
                                raise ValueError("Source changed during capture")
                            digest.update(data)
                            dst.write(data)
                        dst.flush()
                        os.fsync(dst.fileno())
                    after = source.stat()
                    if (before.st_mtime_ns, before.st_size) != (
                        after.st_mtime_ns,
                        after.st_size,
                    ) or count != sizes[index]:
                        raise ValueError("Source changed during capture")
                    artifacts.append(
                        {"name": name, "role": role, "size": count, "sha256": digest.hexdigest()}
                    )
                manifest = {
                    "schema_version": 1,
                    "id": cid,
                    "title": title,
                    "slicer_version": slicer,
                    "plate": plate,
                    "originals": originals,
                    "artifacts": artifacts,
                }
                atomic_json(stage / "manifest.json", manifest)
                stage.rename(self.root / cid)
                return cid
            except Exception:
                # Only files created in this newly minted staging directory.
                if (
                    not stage.resolve().is_relative_to(self.root.resolve())
                    or stage.resolve() == self.root.resolve()
                ):
                    raise ValueError("Staging path escaped the archive queue") from None
                shutil.rmtree(stage)
                raise

    def pending(self) -> list[str]:
        return sorted(
            p.name
            for p in self.root.iterdir()
            if p.is_dir()
            and re.fullmatch(r"[a-f0-9]{32}", p.name)
            and not self.delivered(p)
        )

    @staticmethod
    def delivered(root: Path) -> bool:
        try:
            receipt = json.loads((root / "delivered.json").read_text(encoding="utf-8"))
            return receipt.get("id") == root.name and isinstance(
                receipt.get("finalized"), int | float
            )
        except (OSError, ValueError, AttributeError):
            return False

    def deliver(self, cid: str, bridge: Any) -> dict[str, Any]:
        with self.lock:
            if not re.fullmatch(r"[a-f0-9]{32}", cid):
                raise ValueError("Invalid capture ID")
            root = self.root / cid
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            if manifest.get("id") != cid:
                raise ValueError("Queued capture identity does not match its manifest")
            for index, artifact in enumerate(manifest["artifacts"]):
                with (root / str(index)).open("rb") as source:
                    if hashlib.file_digest(source, "sha256").hexdigest() != artifact["sha256"]:
                        raise ValueError("Queued artifact is damaged")
            path = "/captures/" + cid
            status = bridge.request("POST", "/captures", manifest)
            if status["state"] != "stored":
                for index, artifact in enumerate(manifest["artifacts"]):
                    upload = status["uploads"][artifact["name"]]
                    if upload["stored"]:
                        continue
                    offset = upload["offset"]
                    if not isinstance(offset, int) or not 0 <= offset <= artifact["size"]:
                        raise ValueError("Bridge returned an invalid upload offset")
                    with (root / str(index)).open("rb") as source:
                        source.seek(offset)
                        while data := source.read(CHUNK):
                            name = quote(artifact["name"], safe="")
                            status = bridge.request(
                                "PUT", path + "/files/" + name + "?offset=" + str(offset), data
                            )
                            offset += len(data)
                status = bridge.request("POST", path + "/finalize")
            if status["state"] != "stored":
                raise ValueError("Bridge has not finalized the capture")
            atomic_json(root / "delivered.json", {"id": cid, "finalized": status["finalized"]})
            return status


PAGE = """<!doctype html><meta charset="utf-8"><main style="max-width:900px;margin:24px auto">
<h1>Print library</h1><p>Browse files saved on your bridge.</p>
<p id="status" role="status">Configure your bridge in this plugin's Config tab, then refresh.</p>
<button id="refresh">Refresh library</button><button id="retry">Retry queued uploads</button>
<div id="rows"></div></main><script>
document.getElementById('refresh').onclick=()=>orca.postMessage({action:'list'});
document.getElementById('retry').onclick=()=>orca.postMessage({action:'retry'});
orca.onMessage(data=>{
 document.getElementById('status').textContent=data.message||'';
 if(!data.rows)return;
 const root=document.getElementById('rows');root.replaceChildren();
 for(const row of data.rows){const section=document.createElement('section');
 const title=document.createElement('h2');title.textContent=row.title;section.append(title);
 const note=document.createElement('p');
note.textContent=row.state+' · Plate '+row.plate+' · Originals: '+row.originals;
section.append(note);

 for(const a of row.artifacts){const p=document.createElement('p');
p.textContent=a.role+' — '+a.name;
section.append(p);
}
 root.append(section);}
});</script>"""


def register(orca: Any) -> None:
    class LibraryPage(orca.pages.PagesPluginCapabilityBase):
        def __init__(self) -> None:
            super().__init__()
            self.worker: threading.Thread | None = None

        def get_name(self) -> str:
            return "Bridge Library"

        def get_default_config(self) -> dict[str, str]:
            return {"url": "", "printer_id": "", "upload_key": ""}

        def get_ui(self) -> str:
            return PAGE

        def on_message(self, message: Any) -> None:
            data = json.loads(message) if isinstance(message, str) else message
            if not isinstance(data, dict) or data.get("action") not in {"list", "retry"}:
                return
            if self.worker and self.worker.is_alive():
                return
            config = json.loads(self.get_config())
            root = Path(orca.host.plugin.storage()) / "outbox"

            def work() -> None:
                try:
                    bridge = Bridge(
                        config.get("url", ""),
                        config.get("printer_id", ""),
                        config.get("upload_key", ""),
                    )
                    if data["action"] == "retry":
                        outbox = Outbox(root)
                        for cid in outbox.pending():
                            outbox.deliver(cid, bridge)
                    rows = bridge.request("GET", "/captures")
                    self.post_message(
                        {
                            "rows": rows,
                            "message": ("Archived files. Automatic capture is unavailable "
                                        "in this Orca version."),
                        }
                    )
                except HTTPError as exc:
                    self.post_message(
                        {
                            "message": "Bridge request failed (HTTP "
                            + str(exc.code)
                            + "). Uploads remain queued."
                        }
                    )
                except Exception as exc:
                    self.post_message(
                        {
                            "message": "Library unavailable: "
                            + type(exc).__name__
                            + ". Check plugin configuration."
                        }
                    )

            self.worker = threading.Thread(target=work, daemon=True)
            self.worker.start()

    @orca.plugin
    class LibraryPlugin(orca.base):
        def register_capabilities(self) -> None:
            orca.register_capability(LibraryPage)


try:
    import orca
except ModuleNotFoundError as exc:
    if exc.name != "orca":
        raise
else:
    register(orca)
