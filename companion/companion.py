"""Bridge Library desktop companion; stock Orca remains the slicer."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
from pathlib import Path
from typing import Any

# The packaged application includes the exact same archive client as the Orca
# plugin. Source-checkout execution uses it in place; there is no second client.
_shared = Path(__file__).resolve().parents[1] / "plugins" / "bridge_library"
if _shared.is_dir():
    sys.path.insert(0, str(_shared))

from library_plugin import Bridge, atomic_json  # noqa: E402
from receiver import create_app, tailnet_origin  # noqa: E402
from store import Custody, InstanceLock  # noqa: E402


class Desktop:
    def __init__(self, root: Any, custody: Custody, public_origin: str, port: int):
        import tkinter as tk
        from tkinter import ttk

        self.root, self.custody = root, custody
        self.worker: threading.Thread | None = None
        self.server: Any = None
        self.server_thread: threading.Thread | None = None
        self.results: list[tuple[bool, str]] = []
        self.results_lock = threading.Lock()
        self.input_ids: list[str] = []
        root.title("Bridge Library Companion")
        root.geometry("1000x780")
        root.minsize(820, 680)
        body = ttk.Frame(root, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Bridge Library", font=("Segoe UI", 22, "bold")).pack(anchor="w")
        ttk.Label(
            body,
            text="Keep the original files, the saved Orca project and the exact slice together.",
        ).pack(anchor="w", pady=(0, 14))
        connection = ttk.LabelFrame(body, text="Your bridge", padding=10)
        connection.pack(fill="x")
        self.bridge_url = tk.StringVar(value=os.environ.get("COMPANION_BRIDGE_URL", ""))
        self.printer = tk.StringVar(value=os.environ.get("COMPANION_PRINTER_ID", ""))
        self.key = tk.StringVar(value=os.environ.get("COMPANION_UPLOAD_KEY", ""))
        for index, (caption, value) in enumerate(
            (
                ("HTTPS Tailscale address", self.bridge_url),
                ("Printer ID", self.printer),
                ("Scoped upload key", self.key),
            )
        ):
            ttk.Label(connection, text=caption).grid(row=0, column=index, sticky="w")
            ttk.Entry(connection, textvariable=value, show="•" if value is self.key else "").grid(
                row=1, column=index, sticky="ew", padx=(0, 8)
            )
            connection.columnconfigure(index, weight=2 if index == 0 else 1)

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=12)
        ttk.Button(actions, text="Add original files…", command=lambda: self.pick("original")).pack(
            side="left"
        )
        ttk.Button(
            actions, text="Add saved Orca project…", command=lambda: self.pick("project")
        ).pack(side="left", padx=8)
        ttk.Button(actions, text="Add sliced file…", command=self.pick_slice).pack(side="left")
        ttk.Label(
            body,
            text=(
                "Choose the saved inputs for this upload. Project selection preserves its saved "
                "bytes; it cannot capture unsaved Orca edits."
            ),
            wraplength=900,
        ).pack(anchor="w")
        self.inputs = tk.Listbox(body, selectmode="extended", exportselection=False, height=5)
        self.inputs.pack(fill="x", pady=6)

        ttk.Label(body, text="Received slices", font=("Segoe UI", 12, "bold")).pack(
            anchor="w", pady=(8, 0)
        )
        self.uploads = ttk.Treeview(
            body,
            columns=("name", "plate", "status"),
            show="headings",
            selectmode="browse",
            height=5,
        )
        for name, label in (
            ("name", "Exact uploaded file"),
            ("plate", "Plate"),
            ("status", "Archive state"),
        ):
            self.uploads.heading(name, text=label)
        self.uploads.column("name", width=480)
        self.uploads.column("plate", width=60)
        self.uploads.column("status", width=200)
        self.uploads.pack(fill="both", expand=True, pady=6)
        details = ttk.Frame(body)
        details.pack(fill="x")
        self.title = tk.StringVar(value="Print capture")
        self.slicer = tk.StringVar(value="OrcaSlicer (version not recorded)")
        ttk.Label(details, text="Title").grid(row=0, column=0, sticky="w")
        ttk.Entry(details, textvariable=self.title).grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ttk.Label(details, text="Slicer version").grid(row=0, column=1, sticky="w")
        ttk.Entry(details, textvariable=self.slicer).grid(row=1, column=1, sticky="ew")
        details.columnconfigure(0, weight=2)
        details.columnconfigure(1, weight=1)
        self.confirmed, self.complete = tk.BooleanVar(), tk.BooleanVar()
        ttk.Checkbutton(
            body,
            text="I confirm the selected saved inputs belong to this slice.",
            variable=self.confirmed,
        ).pack(anchor="w", pady=(8, 0))
        ttk.Checkbutton(
            body,
            text="The selected originals include every file I imported.",
            variable=self.complete,
        ).pack(anchor="w")
        self.uploads.bind("<<TreeviewSelect>>", self.selection_changed)
        self.inputs.bind("<<ListboxSelect>>", self.selection_changed)
        controls = ttk.Frame(body)
        controls.pack(fill="x", pady=10)
        ttk.Button(controls, text="Save selected capture to bridge", command=self.archive).pack(
            side="left"
        )
        ttk.Button(controls, text="Retry pending archives", command=self.retry).pack(
            side="left", padx=8
        )
        ttk.Button(controls, text="Open bridge library", command=self.open_library).pack(
            side="left"
        )
        self.status = tk.StringVar(value="Ready. Saving an archive never starts a print.")
        ttk.Label(body, textvariable=self.status, wraplength=900).pack(anchor="w", pady=4)
        self.transport = tk.StringVar(
            value="OctoPrint receiver is off. You can add a sliced file above."
        )
        ttk.Label(body, textvariable=self.transport, wraplength=900).pack(anchor="w")
        self.port, self.public_origin = port, public_origin
        if public_origin:
            ttk.Button(body, text="Copy companion key for Orca", command=self.copy_key).pack(
                anchor="w", pady=4
            )
            self.start_receiver()
        self.refresh()
        root.update_idletasks()
        root.minsize(max(820, root.winfo_reqwidth()), max(680, root.winfo_reqheight()))
        root.after(200, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.close)

    def selection_changed(self, event: Any = None) -> None:
        self.confirmed.set(False)
        self.complete.set(False)

    def background(self, task: Any) -> None:
        if self.worker and self.worker.is_alive():
            self.status.set("An operation is already running.")
            return
        self.status.set("Working…")

        def run() -> None:
            try:
                result = (True, str(task()))
            except Exception as exc:
                # Exception messages from our own validation are helpful. Network
                # URLs/headers and arbitrary server bodies are never echoed.
                result = (
                    False,
                    str(exc)
                    if isinstance(exc, ValueError | FileExistsError)
                    else (
                        f"{type(exc).__name__}: operation failed; "
                        "local files are retained for retry."
                    ),
                )
            with self.results_lock:
                self.results.append(result)

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def pick(self, role: str) -> None:
        from tkinter import filedialog

        paths = filedialog.askopenfilenames(
            title="Choose original inputs" if role == "original" else "Choose a saved Orca project",
            filetypes=[("Orca project", "*.3mf")] if role == "project" else [("All files", "*")],
        )
        if paths:

            def remember() -> str:
                for path in paths:
                    self.custody.remember(Path(path), role)
                return (
                    f"Saved {len(paths)} local snapshot(s). Select the ones for your slice below."
                )

            self.background(remember)

    def pick_slice(self) -> None:
        from tkinter import filedialog, simpledialog

        path = filedialog.askopenfilename(
            title="Choose the exact sliced file", filetypes=[("Sliced 3MF", "*.gcode.3mf")]
        )
        if not path:
            return
        plate = simpledialog.askinteger(
            "Plate", "Plate index inside the sliced file", initialvalue=1, minvalue=1, maxvalue=1000
        )
        if plate is not None:

            def receive() -> str:
                with Path(path).open("rb") as source:
                    self.custody.receive(Path(path).name, plate, source)
                return "Exact slice saved locally. Select it and its saved inputs."

            self.background(receive)

    def bridge(self) -> Bridge:
        return Bridge(self.bridge_url.get(), self.printer.get(), self.key.get())

    def archive(self) -> None:
        selection = self.uploads.selection()
        if not selection:
            self.status.set("Choose one received slice.")
            return
        try:
            bridge = self.bridge()
        except ValueError as exc:
            self.status.set(str(exc))
            return
        rid = selection[0]
        selected_inputs = self.inputs.curselection()  # type: ignore[no-untyped-call]
        inputs = [self.input_ids[i] for i in selected_inputs]
        title, slicer = self.title.get(), self.slicer.get()
        confirmed, complete = self.confirmed.get(), self.complete.get()

        def save() -> str:
            cid = self.custody.freeze(
                rid,
                title,
                slicer,
                inputs,
                association_confirmed=confirmed,
                originals_complete=complete,
            )
            self.custody.deliver(cid, bridge)
            return "Capture saved on the bridge. No print was started."

        self.background(save)

    def retry(self) -> None:
        try:
            bridge = self.bridge()
        except ValueError as exc:
            self.status.set(str(exc))
            return

        def deliver() -> str:
            pending = self.custody.outbox.pending()
            for cid in pending:
                self.custody.deliver(cid, bridge)
            return f"Saved {len(pending)} pending archive(s). No print was started."

        self.background(deliver)

    def open_library(self) -> None:
        import webbrowser

        try:
            webbrowser.open(tailnet_origin(self.bridge_url.get()) + "/app/#/library")
        except ValueError as exc:
            self.status.set(str(exc))

    def start_receiver(self) -> None:
        import uvicorn

        key_path = self.custody.root / "receiver-key.json"
        if not key_path.exists():
            atomic_json(key_path, {"token": "bcs_" + secrets.token_urlsafe(32)})
            key_path.chmod(0o600)
        self.receiver_key = json.loads(key_path.read_text(encoding="utf-8"))["token"]
        app = create_app(self.custody, self.public_origin, self.receiver_key)
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=self.port,
                proxy_headers=False,
                access_log=False,
                log_level="warning",
                log_config=None,
            )
        )
        self.server_thread = threading.Thread(target=self.server.run, daemon=True)
        self.server_thread.start()
        self.transport.set(
            f"Receiver starting on loopback port {self.port}. Orca Host: {self.public_origin} "
            "· Octo/Klipper · Upload only. HTTPS Tailscale Serve must forward to this port."
        )

    def copy_key(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.receiver_key)
        self.status.set(
            "Companion upload key copied. Use it in Orca; it grants no printer controls."
        )

    def refresh(self) -> None:
        # Never stall Tk behind a long archive upload holding the custody lock.
        if not self.custody.lock.acquire(blocking=False):
            return
        try:
            rows = self.custody.rows("inputs")
            ids = [row["id"] for row in rows]
            if ids != self.input_ids:
                self.input_ids = ids
                self.inputs.delete(0, "end")
                for row in rows:
                    self.inputs.insert(
                        "end", f"{row['role']} · {row['name']} · {row['sha256'][:12]}"
                    )
                self.confirmed.set(False)
                self.complete.set(False)
            for row in self.custody.rows("inbox"):
                cid = row.get("capture_id")
                state = (
                    "Saved on bridge"
                    if cid and self.custody.outbox.delivered(self.custody.outbox.root / cid)
                    else "Queued for bridge"
                    if cid
                    else "Choose saved inputs"
                )
                values = (row["name"], row["plate"], state)
                if self.uploads.exists(row["id"]):
                    self.uploads.item(row["id"], values=values)
                else:
                    self.uploads.insert("", 0, iid=row["id"], values=values)
        finally:
            self.custody.lock.release()

    def poll(self) -> None:
        with self.results_lock:
            results, self.results = self.results, []
        for _, message in results:
            self.status.set(message)
        self.refresh()
        if self.server_thread and not self.server_thread.is_alive():
            self.transport.set(
                "OctoPrint receiver stopped. Check that its port is available; "
                "local files are retained."
            )
        self.root.after(750, self.poll)

    def close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.status.set("Wait for the current file operation before closing.")
            return
        if self.server:
            self.server.should_exit = True
            if self.server_thread:
                self.server_thread.join(timeout=2)
                if self.server_thread.is_alive():
                    self.status.set(
                        "Waiting for the current incoming upload to finish. Close again afterward."
                    )
                    return
        self.root.destroy()


def main() -> None:
    import tkinter as tk

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument(
        "--public-origin",
        default="",
        help="Desktop HTTPS Tailscale Serve origin; enables the upload receiver",
    )
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--smoke-report", type=Path, help="Run an isolated local diagnostic and exit"
    )
    args = parser.parse_args()
    if args.data_dir is None:
        app_data = os.environ.get("LOCALAPPDATA")
        try:
            base = Path(app_data) if app_data else Path.home() / ".local" / "share"
        except RuntimeError:
            parser.error("No user data directory is available; supply --data-dir")
        args.data_dir = base / "BridgeLibraryCompanion"
    if args.public_origin:
        args.public_origin = tailnet_origin(args.public_origin)
    if not 1024 <= args.port <= 65535:
        parser.error("Choose an unprivileged loopback port")
    if args.smoke_report and args.data_dir.exists() and any(args.data_dir.iterdir()):
        parser.error("Diagnostics require a new, empty data directory")
    lock = InstanceLock(args.data_dir)
    try:
        root = tk.Tk()
        if args.smoke_report:
            from diagnostics import run

            root.withdraw()
            desktop = Desktop(root, Custody(args.data_dir), "https://smoke.example.ts.net", 0)
            try:
                run(desktop, args.smoke_report)
            finally:
                desktop.close()
        else:
            Desktop(root, Custody(args.data_dir), args.public_origin, args.port)
            root.mainloop()
    finally:
        lock.close()


if __name__ == "__main__":
    main()
