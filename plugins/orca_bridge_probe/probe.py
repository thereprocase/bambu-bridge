# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Bridge Library Compatibility Probe"
# description = "Read-only geometry and API report; does not send or alter prints."
# author = "Bambu Bridge"
# version = "0.1.0"
# ///
"""Sprint 0 probe for Orca's development plugin API, not a print integration.

Run as a Script capability inside a plugin-enabled Orca build. The report is
saved in this plugin's own storage. No sources, profiles or credentials are read.
The CLI performs an offline inspection of an installed Windows binary only.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
from pathlib import Path
from typing import Any

MAX_OBJECTS = 1000
MAX_VOLUMES = 10000


def public_names(value: Any) -> list[str]:
    return sorted(name for name in dir(value) if not name.startswith("_"))


def vector(value: Any) -> list[float]:
    result = [float(n) for n in value]
    if len(result) != 3 or not all(math.isfinite(n) for n in result):
        raise ValueError("Invalid geometry transform")
    return result


def transforms(value: Any) -> dict[str, list[float]]:
    return {
        name: vector(getattr(value, name)())
        for name in ("offset", "rotation", "scaling_factor", "mirror")
    }


def collect(host: Any) -> dict[str, Any]:
    """Copy primitive values during one UI-thread invocation; retain no host refs."""
    plater, model = host.plater(), host.model()
    objects = model.objects()
    if len(objects) > MAX_OBJECTS:
        raise ValueError("Too many objects for the diagnostic probe")
    report: dict[str, Any] = {
        "schema": 1,
        "kind": "compatibility_probe",
        "complete_project": False,
        "selected_plate_verified": False,
        "print_hook_verified": False,
        "project_dirty": bool(plater.is_project_dirty()),
        "public_api": {"host": public_names(host), "plater": public_names(plater)},
        "objects": [],
    }
    total_volumes = 0
    for obj in objects:
        volumes = obj.volumes()
        instances = obj.instances()
        total_volumes += len(volumes)
        if total_volumes > MAX_VOLUMES or len(instances) > MAX_OBJECTS:
            raise ValueError("Too many volumes or instances for the diagnostic probe")
        # A non-empty input_file proves only that Orca remembers a path. Never
        # claim its bytes still exist or are the same bytes that were imported.
        entry = {
            "object_id": int(obj.id()),
            "source_path_recorded": bool(obj.input_file),
            "source_bytes_verified": False,
            "instances": [
                {"printable": bool(inst.printable), **transforms(inst)} for inst in instances
            ],
            "volumes": [],
        }
        for vol in volumes:
            mesh = vol.mesh()
            vertices, triangles = int(mesh.vertex_count()), int(mesh.triangle_count())
            entry["volumes"].append(
                {
                    "model_part": bool(vol.is_model_part()),
                    "negative_volume": bool(vol.is_negative_volume()),
                    "vertex_count": vertices,
                    "triangle_count": triangles,
                    "first_vertex": vector(mesh.vertex(0)) if vertices else None,
                    "first_triangle": [int(n) for n in mesh.triangle(0)] if triangles else None,
                    **transforms(vol),
                }
            )
        report["objects"].append(entry)
    return report


def inspect_binary(path: Path) -> dict[str, Any]:
    """Supporting evidence only: absent strings alone are not an API version test."""
    markers = [b"orca_plugins", b"PythonInterpreter", b"psGCodePostProcess"]
    digest = hashlib.sha256()
    found = {marker.decode(): False for marker in markers}
    tail = b""
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
            window = tail + chunk
            for marker in markers:
                found[marker.decode()] |= (
                    marker in window or marker.decode().encode("utf-16le") in window
                )
            tail = window[-128:]
    return {
        "binary_name": path.name,
        "sha256": digest.hexdigest(),
        "size": size,
        "plugin_markers": found,
        "runtime_plugin_test": "not_run",
        "note": "Corroborate markers with official release source; this is not a runtime test.",
    }


def register(orca: Any) -> None:
    def save_report() -> dict[str, Any]:
        report = collect(orca.host)
        destination = Path(orca.host.plugin.storage()) / "compatibility-report.json"
        destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report

    class Probe(orca.script.ScriptPluginCapabilityBase):
        def get_name(self) -> str:
            return "Bridge Library Compatibility Probe"

        def execute(self) -> Any:
            try:
                report = save_report()
                return orca.ExecutionResult.success(
                    "Read-only probe saved compatibility-report.json in plugin storage. "
                    "This does not prove full project export or automatic print capture.",
                    json.dumps(report),
                )
            except Exception as exc:
                # Host errors can include local filenames: do not copy exception
                # text, profile contents or access credentials into a report.
                return orca.ExecutionResult.failure(
                    orca.PluginResult.RecoverableError,
                    "Bridge probe could not inspect this model: " + type(exc).__name__,
                )

    class ProbePage(orca.pages.PagesPluginCapabilityBase):
        def get_name(self) -> str:
            return "Bridge Compatibility"

        def get_ui(self) -> str:
            # Native page construction calls get_ui on the GUI thread. Do not
            # inspect the live model from the plugin loader's background thread.
            try:
                report = save_report()
            except Exception as exc:
                report = {"status": "error", "error_type": type(exc).__name__}
            return (
                "<!doctype html><meta charset=utf-8><h1>Bridge compatibility probe</h1>"
                "<p>Read-only host inspection. Full project recovery and automatic print "
                "capture are not proven by this report.</p><pre>"
                + html.escape(json.dumps(report, indent=2))
                + "</pre>"
            )

    @orca.plugin
    class BridgeProbe(orca.base):
        def register_capabilities(self) -> None:
            orca.register_capability(Probe)
            orca.register_capability(ProbePage)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect-binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(inspect_binary(args.inspect_binary), indent=2), encoding="utf-8"
    )
else:
    try:
        import orca
    except ModuleNotFoundError as exc:
        if exc.name != "orca":
            raise
    else:
        register(orca)
