"""Auth endpoints. Thin — every decision lives in `services/auth.py`.

The web app authenticates with httpOnly session cookies and a CSRF
double-submit token (`app.core.cookies`); it asks for that by sending
`X-Auth-Transport: cookie`, and its responses then carry no tokens in the body.
Other clients (the mobile app, scripts) keep receiving Bearer tokens.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.core import cookies
from app.core.config import settings
from app.core.deps import CurrentUser, DbSession
from app.models.tenant import Tenant
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    SessionRead,
    SignupRequest,
    TenantRead,
    UserRead,
)
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _deliver(result: AuthResponse, request: Request, response: Response) -> AuthResponse:
    """Always set the session cookies; strip the body tokens for the web app."""
    cookies.set_session_cookies(
        response,
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        access_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    if cookies.wants_cookie_transport(request):
        return result.model_copy(update={"access_token": "", "refresh_token": ""})
    return result


@router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def signup(
    payload: SignupRequest, session: DbSession, request: Request, response: Response
) -> AuthResponse:
    """Create a workspace and its first admin, and sign them straight in."""
    result = await auth_service.signup(session, payload, request)
    return _deliver(result, request, response)


@router.post("/login", response_model=AuthResponse)
async def login(
    payload: LoginRequest, session: DbSession, request: Request, response: Response
) -> AuthResponse:
    result = await auth_service.login(session, payload, request)
    return _deliver(result, request, response)


@router.post("/refresh", response_model=AuthResponse)
async def refresh(
    session: DbSession,
    request: Request,
    response: Response,
    payload: RefreshRequest | None = None,
) -> AuthResponse:
    """Exchange a valid refresh token — from the body or the session cookie — for a fresh pair."""
    if payload is not None:
        token = payload.refresh_token
    else:
        token = request.cookies.get(cookies.REFRESH_COOKIE, "")
        if not token or not cookies.csrf_ok(request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired. Please sign in again.",
            )
    result = await auth_service.refresh(session, token)
    return _deliver(result, request, response)


@router.get("/me", response_model=UserRead)
async def me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)


@router.get("/session", response_model=SessionRead)
async def current_session(user: CurrentUser, session: DbSession) -> SessionRead:
    """Who is signed in and which workspace they are in.

    The web app calls this on load instead of keeping a copy of the user in a
    cookie or local storage the page could be tricked into trusting.
    """
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace not found")
    return SessionRead(user=UserRead.model_validate(user), tenant=TenantRead.model_validate(tenant))


@router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    # FastAPI would otherwise infer a response model from the `-> None`
    # annotation, which a 204 may not carry.
    response_model=None,
)
async def change_password(
    payload: ChangePasswordRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> None:
    """Change the caller's password and end every other session."""
    await auth_service.change_password(
        session, user, payload.current_password, payload.new_password, request
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def logout(user: CurrentUser, response: Response) -> None:
    """End the web session for this browser.

    Clears the session cookies for the caller's own browser. Access tokens are
    short-lived; signing out every device is `POST /opsec/sessions/revoke`.
    """
    _ = user.id  # the caller's own session only
    cookies.clear_session_cookies(response)
