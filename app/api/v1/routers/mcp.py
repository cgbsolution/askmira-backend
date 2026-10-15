"""The MCP endpoint: Streamable HTTP, stateless.

Authenticated by an AskMira API key (`amk_…`, scoped by an admin) or an OAuth
access token (`amo_…`, acting as the person who connected the client). Without
either, the 401 names the protected-resource metadata, which is how a connector
such as claude.ai discovers where to send the person to sign in.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.well_known import resource_metadata_url
from app.core.deps import DbSession
from app.services import integrations, mcp, oauth

router = APIRouter(tags=["mcp"])

_bearer = HTTPBearer(auto_error=False)


@router.post("/mcp", response_model=None)
async def mcp_message(
    request: Request,
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Response:
    challenge = {
        "WWW-Authenticate": f'Bearer resource_metadata="{resource_metadata_url(request)}"'
    }
    presented = credentials.credentials if credentials else ""
    try:
        if presented.startswith("amk_"):
            actor, scopes = await integrations.key_identity(session, presented)
        elif presented.startswith("amo_"):
            actor, scopes = await oauth.token_identity(session, presented)
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Sign in through your MCP client, or send an AskMira API key.",
            )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            raise HTTPException(
                status_code=exc.status_code, detail=exc.detail, headers=challenge
            ) from exc
        raise
    try:
        message: Any = await request.json()
    except ValueError:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Not JSON."}},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    reply = await mcp.handle(session, actor, scopes, message)
    if reply is None:
        return Response(status_code=status.HTTP_202_ACCEPTED)
    return JSONResponse(reply)


@router.get("/mcp", response_model=None)
async def mcp_stream() -> Response:
    """No server-initiated stream is offered; every reply comes back on its POST."""
    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, headers={"Allow": "POST"})
