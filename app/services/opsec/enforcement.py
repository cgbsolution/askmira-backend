"""Server-side enforcement of a tenant's session policy (slice 30).

Called from `app.core.deps.get_current_user` on every authenticated request,
after the token itself has been verified. It does three things, and only for a
tenant that has saved a session policy — an unconfigured tenant keeps exactly
the behaviour it had (sessions end at token expiry or at a forced logout):

1. **IP allowlist for privileged roles.** An admin or HR role outside the
   allowlist is refused with a 403 naming why. An empty allowlist permits
   everything, and the tenant's designated break-glass account passes from any
   address (logged), so a wrong CIDR range is recoverable from inside the product.

2. **Idle timeout and absolute lifetime.** A session is tracked per device —
   the login plus its user agent — in `user_sessions`, with `started_at` taken
   from the last sign-in. A request past the idle timeout or the absolute
   lifetime ends that session (401) and marks the row revoked with the reason.
   A refresh token cannot revive it: a revoked device session is only replaced
   by a *new sign-in*, recognised by `users.last_login_at` being later than the
   revocation.

3. **Single-session revocation.** `POST /opsec/sessions/revoke` marks a row; the
   next request from that device is refused.

What it does not do: it does not read `X-Forwarded-For`. Behind a proxy the
address is the proxy's, and trusting a client-supplied header would let anyone
walk past the allowlist. Configure the proxy to pass the client address at the
ASGI server (`--proxy-headers` with a trusted-hosts list) instead.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.opsec import TenantSessionPolicy, UserSession
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.opsec import access

__all__ = [
    "PRIVILEGED_ROLES",
    "client_address",
    "device_key",
    "enforce_session_policy",
]

logger = logging.getLogger("askmira.opsec")

#: The roles an IP allowlist restricts: anybody who can reach other people's
#: records or payroll.
PRIVILEGED_ROLES: frozenset[UserRole] = frozenset(
    {UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.SUB_ADMIN}
)

#: `last_seen_at` is written at most this often per device. Writing it on
#: every request would turn every read into a write.
TOUCH_INTERVAL = timedelta(seconds=60)


def device_key(user_id: uuid.UUID, user_agent: str | None) -> str:
    """A stable, one-way key for one login on one browser or app."""
    return hashlib.sha256(f"askmira-session:{user_id}:{user_agent or ''}".encode()).hexdigest()


def client_address(request: Request) -> str | None:
    """The socket peer's address, validated. Never a forwarded header."""
    host = request.client.host if request.client else None
    if not host:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def _ended(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def enforce_session_policy(
    request: Request,
    session: AsyncSession,
    user: User,
    token_payload: dict[str, Any],
) -> None:
    policy_row = await session.scalar(
        select(TenantSessionPolicy).where(TenantSessionPolicy.tenant_id == user.tenant_id)
    )
    if policy_row is None:
        return
    await _enforce_allowlist(request, session, user, policy_row)
    await _enforce_session(request, session, user, policy_row, datetime.now(UTC))


async def _is_break_glass(session: AsyncSession, user: User) -> bool:
    tenant = await session.get(Tenant, user.tenant_id)
    designated = ((tenant.settings or {}).get("break_glass_email") if tenant else None) or ""
    return bool(designated) and user.email.lower() == str(designated).lower()


async def _enforce_allowlist(
    request: Request, session: AsyncSession, user: User, row: TenantSessionPolicy
) -> None:
    entries = tuple(row.ip_allowlist or ())
    if not (row.enforce_ip_allowlist_for_privileged and entries and user.role in PRIVILEGED_ROLES):
        return

    address = client_address(request)
    break_glass = await _is_break_glass(session, user)
    if address is None:
        if break_glass:
            logger.warning("Break-glass request with no readable address: user=%s", user.id)
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Your role can only be used from this workspace's allowed networks, "
                "and your network address could not be read."
            ),
        )

    decision = access.check_allowlist(
        address=address, entries=entries, break_glass_active=break_glass
    )
    if not decision.permits:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Your role can only be used from this workspace's allowed networks, and "
                f"{address} is not one of them. A workspace admin can add it, or use the "
                "break-glass account."
            ),
        )
    if decision.outcome is access.AllowlistOutcome.BREAK_GLASS:
        logger.warning(
            "Break-glass account used from outside the IP allowlist: user=%s address=%s",
            user.id,
            address,
        )


