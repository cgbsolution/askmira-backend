"""Operator access: a payroll bureau working inside its clients' workspaces (PAY-09).

═══════════════════════════════════════════════════════════════════════════════
THE CLIENT GRANTS, THE GRANT ESTABLISHES THE TENANT
═══════════════════════════════════════════════════════════════════════════════

Nothing here lets anyone pick a tenant. A client's admin grants one named
person at the bureau — identified by their own workspace and login — access to
payroll, until a date. The operator then sees their clients, and entering one
mints an access token whose `tid` is that client and whose `opg` is the grant.
`get_current_user` re-reads the grant on every request: revoked, expired, or
not this person's, and the request is refused. This file reads across tenants
for exactly two things — finding the operator's login when a grant is made, and
listing an operator's own grants — and both are keyed by something the client
or the operator already holds, never by a tenant id from a request.

═══════════════════════════════════════════════════════════════════════════════
WHAT AN OPERATOR MAY DO IN A CLIENT'S WORKSPACE
═══════════════════════════════════════════════════════════════════════════════

Payroll: runs, salaries, tax, statutory, filings, bank files, loans, gratuity,
contractors and the rest, read and write. Employee, leave and attendance
records: read only, because payroll needs them and does not own them.
Everything else — settings, users, hiring, culture, AI — is refused. They act
as an identity built for the request and never saved, so nothing they do can
write their own login's tenant; every audit row they cause is marked
`operator`, with the grant.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.areas import area_for_path
from app.core.deps import tenant_filter, tenant_scoped
from app.core.security import create_operator_token
from app.core.step_up import Confirm
from app.models.audit import AuditAction
from app.models.operators import OperatorGrant
from app.models.payroll import PayrollRun, PayrollRunStatus
from app.models.payroll_inputs import AdjustmentStatus, PayrollAdjustment
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services import audit

__all__ = [
    "GrantIn",
    "acting_identity",
    "create_grant",
    "enter",
    "list_grants",
    "my_clients",
    "operator_may",
    "revoke_grant",
]

MAX_GRANT_DAYS = 366
SCOPES = ("payroll",)

#: First path segments an operator may read and write: the payroll module's own.
_PAYROLL_SEGMENTS = frozenset(
    {
        "arrears",
        "benefits",
        "contractors",
        "disbursement",
        "encashment",
        "esop",
        "ewa",
        "filings",
        "gratuity",
        "loans",
        "pay-grades",
        "payroll",
        "reimbursements",
        "salaries",
        "salary-components",
        "salary-structures",
        "statutory",
        "statutory-notices",
        "tax",
    }
)
#: Read only: the records payroll needs and does not own.
_READ_AREAS = frozenset({"people", "time"})
#: Read only, outside every area: the portal's own frame.
_READ_SEGMENTS = frozenset({"auth", "approvals", "notifications", "operator"})
#: The few writes outside payroll an operator session needs to function.
_ALWAYS = frozenset({("POST", "auth/logout"), ("POST", "operator/exit")})
_OPERATOR_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN, UserRole.SUPER_ADMIN)


def _trim(path: str) -> str:
    trimmed = path.split("?", 1)[0]
    if "/api/v1/" in trimmed:
        trimmed = trimmed.split("/api/v1/", 1)[1]
    return trimmed.strip("/")


def operator_may(method: str, path: str) -> bool:
    """Whether a request is inside what operator access covers."""
    method = method.upper()
    trimmed = _trim(path)
    if (method, trimmed) in _ALWAYS:
        return True
    parts = trimmed.split("/")
    first = parts[0]
    # A salary is set and read on its employee's path.
    if first == "employees" and len(parts) >= 3 and parts[2] == "salary":
        return True
    if first in _PAYROLL_SEGMENTS:
        return True
    if method not in ("GET", "HEAD"):
        return False
    return area_for_path(path) in _READ_AREAS or first in _READ_SEGMENTS


def _unauthorised(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _live(grant: OperatorGrant, now: datetime) -> bool:
    return grant.revoked_at is None and grant.expires_at > now


async def acting_identity(
    session: AsyncSession, request: Request, operator: User, payload: dict[str, Any]
) -> User:
    """Who an operator token acts as, re-checked against its grant on every request."""
    try:
        grant_id = uuid.UUID(str(payload.get("opg")))
    except ValueError as exc:
        raise _unauthorised("This operator session is not valid.") from exc
    # The grant id from a signed token establishes the tenant; there is no
    # tenant to scope by before it is read.
    grant = await session.scalar(select(OperatorGrant).where(OperatorGrant.id == grant_id))
    now = datetime.now(UTC)
    if (
        grant is None
        or grant.operator_user_id != operator.id
        or str(grant.tenant_id) != str(payload.get("tid"))
        or not _live(grant, now)
    ):
        raise _unauthorised(
            "Your access to this workspace has ended. Sign in again to return to your own."
        )
    client = await session.get(Tenant, grant.tenant_id)
    if client is None or not client.is_active:
        raise _unauthorised("This workspace is not available.")
    if not operator_may(request.method, request.url.path):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Operator access to this workspace covers payroll, and reading the "
                "employee, leave and attendance records it needs. This is outside it."
            ),
        )

    acting = User(
        id=operator.id,
        tenant_id=grant.tenant_id,
        email=operator.email,
        full_name=f"{operator.full_name} ({grant.operator_firm})",
        role=UserRole.SUB_ADMIN,
        is_active=True,
        password_hash="!",
        employee_id=None,
        last_login_at=operator.last_login_at,
        tokens_valid_from=operator.tokens_valid_from,
    )
    acting.operator_grant_id = grant.id
    acting.operator_home_tenant_id = operator.tenant_id
    return acting


# ── The client's side ───────────────────────────────────────────────────────


class GrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The operator firm's workspace, as its sign-in address shows it.
    operator_workspace: str = Field(min_length=2, max_length=120)
    operator_email: str = Field(min_length=3, max_length=320)
    expires_on: date
    note: str | None = Field(default=None, max_length=1000)


def _grant_read(grant: OperatorGrant, now: datetime) -> dict[str, Any]:
    return {
        "id": str(grant.id),
        "operator_email": grant.operator_email,
        "operator_firm": grant.operator_firm,
        "scopes": list(grant.scopes or []),
        "note": grant.note,
        "expires_at": grant.expires_at.isoformat(),
        "revoked_at": grant.revoked_at.isoformat() if grant.revoked_at else None,
        "last_entered_at": grant.last_entered_at.isoformat() if grant.last_entered_at else None,
        "is_live": _live(grant, now),
    }


def _require_admin(user: User) -> None:
    if getattr(user, "operator_grant_id", None) is not None or not permissions.is_admin(user):
        raise permissions.forbidden("Only this workspace's own admin manages operator access.")


async def list_grants(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    _require_admin(user)
    rows = (
        await session.scalars(
            tenant_scoped(
                select(OperatorGrant).order_by(OperatorGrant.created_at.desc()),
                OperatorGrant,
                user,
            )
        )
    ).all()
    now = datetime.now(UTC)
    return [_grant_read(row, now) for row in rows]


async def create_grant(
    session: AsyncSession,
    user: User,
    payload: GrantIn,
    *,
    confirm: Confirm | None,
    request: Request | None,
) -> dict[str, Any]:
    _require_admin(user)
    today = datetime.now(UTC).date()
    if payload.expires_on <= today:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Access has to end on a day after today.",
        )
    if payload.expires_on > today + timedelta(days=MAX_GRANT_DAYS):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Grant access for at most a year at a time; it can be granted again.",
        )

    # The operator firm's workspace and login, found by what the client was
    # told. The only cross-tenant read a client's admin can cause.
    firm = await session.scalar(
        select(Tenant).where(func.lower(Tenant.slug) == payload.operator_workspace.strip().lower())
    )
    if firm is None or not firm.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active AskMira workspace has that address.",
        )
    if firm.id == user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Operator access is for people outside this workspace.",
        )
    operator = await session.scalar(
        select(User).where(
            tenant_filter(User, firm.id),
            func.lower(User.email) == payload.operator_email.strip().lower(),
            User.is_active.is_(True),
        )
    )
    if operator is None or operator.role not in _OPERATOR_ROLES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Nobody with that email administers that workspace. The operator has to be "
                "an admin in their own firm's workspace."
            ),
        )

    now = datetime.now(UTC)
    existing = (
        await session.scalars(
            tenant_scoped(
                select(OperatorGrant).where(OperatorGrant.operator_user_id == operator.id),
                OperatorGrant,
                user,
            )
        )
    ).all()
    if any(_live(row, now) for row in existing):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That person already has access. Revoke it first to change the end date.",
        )

    if confirm is not None:
        await confirm()

    grant = OperatorGrant(
        tenant_id=user.tenant_id,
        operator_user_id=operator.id,
        operator_email=operator.email,
        operator_firm=firm.name,
        scopes=list(SCOPES),
        note=(payload.note or "").strip() or None,
        expires_at=datetime.combine(payload.expires_on, time.max, tzinfo=UTC),
        granted_by_user_id=user.id,
    )
    session.add(grant)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="operator_grant",
        entity_id=grant.id,
        actor=user,
        changes={
            "operator_email": operator.email,
            "operator_firm": firm.name,
            "scopes": list(SCOPES),
            "expires_on": payload.expires_on.isoformat(),
        },
        summary=(
            f"Payroll access granted to {operator.email} at {firm.name} until {payload.expires_on}"
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(grant)
    return _grant_read(grant, now)


async def revoke_grant(
    session: AsyncSession, user: User, grant_id: uuid.UUID, *, request: Request | None
) -> dict[str, Any]:
    _require_admin(user)
    grant = await session.scalar(
        tenant_scoped(
            select(OperatorGrant).where(OperatorGrant.id == grant_id), OperatorGrant, user
        )
    )
    if grant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such grant.")
    now = datetime.now(UTC)
    if grant.revoked_at is None:
        grant.revoked_at = now
        grant.revoked_by_user_id = user.id
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.PERMISSION_CHANGE,
            entity_type="operator_grant",
            entity_id=grant.id,
            actor=user,
            summary=f"Payroll access for {grant.operator_email} revoked",
            request=request,
        )
        await session.commit()
        await session.refresh(grant)
    return _grant_read(grant, now)


# ── The operator's side ─────────────────────────────────────────────────────


async def _work_for(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, Any]:
    """What is waiting in one client's payroll, for the operator's queue."""
    today = datetime.now(UTC).date()
    runs = (
        await session.scalars(
            select(PayrollRun)
            .where(
                tenant_filter(PayrollRun, tenant_id),
                PayrollRun.period_year == today.year,
                PayrollRun.period_month == today.month,
                PayrollRun.status != PayrollRunStatus.CANCELLED,
            )
            .order_by(PayrollRun.created_at.desc())
        )
    ).all()
    open_runs = [
        r for r in runs if r.status not in (PayrollRunStatus.PAID, PayrollRunStatus.APPROVED)
    ]
    pending_adjustments = await session.scalar(
        select(func.count())
        .select_from(PayrollAdjustment)
        .where(
            tenant_filter(PayrollAdjustment, tenant_id),
            PayrollAdjustment.status == AdjustmentStatus.PENDING,
        )
    )
    return {
        "period": f"{today.year}-{today.month:02d}",
        "runs_this_month": [
            {"id": str(r.id), "status": r.status.value, "employees": r.employee_count} for r in runs
        ],
        "no_run_yet": not runs,
        "open_runs": len(open_runs),
        "pending_adjustments": int(pending_adjustments or 0),
    }


async def my_clients(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """The workspaces that granted this person access, and what each is waiting on.

    Read across tenants by the caller's own login id — the one key an operator
    holds — and only from a session in their own workspace.
    """
    if getattr(user, "operator_grant_id", None) is not None:
        raise permissions.forbidden("Return to your own workspace to see your clients.")
    now = datetime.now(UTC)
    grants = (
        await session.scalars(
            select(OperatorGrant)
            .where(
                OperatorGrant.operator_user_id == user.id,
                OperatorGrant.revoked_at.is_(None),
                OperatorGrant.expires_at > now,
            )
            .order_by(OperatorGrant.created_at)
        )
    ).all()
    out: list[dict[str, Any]] = []
    for grant in grants:
        client = await session.get(Tenant, grant.tenant_id)
        if client is None or not client.is_active:
            continue
        out.append(
            {
                "grant_id": str(grant.id),
                "client": client.name,
                "client_workspace": client.slug,
                "scopes": list(grant.scopes or []),
                "expires_at": grant.expires_at.isoformat(),
                "work": await _work_for(session, grant.tenant_id),
            }
        )
    return out


async def enter(
    session: AsyncSession, user: User, grant_id: uuid.UUID, *, request: Request | None
) -> tuple[str, dict[str, Any]]:
    """An access token for one client's workspace, under its grant."""
    if getattr(user, "operator_grant_id", None) is not None:
        raise permissions.forbidden("Return to your own workspace before entering another.")
    now = datetime.now(UTC)
    grant = await session.scalar(
        select(OperatorGrant).where(
            OperatorGrant.id == grant_id, OperatorGrant.operator_user_id == user.id
        )
    )
    if grant is None or not _live(grant, now):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="You do not have access to that workspace.",
        )
    client = await session.get(Tenant, grant.tenant_id)
    if client is None or not client.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
    grant.last_entered_at = now
    await audit.record(
        session,
        tenant_id=grant.tenant_id,
        action=AuditAction.LOGIN,
        entity_type="operator_grant",
        entity_id=grant.id,
        actor_user_id=user.id,
        actor_type="operator",
        summary=f"{grant.operator_email} ({grant.operator_firm}) entered this workspace",
        request=request,
    )
    await session.commit()
    token = create_operator_token(
        user_id=str(user.id), tenant_id=str(grant.tenant_id), grant_id=str(grant.id)
    )
    return token, {"client": client.name, "grant_id": str(grant.id)}


async def session_state(session: AsyncSession, user: User) -> dict[str, Any]:
    """Whether this request is an operator inside a client's workspace."""
    grant_id = getattr(user, "operator_grant_id", None)
    if grant_id is None:
        return {"active": False}
    grant = await session.scalar(
        tenant_scoped(
            select(OperatorGrant).where(OperatorGrant.id == grant_id), OperatorGrant, user
        )
    )
    client = await session.get(Tenant, user.tenant_id)
    return {
        "active": True,
        "grant_id": str(grant_id),
        "client": client.name if client else None,
        "operator_firm": grant.operator_firm if grant else None,
        "expires_at": grant.expires_at.isoformat() if grant else None,
    }
