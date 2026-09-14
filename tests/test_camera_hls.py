from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from bambu_bridge.api import camera


@pytest.mark.parametrize(
    "resource,query,valid",
    [
        ("index.m3u8", {}, True),
        ("c40e5d2d4222_video1_init.mp4", {"cookieCheck": "1", "session": "test-session"}, True),
        ("index.m3u8", {"cookieCheck": "2"}, False),
        ("video1_stream.m3u8", {"session": "45883863-8db1-4e63-8773-eb95b6906f1d"}, True),
        ("video1_part42.mp4", {"_HLS_msn": "12", "_HLS_part": "2"}, True),
        ("video1_stream.m3u8", {"_HLS_skip": "YES"}, True),
        ("../index.m3u8", {}, False),
        ("http://evil/index.m3u8", {}, False),
        ("index.html", {}, False),
        ("index.m3u8", {"url": "http://evil"}, False),
        ("index.m3u8", {"_HLS_msn": "-1"}, False),
        ("index.m3u8", {"token": "secret"}, False),
    ],
)
def test_hls_resource_allowlist(resource, query, valid):
    assert camera.hls_resource(resource, query) == valid


def request(scheme="https", ready=True):
    gateway = SimpleNamespace(
        config={"printer_id": "printer"},
        video=SimpleNamespace(ready=ready),
        saved_code=lambda: "test-native-code",
    )
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(native_gateway=gateway)),
        url=SimpleNamespace(scheme=scheme),
        query_params={},
    )


@pytest.mark.parametrize("scheme,ready", [("http", True), ("https", False)])
async def test_hls_fails_closed_without_https_or_backend(scheme, ready):
    with pytest.raises(HTTPException) as error:
        await camera.camera_hls("printer", "index.m3u8", request(scheme, ready))
    assert error.value.status_code == 404


async def test_hls_serves_only_authenticated_printer_ladder():
    async def read(resource):
        assert resource == "index.m3u8"
        return b"#EXTM3U\nlow.m3u8\n"

    req = request()
    req.app.state.native_gateway.video.adaptive = SimpleNamespace(read=read)
    response = await camera.camera_hls("printer", "index.m3u8", req)
    assert response.body == b"#EXTM3U\nlow.m3u8\n"
    assert "authorization" not in response.headers
    assert response.headers["cache-control"] == "no-store"