async def _enforce_session(
    request: Request,
    session: AsyncSession,
    user: User,
    row: TenantSessionPolicy,
    now: datetime,
) -> None:
    policy = access.SessionPolicy(
        idle_timeout_minutes=row.idle_timeout_minutes,
        absolute_lifetime_hours=row.absolute_lifetime_hours,
        max_concurrent_sessions=row.max_concurrent_sessions,
        enforce_ip_allowlist_for_privileged=row.enforce_ip_allowlist_for_privileged,
    )
    user_agent = request.headers.get("user-agent")
    key = device_key(user.id, user_agent)
    tracked = await session.scalar(
        select(UserSession)
        .where(
            UserSession.tenant_id == user.tenant_id,
            UserSession.user_id == user.id,
            UserSession.token_hash == key,
        )
        .order_by(UserSession.started_at.desc())
        .limit(1)
    )

    signed_in_at = user.last_login_at
    if tracked is not None and tracked.revoked_at is not None:
        if signed_in_at is not None and signed_in_at > tracked.revoked_at:
            tracked = None  # a fresh sign-in on this device replaces the ended session
        else:
            raise _ended(
                (tracked.revoked_reason or "This session was signed out.")
                + " Please sign in again."
            )

    is_new = tracked is None
    if tracked is None:
        # Evict before adding, so the new row cannot be counted against its own limit.
        await _evict_over_limit(session, user, policy, now)
        started = signed_in_at if signed_in_at is not None and signed_in_at <= now else now
        tracked = UserSession(
            tenant_id=user.tenant_id,
            user_id=user.id,
            token_hash=key,
            started_at=started,
            last_seen_at=now,
            ip_address=client_address(request),
            user_agent=(user_agent or "")[:512] or None,
        )
        session.add(tracked)

    state = access.evaluate_session(
        started_at=tracked.started_at,
        last_seen_at=tracked.last_seen_at,
        revoked_at=None,
        tokens_valid_from=None,
        policy=policy,
        now=now,
    )
    if state is access.SessionState.IDLE_EXPIRED:
        reason = f"Signed out after {policy.idle_timeout_minutes} minutes without activity."
    elif state is access.SessionState.LIFETIME_EXPIRED:
        reason = (
            f"Signed out: sessions in this workspace last at most "
            f"{policy.absolute_lifetime_hours} hours."
        )
    else:
        reason = None

    if reason is not None:
        tracked.revoked_at = now
        tracked.revoked_reason = reason[:160]
        await session.commit()
        raise _ended(f"{reason} Please sign in again.")

    if is_new or now - tracked.last_seen_at >= TOUCH_INTERVAL:
        tracked.last_seen_at = now
        tracked.ip_address = client_address(request)
        await session.commit()


async def _evict_over_limit(
    session: AsyncSession, user: User, policy: access.SessionPolicy, now: datetime
) -> None:
    """Oldest first, when the tenant caps concurrent sessions."""
    if policy.max_concurrent_sessions <= 0:
        return
    active = (
        await session.scalars(
            select(UserSession)
            .where(
                UserSession.tenant_id == user.tenant_id,
                UserSession.user_id == user.id,
                UserSession.revoked_at.is_(None),
            )
            .order_by(UserSession.started_at)
        )
    ).all()
    evict = access.sessions_to_evict(
        active_started_at=[r.started_at for r in active], policy=policy
    )
    for stale in active[:evict]:
        stale.revoked_at = now
        stale.revoked_reason = "Displaced by a newer sign-in under the concurrent session limit."
