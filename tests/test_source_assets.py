from bambu_bridge.source_assets import SourceAssets


def test_source_references_verify_contents_and_reject_corruption(tmp_path):
    assets = SourceAssets(tmp_path)
    digest = assets.put("fixture reference", b"original")
    assert SourceAssets(tmp_path).read("fixture reference") == b"original"
    (assets.directory / digest).write_bytes(b"damaged")
    assert assets.read("fixture reference") is None


def test_source_replacement_changes_content_identity(tmp_path):
    assets = SourceAssets(tmp_path)
    first = assets.put("same name revision one", b"one")
    second = assets.put("same name revision two", b"two")
    assert first != second
    assert assets.read("same name revision one") == b"one"
    assert assets.read("same name revision two") == b"two"
