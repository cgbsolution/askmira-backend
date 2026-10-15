"""OAuth discovery documents at the host root, where connectors look for them.

RFC 9728 (protected resource metadata) for the MCP endpoint, and RFC 8414
(authorization server metadata) for this API as its own authorization server.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.core.config import settings
from app.services import oauth

router = APIRouter(tags=["oauth"])


def api_base(request: Request) -> str:
    """The origin a connector reached us on.

    Configured, or the portal's when the request came through its proxy (the
    address people paste is the portal's), or this API's own.
    """
    if settings.PUBLIC_API_URL:
        return settings.PUBLIC_API_URL.rstrip("/")
    forwarded = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    if forwarded:
        scheme = request.headers.get("x-forwarded-proto", "https").split(",")[0].strip()
        return f"{scheme}://{forwarded}"
    return str(request.base_url).rstrip("/")


def _web_base() -> str:
    base = settings.WEB_APP_URL or (settings.CORS_ORIGINS[0] if settings.CORS_ORIGINS else "")
    return base.rstrip("/")


def resource_metadata_url(request: Request) -> str:
    return f"{api_base(request)}/.well-known/oauth-protected-resource{settings.API_V1_PREFIX}/mcp"


def _protected_resource(request: Request) -> dict[str, Any]:
    base = api_base(request)
    return {
        "resource": f"{base}{settings.API_V1_PREFIX}/mcp",
        "authorization_servers": [base],
        "scopes_supported": list(oauth.ALL_SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "AskMira HR",
    }


@router.get("/.well-known/oauth-protected-resource")
async def protected_resource(request: Request) -> dict[str, Any]:
    return _protected_resource(request)


@router.get("/.well-known/oauth-protected-resource/{path:path}")
async def protected_resource_for_path(path: str, request: Request) -> dict[str, Any]:
    return _protected_resource(request)


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server(request: Request) -> dict[str, Any]:
    base = api_base(request)
    api = f"{base}{settings.API_V1_PREFIX}/oauth"
    return {
        "issuer": base,
        "authorization_endpoint": f"{_web_base()}/oauth/authorize",
        "token_endpoint": f"{api}/token",
        "registration_endpoint": f"{api}/register",
        "revocation_endpoint": f"{api}/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": list(oauth.ALL_SCOPES),
    }
