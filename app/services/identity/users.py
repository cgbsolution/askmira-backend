"""Logins, roles, modules and company settings, as a workspace admin manages them.

The escalation rules, all enforced here and all audited as PERMISSION_CHANGE:

- **Nobody changes their own role**, and nobody deactivates themselves.
- **Nobody grants `super_admin`.** It is ours, not a customer's, and no path
  in the product creates one.
- **Only an admin manages logins.** The routes are admin-only, so a sub-admin
  can neither grant a role nor demote the people above them.
- A super admin's login is never managed from inside a workspace.
- A role change or deactivation bumps `tokens_valid_from`, so the person's
  existing sessions end at once instead of carrying the old role until expiry.
- The designated break-glass account cannot be demoted or deactivated while it
  is designated; a workspace whose SSO fails must still have a way in.
- The last active admin cannot be demoted, deactivated or deleted.
- A deleted login keeps its row, for the audit trail, but leaves the list for
  good: its email is moved aside so the address can be given a login again,
  here or in another workspace, and its employee is unlinked so that person
  can be given a new one.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.core.security import hash_password
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.platform_admin import (
    EDITABLE_TENANT_SETTINGS,
    MODULE_CATALOGUE,
    BulkLoginCreate,
    BulkLoginOutcome,
    BulkLoginResult,
    ManagedUserCreate,
    ManagedUserCreated,
    ManagedUserRead,
    TenantSettingsUpdate,
    WelcomeEmailRead,
)
from app.services import audit
from app.services.auth import RELEASED_EMAIL_DOMAIN, released_email
from app.services.identity.welcome import WelcomeEmail, send_welcome

__all__ = [
    "create_logins_for_employees",
    "create_user",
    "delete_login",
    "end_logins_for_employee",
    "follow_work_email",
    "list_users",
    "module_state",
    "resend_welcome",
    "set_active",
    "set_modules",
    "set_role",
    "update_tenant",
]


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


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
            "invite_pending": target.must_change_password,
            "is_you": target.id == caller.id,
            "is_break_glass": bool(_break_glass_email(tenant))
            and target.email.lower() == _break_glass_email(tenant),
        }
    )


def _is_deleted(target: User) -> bool:
    return target.email.endswith(f"@{RELEASED_EMAIL_DOMAIN}")


async def _target(session: AsyncSession, user: User, user_id: uuid.UUID) -> User:
    target = await session.scalar(tenant_scoped(select(User).where(User.id == user_id), User, user))
    if target is None or _is_deleted(target):
        # Another tenant's login is not found, not forbidden.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target.role is UserRole.SUPER_ADMIN:
        raise _forbidden("A super admin's login is not managed from a workspace.")
    return target


async def _active_admins(session: AsyncSession, user: User) -> int:
    return int(
        await session.scalar(
            tenant_scoped(
                select(func.count(User.id)).where(
                    User.role == UserRole.ADMIN, User.is_active.is_(True)
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
                .where(
                    User.role != UserRole.SUPER_ADMIN,
                    ~User.email.endswith(f"@{RELEASED_EMAIL_DOMAIN}"),
                )
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
    commit: bool = True,
) -> ManagedUserCreated:
    """Create a login, optionally linked to an employee, with a one-time password.

    `commit=False` for a caller that creates the employee in the same transaction.
    """
    tenant = await _tenant(session, user)
    _check_grant(payload.role)

    email = payload.email.lower()
    # One person, one login across the platform: sign-in finds a person by
    # email alone, so the same address in two workspaces would be ambiguous.
    from app.services.auth import email_in_use

    if await email_in_use(session, email):
        raise _bad_request(
            "That email already signs in to AskMira. Each person has one login; use "
            "another address."
        )

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
        must_change_password=True,
        employee_id=emp.id if emp else None,
    )
    session.add(target)
    await session.flush()

    welcome = None
    if payload.send_welcome_email:
        welcome = await send_welcome(
            session, target=target, inviter_name=user.full_name, request=request
        )
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
            "welcome_email": welcome.status if welcome else "not_sent",
        },
        summary=f"Created a {payload.role.value} login for {email}",
        request=request,
    )
    if commit:
        await session.commit()
    await session.refresh(target)
    return ManagedUserCreated(
        user=_read(target, user, tenant, emp),
        temporary_password=temporary,
        welcome_email=_welcome_read(welcome) if welcome else None,
    )


#: Roles a bulk request may give. Admin rights go to one person at a time.
BULK_ROLES = frozenset({UserRole.EMPLOYEE, UserRole.SUB_ADMIN})


async def create_logins_for_employees(
    session: AsyncSession,
    user: User,
    payload: BulkLoginCreate,
    *,
    request: Request | None = None,
) -> BulkLoginResult:
    """Logins for many employees at once, each at the work email on their record.

    The login `create_user` makes: the chosen role, a one-time password they
    change at first sign-in, linked to their record. Made for every selected
    person who can have one; the rest come back with the reason (they have
    left, already have a login, have no work email, or that email already signs
    in). Every login and its audit row commit together, or none do.

    Welcome emails are queued for the delivery job rather than sent while the
    admin waits: a hundred of them through a mail relay would outlast the
    request.
    """
    from app.services.auth import email_in_use

    if payload.role not in BULK_ROLES:
        raise _bad_request(
            "Logins made in bulk are employee or HR sub-admin logins. Give admin rights one "
            "person at a time, under Settings → Users & roles."
        )

    ids = list(dict.fromkeys(payload.employee_ids))
    employees = {
        emp.id: emp
        for emp in (
            await session.scalars(
                tenant_scoped(select(Employee).where(Employee.id.in_(ids)), Employee, user)
            )
        ).all()
    }
    linked = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(User.employee_id).where(User.employee_id.in_(ids)), User, user
                )
            )
        ).all()
    )

    outcomes: list[BulkLoginOutcome] = []
    claimed: set[str] = set()
    for employee_id in ids:
        emp = employees.get(employee_id)
        if emp is None:
            outcomes.append(
                BulkLoginOutcome(
                    employee_id=employee_id, created=False, reason="Not found in this workspace."
                )
            )
            continue
        email = (emp.work_email or "").strip().lower()
        outcome = BulkLoginOutcome(
            employee_id=emp.id,
            employee_name=emp.full_name,
            employee_code=emp.employee_code,
            email=email or None,
            created=False,
        )
        reason = None
        if emp.status is EmploymentStatus.EXITED:
            reason = "Has left the company."
        elif emp.id in linked:
            reason = "Already has a login."
        elif "@" not in email:
            reason = "No work email on their record. Add one, then try again."
        elif email in claimed:
            reason = "Another selected employee has the same work email."
        elif await email_in_use(session, email):
            reason = "This email already signs in to AskMira, and each person has one login."
        if reason:
            outcomes.append(outcome.model_copy(update={"reason": reason}))
            continue
        claimed.add(email)

        temporary = secrets.token_urlsafe(12) + "-Aa1"
        # Argon2 is slow on purpose. Off the event loop, a hundred hashes do not
        # stall every other request this server is answering.
        password_hash = await asyncio.to_thread(hash_password, temporary)
        target = User(
            tenant_id=user.tenant_id,
            email=email,
            full_name=emp.full_name,
            role=payload.role,
            is_active=True,
            password_hash=password_hash,
            must_change_password=True,
            employee_id=emp.id,
        )
        session.add(target)
        await session.flush()

        welcome = None
        if payload.send_welcome_email:
            welcome = await send_welcome(
                session,
                target=target,
                inviter_name=user.full_name,
                request=request,
                deliver=False,
            )
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.PERMISSION_CHANGE,
            entity_type="user",
            entity_id=target.id,
            actor=user,
            changes={
                "created": True,
                "bulk": True,
                "email": email,
                "role": payload.role.value,
                "employee_id": str(emp.id),
                "welcome_email": welcome.status if welcome else "not_sent",
            },
            summary=f"Created a {payload.role.value} login for {email}",
            request=request,
        )
        outcomes.append(
            outcome.model_copy(
                update={
                    "created": True,
                    "user_id": target.id,
                    "temporary_password": temporary,
                    "welcome_email": _welcome_read(welcome) if welcome else None,
                }
            )
        )

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A login for one of these people was created at the same moment elsewhere. "
                "Nothing was created; try again."
            ),
        ) from exc
    created = sum(1 for outcome in outcomes if outcome.created)
    return BulkLoginResult(created=created, skipped=len(outcomes) - created, results=outcomes)


def _welcome_read(welcome: WelcomeEmail) -> WelcomeEmailRead:
    return WelcomeEmailRead(
        status=welcome.status,
        detail=welcome.detail,
        sent_to=welcome.sent_to,
        link_expires_at=welcome.link_expires_at,
    )


async def resend_welcome(
    session: AsyncSession,
    user: User,
    *,
    user_id: uuid.UUID,
    request: Request | None = None,
) -> WelcomeEmailRead:
    """Send a login's welcome email again, with a fresh link.

    Only while they are still on the password they were given: somebody who
    has chosen their own uses Forgot password, not a welcome. The new link
    retires the old one.
    """
    target = await _target(session, user, user_id)
    if not target.is_active:
        raise _bad_request("This login is deactivated. Reactivate it first.")
    if not target.must_change_password:
        raise _bad_request(
            "They have already set their own password. If they have forgotten it, they can "
            "use Forgot password on the sign-in page."
        )
    welcome = await send_welcome(
        session, target=target, inviter_name=user.full_name, request=request
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="user",
        entity_id=target.id,
        actor=user,
        changes={"welcome_email": welcome.status},
        summary=f"Resent the welcome email to {target.email}",
        request=request,
    )
    await session.commit()
    return _welcome_read(welcome)


async def link_employee(
    session: AsyncSession,
    user: User,
    *,
    user_id: uuid.UUID,
    employee_id: uuid.UUID | None,
    reason: str,
    request: Request | None = None,
) -> ManagedUserRead:
    """Link a login to an employee record, move it to another, or unlink it.

    One login per employee, and one employee per login: self-service,
    approvals and payslips all resolve "me" through this link.
    """
    tenant = await _tenant(session, user)
    target = await _target(session, user, user_id)
    emp = None
    if employee_id is not None:
        emp = await session.scalar(
            tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
        )
        if emp is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
        linked = await session.scalar(
            tenant_scoped(
                select(User.id).where(User.employee_id == emp.id, User.id != target.id),
                User,
                user,
            )
        )
        if linked is not None:
            raise _bad_request("This employee already has a login. Unlink that one first.")

    before = str(target.employee_id) if target.employee_id else None
    target.employee_id = emp.id if emp else None
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="user",
        entity_id=target.id,
        actor=user,
        changes={
            "employee_id": {"from": before, "to": str(emp.id) if emp else None},
            "reason": reason,
        },
        summary=(
            f"Linked {target.email} to employee {emp.employee_code}"
            if emp
            else f"Unlinked {target.email} from its employee record"
        ),
        request=request,
    )
    await session.flush()
    await session.commit()
    await session.refresh(target)
    return _read(target, user, tenant, emp)


async def issue_temporary_password(
    session: AsyncSession,
    user: User,
    *,
    user_id: uuid.UUID,
    reason: str,
    request: Request | None = None,
) -> ManagedUserCreated:
    """Replace somebody's password with a one-time one they must change at sign-in.

    Every session they have ends. Not for your own login — use change password.
    """
    tenant = await _tenant(session, user)
    target = await _target(session, user, user_id)
    if target.id == user.id:
        raise _forbidden("Change your own password from your profile instead.")
    temporary = secrets.token_urlsafe(12) + "-Aa1"
    target.password_hash = hash_password(temporary)
    target.must_change_password = True
    target.tokens_valid_from = datetime.now(UTC)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="user",
        entity_id=target.id,
        actor=user,
        changes={"password_reset": True, "reason": reason},
        summary=f"Issued a temporary password for {target.email}; their sessions were ended",
        request=request,
    )
    await session.commit()
    await session.refresh(target)
    emp = None
    if target.employee_id is not None:
        emp = await session.get(Employee, target.employee_id)
    return ManagedUserCreated(user=_read(target, user, tenant, emp), temporary_password=temporary)


def _check_grant(role: UserRole) -> None:
    if role is UserRole.SUPER_ADMIN:
        raise _forbidden("The super admin role cannot be granted from a workspace.")


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
    _check_grant(role)
    if target.role is role:
        raise _bad_request(f"This person is already {role.value}.")

    tenant = await _tenant(session, user)
    if target.role is UserRole.ADMIN and await _active_admins(session, user) <= 1:
        raise _bad_request("This is the last admin; the workspace needs one.")
    if target.email.lower() == _break_glass_email(tenant) and role is not UserRole.ADMIN:
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
    if target.is_active is active:
        raise _bad_request(f"This login is already {'active' if active else 'deactivated'}.")

    tenant = await _tenant(session, user)
    if not active:
        if target.email.lower() == _break_glass_email(tenant):
            raise _bad_request(
                "This is the break-glass account. Designate another one before deactivating it."
            )
        if target.role is UserRole.ADMIN and await _active_admins(session, user) <= 1:
            raise _bad_request("This is the last active admin; the workspace needs one.")

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


async def _check_can_close(session: AsyncSession, user: User, target: User) -> None:
    """The guards deactivation applies, for anything that ends a login for good."""
    tenant = await _tenant(session, user)
    if target.email.lower() == _break_glass_email(tenant):
        raise _bad_request(
            "This is the break-glass account. Designate another one before removing it."
        )
    if (
        target.role is UserRole.ADMIN
        and target.is_active
        and await _active_admins(session, user) <= 1
    ):
        raise _bad_request("This is the last active admin; the workspace needs one.")


async def delete_login(
    session: AsyncSession,
    user: User,
    *,
    user_id: uuid.UUID,
    reason: str,
    request: Request | None = None,
) -> None:
    """Delete a login: it never signs in again, and its email is free.

    For a login made with the wrong address, or for somebody who has gone. The
    row stays because the audit trail points at it; everything else about it
    ends here (see the module docstring).
    """
    if user_id == user.id:
        raise _forbidden("You cannot delete your own login.")
    target = await _target(session, user, user_id)
    await _check_can_close(session, user, target)

    email, employee_id = target.email, target.employee_id
    target.is_active = False
    target.tokens_valid_from = datetime.now(UTC)
    target.email = released_email(target.id)
    target.employee_id = None
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="user",
        entity_id=target.id,
        actor=user,
        changes={
            "email": email,
            "employee_id": str(employee_id) if employee_id else None,
            "reason": reason,
        },
        summary=f"Deleted the login for {email}; the address can be used again",
        request=request,
    )
    await session.commit()


async def end_logins_for_employee(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> list[str]:
    """Switch off the logins of an employee being deactivated, in the caller's
    transaction. A person who has left the directory must not still sign in.

    Returns the emails switched off, for the caller's audit row.
    """
    logins = (
        await session.scalars(
            tenant_scoped(
                select(User).where(
                    User.employee_id == employee_id,
                    User.is_active.is_(True),
                    User.role != UserRole.SUPER_ADMIN,
                ),
                User,
                user,
            )
        )
    ).all()
    now = datetime.now(UTC)
    for login in logins:
        await _check_can_close(session, user, login)
        login.is_active = False
        login.tokens_valid_from = now
    return [login.email for login in logins]


async def follow_work_email(
    session: AsyncSession,
    user: User,
    employee: Employee,
    *,
    old: str,
    new: str,
    request: Request | None = None,
) -> str | None:
    """Move an employee's login to their new work email, in the caller's transaction.

    Only a login that was on the old work email moves; one deliberately kept on
    another address (an admin signing in with a personal one) is left alone.
    The password, role and sessions stay: it is the same person. Any
    set-password link still outstanding went to the old address, so it is
    retired; a welcome can be resent to the new one.

    Only an admin may move a login: a sub-admin who could point somebody's
    login at an inbox of their own could then take it over with Forgot password.

    Returns the login's previous email when it moved.
    """
    from app.services.auth import email_in_use, retire_password_links

    new = new.strip().lower()
    if new == old.strip().lower():
        return None
    login = await session.scalar(
        tenant_scoped(
            select(User).where(
                User.employee_id == employee.id,
                User.role != UserRole.SUPER_ADMIN,
                func.lower(User.email) == old.strip().lower(),
            ),
            User,
            user,
        )
    )
    if login is None:
        return None
    if user.role not in (UserRole.ADMIN, UserRole.SUPER_ADMIN):
        raise _forbidden(
            f"{employee.first_name} signs in with {login.email}, so changing this email moves "
            "their login too. Only an admin can do that; ask an admin to change it."
        )
    if await email_in_use(session, new):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{new} already signs in to AskMira as another login, and each person has one "
                "login. Use a different email, or delete that login first."
            ),
        )

    previous = login.email
    tenant = await _tenant(session, user)
    break_glass = previous.lower() == _break_glass_email(tenant)
    if break_glass:
        tenant.settings = {**(tenant.settings or {}), "break_glass_email": new}
    login.email = new
    login.is_email_verified = False
    await retire_password_links(session, login)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="user",
        entity_id=login.id,
        actor=user,
        changes={
            "email": {"from": previous, "to": new},
            "reason": f"Work email of {employee.employee_code} changed",
            **({"break_glass_email": {"from": previous, "to": new}} if break_glass else {}),
        },
        summary=f"Login email moved from {previous} to {new} with the work email",
        request=request,
    )
    return previous


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
