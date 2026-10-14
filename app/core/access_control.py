"""Working out, per request, how much of a sub-admin's HR power applies.

Called by `get_current_user` for every signed-in request. For an admin or an
employee it only resets the request state; for a sub-admin it reads their
rights in the request's area (their own row, else their role's, else full) and
their department/location scope, and records both on the request.
"""

from __future__ import annotations

import uuid

from fastapi import Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import areas, hr_scope
from app.core.deps import tenant_scoped
from app.models.user import User, UserRole

__all__ = ["load_request_access", "rights_for"]


async def rights_for(session: AsyncSession, user: User, area: str) -> areas.AreaRights | None:
    """The person's rights in an area: their own row, else their role's, else None (full)."""
    from app.models.access import UserAreaRights
    from app.models.audit import RoleModulePermission

    own = await session.scalar(
        tenant_scoped(
            select(UserAreaRights).where(
                UserAreaRights.user_id == user.id, UserAreaRights.area == area
            ),
            UserAreaRights,
            user,
        )
    )
    row = own or await session.scalar(
        tenant_scoped(
            select(RoleModulePermission).where(
                RoleModulePermission.role == user.role.value,
                RoleModulePermission.module == area,
            ),
            RoleModulePermission,
            user,
        )
    )
    if row is None:
        return None
    return areas.AreaRights(**{right: bool(getattr(row, right)) for right in areas.RIGHTS})


async def load_request_access(request: Request, session: AsyncSession, user: User) -> None:
    hr_scope.set_scope(None)
    user.hr_allowed = True
    user.request_area = None
    if user.role is not UserRole.SUB_ADMIN:
        return

    area = areas.area_for_path(request.url.path)
    user.request_area = area
    if area is not None:
        rights = await rights_for(session, user, area)
        if rights is not None:
            user.hr_allowed = rights.allows(
                areas.required_right(request.method, request.url.path)
            )

    from app.models.access import AdminScope
    from app.models.employee import Employee

    scope = await session.scalar(
        tenant_scoped(select(AdminScope).where(AdminScope.user_id == user.id), AdminScope, user)
    )
    if scope is None or not (scope.department_ids or scope.location_ids):
        return
    conditions = []
    if scope.department_ids:
        conditions.append(
            Employee.department_id.in_([uuid.UUID(str(d)) for d in scope.department_ids])
        )
    if scope.location_ids:
        conditions.append(
            Employee.location_id.in_([uuid.UUID(str(loc)) for loc in scope.location_ids])
        )
    if user.employee_id is not None:
        conditions.append(Employee.id == user.employee_id)
    covered = (
        await session.scalars(
            tenant_scoped(select(Employee.id).where(or_(*conditions)), Employee, user)
        )
    ).all()
    hr_scope.set_scope(hr_scope.HrScope(employee_ids=tuple(covered)))
