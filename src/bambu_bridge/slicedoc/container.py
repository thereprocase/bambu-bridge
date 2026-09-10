"""Assemble (and parse) the 15-member ``.gcode.3mf`` container.

Member set and order reproduced from `probes/3DBenchy_PETG_slot2.gcode.3mf`
(the real firmware-*accepted* container — its §6.3 failure was semantic, not
structural, so its structure is the ground truth).

Three members are **derived and owned** by this package:

* ``Metadata/plate_1.gcode``       — the sliced gcode
* ``Metadata/plate_1.gcode.md5``   — UPPERCASE hex md5, no filename, no newline
* ``Metadata/slice_info.config``   — rendered from the :class:`FeedPlan`

The other twelve (model, thumbnails, project/model settings, rels) are static
and carried through from the slicer's own output via
:meth:`StaticMembers.from_zip`. We never patch a donor's `slice_info` — we
regenerate it from a single source of truth.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass

from bambu_bridge.slicedoc.errors import ContainerError, SliceConsistencyError
from bambu_bridge.slicedoc.feed import AmsFeed, FeedPlan
from bambu_bridge.slicedoc.gcode import (
    assert_temperature_envelope,
    normalize_ams_selectors,
    scan_gcode,
)
from bambu_bridge.slicedoc.slice_info import PlateInfo, render_slice_info

GCODE_MEMBER = "Metadata/plate_1.gcode"
MD5_MEMBER = "Metadata/plate_1.gcode.md5"
SLICE_INFO_MEMBER = "Metadata/slice_info.config"

DERIVED_MEMBERS: frozenset[str] = frozenset({GCODE_MEMBER, MD5_MEMBER, SLICE_INFO_MEMBER})

# Canonical write order == the observed firmware-accepted container's order.
MEMBER_ORDER: tuple[str, ...] = (
    "3D/3dmodel.model",
    "Metadata/_rels/model_settings.config.rels",
    "Metadata/model_settings.config",
    "Metadata/pick_1.png",
    GCODE_MEMBER,
    MD5_MEMBER,
    "Metadata/plate_1.json",
    "Metadata/plate_1.png",
    "Metadata/plate_1_small.png",
    "Metadata/plate_no_light_1.png",
    "Metadata/project_settings.config",
    SLICE_INFO_MEMBER,
    "Metadata/top_1.png",
    "[Content_Types].xml",
    "_rels/.rels",
)
REQUIRED_MEMBERS: frozenset[str] = frozenset(MEMBER_ORDER)
STATIC_MEMBERS: frozenset[str] = REQUIRED_MEMBERS - DERIVED_MEMBERS

# What the *validation gate* (validate.py) actually has to read to re-derive
# the §6.3 invariant: the gcode, its md5, and slice_info. Deliberately NOT
# the full donor MEMBER_ORDER — thumbnails/rels/model are cosmetic-or-
# structural and a real OrcaSlicer container legitimately omits the donor's
# 5 PNGs. Requiring them rejected a hardware-correct slice (print #4). The
# binding consistency is proven from these three, source-agnostically.
GATE_REQUIRED_MEMBERS: frozenset[str] = frozenset({GCODE_MEMBER, MD5_MEMBER, SLICE_INFO_MEMBER})

_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)  # deterministic output


def gcode_md5(gcode: bytes) -> str:
    """The mandatory contract: UPPERCASE hex, no filename, no newline."""
    return hashlib.md5(gcode).hexdigest().upper()  # noqa: S324 — printer spec


@dataclass(frozen=True, slots=True)
class StaticMembers:
    """The twelve carried-through (non-AMS-critical) container members."""

    members: dict[str, bytes]

    @classmethod
    def from_zip(cls, data: bytes) -> StaticMembers:
        out: dict[str, bytes] = {}
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            missing = STATIC_MEMBERS - names
            if missing:
                raise ContainerError(
                    f"source .gcode.3mf missing static members: " f"{sorted(missing)}"
                )
            for name in STATIC_MEMBERS:
                out[name] = zf.read(name)
        return cls(out)


def synthesize(
    *,
    gcode: bytes,
    feed: FeedPlan,
    plate: PlateInfo,
    static: StaticMembers,
    layer_lists: list[tuple[int, str]] | None = None,
    normalize_ams: bool = False,
) -> bytes:
    """Build a self-consistent ``.gcode.3mf``.

    Order of operations is the safety story: optionally normalize the gcode's
    tray selectors, scan it, refuse an unsafe temperature, refuse an AMS
    binding the gcode contradicts (the §6.3 / 2026-05-19 trap — now incl.
    M621 finishes and T tool-selects), *then* assemble. A container only
    exists if it is consistent.

    ``normalize_ams`` rewrites every real-tray ``M620``/``M621``/``T`` to the
    single bound tray first (single-filament :class:`AmsFeed` only). Callers
    that need the change audit should call
    :func:`normalize_ams_selectors` themselves and pass the result.
    """
    if normalize_ams:
        if not isinstance(feed, AmsFeed):
            raise SliceConsistencyError("normalize_ams requires a single-filament AmsFeed")
        gcode, _changes = normalize_ams_selectors(gcode, feed.bound_tray)
    scan = scan_gcode(gcode)
    assert_temperature_envelope(scan)
    feed.assert_gcode_consistent(scan)

    slice_info = render_slice_info(plate, feed, layer_lists=layer_lists)
    derived: dict[str, bytes] = {
        GCODE_MEMBER: gcode,
        MD5_MEMBER: gcode_md5(gcode).encode("ascii"),
        SLICE_INFO_MEMBER: slice_info.encode("utf-8"),
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in MEMBER_ORDER:
            payload = derived[name] if name in derived else static.members[name]
            info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, payload)
    return buf.getvalue()


def read_member(container: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(container)) as zf:
        return zf.read(name)
