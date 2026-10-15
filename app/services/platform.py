"""The platform console: the super admin creates and runs every workspace.

═══════════════════════════════════════════════════════════════════════════════
NOBODY ELSE CAN MAKE A WORKSPACE
═══════════════════════════════════════════════════════════════════════════════

There is no public sign-up. A workspace exists because the platform's super
admin created it, with its first admin. Everything inside it — departments,
people, roles, payroll — is then that admin's to set up, and the super admin
has no way into it: this console lists workspaces and their admins' logins,
and reads nothing else of theirs.

Listing every workspace is this file's job, so its queries are not scoped to a
tenant. Every entry point refuses anyone but a super admin signed in to their
own workspace, and every action is written to the audit trail of the workspace
it touched.

A new admin gets a temporary password, shown once to the super admin, and has
to choose their own at first sign-in. Nothing is emailed from here.
"""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import func, select

from app.core import permissions
from app.core.deps import tenant_filter
from app.core.security import hash_password
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services import audit
from app.services.auth import create_workspace, email_in_use

__all__ = [
    "AdminIn",
    "WorkspaceIn",
    "add_admin",
    "create",
    "list_workspaces",
    "reset_admin_password",
    "set_active",
]

_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class WorkspaceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str = Field(min_length=2, max_length=255)
    #: Optional; made from the company name when left out.
    workspace: str | None = Field(default=None, max_length=64)
    admin_full_name: str = Field(min_length=2, max_length=255)
    admin_email: EmailStr
    admin_phone: str | None = Field(default=None, max_length=32)


class AdminIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=32)


def _require_super(user: User) -> None:
    if user.role is not UserRole.SUPER_ADMIN or getattr(user, "operator_grant_id", None):
        raise permissions.forbidden("Only the platform's super admin manages workspaces.")


def _temporary_password() -> str:
    # Meets the password policy: letters, a digit, length.
    return secrets.token_urlsafe(12) + "-Aa1"


async def _workspace(session: Any, tenant_id: uuid.UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such workspace.")
    return tenant


def _admin_read(row: User) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "full_name": row.full_name,
        "email": row.email,
        "is_active": row.is_active,
        "must_change_password": bool(row.must_change_password),
        "last_login_at": row.last_login_at.isoformat() if row.last_login_at else None,
    }


async def list_workspaces(session: Any, user: User) -> list[dict[str, Any]]:
    """Every workspace, with its admins and how big it is. Nothing from inside it."""
    _require_super(user)
    tenants = (await session.scalars(select(Tenant).order_by(Tenant.created_at))).all()
    out: list[dict[str, Any]] = []
    for tenant in tenants:
        admins = (
            await session.scalars(
                select(User)
                .where(tenant_filter(User, tenant.id), User.role == UserRole.ADMIN)
                .order_by(User.created_at)
            )
        ).all()
        logins = await session.scalar(
            select(func.count(User.id)).where(
                tenant_filter(User, tenant.id), User.is_active.is_(True)
            )
        )
        employees = await session.scalar(
            select(func.count(Employee.id)).where(
                tenant_filter(Employee, tenant.id), Employee.deleted_at.is_(None)
            )
        )
        last_sign_in = await session.scalar(
            select(func.max(User.last_login_at)).where(tenant_filter(User, tenant.id))
        )
        out.append(
            {
                "id": str(tenant.id),
                "name": tenant.name,
                "workspace": tenant.slug,
                "is_active": tenant.is_active,
                "is_own": tenant.id == user.tenant_id,
                "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
                "active_logins": int(logins or 0),
                "employees": int(employees or 0),
                "last_sign_in": last_sign_in.isoformat() if last_sign_in else None,
                "modules_on": sorted(k for k, on in (tenant.enabled_modules or {}).items() if on),
                "admins": [_admin_read(a) for a in admins],
            }
        )
    return out


