"""Auth endpoints. Thin — every decision lives in `services/auth.py`.

The web app authenticates with httpOnly session cookies and a CSRF
double-submit token (`app.core.cookies`); it asks for that by sending
`X-Auth-Transport: cookie`, and its responses then carry no tokens in the body.
Other clients (the mobile app, scripts) keep receiving Bearer tokens.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.core import cookies
from app.core.config import settings
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.tenant import Tenant
from app.models.user import UserRole
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    MfaChallengeRead,
    MfaCodeRequest,
    MfaDisableRequest,
    MfaLoginRequest,
    RefreshRequest,
    ResetPasswordRequest,
    SessionRead,
    TenantRead,
    UserRead,
)
from app.services import auth as auth_service
from app.services import mfa as mfa_service
from app.services.hiring.public_guard import Limit, throttle
from app.services.identity import directory, social

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


class IdentifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login: str = Field(min_length=3, max_length=320)


class SocialStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    redirect_uri: str = Field(min_length=10, max_length=1024)


class SocialCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=1, max_length=256)


#: Finding a workspace from an email or number. Generous for a person, too
#: tight to sweep a list of addresses with.
IDENTIFY_LIMIT = Limit("identify", 20, 600)
SOCIAL_LIMIT = Limit("social_signin", 20, 600)


@router.post("/identify")
async def identify(
    payload: IdentifyRequest, session: DbSession, request: Request
) -> dict[str, Any]:
    """Step one of sign-in: which company an email or mobile number belongs to.

    Answers with that company's sign-in page — its name, logo and single
    sign-on — and nothing about the person. See `services/identity/directory.py`.
    """
    throttle(request, IDENTIFY_LIMIT)
    try:
        user = await directory.find_login(session, payload.login)
    except directory.AmbiguousLoginError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This mobile number belongs to more than one login. Use your work "
                "email instead."
            ),
        ) from exc
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "We could not find an AskMira login for that email or mobile number. "
                "Check it, or ask your HR team."
            ),
        )
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        raise directory.suspended()
    brand = await directory.branding(session, tenant)
    return {
        **brand.as_dict(),
        "login_kind": "email" if directory.looks_like_email(payload.login) else "mobile",
    }


@router.get("/social/providers")
async def social_providers() -> dict[str, Any]:
    """Which 'Continue with …' buttons this AskMira offers."""
    return {"providers": directory.social_providers()}


@router.post("/social/{provider}/start")
async def social_start(
    provider: str, payload: SocialStartRequest, session: DbSession, request: Request
) -> dict[str, str]:
    throttle(request, SOCIAL_LIMIT)
    url = await social.start(session, provider, payload.redirect_uri)
    return {"authorization_url": url}


@router.post("/social/{provider}/callback", response_model=AuthResponse | MfaChallengeRead)
async def social_callback(
    provider: str,
    payload: SocialCallbackRequest,
    session: DbSession,
    request: Request,
    response: Response,
) -> AuthResponse | MfaChallengeRead:
    throttle(request, SOCIAL_LIMIT)
    result = await social.complete(
        session, provider, code=payload.code, state=payload.state, request=request
    )
    if isinstance(result, MfaChallengeRead):
        return result
    return _deliver(result, request, response)


@router.post("/login", response_model=AuthResponse | MfaChallengeRead)
async def login(
    payload: LoginRequest, session: DbSession, request: Request, response: Response
) -> AuthResponse | MfaChallengeRead:
    """Signs in, or — with two-step sign-in on — asks for the code next."""
    result = await auth_service.login(session, payload, request)
    if isinstance(result, MfaChallengeRead):
        return result
    return _deliver(result, request, response)


@router.post("/login/mfa", response_model=AuthResponse)
async def login_mfa(
    payload: MfaLoginRequest, session: DbSession, request: Request, response: Response
) -> AuthResponse:
    """The second step of sign-in: the token from the first, and a code."""
    result = await auth_service.complete_mfa_login(
        session, payload.mfa_token, payload.code, request
    )
    return _deliver(result, request, response)


@router.get("/mfa")
async def mfa_status(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return await mfa_service.mfa_status(session, user)


@router.post("/mfa/setup")
async def mfa_setup(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """A secret for the authenticator app. Nothing is switched on until a code confirms it."""
    result = await mfa_service.begin_setup(session, user)
    await session.commit()
    return result


@router.post("/mfa/enable")
async def mfa_enable(
    payload: MfaCodeRequest, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Switch it on with a code; the recovery codes come back once, here."""
    result = await mfa_service.enable(session, user, code=payload.code, request=request)
    await session.commit()
    return result


@router.post("/mfa/disable", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def mfa_disable(
    payload: MfaDisableRequest, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await mfa_service.disable(
        session, user, password=payload.password, code=payload.code, request=request
    )
    await session.commit()


@router.post(
    "/mfa/reset/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def mfa_admin_reset(
    user_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    """An admin clears somebody else's two-step sign-in after a lost phone."""
    await mfa_service.admin_reset(session, user, user_id, request=request)
    await session.commit()


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


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED, response_model=None)
async def forgot_password(
    payload: ForgotPasswordRequest, session: DbSession, request: Request
) -> None:
    """Email a reset link. The answer is the same whether or not the address has an account."""
    await auth_service.request_password_reset(session, payload.email, request)


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def reset_password(
    payload: ResetPasswordRequest, session: DbSession, request: Request
) -> None:
    """Set a new password from an emailed link. Every existing session ends."""
    await auth_service.reset_password(session, payload.token, payload.new_password, request)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def logout(user: CurrentUser, response: Response) -> None:
    """End the web session for this browser.

    Clears the session cookies for the caller's own browser. Access tokens are
    short-lived; signing out every device is `POST /opsec/sessions/revoke`.
    """
    _ = user.id  # the caller's own session only
    cookies.clear_session_cookies(response)
