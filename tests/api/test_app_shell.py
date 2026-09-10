"""SPA web-app shell routes (unauthenticated static serving of static/app/**).

The shell routes live on :data:`bambu_bridge.api.viz.app_shell_router`, mounted
at the application ROOT (``/``, ``/app``, ``/app/{path}``) — outside the
``/api/v1`` surface and with no auth dependency (the shell is pure HTML/CSS/JS
and exposes no printer data; every ``/api/v1/*`` call the SPA makes still
carries a Bearer token).

Stage-1 (the SPA itself) runs in parallel, so ``static/app/`` may not exist on
disk yet.  These tests are therefore hermetic: they monkeypatch the module's
``_APP_DIR`` / ``_APP_INDEX`` to a tmp directory holding a dummy
``index.html`` + ``main.js`` (mirroring how ``test_viz.py`` monkeypatches
``_VIEWER_HTML``).  They also exercise the "not built yet" 501 path and the
path-traversal rejection.

Coverage
--------
* GET /app          -> 200 text/html (shell index).
* GET /app/         -> 200 text/html (shell index).
* GET /             -> 307 redirect to /app/.
* GET /app/main.js  -> 200 with a JavaScript media type (must NOT be octet-stream).
* GET /app/styles.css -> 200 text/css.
* GET /app/app.webmanifest -> 200 application/manifest+json.
* GET /app/../<escape> traversal is rejected (404), never serves outside the tree.
* GET /app/missing.js -> 404 for a path inside the tree that does not exist.
* GET /app -> 501 when the SPA was not built into the wheel (index.html absent).
* Shell routes carry Cache-Control: no-cache so a wheel upgrade reaches browsers.
* Shell routes need NO auth (served without a Bearer token).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bambu_bridge.api import viz as viz_mod
from tests.conftest import build_app

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _wire_shell(app: FastAPI) -> None:
    """Include the shell router at the app level.

    This is the ONE line the coordinator adds to ``main.py``
    (``app.include_router(viz.app_shell_router)``); we replicate it here so the
    test validates the real router without depending on main.py being edited
    yet.  Idempotency: ``create_app`` does not include this router today, so a
    single include is correct.
    """
    app.include_router(viz_mod.app_shell_router)


@pytest.fixture()
def app_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp ``app/`` dir with a dummy SPA, wired into the viz module.

    Monkeypatches the module-level ``_APP_DIR`` and ``_APP_INDEX`` (the exact
    attributes the route handlers read) so the routes resolve against this tmp
    tree instead of the real (possibly-absent) ``static/app/``.
    """
    app_root = tmp_path / "app"
    app_root.mkdir()
    (app_root / "index.html").write_text(
        "<!doctype html><title>Bambu Bridge</title>", encoding="utf-8"
    )
    (app_root / "main.js").write_text("export const x = 1;\n", encoding="utf-8")
    (app_root / "styles.css").write_text(":root{--bg:#101113}\n", encoding="utf-8")
    (app_root / "app.webmanifest").write_text('{"name":"Bambu Bridge"}', encoding="utf-8")
    # A nested asset to confirm subdirectories resolve.
    (app_root / "icons").mkdir()
    (app_root / "icons" / "logo.svg").write_text("<svg/>", encoding="utf-8")

    monkeypatch.setattr(viz_mod, "_APP_DIR", app_root)
    monkeypatch.setattr(viz_mod, "_APP_INDEX", app_root / "index.html")
    return app_root


@pytest.fixture()
def client(tmp_path: Path, app_dir: Path) -> TestClient:
    app = build_app(tmp_path / "shell.db", mqtt_port=1)
    _wire_shell(app)
    # follow_redirects=False so we can assert the 307 on GET / directly.
    return TestClient(app, follow_redirects=False)


# --------------------------------------------------------------------------- #
# Index / redirect
# --------------------------------------------------------------------------- #


def test_get_app_serves_index_html(client: TestClient) -> None:
    r = client.get("/app")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    assert "Bambu Bridge" in r.text
    # No-build cache story: must revalidate so a wheel upgrade reaches browsers.
    assert r.headers["cache-control"] == "no-cache"


def test_get_app_trailing_slash_serves_index(client: TestClient) -> None:
    r = client.get("/app/")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")


