import gzip

from bambu_bridge.preview_assets import GeometryAssets

GCODE = b"M83\nG1 X100 Y100 Z0.2\nG1 X110 E1\nG1 Y110 E1\nG1 X100 E1\nG1 Y100 E1\n"


def test_restart_reuses_geometry_and_corruption_rebuilds(tmp_path, monkeypatch):
    cache = GeometryAssets(tmp_path)
    shape = cache.load(GCODE, 1)
    assert shape is not None and shape.content_id
    from bambu_bridge import preview_assets

    original = preview_assets.archive_shape
    monkeypatch.setattr(
        preview_assets,
        "archive_shape",
        lambda *args: (_ for _ in ()).throw(AssertionError("Reparsed")),
    )
    assert GeometryAssets(tmp_path).load(GCODE, 1) == shape
    monkeypatch.setattr(preview_assets, "archive_shape", original)
    path = next(cache.directory.glob("*.gz"))
    with gzip.open(path, "wb") as f:
        f.write(b'{"faces": []}')
    assert cache.load(GCODE, 1) == shape


def test_plate_has_independent_identity(tmp_path):
    cache = GeometryAssets(tmp_path)
    assert cache.load(GCODE, 1).content_id != cache.load(GCODE, 2).content_id
