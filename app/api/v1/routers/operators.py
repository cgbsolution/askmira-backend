"""Operator access (PAY-09): a client grants a payroll bureau in; the bureau works its queue."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.core import cookies
from app.core.config import settings
from app.core.deps import CurrentUser, DbSession, require_roles
from app.core.step_up import confirmation
from app.models.user import User, UserRole
from app.schemas.auth import AuthResponse
from app.services import auth as auth_service
from app.services import operators

router = APIRouter(tags=["operators"])

ADMIN_ONLY = [Depends(require_roles(UserRole.ADMIN))]
HR_ONLY = [Depends(require_roles(UserRole.ADMIN, UserRole.SUB_ADMIN))]


@router.get("/admin/operators", dependencies=ADMIN_ONLY)
async def list_grants(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Who outside this workspace may work on its payroll, and until when."""
    return await operators.list_grants(session, user)


@router.post("/admin/operators", status_code=status.HTTP_201_CREATED, dependencies=ADMIN_ONLY)
async def create_grant(
    payload: operators.GrantIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await operators.create_grant(
        session,
        user,
        payload,
        confirm=confirmation(session, user, "operator.grant"),
        request=request,
    )


@router.post("/admin/operators/{grant_id}/revoke", dependencies=ADMIN_ONLY)
async def revoke_grant(
    grant_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await operators.revoke_grant(session, user, grant_id, request=request)


@router.get("/operator/clients", dependencies=HR_ONLY)
async def my_clients(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """The client workspaces that granted the caller access, with what each is waiting on."""
    return await operators.my_clients(session, user)


@router.post("/operator/clients/{grant_id}/enter", dependencies=HR_ONLY)
async def enter_client(
    grant_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    token, detail = await operators.enter(session, user, grant_id, request=request)
    cookies.set_access_cookie(
        response, access_token=token, access_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    )
    if cookies.wants_cookie_transport(request):
        return detail
    return {**detail, "access_token": token}


@router.get("/operator/session")
async def operator_session(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """Whether this session is an operator inside a client's workspace."""
    return await operators.session_state(session, user)


@router.post("/operator/exit", response_model=AuthResponse)
async def exit_client(
    user: CurrentUser, session: DbSession, request: Request, response: Response
) -> AuthResponse:
    """Back to the operator's own workspace, with a fresh session there."""
    if getattr(user, "operator_grant_id", None) is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="You are already in your own workspace."
        )
    own = await session.get(User, user.id)
    if own is None or not own.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in again.")
    result = await auth_service.issue_session(session, own)
    cookies.set_session_cookies(
        response,
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        access_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    if cookies.wants_cookie_transport(request):
        return result.model_copy(update={"access_token": "", "refresh_token": ""})
    return result