async def create(
    session: Any, user: User, payload: WorkspaceIn, *, request: Request | None
) -> dict[str, Any]:
    """A new workspace and its first admin; the temporary password comes back once."""
    _require_super(user)
    slug = None
    if payload.workspace:
        slug = payload.workspace.strip().lower()
        if not _SLUG.match(slug):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A workspace address uses lowercase letters, digits and hyphens.",
            )
        taken = await session.scalar(select(Tenant.id).where(Tenant.slug == slug))
        if taken is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Another workspace already has that address.",
            )

    temporary = _temporary_password()
    tenant, admin = await create_workspace(
        session,
        company_name=payload.company_name,
        admin_full_name=payload.admin_full_name,
        admin_email=str(payload.admin_email),
        admin_phone=payload.admin_phone,
        password_hash=hash_password(temporary),
        slug=slug,
        must_change_password=True,
    )
    await audit.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.CREATE,
        entity_type="tenant",
        entity_id=tenant.id,
        actor_user_id=user.id,
        actor_type="platform",
        summary=f"Workspace {tenant.name} created with admin {admin.email} by the platform",
        request=request,
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="platform_workspace",
        entity_id=tenant.id,
        actor=user,
        summary=f"Created workspace {tenant.name} ({tenant.slug}) with admin {admin.email}",
        request=request,
    )
    await session.commit()
    return {
        "workspace": {"id": str(tenant.id), "name": tenant.name, "workspace": tenant.slug},
        "admin": {"email": admin.email, "temporary_password": temporary},
        "note": (
            "Give the admin this password yourself. It is shown once, and they choose "
            "their own at first sign-in."
        ),
    }


async def add_admin(
    session: Any, user: User, tenant_id: uuid.UUID, payload: AdminIn, *, request: Request | None
) -> dict[str, Any]:
    """Another admin for a workspace — when its only admin has left, for instance."""
    _require_super(user)
    tenant = await _workspace(session, tenant_id)
    email = str(payload.email).strip().lower()
    if await email_in_use(session, email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That email already signs in to AskMira. Each person has one login.",
        )
    temporary = _temporary_password()
    admin = User(
        tenant_id=tenant.id,
        email=email,
        password_hash=hash_password(temporary),
        full_name=payload.full_name.strip(),
        phone=payload.phone,
        role=UserRole.ADMIN,
        is_active=True,
        is_email_verified=False,
        must_change_password=True,
    )
    session.add(admin)
    await session.flush()
    await audit.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="user",
        entity_id=admin.id,
        actor_user_id=user.id,
        actor_type="platform",
        summary=f"Admin {admin.email} added by the platform",
        request=request,
    )
    await session.commit()
    return {
        "admin": {"email": admin.email, "temporary_password": temporary},
        "note": "Shown once. Their admin can link this login to an employee record.",
    }


async def reset_admin_password(
    session: Any,
    user: User,
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    *,
    request: Request | None,
) -> dict[str, Any]:
    """A temporary password for a workspace admin who is locked out."""
    _require_super(user)
    tenant = await _workspace(session, tenant_id)
    admin = await session.scalar(
        select(User).where(
            tenant_filter(User, tenant.id), User.id == admin_id, User.role == UserRole.ADMIN
        )
    )
    if admin is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such admin.")
    temporary = _temporary_password()
    admin.password_hash = hash_password(temporary)
    admin.must_change_password = True
    # Every session they had ends now.
    admin.tokens_valid_from = datetime.now(UTC)
    await audit.record(
        session,
        tenant_id=tenant.id,
        action=AuditAction.UPDATE,
        entity_type="user",
        entity_id=admin.id,
        actor_user_id=user.id,
        actor_type="platform",
        summary=f"Temporary password issued to admin {admin.email} by the platform",
        request=request,
    )
    await session.commit()
    return {
        "admin": {"email": admin.email, "temporary_password": temporary},
        "note": "Shown once. Their sessions were ended; they choose a new password at sign-in.",
    }


async def set_active(
    session: Any,
    user: User,
    tenant_id: uuid.UUID,
    *,
    active: bool,
    reason: str,
    request: Request | None,
) -> dict[str, Any]:
    """Suspend or reactivate a workspace. Suspended, nobody in it can sign in or act."""
    _require_super(user)
    tenant = await _workspace(session, tenant_id)
    if tenant.id == user.tenant_id and not active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The platform's own workspace cannot be suspended from here.",
        )
    if not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Give a reason."
        )
    if tenant.is_active != active:
        tenant.is_active = active
        await audit.record(
            session,
            tenant_id=tenant.id,
            action=AuditAction.UPDATE,
            entity_type="tenant",
            entity_id=tenant.id,
            actor_user_id=user.id,
            actor_type="platform",
            summary=(
                f"Workspace {'reactivated' if active else 'suspended'} by the platform: "
                f"{reason.strip()}"
            ),
            request=request,
        )
        await session.commit()
    return {"id": str(tenant.id), "is_active": tenant.is_active}
