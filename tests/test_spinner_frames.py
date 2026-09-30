from __future__ import annotations

from collections import Counter

from PIL import Image

from bambu_bridge import spinner_frames
from bambu_bridge.turntable_overlay import Shape


def test_rotation_renders_each_angle_once_and_replays_without_geometry(monkeypatch):
    calls = Counter()

    def render(shape, seconds, width, height, font):
        calls[seconds] += 1
        return Image.new("RGBA", (width, height), (int(seconds * 4), 20, 30, 255))

    monkeypatch.setattr(spinner_frames, "render_panel", render)
    rotation = spinner_frames.Rotation(Shape((), 180, 20), 20, 16, 9)
    rotation.future.result(timeout=5)
    assert len(calls) == spinner_frames.FRAME_COUNT
    assert set(calls.values()) == {1}
    first = rotation.panel(5).tobytes()
    assert first == rotation.panel(65).tobytes()
    assert first != rotation.panel(15).tobytes()
    for index in range(120):
        rotation.panel(index / 2)
    assert len(rotation.decoded) <= spinner_frames.DECODED_FRAMES
    assert sum(calls.values()) == spinner_frames.FRAME_COUNT
    rotation.close()


def test_shared_sequences_reuse_equal_geometry_and_bound_eviction(monkeypatch):
    monkeypatch.setattr(
        spinner_frames,
        "render_panel",
        lambda shape, seconds, width, height, font: Image.new("RGBA", (width, height)),
    )
    cache = spinner_frames.SpinnerFrames()
    first = Shape((), 180, 20)
    cache.panel(first, 0, 20, 16, 9)
    original = next(iter(cache.sequences.values()))
    cache.panel(Shape((), 180, 20), 1, 20, 16, 9)
    assert next(iter(cache.sequences.values())) is original
    for height in (30, 40):
        cache.panel(Shape((), 180, height), 0, 20, 16, 9)
    assert len(cache.sequences) == spinner_frames.MAX_SEQUENCES
    assert original.cancelled.is_set()
    cache.close()


def test_background_failure_retains_first_frame(monkeypatch):
    def render(shape, seconds, width, height, font):
        if seconds:
            raise ValueError("synthetic render failure")
        return Image.new("RGBA", (width, height), "blue")

    monkeypatch.setattr(spinner_frames, "render_panel", render)
    rotation = spinner_frames.Rotation(Shape((), 180, 20), 20, 16, 9)
    rotation.future.result(timeout=5)
    assert rotation.error == "ValueError"
    assert rotation.panel(45).getpixel((0, 0)) == (0, 0, 255, 255)
    rotation.close()


def test_playback_interpolates_cached_neighbors_and_wraps(monkeypatch):
    monkeypatch.setattr(
        spinner_frames,
        "render_panel",
        lambda shape, seconds, width, height, font: Image.new(
            "RGBA", (width, height), (round(seconds * 4), 0, 0, 255)
        ),
    )
    rotation = spinner_frames.Rotation(Shape((), 180, 20), 20, 16, 9)
    rotation.future.result(timeout=5)
    a, b = rotation.panel(5).getpixel((0, 0))[0], rotation.panel(5 + 1 / 6).getpixel((0, 0))[0]
    middle = rotation.panel(5 + 1 / 12).getpixel((0, 0))[0]
    assert a <= middle <= b
    assert rotation.panel(59.99).tobytes() == rotation.panel(119.99).tobytes()
    rotation.close()


def test_persistent_rotation_reopens_without_geometry_work(tmp_path, monkeypatch):
    monkeypatch.setattr(
        spinner_frames,
        "render_panel",
        lambda shape, seconds, width, height, font: Image.new("RGBA", (width, height), "green"),
    )
    path = tmp_path / "sequence.zip"
    shape = Shape((), 180, 20)
    original = spinner_frames.Rotation(shape, 20, 16, 9, path)
    original.future.result(timeout=5)
    original.close()

    def forbidden(*args):
        raise AssertionError("Cached sequence should supply its panels")

    monkeypatch.setattr(spinner_frames, "render_panel", forbidden)
    reopened = spinner_frames.Rotation(shape, 20, 16, 9, path)
    reopened.future.result(timeout=5)
    assert len(reopened.frames) == spinner_frames.FRAME_COUNT
    assert reopened.panel(32).getpixel((0, 0)) == (0, 128, 0, 255)
    assert reopened.error is None
    reopened.close()


def test_corrupt_rotation_is_regenerated(tmp_path, monkeypatch):
    path = tmp_path / "sequence.zip"
    path.write_bytes(b"interrupted cache write")
    monkeypatch.setattr(
        spinner_frames,
        "render_panel",
        lambda shape, seconds, width, height, font: Image.new("RGBA", (width, height)),
    )
    rotation = spinner_frames.Rotation(Shape((), 180, 20), 20, 16, 9, path)
    rotation.future.result(timeout=5)
    assert len(rotation.frames) == spinner_frames.FRAME_COUNT
    assert rotation.error is None
    rotation.close()
