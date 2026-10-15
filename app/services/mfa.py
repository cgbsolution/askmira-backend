"""Two-step sign-in: an authenticator app, with recovery codes for a lost phone.

Turned on by the person themselves — set-up shows a secret, and only a code
from it switches it on, so nobody is locked out by a secret their app never
received. Once on, it is asked for at sign-in and at every step-up (payroll
approval, a bank file release), so a stolen password is not enough for either.

A recovery code works once. An admin can reset somebody else's two-step
sign-in (a lost phone with no codes left), never their own, and it is audited.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions, totp
from app.core.config import settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.deps import tenant_scoped
from app.core.security import verify_password
from app.models.audit import AuditAction
from app.models.mfa import UserMfa
from app.models.user import User
from app.services import audit

__all__ = [
    "RECOVERY_CODES",
    "admin_reset",
    "begin_setup",
    "check_code",
    "disable",
    "enable",
    "is_enabled",
    "mfa_status",
]

RECOVERY_CODES = 10
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I to misread


def _normalise(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _hash(code: str) -> str:
    return hashlib.sha256(_normalise(code).encode("ascii")).hexdigest()


def _recovery_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(10))
    return f"{raw[:5]}-{raw[5:]}"


async def _row(session: AsyncSession, user: User) -> UserMfa | None:
    return await session.scalar(
        tenant_scoped(select(UserMfa).where(UserMfa.user_id == user.id), UserMfa, user)
    )


async def is_enabled(session: AsyncSession, user: User) -> bool:
    row = await _row(session, user)
    return row is not None and row.enabled_at is not None


async def mfa_status(session: AsyncSession, user: User) -> dict[str, Any]:
    """The caller's own two-step sign-in: on, mid-set-up, or off."""
    row = await session.scalar(
        tenant_scoped(select(UserMfa).where(UserMfa.user_id == user.id), UserMfa, user)
    )
    enabled = row is not None and row.enabled_at is not None
    return {
        "enabled": enabled,
        "enabled_at": row.enabled_at.isoformat() if enabled and row else None,
        "setup_pending": row is not None and row.enabled_at is None,
        "recovery_codes_left": len(row.recovery_code_hashes or []) if enabled and row else 0,
    }


async def begin_setup(session: AsyncSession, user: User) -> dict[str, Any]:
    """A fresh secret to add to an authenticator app. Nothing changes until it is confirmed."""
    row = await _row(session, user)
    if row is not None and row.enabled_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Two-step sign-in is already on. Turn it off first to move it to a new phone.",
        )
    secret = totp.new_secret()
    if row is None:
        row = UserMfa(tenant_id=user.tenant_id, user_id=user.id, secret_encrypted="")
        session.add(row)
    row.secret_encrypted = encrypt_secret(secret, user.tenant_id)
    row.last_used_step = None
    row.recovery_code_hashes = []
    await session.flush()
    return {
        "secret": secret,
        "otpauth_uri": totp.provisioning_uri(secret, account=user.email, issuer=settings.APP_NAME),
        "issuer": settings.APP_NAME,
        "account": user.email,
    }


async def enable(
    session: AsyncSession, user: User, *, code: str, request: Request | None = None
) -> dict[str, Any]:
    """Switch it on with a code from the app; hand back recovery codes, shown once."""
    row = await _row(session, user)
    if row is None or row.enabled_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Start set-up first, then enter a code from your authenticator app.",
        )
    secret = decrypt_secret(row.secret_encrypted, user.tenant_id) or ""
    step = totp.verify(secret, code)
    if step is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "That code does not match. Check that your phone's clock is set automatically, "
                "and enter the code showing now."
            ),
        )
    codes = [_recovery_code() for _ in range(RECOVERY_CODES)]
    row.enabled_at = datetime.now(UTC)
    row.last_used_step = step
    row.recovery_code_hashes = [_hash(c) for c in codes]
    await session.flush()
    await audit.record(
        session, tenant_id=user.tenant_id, actor=user, action=AuditAction.UPDATE,
        entity_type="user", entity_id=user.id, summary="Two-step sign-in turned on",
        request=request,
    )
    return {"recovery_codes": codes}


async def check_code(session: AsyncSession, user: User, code: str | None) -> bool:
    """A current authenticator code (never a replay), or an unused recovery code, used up."""
    row = await _row(session, user)
    if row is None or row.enabled_at is None or not (code or "").strip():
        return False
    secret = decrypt_secret(row.secret_encrypted, user.tenant_id) or ""
    step = totp.verify(secret, code or "", after_step=row.last_used_step)
    if step is not None:
        row.last_used_step = step
        await session.flush()
        return True
    hashed = _hash(code or "")
    remaining = list(row.recovery_code_hashes or [])
    if hashed in remaining:
        remaining.remove(hashed)
        row.recovery_code_hashes = remaining
        await session.flush()
        return True
    return False


async def disable(
    session: AsyncSession,
    user: User,
    *,
    password: str,
    code: str,
    request: Request | None = None,
) -> None:
    """Turning it off needs both the password and a code: the point is that one is not enough."""
    row = await _row(session, user)
    if row is None or row.enabled_at is None:
        raise HTTPException(status_code=409, detail="Two-step sign-in is not on.")
    from app.models.opsec import TenantSessionPolicy
    from app.services.opsec.enforcement import two_step_applies

    policy = await session.scalar(
        tenant_scoped(select(TenantSessionPolicy), TenantSessionPolicy, user)
    )
    if two_step_applies(policy, user):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This workspace requires two-step sign-in of you, so it cannot be turned off.",
        )
    if not verify_password(password, user.password_hash) or not await check_code(
        session, user, code
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The password or the code is wrong.",
        )
    await session.delete(row)
    await session.flush()
    await audit.record(
        session, tenant_id=user.tenant_id, actor=user, action=AuditAction.UPDATE,
        entity_type="user", entity_id=user.id, summary="Two-step sign-in turned off",
        request=request,
    )


async def admin_reset(
    session: AsyncSession, admin: User, user_id: uuid.UUID, *, request: Request | None = None
) -> None:
    """For somebody who lost their phone and their codes. Never one's own."""
    if not permissions.is_admin(admin):
        raise permissions.forbidden("Only an admin resets somebody's two-step sign-in.")
    if user_id == admin.id:
        raise permissions.forbidden(
            "Another admin resets yours: resetting your own would let a stolen password do it."
        )
    target = await session.scalar(
        tenant_scoped(select(User).where(User.id == user_id), User, admin)
    )
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    row = await _row(session, target)
    if row is None:
        raise HTTPException(status_code=409, detail=f"{target.full_name} has no two-step sign-in.")
    await session.delete(row)
    await session.flush()
    await audit.record(
        session, tenant_id=admin.tenant_id, actor=admin, action=AuditAction.UPDATE,
        entity_type="user", entity_id=target.id,
        summary=f"Two-step sign-in reset for {target.full_name}; they set it up again at sign-in",
        request=request,
    )
