"""Editing what HR operators may do: rights by area, per role or per person, and scope.

Only the sub-admin role is narrowed. An admin's powers are the tenant's
highest and are never narrowed here (somebody must be able to undo a mistake);
an employee's reach is their own records and their reporting line, decided by
the org chart. Every change is a privilege change, so it is audited with the
before and after, and asks for fresh proof of identity at the route.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import areas, permissions
from app.core.deps import tenant_scoped
from app.core.step_up import Confirm
from app.models.access import AdminScope, UserAreaRights
from app.models.audit import AuditAction, RoleModulePermission
from app.models.org import Department, Location
from app.models.user import User, UserRole
from app.services import audit

__all__ = [
    "access_overview",
    "clear_role_rights",
    "clear_user_rights",
    "set_role_rights",
    "set_scope",
    "set_user_rights",
]

NARROWABLE_ROLE = UserRole.SUB_ADMIN.value


def _require_admin(user: User) -> None:
    if not permissions.is_admin(user):
        raise permissions.forbidden("Only an admin changes what HR operators may do.")


def _check_area(area: str) -> None:
    if area not in areas.AREAS:
        raise HTTPException(status_code=422, detail=f"No area called '{area}'.")


def _flags(row: Any) -> dict[str, bool]:
    return {right: bool(getattr(row, right)) for right in areas.RIGHTS}


async def _sub_admin(session: AsyncSession, admin: User, user_id: uuid.UUID) -> User:
    target = await session.scalar(
        tenant_scoped(select(User).where(User.id == user_id), User, admin)
    )
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.role is not UserRole.SUB_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{target.full_name} is not a sub-admin. Rights and scope narrow HR operators; "
                "an admin is never narrowed, and an employee's reach is their reporting line."
            ),
        )
    return target


async def access_overview(session: AsyncSession, admin: User) -> dict[str, Any]:
    _require_admin(admin)
    role_rows = (
        await session.scalars(
            tenant_scoped(
                select(RoleModulePermission).where(RoleModulePermission.role == NARROWABLE_ROLE),
                RoleModulePermission,
                admin,
            )
        )
    ).all()
    sub_admins = (
        await session.scalars(
            tenant_scoped(
                select(User)
                .where(User.role == UserRole.SUB_ADMIN, User.is_active.is_(True))
                .order_by(User.full_name),
                User,
                admin,
            )
        )
    ).all()
    ids = [u.id for u in sub_admins]
    user_rows = (
        await session.scalars(
            tenant_scoped(
                select(UserAreaRights).where(UserAreaRights.user_id.in_(ids or [uuid.uuid4()])),
                UserAreaRights,
                admin,
            )
        )
    ).all()
    scopes = {
        s.user_id: s
        for s in (
            await session.scalars(
                tenant_scoped(
                    select(AdminScope).where(AdminScope.user_id.in_(ids or [uuid.uuid4()])),
                    AdminScope,
                    admin,
                )
            )
        ).all()
    }
    by_user: dict[uuid.UUID, dict[str, dict[str, bool]]] = {}
    for row in user_rows:
        by_user.setdefault(row.user_id, {})[row.area] = _flags(row)
    return {
        "areas": [{"key": key, "description": text} for key, text in areas.AREAS.items()],
        "rights": list(areas.RIGHTS),
        "role": NARROWABLE_ROLE,
        "role_rights": {row.module: _flags(row) for row in role_rows if row.module in areas.AREAS},
        "people": [
            {
                "user_id": str(u.id),
                "full_name": u.full_name,
                "email": u.email,
                "rights": by_user.get(u.id, {}),
                "scope": (
                    {
                        "department_ids": list(scopes[u.id].department_ids or []),
                        "location_ids": list(scopes[u.id].location_ids or []),
                    }
                    if u.id in scopes
                    else None
                ),
            }
            for u in sub_admins
        ],
        "note": (
            "No row means full HR rights in that area. A person's own row wins over the "
            "role's. Withholding a right makes the sub-admin an ordinary employee (or "
            "manager) for that action, so their own self-service keeps working."
        ),
    }


async def set_role_rights(
    session: AsyncSession,
    admin: User,
    area: str,
    rights: dict[str, bool],
    *,
    confirm: Confirm | None = None,
    request: Request | None = None,
) -> dict[str, bool]:
    _require_admin(admin)
    _check_area(area)
    if confirm is not None:
        await confirm()
    row = await session.scalar(
        tenant_scoped(
            select(RoleModulePermission).where(
                RoleModulePermission.role == NARROWABLE_ROLE, RoleModulePermission.module == area
            ),
            RoleModulePermission,
            admin,
        )
    )
    before = _flags(row) if row else None
    if row is None:
        row = RoleModulePermission(tenant_id=admin.tenant_id, role=NARROWABLE_ROLE, module=area)
        session.add(row)
    for right in areas.RIGHTS:
        setattr(row, right, bool(rights.get(right, False)))
    await session.flush()
    await audit.record(
        session,
        tenant_id=admin.tenant_id,
        actor=admin,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="role_permission",
        entity_id=row.id,
        summary=f"Sub-admin rights in {area} set",
        changes={"from": before, "to": _flags(row)},
        request=request,
    )
    return _flags(row)


async def clear_role_rights(
    session: AsyncSession,
    admin: User,
    area: str,
    *,
    confirm: Confirm | None = None,
    request: Request | None = None,
) -> None:
    _require_admin(admin)
    _check_area(area)
    if confirm is not None:
        await confirm()
    row = await session.scalar(
        tenant_scoped(
            select(RoleModulePermission).where(
                RoleModulePermission.role == NARROWABLE_ROLE, RoleModulePermission.module == area
            ),
            RoleModulePermission,
            admin,
        )
    )
    if row is None:
        return
    before = _flags(row)
    row_id = row.id
    await session.delete(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=admin.tenant_id,
        actor=admin,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="role_permission",
        entity_id=row_id,
        summary=f"Sub-admin rights in {area} back to full",
        changes={"from": before, "to": None},
        request=request,
    )


async def set_user_rights(
    session: AsyncSession,
    admin: User,
    user_id: uuid.UUID,
    area: str,
    rights: dict[str, bool],
    *,
    confirm: Confirm | None = None,
    request: Request | None = None,
) -> dict[str, bool]:
    _require_admin(admin)
    _check_area(area)
    target = await _sub_admin(session, admin, user_id)
    if confirm is not None:
        await confirm()
    row = await session.scalar(
        tenant_scoped(
            select(UserAreaRights).where(
                UserAreaRights.user_id == target.id, UserAreaRights.area == area
            ),
            UserAreaRights,
            admin,
        )
    )
    before = _flags(row) if row else None
    if row is None:
        row = UserAreaRights(tenant_id=admin.tenant_id, user_id=target.id, area=area)
        session.add(row)
    for right in areas.RIGHTS:
        setattr(row, right, bool(rights.get(right, False)))
    await session.flush()
    await audit.record(
        session,
        tenant_id=admin.tenant_id,
        actor=admin,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="user",
        entity_id=target.id,
        summary=f"{target.full_name}'s rights in {area} set",
        changes={"from": before, "to": _flags(row)},
        request=request,
    )
    return _flags(row)


async def clear_user_rights(
    session: AsyncSession,
    admin: User,
    user_id: uuid.UUID,
    area: str,
    *,
    confirm: Confirm | None = None,
    request: Request | None = None,
) -> None:
    _require_admin(admin)
    _check_area(area)
    target = await _sub_admin(session, admin, user_id)
    if confirm is not None:
        await confirm()
    row = await session.scalar(
        tenant_scoped(
            select(UserAreaRights).where(
                UserAreaRights.user_id == target.id, UserAreaRights.area == area
            ),
            UserAreaRights,
            admin,
        )
    )
    if row is None:
        return
    before = _flags(row)
    await session.delete(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=admin.tenant_id,
        actor=admin,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="user",
        entity_id=target.id,
        summary=f"{target.full_name}'s rights in {area} back to the role's",
        changes={"from": before, "to": None},
        request=request,
    )


async def set_scope(
    session: AsyncSession,
    admin: User,
    user_id: uuid.UUID,
    *,
    department_ids: list[uuid.UUID],
    location_ids: list[uuid.UUID],
    confirm: Confirm | None = None,
    request: Request | None = None,
) -> dict[str, Any] | None:
    """Limit whose records a sub-admin acts on. Both lists empty removes the limit."""
    _require_admin(admin)
    target = await _sub_admin(session, admin, user_id)
    departments = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(Department.id).where(
                        Department.id.in_(department_ids or [uuid.uuid4()])
                    ),
                    Department,
                    admin,
                )
            )
        ).all()
    )
    locations = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(Location.id).where(Location.id.in_(location_ids or [uuid.uuid4()])),
                    Location,
                    admin,
                )
            )
        ).all()
    )
    unknown = (set(department_ids) - departments) | (set(location_ids) - locations)
    if unknown:
        raise HTTPException(status_code=422, detail="Some departments or locations do not exist.")
    if confirm is not None:
        await confirm()
    row = await session.scalar(
        tenant_scoped(select(AdminScope).where(AdminScope.user_id == target.id), AdminScope, admin)
    )
    before = (
        {"department_ids": row.department_ids, "location_ids": row.location_ids} if row else None
    )
    after: dict[str, Any] | None = None
    if not department_ids and not location_ids:
        if row is not None:
            await session.delete(row)
    else:
        if row is None:
            row = AdminScope(tenant_id=admin.tenant_id, user_id=target.id)
            session.add(row)
        row.department_ids = [str(d) for d in department_ids]
        row.location_ids = [str(loc) for loc in location_ids]
        after = {"department_ids": row.department_ids, "location_ids": row.location_ids}
    await session.flush()
    await audit.record(
        session,
        tenant_id=admin.tenant_id,
        actor=admin,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="user",
        entity_id=target.id,
        summary=(
            f"{target.full_name}'s HR scope "
            + ("removed: covers everybody" if after is None else "limited")
        ),
        changes={"from": before, "to": after},
        request=request,
    )
    return after
