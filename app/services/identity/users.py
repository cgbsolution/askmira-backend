"""Logins, roles, modules and company settings, as a workspace admin manages them.

The escalation rules, all enforced here and all audited as PERMISSION_CHANGE:

- **Nobody changes their own role**, and nobody deactivates themselves.
- **Nobody grants `platform_admin`.** It is ours, not a customer's, and no path
  in the product creates one.
- **Only a tenant admin may grant `tenant_admin`**, and only a tenant admin may
  change or deactivate somebody who already is one — an HR admin cannot demote
  the people above them.
- A platform admin's login is never managed from inside a workspace.
- A role change or deactivation bumps `tokens_valid_from`, so the person's
  existing sessions end at once instead of carrying the old role until expiry.
- The designated break-glass account cannot be demoted or deactivated while it
  is designated; a workspace whose SSO fails must still have a way in.
- The last active tenant admin cannot be demoted or deactivated.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.core.security import hash_password
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.platform_admin import (
    EDITABLE_TENANT_SETTINGS,
    MODULE_CATALOGUE,
    ManagedUserCreate,
    ManagedUserCreated,
    ManagedUserRead,
    TenantSettingsUpdate,
)
from app.services import audit

__all__ = [
    "create_user",
    "list_users",
    "module_state",
    "set_active",
    "set_modules",
    "set_role",
    "update_tenant",
]


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _is_tenant_admin(user: User) -> bool:
    return user.role in (UserRole.TENANT_ADMIN, UserRole.PLATFORM_ADMIN)


async def _tenant(session: AsyncSession, user: User) -> Tenant:
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    return tenant


def _break_glass_email(tenant: Tenant) -> str:
    return str((tenant.settings or {}).get("break_glass_email") or "").lower()


def _read(target: User, caller: User, tenant: Tenant, emp: Employee | None) -> ManagedUserRead:
    name = None
    if emp is not None:
        name = emp.display_name or " ".join(p for p in (emp.first_name, emp.last_name) if p)
    return ManagedUserRead.model_validate(target).model_copy(
        update={
            "employee_name": name,
            "employee_code": emp.employee_code if emp else None,
            "is_you": target.id == caller.id,
            "is_break_glass": bool(_break_glass_email(tenant))
            and target.email.lower() == _break_glass_email(tenant),
        }
    )


async def _target(session: AsyncSession, user: User, user_id: uuid.UUID) -> User:
    target = await session.scalar(
        tenant_scoped(select(User).where(User.id == user_id), User, user)
    )
    if target is None:
        # Another tenant's login is not found, not forbidden.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target.role is UserRole.PLATFORM_ADMIN:
        raise _forbidden("A platform administrator's login is not managed from a workspace.")
    return target


async def _active_tenant_admins(session: AsyncSession, user: User) -> int:
    return int(
        await session.scalar(
            tenant_scoped(
                select(func.count(User.id)).where(
                    User.role == UserRole.TENANT_ADMIN, User.is_active.is_(True)
                ),
                User,
                user,
            )
        )
        or 0
    )


# ── Listing and creating ────────────────────────────────────────────────────


async def list_users(session: AsyncSession, user: User) -> list[ManagedUserRead]:
    tenant = await _tenant(session, user)
    rows = (
        await session.execute(
            tenant_scoped(
                select(User, Employee)
                .outerjoin(Employee, Employee.id == User.employee_id)
                .where(User.role != UserRole.PLATFORM_ADMIN)
                .order_by(User.full_name),
                User,
                user,
            )
        )
    ).all()
    return [_read(target, user, tenant, emp) for target, emp in rows]


async def create_user(
    session: AsyncSession,
    user: User,
    payload: ManagedUserCreate,
    *,
    request: Request | None = None,
) -> ManagedUserCreated:
    """Create a login, optionally linked to an employee, with a one-time password."""
    tenant = await _tenant(session, user)
    _check_grant(user, payload.role)

    email = payload.email.lower()
    clash = await session.scalar(
        tenant_scoped(select(User.id).where(func.lower(User.email) == email), User, user)
    )
    if clash is not None:
        raise _bad_request("A login with this email already exists in this workspace.")

    emp = None
    if payload.employee_id is not None:
        emp = await session.scalar(
            tenant_scoped(
                select(Employee).where(Employee.id == payload.employee_id), Employee, user
            )
        )
        if emp is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
        linked = await session.scalar(
            tenant_scoped(select(User.id).where(User.employee_id == emp.id), User, user)
        )
        if linked is not None:
            raise _bad_request("This employee already has a login.")

    temporary = secrets.token_urlsafe(12) + "-Aa1"
    target = User(
        tenant_id=user.tenant_id,
        email=email,
        full_name=payload.full_name.strip(),
        role=payload.role,
        is_active=True,
        password_hash=hash_password(temporary),
        employee_id=emp.id if emp else None,
    )
    session.add(target)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="user",
        entity_id=target.id,
        actor=user,
        changes={
            "created": True,
            "email": email,
            "role": payload.role.value,
            "employee_id": str(emp.id) if emp else None,
        },
        summary=f"Created a {payload.role.value} login for {email}",
        request=request,
    )
    await session.commit()
    await session.refresh(target)
    return ManagedUserCreated(
        user=_read(target, user, tenant, emp), temporary_password=temporary
    )


def _check_grant(user: User, role: UserRole) -> None:
    if role is UserRole.PLATFORM_ADMIN:
        raise _forbidden("The platform administrator role cannot be granted from a workspace.")
    if role is UserRole.TENANT_ADMIN and not _is_tenant_admin(user):
        raise _forbidden("Only a workspace owner (tenant admin) can grant that role.")


# ── Changing roles and access ───────────────────────────────────────────────


async def set_role(
    session: AsyncSession,
    user: User,
    *,
    user_id: uuid.UUID,
    role: UserRole,
    reason: str,
    request: Request | None = None,
) -> ManagedUserRead:
    if user_id == user.id:
        raise _forbidden("You cannot change your own role. Ask another administrator.")
    target = await _target(session, user, user_id)
    _check_grant(user, role)
    if target.role is UserRole.TENANT_ADMIN and not _is_tenant_admin(user):
        raise _forbidden("Only a tenant admin can change another tenant admin's role.")
    if target.role is role:
        raise _bad_request(f"This person is already {role.value}.")

    tenant = await _tenant(session, user)
    if target.role is UserRole.TENANT_ADMIN and await _active_tenant_admins(session, user) <= 1:
        raise _bad_request("This is the last tenant admin; the workspace needs one.")
    if target.email.lower() == _break_glass_email(tenant) and role not in (
        UserRole.TENANT_ADMIN,
        UserRole.HR_ADMIN,
    ):
        raise _bad_request(
            "This is the break-glass account and must stay an administrator. Designate "
            "another break-glass account first."
        )

    previous = target.role
    target.role = role
    target.tokens_valid_from = datetime.now(UTC)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="user",
        entity_id=target.id,
        actor=user,
        changes={"role": {"from": previous.value, "to": role.value}, "reason": reason},
        summary=f"Role of {target.email} changed from {previous.value} to {role.value}",
        request=request,
    )
    await session.commit()
    await session.refresh(target)
    emp = await session.get(Employee, target.employee_id) if target.employee_id else None
    return _read(target, user, tenant, emp)


async def set_active(
    session: AsyncSession,
    user: User,
    *,
    user_id: uuid.UUID,
    active: bool,
    reason: str,
    request: Request | None = None,
) -> ManagedUserRead:
    if user_id == user.id:
        raise _forbidden("You cannot deactivate or reactivate your own login.")
    target = await _target(session, user, user_id)
    if target.role is UserRole.TENANT_ADMIN and not _is_tenant_admin(user):
        raise _forbidden("Only a tenant admin can change another tenant admin's access.")
    if target.is_active is active:
        raise _bad_request(f"This login is already {'active' if active else 'deactivated'}.")

    tenant = await _tenant(session, user)
    if not active:
        if target.email.lower() == _break_glass_email(tenant):
            raise _bad_request(
                "This is the break-glass account. Designate another one before deactivating it."
            )
        if target.role is UserRole.TENANT_ADMIN and await _active_tenant_admins(session, user) <= 1:
            raise _bad_request("This is the last active tenant admin; the workspace needs one.")

    target.is_active = active
    target.tokens_valid_from = datetime.now(UTC)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="user",
        entity_id=target.id,
        actor=user,
        changes={"is_active": active, "reason": reason, "sessions_ended": True},
        summary=f"{'Reactivated' if active else 'Deactivated'} the login for {target.email}",
        request=request,
    )
    await session.commit()
    await session.refresh(target)
    emp = await session.get(Employee, target.employee_id) if target.employee_id else None
    return _read(target, user, tenant, emp)


# ── Modules ─────────────────────────────────────────────────────────────────


def _modules_dict(tenant: Tenant) -> dict[str, Any]:
    enabled = tenant.enabled_modules or {}
    return {
        "modules": [
            {"key": key, "description": description, "enabled": bool(enabled.get(key))}
            for key, description in MODULE_CATALOGUE.items()
        ]
    }


async def module_state(session: AsyncSession, user: User) -> dict[str, Any]:
    return _modules_dict(await _tenant(session, user))


async def set_modules(
    session: AsyncSession,
    user: User,
    *,
    modules: dict[str, bool],
    request: Request | None = None,
) -> dict[str, Any]:
    unknown = sorted(set(modules) - set(MODULE_CATALOGUE))
    if unknown:
        raise _bad_request(f"Unknown module(s): {', '.join(unknown)}.")
    tenant = await _tenant(session, user)
    before = dict(tenant.enabled_modules or {})
    after = {**before, **{key: bool(value) for key, value in modules.items()}}
    changed = {k: after[k] for k in modules if bool(before.get(k)) != after[k]}
    if not changed:
        return _modules_dict(tenant)
    # A new dict, so the JSONB column is flagged dirty.
    tenant.enabled_modules = after
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="tenant",
        entity_id=tenant.id,
        actor=user,
        changes={"enabled_modules": changed},
        summary="Modules changed: "
        + ", ".join(f"{k} {'on' if v else 'off'}" for k, v in sorted(changed.items())),
        request=request,
    )
    await session.commit()
    return _modules_dict(tenant)


# ── Company settings ────────────────────────────────────────────────────────


async def update_tenant(
    session: AsyncSession,
    user: User,
    payload: TenantSettingsUpdate,
    *,
    request: Request | None = None,
) -> Tenant:
    tenant = await _tenant(session, user)
    changes: dict[str, Any] = {}
    if payload.name is not None and payload.name.strip() != tenant.name:
        changes["name"] = {"from": tenant.name, "to": payload.name.strip()}
        tenant.name = payload.name.strip()
    if payload.timezone is not None:
        try:
            ZoneInfo(payload.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise _bad_request(f"{payload.timezone!r} is not a recognised timezone.") from exc

    settings = dict(tenant.settings or {})
    for key in EDITABLE_TENANT_SETTINGS:
        value = getattr(payload, key)
        if value is not None and settings.get(key) != value:
            changes[key] = {"from": settings.get(key), "to": value}
            settings[key] = value
    if not changes:
        return tenant
    tenant.settings = settings
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="tenant",
        entity_id=tenant.id,
        actor=user,
        changes=changes,
        summary="Company settings updated",
        request=request,
    )
    await session.commit()
    await session.refresh(tenant)
    return tenant
