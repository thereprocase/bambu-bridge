"""Single shared-key Bearer auth (spec 10).

Tailscale is the network-layer auth; this Bearer key is defense in depth for
the case Tailscale leaks. One key, no per-user accounts (single-tenant).

Fail closed: if ``BRIDGE_API_KEY`` is unset, every authenticated route
returns 503 rather than silently running open. ``/health`` and ``/version``
stay reachable so the misconfig is observable.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from bambu_bridge.config import Settings

_bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    """The Settings stored on app.state by the lifespan."""
    return request.app.state.settings  # type: ignore[no-any-return]


def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    """Reject anything without ``Authorization: Bearer <BRIDGE_API_KEY>``."""
    if not settings.bridge_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="bridge API key not configured",
        )
    if credentials is None or not hmac.compare_digest(
        credentials.credentials, settings.bridge_api_key
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_media_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    """Auth for media endpoints (camera).

    A browser ``<img src=…>`` / RN ``<Image>`` can't always set Authorization,
    so the token may also arrive as ``?token=`` (same model as the WebSocket).
    """
    if not settings.bridge_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="bridge API key not configured",
        )
    presented = credentials.credentials if credentials else request.query_params.get(
        "token"
    )
    if presented is None or not hmac.compare_digest(
        presented, settings.bridge_api_key
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_read_or_viz(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    """Accept master key OR viz token for read-only routes (snapshot/viz/mesh).

    Token sources (in priority order):
      1. ``Authorization: Bearer …`` header (either key accepted).
      2. ``?token=`` query parameter (viz token OR master key accepted).

    When ``BRIDGE_VIZ_TOKEN`` is unset or empty, only the master key is
    accepted — the feature is off, not open.

    Constant-time comparison on every presented credential to prevent
    timing-based oracle attacks.
    """
    if not settings.bridge_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="bridge API key not configured",
        )

    # Determine the presented token: header wins over query param.
    presented = (
        credentials.credentials
        if credentials is not None
        else request.query_params.get("token")
    )

    if presented is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Always compare against the master key.
    master_ok = hmac.compare_digest(presented, settings.bridge_api_key)

    # Compare against viz token only when it is configured (non-empty).
    viz_token = settings.bridge_viz_token or ""
    viz_ok = bool(viz_token) and hmac.compare_digest(presented, viz_token)

    if not (master_ok or viz_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def check_ws_token(token: str | None, settings: Settings) -> bool:
    """WebSocket auth (spec 10).

    Browsers can't set Authorization on a WebSocket, so the token may also
    arrive as ``?token=``. Returns whether the connection is authorized.
    """
    if not settings.bridge_api_key:
        return False
    return token is not None and hmac.compare_digest(token, settings.bridge_api_key)
