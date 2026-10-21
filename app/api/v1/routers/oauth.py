"""OAuth 2.1 for MCP connectors: registration, consent, tokens, and the person's connections.

`/register`, `/token` and `/revoke` are called by the connector itself and speak
OAuth's own JSON errors. `/authorize` is called by the web app's consent page,
signed in as the person approving.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.core.deps import CurrentUser, DbSession
from app.services import oauth
from app.services.hiring import public_guard

router = APIRouter(prefix="/oauth", tags=["oauth"])

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _oauth_response(exc: HTTPException) -> JSONResponse:
    body = (
        exc.detail
        if isinstance(exc.detail, dict)
        else {"error": "invalid_request", "error_description": str(exc.detail)}
    )
    return JSONResponse(body, status_code=exc.status_code, headers=_NO_STORE)


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=None)
async def register(request: Request, session: DbSession) -> JSONResponse:
    """RFC 7591 dynamic client registration, for public (PKCE) clients."""
    public_guard.throttle(request, public_guard.OAUTH_REGISTER)
    try:
        payload = oauth.ClientIn.model_validate(await request.json())
        return JSONResponse(
            await oauth.register_client(session, payload), status_code=201, headers=_NO_STORE
        )
    except HTTPException as exc:
        return _oauth_response(exc)
    except ValueError as exc:
        return JSONResponse(
            {"error": "invalid_client_metadata", "error_description": str(exc)[:300]},
            status_code=400,
        )


@router.post("/token", response_model=None)
async def token(request: Request, session: DbSession) -> JSONResponse:
    """authorization_code (with PKCE) and refresh_token grants, form-encoded."""
    form = await request.form()
    grant_type = str(form.get("grant_type") or "")
    client_id = str(form.get("client_id") or "")
    try:
        if grant_type == "authorization_code":
            issued = await oauth.exchange_code(
                session,
                code=str(form.get("code") or ""),
                client_id=client_id,
                redirect_uri=str(form.get("redirect_uri") or ""),
                code_verifier=str(form.get("code_verifier") or ""),
            )
        elif grant_type == "refresh_token":
            issued = await oauth.refresh(
                session, refresh_token=str(form.get("refresh_token") or ""), client_id=client_id
            )
        else:
            return JSONResponse(
                {"error": "unsupported_grant_type"}, status_code=400, headers=_NO_STORE
            )
    except HTTPException as exc:
        return _oauth_response(exc)
    return JSONResponse(issued, headers=_NO_STORE)


@router.post("/revoke", response_model=None)
async def revoke(request: Request, session: DbSession) -> JSONResponse:
    """RFC 7009. Always 200, known token or not."""
    form = await request.form()
    await oauth.revoke_token(session, token=str(form.get("token") or ""))
    return JSONResponse({}, headers=_NO_STORE)


@router.get("/authorize/details")
async def authorize_details(
    user: CurrentUser,
    session: DbSession,
    client_id: str,
    redirect_uri: str,
    scope: str | None = None,
) -> dict[str, Any]:
    """For the consent page: which client asks, and what the caller may grant it."""
    return await oauth.authorize_details(
        session, user, client_id=client_id, redirect_uri=redirect_uri, scope=scope
    )


@router.post("/authorize")
async def authorize(
    payload: oauth.AuthorizeIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, str]:
    """The caller approves or declines; returns where their browser goes next."""
    return await oauth.authorize(session, user, payload, request=request)


@router.get("/connections")
async def connections(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """The MCP clients the caller has connected."""
    return await oauth.list_connections(session, user)


@router.post(
    "/connections/{grant_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def revoke_connection(
    grant_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await oauth.revoke_connection(session, user, grant_id, request=request)
