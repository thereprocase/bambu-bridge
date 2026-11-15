"""F25/F26 builders: non-finite numbers and out-of-range feeds/temperatures raise ValueError."""

from __future__ import annotations

import pytest

from bambu_bridge.protocol import commands


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_extrude_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        commands.extrude(bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_move_axis_rejects_non_finite_distance(bad: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        commands.move_axis("X", bad)


@pytest.mark.parametrize("fn", [commands.set_nozzle_temp, commands.set_bed_temp])
@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_temperature_builders_reject_non_finite(fn, bad: float) -> None:
    with pytest.raises(ValueError):
        fn(bad)


@pytest.mark.parametrize(
    ("axis", "feed", "ok"),
    [
        ("Z", 900, True),
        ("Z", 901, False),
        ("X", 3000, True),
        ("Y", 3001, False),
        ("x", 100000, False),
    ],
)
def test_move_axis_feed_ceiling_per_axis(axis: str, feed: int, ok: bool) -> None:
    if ok:
        commands.move_axis(axis, 1.0, feed_mm_min=feed)
    else:
        with pytest.raises(ValueError, match="feed"):
            commands.move_axis(axis, 1.0, feed_mm_min=feed)


@pytest.mark.parametrize("builder", ["ams_change_filament", "unload_filament"])
@pytest.mark.parametrize(
    "kw", [{"cur_temp": 301}, {"tar_temp": 301}, {"cur_temp": -1}, {"tar_temp": 99999}]
)
def test_ams_temperatures_bounded(builder: str, kw: dict) -> None:
    fn = getattr(commands, builder)
    args = (0,) if builder == "ams_change_filament" else ()
    with pytest.raises(ValueError, match="out of range"):
        fn(*args, **kw)


def test_ams_temperatures_accept_limits() -> None:
    commands.ams_change_filament(0, cur_temp=0, tar_temp=300)
    commands.unload_filament(cur_temp=300, tar_temp=0)
