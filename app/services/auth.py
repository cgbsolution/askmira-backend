"""Authentication logic.

Self-contained: this project mints and verifies its own JWTs and hashes its own
passwords. Supabase provides the Postgres instance and nothing else.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import jwt
from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import has_direct_reports, tenant_filter
from app.core.security import (
    create_access_token,
    create_mfa_token,
    create_refresh_token,
    decode_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.models.attendance import Shift
from app.models.audit import AuditAction, AuditLog
from app.models.employee import Employee, EmploymentStatus
from app.models.notifications import Channel, DigestMode
from app.models.tenant import Tenant
from app.models.user import PasswordResetToken, User, UserRole
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    MfaChallengeRead,
    TenantRead,
    UserRead,
)
from app.services import audit
from app.services.notifications.dispatch import notify, register_event

# Modules a pilot tenant starts with. V2+ modules are switched on per tenant.
DEFAULT_ENABLED_MODULES = {
    "core_hr": True,
    "leave": True,
    "payroll": True,
    "culture": True,
    "hiring": True,
    "analytics": False,
    "benefits": False,
    "rewards": False,
}

#: Failed sign-ins within the window that lock an account until the window passes.
LOCKOUT_THRESHOLD = 5
LOCKOUT_WINDOW = timedelta(minutes=15)

INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Incorrect email or password",
)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "tenant"


async def _unique_slug(session: AsyncSession, base: str) -> str:
    slug = _slugify(base)
    candidate = slug
    suffix = 1
    while await session.scalar(select(Tenant.id).where(Tenant.slug == candidate)) is not None:
        suffix += 1
        candidate = f"{slug}-{suffix}"
    return candidate


async def _build_response(session: AsyncSession, user: User, tenant: Tenant) -> AuthResponse:
    user.has_reports = await has_direct_reports(session, user)
    return AuthResponse(
        access_token=create_access_token(
            user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
        ),
        refresh_token=create_refresh_token(user_id=str(user.id), tenant_id=str(user.tenant_id)),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserRead.model_validate(user),
        tenant=TenantRead.model_validate(tenant),
    )


async def issue_session(session: AsyncSession, user: User) -> AuthResponse:
    """A fresh token pair for a signed-in user in their own workspace."""
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace not found")
    return await _build_response(session, user, tenant)


async def email_in_use(session: AsyncSession, email: str) -> bool:
    """Whether any login on the platform already uses this address.

    One person, one login: sign-in looks a person up by email alone, so the
    same address in two workspaces would let it pick the wrong one.
    """
    found = await session.scalar(
        select(User.id).where(func.lower(User.email) == email.strip().lower()).limit(1)
    )
    return found is not None


async def create_workspace(
    session: AsyncSession,
    *,
    company_name: str,
    admin_full_name: str,
    admin_email: str,
    admin_phone: str | None,
    password_hash: str,
    slug: str | None = None,
    must_change_password: bool = True,
) -> tuple[Tenant, User]:
    """A new workspace and its first ADMIN. Flushed, not committed.

    Only the platform console calls this; there is no public sign-up. The admin
    is also an employee — without an employee record they could not punch,
    apply for leave or see a payslip — and a general shift is the default so
    attendance can judge a day from the first punch.
    """
    email = admin_email.strip().lower()
    if await email_in_use(session, email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That email already signs in to AskMira. Each person has one login.",
        )

    tenant = Tenant(
        name=company_name.strip(),
        slug=slug or await _unique_slug(session, company_name),
        enabled_modules=dict(DEFAULT_ENABLED_MODULES),
        settings={"fiscal_year_start_month": 4, "week_start": "monday", "currency": "INR"},
    )
    session.add(tenant)
    await session.flush()

    user = User(
        tenant_id=tenant.id,
        email=email,
        password_hash=password_hash,
        full_name=admin_full_name.strip(),
        phone=admin_phone,
        role=UserRole.ADMIN,
        is_active=True,
        is_email_verified=False,
        must_change_password=must_change_password,
    )
    session.add(user)
    await session.flush()

    first, _, last = admin_full_name.strip().partition(" ")
    employee = Employee(
        tenant_id=tenant.id,
        employee_code="EMP0001",
        first_name=first or admin_full_name.strip(),
        last_name=last.strip() or None,
        work_email=email,
        phone=admin_phone,
        date_of_joining=datetime.now(ZoneInfo("Asia/Kolkata")).date(),
        status=EmploymentStatus.ACTIVE,
    )
    session.add(employee)
    await session.flush()
    user.employee_id = employee.id

    session.add(
        Shift(
            tenant_id=tenant.id,
            code="GEN",
            name="General shift",
            start_time=time(9, 30),
            end_time=time(18, 30),
            break_minutes=60,
            grace_in_minutes=15,
            grace_out_minutes=15,
            half_day_threshold_minutes=240,
            full_day_threshold_minutes=420,
            weekly_off_days=[6, 7],
            is_default=True,
        )
    )
    await session.flush()
    return tenant, user


async def login(
    session: AsyncSession, payload: LoginRequest, request: Request | None = None
) -> AuthResponse | MfaChallengeRead:
    user = await session.scalar(
        select(User).where(func.lower(User.email) == payload.email.lower())
    )

    # Verify even when the user is absent, against a throwaway hash, so a
    # missing account and a wrong password take the same time to answer.
    if user is None:
        hash_password(payload.password)
        raise INVALID_CREDENTIALS

    # Lockout, counted from the audit log's own failed-sign-in rows: no second
    # counter to drift from the record, and it survives a restart.
    window_start = datetime.now(UTC) - LOCKOUT_WINDOW
    recent_failures = await session.scalar(
        select(func.count(AuditLog.id)).where(
            tenant_filter(AuditLog, user.tenant_id),
            AuditLog.entity_id == user.id,
            AuditLog.action == AuditAction.LOGIN_FAILED,
            AuditLog.created_at >= window_start,
        )
    )
    if (recent_failures or 0) >= LOCKOUT_THRESHOLD:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "Too many failed sign-in attempts. Try again in "
                f"{int(LOCKOUT_WINDOW.total_seconds() // 60)} minutes, or reset your password."
            ),
            headers={"Retry-After": str(int(LOCKOUT_WINDOW.total_seconds()))},
        )

    if not verify_password(payload.password, user.password_hash):
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.LOGIN_FAILED,
            entity_type="user",
            entity_id=user.id,
            actor=user,
            summary="Failed sign-in attempt",
            request=request,
        )
        await session.commit()
        raise INVALID_CREDENTIALS

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deactivated. Contact your admin.",
        )

    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This workspace is not active",
        )

    # Migrate the hash forward if the cost parameters have since been raised.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    # The password alone is not enough for somebody who turned on two-step sign-in.
    from app.services import mfa

    if await mfa.is_enabled(session, user):
        await session.commit()
        return MfaChallengeRead(
            mfa_token=create_mfa_token(user_id=str(user.id), tenant_id=str(user.tenant_id)),
            expires_in=300,
        )

    user.last_login_at = datetime.now(UTC)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.LOGIN,
        entity_type="user",
        entity_id=user.id,
        actor=user,
        summary="Signed in",
        request=request,
    )
    await session.commit()
    await session.refresh(user)
    return await _build_response(session, user, tenant)


async def complete_mfa_login(
    session: AsyncSession, mfa_token: str, code: str, request: Request | None = None
) -> AuthResponse:
    """The second step: the token from a right password, and a code."""
    from app.services import mfa

    try:
        payload = decode_token(mfa_token, expected_type="mfa")
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="That sign-in took too long. Enter your email and password again.",
        ) from exc
    user = await session.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise INVALID_CREDENTIALS

    window_start = datetime.now(UTC) - LOCKOUT_WINDOW
    recent_failures = await session.scalar(
        select(func.count(AuditLog.id)).where(
            tenant_filter(AuditLog, user.tenant_id),
            AuditLog.entity_id == user.id,
            AuditLog.action == AuditAction.LOGIN_FAILED,
            AuditLog.created_at >= window_start,
        )
    )
    if (recent_failures or 0) >= LOCKOUT_THRESHOLD:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Try again later, or ask an admin to help.",
            headers={"Retry-After": str(int(LOCKOUT_WINDOW.total_seconds()))},
        )
    if not await mfa.check_code(session, user, code):
        await audit.record(
            session, tenant_id=user.tenant_id, action=AuditAction.LOGIN_FAILED,
            entity_type="user", entity_id=user.id, actor=user,
            summary="Wrong two-step sign-in code", request=request,
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="That code is not right. Use the code showing now, or a recovery code.",
        )

    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This workspace is not active"
        )
    user.last_login_at = datetime.now(UTC)
    await audit.record(
        session, tenant_id=user.tenant_id, action=AuditAction.LOGIN, entity_type="user",
        entity_id=user.id, actor=user, summary="Signed in with two-step sign-in",
        request=request,
    )
    await session.commit()
    await session.refresh(user)
    return await _build_response(session, user, tenant)


async def refresh(session: AsyncSession, refresh_token: str) -> AuthResponse:
    try:
        payload = decode_token(refresh_token, expected_type="refresh")
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has expired. Please sign in again.",
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        ) from exc

    user = await session.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    issued_at = payload.get("iat")
    # Whole seconds: `iat` has no fraction, so a sign-in in the same second as
    # the reset would otherwise read as older than it and be refused.
    cutoff = int(user.tokens_valid_from.timestamp()) if user.tokens_valid_from else None
    if cutoff is not None and issued_at and issued_at < cutoff:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session was ended. Please sign in again.",
        )

    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace is not active")

    return await _build_response(session, user, tenant)


async def change_password(
    session: AsyncSession,
    user: User,
    current_password: str,
    new_password: str,
    request: Request | None = None,
) -> None:
    if not verify_password(current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect"
        )

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    # Invalidates every token issued before now, on every device.
    user.tokens_valid_from = datetime.now(UTC)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="user",
        entity_id=user.id,
        actor=user,
        summary="Password changed; all existing sessions invalidated",
        request=request,
    )
    await session.commit()


# ── Forgot password ─────────────────────────────────────────────────────────

RESET_TOKEN_LIFETIME = timedelta(minutes=30)
RESET_REQUESTS_PER_HOUR = 3

register_event(
    "auth.password.reset",
    "Reset your AskMira password",
    "Hi {name}, use this link to set a new AskMira password: {link} — it works once and "
    "expires in {minutes} minutes. If you did not ask for this, ignore this email; your "
    "password has not changed.",
    preferences={Channel.EMAIL: DigestMode.IMMEDIATE},
)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _web_app_url() -> str:
    base = settings.WEB_APP_URL or (settings.CORS_ORIGINS[0] if settings.CORS_ORIGINS else "")
    return base.rstrip("/")


async def request_password_reset(
    session: AsyncSession, email: str, request: Request | None = None
) -> None:
    """Email a single-use reset link, if the address belongs to an active login.

    Answers the same way whether or not the address exists, so the endpoint
    cannot be used to find out who has an account.
    """
    user = await session.scalar(select(User).where(func.lower(User.email) == email.lower()))
    if user is None or not user.is_active:
        return

    now = datetime.now(UTC)
    # A throttle, so the endpoint cannot be used to flood someone's inbox.
    recent = await session.scalar(
        select(func.count(PasswordResetToken.id)).where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.created_at >= now - timedelta(hours=1),
        )
    )
    if (recent or 0) >= RESET_REQUESTS_PER_HOUR:
        return

    # Only the newest link works: asking again retires the earlier ones.
    outstanding = (
        await session.scalars(
            select(PasswordResetToken).where(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > now,
            )
        )
    ).all()
    for row in outstanding:
        row.expires_at = now

    token = secrets.token_urlsafe(32)
    session.add(
        PasswordResetToken(
            tenant_id=user.tenant_id,
            user_id=user.id,
            token_hash=_token_hash(token),
            expires_at=now + RESET_TOKEN_LIFETIME,
            requested_ip=request.client.host if request and request.client else None,
        )
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="user",
        entity_id=user.id,
        actor_user_id=user.id,
        summary="Password reset link requested",
        request=request,
    )
    await notify(
        session,
        tenant_id=user.tenant_id,
        event_key="auth.password.reset",
        recipient_user_ids=[user.id],
        payload={
            "name": user.full_name.split(" ")[0],
            "link": f"{_web_app_url()}/reset-password?token={token}",
            "minutes": int(RESET_TOKEN_LIFETIME.total_seconds() // 60),
        },
        commit=False,
    )
    await session.commit()


async def reset_password(
    session: AsyncSession, token: str, new_password: str, request: Request | None = None
) -> None:
    """Set a new password from a reset link, and sign out every existing session."""
    row = await session.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == _token_hash(token))
    )
    now = datetime.now(UTC)
    if row is None or row.used_at is not None or row.expires_at <= now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This reset link is invalid or has expired. Ask for a new one.",
        )
    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This reset link is invalid or has expired. Ask for a new one.",
        )

    row.used_at = now
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.tokens_valid_from = now
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="user",
        entity_id=user.id,
        actor_user_id=user.id,
        summary="Password reset from an emailed link; all existing sessions invalidated",
        request=request,
    )
    await session.commit()
