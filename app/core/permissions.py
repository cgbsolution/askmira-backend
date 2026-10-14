"""Who may act on whose records, inside one tenant.

═══════════════════════════════════════════════════════════════════════════════
TENANT ISOLATION IS NOT THE SAME RULE AS THIS ONE
═══════════════════════════════════════════════════════════════════════════════

`tenant_scoped()` stops one company reading another company's rows. It says
nothing about one employee reading a colleague's leave reason, redeeming a
colleague's points, or recording a check-in on somebody else's PIP — and for a
while nothing else did either, because every one of those endpoints took an
`employee_id` or a record id from the request and trusted it.

So this module is the one place the in-tenant rule lives, in the same spirit as
`tenant_scoped`: there is exactly one implementation to audit, and an invariant
fails the build when an endpoint with no role gate does not come through here.

═══════════════════════════════════════════════════════════════════════════════
THE THREE RELATIONSHIPS
═══════════════════════════════════════════════════════════════════════════════

- **Self.** The record is about the caller's own employee record.
- **Reporting line.** The record is about somebody who reports to the caller,
  directly or indirectly. Walked breadth-first and depth-capped, because two
  people set as each other's manager mid-reorg is a real state and must not
  loop forever.
- **HR.** Admin and sub-admin act on anybody's record in their tenant. Super
  admin passes as it does everywhere else.

Being a manager is the second relationship, not a role: anybody with a direct
report has a reporting line, whatever their login role.

Some things only the person can do for themselves — answering a survey,
acknowledging a policy. HR cannot do those on anybody's behalf, and
`require_self` exists for exactly that.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.employee import Employee
from app.models.user import User, UserRole

__all__ = [
    "ADMIN_ROLES",
    "HR_ROLES",
    "forbidden",
    "is_admin",
    "is_hr",
    "is_line_manager_of",
    "is_manager_or_hr",
    "manager_or_hr_login",
    "manages",
    "own_employee_id",
    "reporting_line",
    "require_hr",
    "require_manager_or_hr",
    "require_role",
    "require_self",
    "require_self_or_hr",
    "resolve_employee",
    "scope_role",
    "visible_employee_ids",
]

#: Roles that run the workspace: settings, users, approvals and money.
ADMIN_ROLES: frozenset[UserRole] = frozenset({UserRole.ADMIN})

#: Roles that act on anybody's record in their own tenant.
HR_ROLES: frozenset[UserRole] = frozenset({UserRole.ADMIN, UserRole.SUB_ADMIN})

#: A reporting line deeper than this is a cycle or a data error, not an org.
MAX_REPORTING_DEPTH = 10


def forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def is_hr(user: User) -> bool:
    """Acts on anybody's record — unless this request is outside the caller's HR rights.

    `hr_allowed` is worked out per request for a sub-admin narrowed by area and
    right; when it is False they are treated as the employee (or manager) they
    also are, so their own self-service still works and the HR action does not.
    """
    if user.role is UserRole.SUPER_ADMIN:
        return True
    return user.role in HR_ROLES and getattr(user, "hr_allowed", True)


def is_admin(user: User) -> bool:
    """The tenant admin role: decisions a sub-admin prepares but does not sign."""
    return user.role in ADMIN_ROLES or user.role is UserRole.SUPER_ADMIN


def is_manager_or_hr(user: User) -> bool:
    """May take a people-manager action at all.

    Whether they may take it for a *particular* person is the reporting-line
    check, not this one.
    """
    return is_hr(user) or user.has_reports


def scope_role(user: User) -> str:
    """The key for the role-keyed reach tables in reporting and grounding.

    The login role, except that an employee with a direct report reads as
    `manager`: those tables give a manager their reporting tree, and managing
    people is a place in the org chart rather than a role anybody is given.
    """
    if user.role is UserRole.EMPLOYEE and user.has_reports:
        return "manager"
    if user.role is UserRole.SUB_ADMIN and not getattr(user, "hr_allowed", True):
        return "manager" if user.has_reports else "employee"
    return user.role.value


def manager_or_hr_login(user: User) -> ColumnElement[bool]:
    """A `User` filter: in HR, or somebody in the caller's tenant reports to them.

    For picking *another* login — a clearance owner, an asset custodian — where
    `User.has_reports` was never worked out because they are not the caller.
    """
    reported_to = tenant_scoped(
        select(Employee.manager_id).where(
            Employee.manager_id.is_not(None), Employee.deleted_at.is_(None)
        ),
        Employee,
        user,
    )
    return or_(User.role.in_(HR_ROLES), User.employee_id.in_(reported_to))


def own_employee_id(user: User) -> uuid.UUID:
    """The caller's employee record, or a clear refusal.

    A login with no employee record — a consultant, a super admin — has no
    "own" record to act on, and pretending otherwise would act on nothing.
    """
    if user.employee_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Your login is not linked to an employee record",
        )
    return user.employee_id


def require_role(
    user: User,
    roles: frozenset[UserRole],
    *,
    detail: str,
    line_managers: bool = False,
) -> None:
    """The service-layer twin of `require_roles`, line managers included on request."""
    if line_managers and user.has_reports:
        return
    if user.role not in roles and user.role is not UserRole.SUPER_ADMIN:
        raise forbidden(detail)


def require_hr(user: User, *, detail: str) -> None:
    if not is_hr(user):
        raise forbidden(detail)


def resolve_employee(
    user: User,
    supplied: uuid.UUID | None,
    *,
    detail: str = "You can only act on your own records.",
) -> uuid.UUID:
    """Whose record this touches: the caller's own unless HR says otherwise.

    Omitting the id means "mine". Supplying somebody else's is refused for
    anybody outside HR — refused, not silently replaced with the caller's own,
    because a request that quietly did something different from what it asked
    is a bug report nobody can reproduce.
    """
    if supplied is None:
        return own_employee_id(user)
    if supplied != user.employee_id and not is_hr(user):
        raise forbidden(detail)
    return supplied


def require_self_or_hr(
    user: User,
    employee_id: uuid.UUID | None,
    *,
    detail: str = "You can only act on your own records.",
) -> None:
    if is_hr(user):
        return
    if employee_id is None or employee_id != user.employee_id:
        raise forbidden(detail)


def require_self(user: User, employee_id: uuid.UUID | None, *, detail: str) -> None:
    """Things nobody may do on another person's behalf — HR included.

    An acknowledgement recorded by somebody else is not an acknowledgement, and
    a survey answered by somebody else is a forged vote.
    """
    if user.employee_id is None or employee_id != user.employee_id:
        raise forbidden(detail)


async def reporting_line(session: AsyncSession, user: User) -> set[uuid.UUID]:
    """Everybody who reports to the caller, directly or indirectly.

    Excludes the caller. Depth-capped, because a cycle in the manager chain is a
    real state during a reorg and must not become an infinite loop.
    """
    if user.employee_id is None:
        return set()
    reachable: set[uuid.UUID] = set()
    frontier: set[uuid.UUID] = {user.employee_id}
    for _ in range(MAX_REPORTING_DEPTH):
        if not frontier:
            break
        stmt = tenant_scoped(
            select(Employee.id).where(Employee.manager_id.in_(frontier)),
            Employee,
            user,
        )
        found = set((await session.execute(stmt)).scalars().all())
        new = found - reachable - {user.employee_id}
        if not new:
            break
        reachable |= new
        frontier = new
    return reachable


async def manages(session: AsyncSession, user: User, employee_id: uuid.UUID) -> bool:
    return employee_id in await reporting_line(session, user)


async def is_line_manager_of(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> bool:
    """This person is somewhere in the caller's reporting line."""
    return await manages(session, user, employee_id)


async def require_manager_or_hr(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    *,
    detail: str,
    allow_self: bool = False,
) -> None:
    """A people-manager action on one person.

    Self is refused by default, **HR included**: nobody recommends their own
    increment or records a check-in on their own improvement plan, whatever
    their role.
    """
    if user.employee_id is not None and employee_id == user.employee_id:
        if allow_self:
            return
        raise forbidden(
            "This cannot be done for yourself. It needs somebody in your "
            "reporting line or in HR."
        )
    if is_hr(user):
        return
    if await manages(session, user, employee_id):
        return
    raise forbidden(detail)


async def visible_employee_ids(
    session: AsyncSession, user: User
) -> set[uuid.UUID] | None:
    """Whose records a list may return to this caller.

    `None` means everybody in the tenant (HR). Otherwise the caller's own record
    plus their reporting line, which is empty for somebody with no reports.
    """
    if is_hr(user):
        return None
    ids: set[uuid.UUID] = set()
    if user.employee_id is not None:
        ids.add(user.employee_id)
    ids |= await reporting_line(session, user)
    return ids
