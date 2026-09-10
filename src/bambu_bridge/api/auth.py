"""Owner key, read-only viewer key, and HTTPS-only paired device credentials."""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from bambu_bridge.config import Settings
from bambu_bridge.pairing import PairingStore

_bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _matches(presented: str | None, expected: str | None) -> bool:
    return bool(
        presented and expected and hmac.compare_digest(presented.encode(), expected.encode())
    )


def check_ws_token(
    token: str | None, settings: Settings, pairing: PairingStore | None = None, secure: bool = False
) -> bool:
    return _matches(token, settings.bridge_api_key) or bool(
        secure and pairing and pairing.authenticate(token)
    )


def _check(
    request: Request,
    settings: Settings,
    presented: str | None,
    *,
    viewer: bool = False,
    owner: bool = False,
) -> None:
    pairing = getattr(request.app.state, "pairing", None)
    if not settings.bridge_api_key and (owner or pairing is None):
        raise HTTPException(503, "bridge API key not configured")
    valid = _matches(presented, settings.bridge_api_key)
    if not owner:
        valid |= bool(request.url.scheme == "https" and pairing and pairing.authenticate(presented))
        if viewer:
            valid |= _matches(presented, settings.bridge_viz_token)
    if not valid:
        raise HTTPException(
            401, "invalid or missing bearer token", headers={"WWW-Authenticate": "Bearer"}
        )


def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    _check(request, settings, credentials.credentials if credentials else None)


def require_owner(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    _check(request, settings, credentials.credentials if credentials else None, owner=True)


def require_media_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    _check(
        request,
        settings,
        credentials.credentials if credentials else request.query_params.get("token"),
    )


def require_read_or_viz(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    _check(
        request,
        settings,
        credentials.credentials if credentials else request.query_params.get("token"),
        viewer=True,
    )
