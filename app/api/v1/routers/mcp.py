"""The MCP endpoint: Streamable HTTP, stateless, authenticated by an API key."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.deps import DbSession
from app.services import integrations, mcp

router = APIRouter(tags=["mcp"])

_bearer = HTTPBearer(auto_error=False)


@router.post("/mcp", response_model=None)
async def mcp_message(
    request: Request,
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Response:
    if credentials is None or not credentials.credentials.startswith("amk_"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Send an AskMira API key as a Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    actor, scopes = await integrations.key_identity(session, credentials.credentials)
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
