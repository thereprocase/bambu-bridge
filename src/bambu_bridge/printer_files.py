"""Which bridge job files on a printer's storage may be deleted.

Every upload the bridge delivers lands on the printer as ``beluga-<id><suffix>``
and stayed there forever: 270 of them on the owner's P1S by 2026-10-29, when
the printer began refusing to parse freshly uploaded jobs (05004003) and
refusing root listings (550). The selection here is pure so it can be tested
without a printer; the gateway lists, calls this, and deletes.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Mapping
from typing import Any

from bambu_bridge.native_inbox import TERMINAL_STATES

BRIDGE_FILE = re.compile(r"beluga-([0-9a-f]{32})\.[A-Za-z0-9.]{1,16}")


def prunable(
    listing: Iterable[str],
    rows: Iterable[Mapping[str, Any]],
    current: str | None,
    *,
    keep: int,
    min_age_s: float,
    now: float,
) -> list[str]:
    """Bridge job files in ``listing`` that no start still needs, oldest first.

    ``rows`` are this printer's inbox uploads (``id``, ``state``,
    ``start_state``, ``created``). A file is kept while any of these holds:
    it is the file the printer reports as current; its upload is still being
    delivered or its start is still owned; it is one of the newest ``keep``
    delivered uploads; it is younger than ``min_age_s``. Files with no inbox
    row at all are orphans from before the inbox's own retention and go.
    """
    by_id = {str(r["id"]): r for r in rows}
    settled = [
        r
        for r in by_id.values()
        if r.get("state") == "delivered"
        and (r.get("start_state") is None or r.get("start_state") in TERMINAL_STATES)
    ]
    settled.sort(key=lambda r: float(r.get("created") or 0), reverse=True)
    recent = {str(r["id"]) for r in settled[:keep]}
    current_name = posixpath.basename(current or "")
    doomed: list[tuple[float, str]] = []
    for raw in listing:
        name = posixpath.basename(raw)
        match = BRIDGE_FILE.fullmatch(name)
        if not match or name == current_name:
            continue
        row = by_id.get(match.group(1))
        if row is None:
            doomed.append((0.0, name))
            continue
        if row.get("state") != "delivered":
            continue
        start_state = row.get("start_state")
        if start_state is not None and start_state not in TERMINAL_STATES:
            continue
        created = float(row.get("created") or 0)
        if match.group(1) in recent or now - created < min_age_s:
            continue
        doomed.append((created, name))
    return [name for _, name in sorted(doomed)]
