"""Authentication logic.

Self-contained: this project mints and verifies its own JWTs and hashes its own
passwords. Supabase provides the Postgres instance and nothing else.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

import jwt
from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.models.audit import AuditAction
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.auth import AuthResponse, LoginRequest, SignupRequest, TenantRead, UserRead
from app.services import audit

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


def _build_response(user: User, tenant: Tenant) -> AuthResponse:
    return AuthResponse(
        access_token=create_access_token(
            user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
        ),
        refresh_token=create_refresh_token(user_id=str(user.id), tenant_id=str(user.tenant_id)),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserRead.model_validate(user),
        tenant=TenantRead.model_validate(tenant),
    )


async def signup(
    session: AsyncSession, payload: SignupRequest, request: Request | None = None
) -> AuthResponse:
    """Create a tenant and its first TENANT_ADMIN."""
    existing = await session.scalar(
        select(User.id).where(func.lower(User.email) == payload.email.lower())
    )
    if existing is not None:
        # Signup creates a tenant, so an existing address anywhere is a
        # conflict — there is no tenant context yet to disambiguate against.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        )

    tenant = Tenant(
        name=payload.company_name,
        slug=await _unique_slug(session, payload.company_name),
        enabled_modules=dict(DEFAULT_ENABLED_MODULES),
        settings={"fiscal_year_start_month": 4, "week_start": "monday", "currency": "INR"},
    )
    session.add(tenant)
    await session.flush()

    user = User(
        tenant_id=tenant.id,
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        phone=payload.phone,
        role=UserRole.TENANT_ADMIN,
        is_active=True,
        # Pilot-phase convenience. Wire real email verification before GA.
        is_email_verified=False,
        last_login_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()

    await audit.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.CREATE,
        entity_type="tenant",
        entity_id=tenant.id,
        actor=user,
        summary=f"Tenant {tenant.name} created with admin {user.email}",
        request=request,
    )
    await session.commit()
    await session.refresh(user)
    return _build_response(user, tenant)


async def login(
    session: AsyncSession, payload: LoginRequest, request: Request | None = None
) -> AuthResponse:
    user = await session.scalar(
        select(User).where(func.lower(User.email) == payload.email.lower())
    )

    # Verify even when the user is absent, against a throwaway hash, so a
    # missing account and a wrong password take the same time to answer.
    if user is None:
        hash_password(payload.password)
        raise INVALID_CREDENTIALS

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
            detail="This account has been deactivated. Contact your HR admin.",
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
    return _build_response(user, tenant)


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
    if user.tokens_valid_from and issued_at and issued_at < user.tokens_valid_from.timestamp():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session was ended. Please sign in again.",
        )

    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace is not active")

    return _build_response(user, tenant)


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
