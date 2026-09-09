"""The feed plan — the §6.3 lesson encoded as a type.

`REPORT.md` §6.3 / `SYNTHESIS` §4.1 / Sauron's appendix: a print fails silently
("printed air" — heat + motion to layer 38, zero extrusion, no AMS engagement)
when the `.gcode.3mf` is *internally inconsistent*: the `slice_info.config`
filament arity, the MQTT `ams_mapping`, and the gcode's `M620 S<n>A` tray
selectors resolve to different physical trays. The failed donor had
``filament_maps "1 1 1 1 1"`` (5 slots) + a lone ``<filament id="5">`` +
``layer_filament_list filament_list="4"`` + ``ams_mapping:[1]`` — four numbers,
no two agreeing.

The fix is structural, not a check bolted on afterward: a :class:`FeedPlan` is
the *single source of truth* for every one of those numbers. You cannot
construct an :class:`AmsFeed` whose parts disagree — the invariant is the
constructor. `slice_info.config`, the `ams_mapping` command field, and the
expected gcode selectors are all *derived* from it, so they cannot drift.

Two variants, per the corrected contract (Sauron §4 / appendix):

* :class:`AmsFeed` — one or more filaments fed from AMS trays.
  ``use_ams:true``; ``filament_maps`` = ``"1"`` per filament; ``<filament>``
  ids 1-based; ``ams_mapping`` = 0-based physical tray per filament; gcode
  ``M620 S<tray>A`` must match.
* :class:`ExternalSpoolFeed` — the external/virtual spool (VT_TRAY).
  ``use_ams:false``; ``ams_mapping:[]``; gcode uses the ``S255`` sentinel.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from bambu_bridge.slicedoc.errors import SliceConsistencyError
from bambu_bridge.slicedoc.gcode import GcodeScan

# P1S: one AMS unit, four trays. 0-based everywhere (Sauron: 0=slot1 … 3=slot4).
_MAX_TRAY = 3
EXTERNAL_SPOOL_SENTINEL = 255  # gcode M620 S255 / T255 / telemetry tray_id 254


@dataclass(frozen=True, slots=True)
class Filament:
    """One physical filament. ``tray_info_idx`` must match the RFID-reported
    tag on the loaded spool — patching it (donor said ``GFG99``, the tray
    actually read ``GFG96``) was inconsistency *C* in §6.3."""

    tray_info_idx: str
    material: str  # slice_info `type`, e.g. "PETG"
    color: str  # "#RRGGBB"
    used_m: float
    used_g: float

    def __post_init__(self) -> None:
        if not self.tray_info_idx:
            raise SliceConsistencyError("filament.tray_info_idx is required")
        if not self.material:
            raise SliceConsistencyError("filament.material is required")
        if not (self.color.startswith("#") and len(self.color) == 7):
            raise SliceConsistencyError(
                f"filament.color must be #RRGGBB, got {self.color!r}"
            )


@dataclass(frozen=True, slots=True)
class AmsFilament:
    """A filament bound to a specific 0-based physical AMS tray."""

    filament: Filament
    tray: int


class FeedPlan(ABC):
    """Single source of truth for every tray-binding number in the package."""

    @property
    @abstractmethod
    def use_ams(self) -> bool: ...

    @property
    @abstractmethod
    def ams_mapping(self) -> list[int]:
        """The MQTT ``project_file`` ``ams_mapping`` field (0-based trays)."""

    @abstractmethod
    def slice_filaments(self) -> list[tuple[int, Filament]]:
        """``(1-based slice_info id, filament)`` in slice/tool order."""

    @abstractmethod
    def filament_maps_value(self) -> str:
        """The ``slice_info`` ``filament_maps`` metadata value."""

    @abstractmethod
    def limit_filament_maps_value(self) -> str: ...

    @abstractmethod
    def assert_gcode_consistent(self, scan: GcodeScan) -> None:
        """Raise :class:`SliceConsistencyError` unless the gcode's load
        **and finish and tool** selectors all agree with this plan. (The
        2026-05-19 recurrence: M620 S1A vs M621 S0A — finishes were ignored.)
        """


@dataclass(frozen=True, slots=True)
class AmsFeed(FeedPlan):
    """One or more AMS-fed filaments. Inconsistency is unconstructable."""

    items: tuple[AmsFilament, ...]

    def __post_init__(self) -> None:
        if not self.items:
            raise SliceConsistencyError("AmsFeed needs at least one filament")
        trays = [it.tray for it in self.items]
        for t in trays:
            if not (0 <= t <= _MAX_TRAY):
                raise SliceConsistencyError(
                    f"AMS tray index {t} out of range 0..{_MAX_TRAY} (0-based)"
                )
        if len(set(trays)) != len(trays):
            raise SliceConsistencyError(
                f"two filaments map to the same AMS tray: {trays}"
            )

    @classmethod
    def single(cls, filament: Filament, tray: int) -> AmsFeed:
        """The print-blocker case (FOLLOWUP): exactly one AMS tray."""
        return cls((AmsFilament(filament, tray),))

    @property
    def bound_tray(self) -> int:
        """The single bound tray (only defined for single-filament feeds)."""
        if len(self.items) != 1:
            raise SliceConsistencyError(
                f"bound_tray is single-filament only ({len(self.items)} "
                "filaments) — multi-filament selector normalize unsupported"
            )
        return self.items[0].tray

    @property
    def use_ams(self) -> bool:
        return True

    @property
    def ams_mapping(self) -> list[int]:
        return [it.tray for it in self.items]

    def slice_filaments(self) -> list[tuple[int, Filament]]:
        # slice_info `<filament id=...>` is 1-based (Sauron appendix).
        return [(i, it.filament) for i, it in enumerate(self.items, start=1)]

    def filament_maps_value(self) -> str:
        # Every filament is fed from AMS unit 1 → "1" repeated N times.
        return " ".join("1" for _ in self.items)

    def limit_filament_maps_value(self) -> str:
        return " ".join("0" for _ in self.items)

    def assert_gcode_consistent(self, scan: GcodeScan) -> None:
        expected = set(self.ams_mapping)
        if not scan.load_trays and not scan.tool_trays:
            if scan.has_external:
                raise SliceConsistencyError(
                    "AmsFeed but the gcode only drives the external spool "
                    f"(S{EXTERNAL_SPOOL_SENTINEL}); use ExternalSpoolFeed"
                )
            raise SliceConsistencyError(
                "AmsFeed but the gcode has no M620 S<n>A material load"
            )
        # The §6.3 / 2026-05-19 fix: loads AND finishes AND tool selects must
        # every one resolve to a tray this plan binds. (M620 S1A with
        # M621 S0A while ams_mapping=[1] is exactly the air-print bug.)
        stray = scan.bound_trays - expected
        if stray:
            raise SliceConsistencyError(
                f"gcode references AMS tray(s) {sorted(stray)} not in "
                f"ams_mapping {sorted(expected)} — loads={sorted(scan.load_trays)} "
                f"finishes={sorted(scan.finish_trays)} "
                f"tools={sorted(scan.tool_trays)}. Every M620/M621 S<n>A and "
                "T<n> must equal the bound tray (run normalize_ams_selectors)."
            )


@dataclass(frozen=True, slots=True)
class ExternalSpoolFeed(FeedPlan):
    """The external/virtual spool (VT_TRAY). ``use_ams:false``."""

    filament: Filament

    @property
    def use_ams(self) -> bool:
        return False

    @property
    def ams_mapping(self) -> list[int]:
        return []

    def slice_filaments(self) -> list[tuple[int, Filament]]:
        return [(1, self.filament)]

    def filament_maps_value(self) -> str:
        return "1"

    def limit_filament_maps_value(self) -> str:
        return "0"

    def assert_gcode_consistent(self, scan: GcodeScan) -> None:
        if scan.bound_trays:
            raise SliceConsistencyError(
                "ExternalSpoolFeed but the gcode binds real AMS tray(s) "
                f"{sorted(scan.bound_trays)}; external spool must use the "
                f"S{EXTERNAL_SPOOL_SENTINEL} sentinel only"
            )
