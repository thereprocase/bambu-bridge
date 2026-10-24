"""Validate the frozen record a print attempt is counted against."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from bambu_bridge.gcode_usage import PARSER_VERSION

FIELDS = frozenset(
    {
        "artifact_sha256",
        "plate",
        "parser_version",
        "provider_id",
        "assignment_revision",
        "sliced_settings",
        "planned_grams",
        "analysis_issues",
        "layer_grams",
        "tool_wires",
        "wire_spools",
        "tray_materials",
        "slot_identities",
    }
)


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _slot_map(mapping: Any) -> bool:
    return isinstance(mapping, dict) and all(
        isinstance(k, str) and k.isdigit() and type(v) is int and v >= 0 for k, v in mapping.items()
    )


def encode_manifest(value: dict[str, Any]) -> str:
    """Canonical JSON for storage; ValueError names the first field that is wrong."""
    if set(value) != FIELDS:
        raise ValueError("ACCOUNTING_MANIFEST_FIELDS")
    if not isinstance(value["artifact_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", value["artifact_sha256"]
    ):
        raise ValueError("ACCOUNTING_ARTIFACT_DIGEST")
    for key in ("plate", "assignment_revision"):
        if type(value[key]) is not int or value[key] < (1 if key == "plate" else 0):
            raise ValueError("ACCOUNTING_MANIFEST_NUMBER")
    if value["parser_version"] != PARSER_VERSION:
        raise ValueError("ACCOUNTING_PARSER_VERSION")
    if not isinstance(value["provider_id"], str) or not value["provider_id"]:
        raise ValueError("ACCOUNTING_PROVIDER_REQUIRED")
    if not isinstance(value["sliced_settings"], dict):
        raise ValueError("ACCOUNTING_CONTEXT_REQUIRED")
    plan = value["layer_grams"]
    if not isinstance(plan, list) or any(
        not isinstance(row, dict)
        or any(not isinstance(k, str) or not k.isdigit() or not _finite(g) for k, g in row.items())
        for row in plan
    ):
        raise ValueError("ACCOUNTING_LAYER_GRAMS")
    if not _slot_map(value["tool_wires"]) or not _slot_map(value["wire_spools"]):
        raise ValueError("ACCOUNTING_SLOT_MAP")
    trays = value["tray_materials"]
    if not isinstance(trays, dict) or any(
        not isinstance(k, str)
        or not k.isdigit()
        or not isinstance(v, dict)
        or set(v) != {"material", "color"}
        or any(x is not None and not isinstance(x, str) for x in v.values())
        for k, v in trays.items()
    ):
        raise ValueError("ACCOUNTING_TRAY_MATERIALS")
    identities = value["slot_identities"]
    if not isinstance(identities, dict) or any(
        not isinstance(k, str) or not k.isdigit() or not isinstance(v, str)
        for k, v in identities.items()
    ):
        raise ValueError("ACCOUNTING_SLOT_IDENTITIES")
    if not _finite(value["planned_grams"]) or value["planned_grams"] < 0:
        raise ValueError("ACCOUNTING_PLANNED_MASS")
    if not isinstance(value["analysis_issues"], list) or any(
        not isinstance(issue, str) for issue in value["analysis_issues"]
    ):
        raise ValueError("ACCOUNTING_ANALYSIS_ISSUES")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    # A layer plan is about 20 bytes per layer per filament.
    if len(encoded.encode()) > 4 * 1024 * 1024:
        raise ValueError("ACCOUNTING_MANIFEST_TOO_LARGE")
    return encoded
