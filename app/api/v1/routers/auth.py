"""Auth endpoints. Thin — every decision lives in `services/auth.py`."""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from app.core.deps import CurrentUser, DbSession
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    SignupRequest,
    UserRead,
)
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def signup(payload: SignupRequest, session: DbSession, request: Request) -> AuthResponse:
    """Create a workspace and its first admin, and sign them straight in."""
    return await auth_service.signup(session, payload, request)


@router.post("/login", response_model=AuthResponse)
async def login(payload: LoginRequest, session: DbSession, request: Request) -> AuthResponse:
    return await auth_service.login(session, payload, request)


@router.post("/refresh", response_model=AuthResponse)
async def refresh(payload: RefreshRequest, session: DbSession) -> AuthResponse:
    """Exchange a valid refresh token for a fresh pair."""
    return await auth_service.refresh(session, payload.refresh_token)


@router.get("/me", response_model=UserRead)
async def me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)


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
async def logout(user: CurrentUser) -> None:
    """Client-side logout.

    Access tokens are stateless and short-lived, so there is nothing to revoke
    here — the client drops its tokens. The endpoint exists so the frontend has
    one call to make, and so a future token-denylist has somewhere to live.
    """
    return None