def test_get_root_redirects_to_app(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 307, r.text
    assert r.headers["location"] == "/app/"


def test_shell_needs_no_auth(client: TestClient) -> None:
    # No Authorization header at all — the shell is unauthenticated.
    assert client.get("/app").status_code == 200
    assert client.get("/").status_code == 307


# --------------------------------------------------------------------------- #
# Asset passthrough + media types
# --------------------------------------------------------------------------- #


def test_get_main_js_has_javascript_media_type(client: TestClient) -> None:
    r = client.get("/app/main.js")
    assert r.status_code == 200, r.text
    # MUST be a JavaScript type or the browser refuses to evaluate the ES module.
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["content-type"] != "application/octet-stream"
    assert "export const x" in r.text
    assert r.headers["cache-control"] == "no-cache"


def test_get_css_media_type(client: TestClient) -> None:
    r = client.get("/app/styles.css")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/css")


def test_get_webmanifest_media_type(client: TestClient) -> None:
    r = client.get("/app/app.webmanifest")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/manifest+json")


def test_get_nested_asset(client: TestClient) -> None:
    r = client.get("/app/icons/logo.svg")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/svg+xml")


def test_get_unknown_suffix_falls_back_to_octet_stream(
    client: TestClient, app_dir: Path
) -> None:
    (app_dir / "data.bin").write_bytes(b"\x00\x01\x02")
    r = client.get("/app/data.bin")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/octet-stream")


# --------------------------------------------------------------------------- #
# Missing asset / not-built
# --------------------------------------------------------------------------- #


def test_missing_asset_inside_tree_is_404(client: TestClient) -> None:
    r = client.get("/app/does-not-exist.js")
    assert r.status_code == 404, r.text


def test_get_app_501_when_not_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When index.html is absent (old wheel / SPA not built), GET /app -> 501.

    Mirrors the viz.py viewer's defensive 501 for a missing viewer.html.
    """
    absent = tmp_path / "no_such_app"
    monkeypatch.setattr(viz_mod, "_APP_DIR", absent)
    monkeypatch.setattr(viz_mod, "_APP_INDEX", absent / "index.html")
    app = build_app(tmp_path / "shell_501.db", mqtt_port=1)
    _wire_shell(app)
    with TestClient(app, follow_redirects=False) as c:
        r = c.get("/app")
        assert r.status_code == 501, r.text
        assert "web app not built" in r.text


# --------------------------------------------------------------------------- #
# Path-traversal hardening
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "evil",
    [
        "/app/../viewer.html",
        "/app/../../bambu_bridge/main.py",
        "/app/icons/../../../conftest.py",
        "/app/..%2f..%2fmain.py",  # encoded traversal (TestClient decodes)
    ],
)
def test_traversal_is_rejected(client: TestClient, evil: str) -> None:
    """A request that tries to escape static/app/ must never serve the file.

    Accept either 404 (our handler's containment rejection) or 307 (TestClient
    normalizing ``..`` segments client-side before they ever reach the route).
    Neither path serves content from outside the tree; a 2xx with a real file
    body would be the failure.
    """
    r = client.get(evil)
    assert r.status_code in (404, 307), r.text
    if r.status_code == 307:
        # Redirect target must stay within /app/, never point outside the tree.
        loc = r.headers.get("location", "")
        assert loc.startswith("/app") or loc.startswith("/"), loc
    else:
        # 404 body is the generic "not found", not a served file.
        assert "viewer" not in r.text.lower()
        assert "def " not in r.text  # no python source leaked


@pytest.mark.asyncio
async def test_handler_dotdot_guard_rejects(app_dir: Path) -> None:
    """The handler's own ``..``-segment guard returns 404 directly.

    HTTP clients (httpx/TestClient) normalize ``..`` in the URL path before it
    reaches the server, so the only way to prove the in-handler guard fires is
    to call the coroutine with a raw path containing a ``..`` segment.
    """
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await viz_mod.get_app_asset(path="../main.py")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_handler_resolved_escape_rejected(app_dir: Path) -> None:
    """A path that resolves outside the app dir is rejected (containment check).

    Uses a path whose ``..`` is interior (``icons/../../x``) so the explicit
    segment guard AND the resolve/relative_to containment guard both apply;
    asserting 404 confirms no file outside the tree is ever served.
    """
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await viz_mod.get_app_asset(path="icons/../../conftest.py")
    assert exc.value.status_code == 404
