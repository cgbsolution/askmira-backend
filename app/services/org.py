"""Organisation structure: legal entities, locations and departments (FRD 5.1).

Admin-only writes, each audited in the same transaction. Nothing here is ever
deleted: payroll runs, filings and employee records point at these rows, so a
closed office is deactivated, not removed.

A location's `state_code` is load-bearing — Professional Tax and the holiday
calendar follow it — so a change to it is audited with its before and after.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.attendance import Shift
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.org import Department, LegalEntity, Location
from app.models.user import User
from app.schemas.org import (
    DepartmentCreate,
    DepartmentUpdate,
    LegalEntityCreate,
    LegalEntityUpdate,
    LocationCreate,
    LocationUpdate,
)
from app.services import audit

__all__ = [
    "create_department",
    "create_legal_entity",
    "create_location",
    "list_departments",
    "list_legal_entities",
    "list_locations",
    "update_department",
    "update_legal_entity",
    "update_location",
]

#: A department tree deeper than this is a loop or a data error.
MAX_DEPTH = 20


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


async def _get(session: AsyncSession, user: User, model: type, row_id: uuid.UUID, label: str):
    row = await session.scalar(tenant_scoped(select(model).where(model.id == row_id), model, user))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{label} not found")
    return row


async def _code_free(
    session: AsyncSession, user: User, model: type, code: str, *, label: str, exclude=None
) -> None:
    stmt = select(model.id).where(model.code == code)
    if exclude is not None:
        stmt = stmt.where(model.id != exclude)
    if await session.scalar(tenant_scoped(stmt, model, user)) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A {label} with code {code} already exists",
        )


def _diff(row: Any, changes: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {
        key: {"from": str(getattr(row, key)), "to": str(value)}
        for key, value in changes.items()
        if getattr(row, key) != value
    }


def _no_nulls(changes: dict[str, Any], *keys: str) -> None:
    for key in keys:
        if key in changes and changes[key] is None:
            raise _unprocessable(f"'{key}' cannot be cleared.")


async def _record(
    session: AsyncSession,
    user: User,
    *,
    action: AuditAction,
    entity_type: str,
    entity_id: uuid.UUID,
    summary: str,
    changes: dict[str, Any],
    request: Request | None,
) -> None:
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor=user,
        changes=changes,
        summary=summary,
        request=request,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Reads
# ═══════════════════════════════════════════════════════════════════════════


async def list_legal_entities(session: AsyncSession, user: User) -> list[LegalEntity]:
    stmt = tenant_scoped(select(LegalEntity).order_by(LegalEntity.name), LegalEntity, user)
    return list((await session.scalars(stmt)).all())


async def list_locations(session: AsyncSession, user: User) -> list[Location]:
    stmt = tenant_scoped(select(Location).order_by(Location.name), Location, user)
    return list((await session.scalars(stmt)).all())


async def list_departments(session: AsyncSession, user: User) -> list[Department]:
    stmt = tenant_scoped(select(Department).order_by(Department.name), Department, user)
    return list((await session.scalars(stmt)).all())


# ═══════════════════════════════════════════════════════════════════════════
# Legal entities
# ═══════════════════════════════════════════════════════════════════════════


async def _check_work_week(session: AsyncSession, user: User, shift_id: uuid.UUID | None) -> None:
    """A legal entity's work week must be an active shift of this workspace."""
    if shift_id is None:
        return
    found = await session.scalar(
        tenant_scoped(
            select(Shift.id).where(Shift.id == shift_id, Shift.active.is_(True)), Shift, user
        )
    )
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Choose an active shift for the work week.",
        )


async def create_legal_entity(
    session: AsyncSession, user: User, payload: LegalEntityCreate, *, request: Request | None
) -> LegalEntity:
    await _code_free(session, user, LegalEntity, payload.code, label="legal entity")
    await _check_work_week(session, user, payload.default_shift_id)
    values = payload.model_dump()
    values["pt_registrations"] = values.get("pt_registrations") or {}
    values["salary_days_basis"] = values.get("salary_days_basis") or "calendar"
    entity = LegalEntity(tenant_id=user.tenant_id, **values)
    session.add(entity)
    await session.flush()
    await _record(
        session,
        user,
        action=AuditAction.CREATE,
        entity_type="legal_entity",
        entity_id=entity.id,
        summary=f"Created legal entity {entity.code}",
        changes=payload.model_dump(mode="json"),
        request=request,
    )
    await session.commit()
    await session.refresh(entity)
    return entity


async def update_legal_entity(
    session: AsyncSession,
    user: User,
    entity_id: uuid.UUID,
    payload: LegalEntityUpdate,
    *,
    request: Request | None,
) -> LegalEntity:
    entity = await _get(session, user, LegalEntity, entity_id, "Legal entity")
    changes = payload.model_dump(exclude_unset=True)
    _no_nulls(changes, "name", "code", "is_active", "salary_days_basis")
    if "pt_registrations" in changes and changes["pt_registrations"] is None:
        changes["pt_registrations"] = {}
    if changes.get("default_shift_id"):
        await _check_work_week(session, user, changes["default_shift_id"])
    basis = changes.get("salary_days_basis", entity.salary_days_basis)
    fixed = changes.get("salary_fixed_days", entity.salary_fixed_days)
    if basis == "fixed" and not fixed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Say how many days a month is divided by, such as 26 or 30.",
        )
    if changes.get("code") and changes["code"] != entity.code:
        await _code_free(
            session, user, LegalEntity, changes["code"], label="legal entity", exclude=entity.id
        )
    diff = _diff(entity, changes)
    for key, value in changes.items():
        setattr(entity, key, value)
    await _record(
        session,
        user,
        action=AuditAction.UPDATE,
        entity_type="legal_entity",
        entity_id=entity.id,
        summary=f"Updated legal entity {entity.code}",
        changes=diff,
        request=request,
    )
    await session.commit()
    await session.refresh(entity)
    return entity


# ═══════════════════════════════════════════════════════════════════════════
# Locations
# ═══════════════════════════════════════════════════════════════════════════


async def _entity_in_tenant(session: AsyncSession, user: User, entity_id: uuid.UUID) -> None:
    found = await session.scalar(
        tenant_scoped(select(LegalEntity.id).where(LegalEntity.id == entity_id), LegalEntity, user)
    )
    if found is None:
        raise _unprocessable("That legal entity is not part of this workspace.")


async def create_location(
    session: AsyncSession, user: User, payload: LocationCreate, *, request: Request | None
) -> Location:
    await _entity_in_tenant(session, user, payload.legal_entity_id)
    await _code_free(session, user, Location, payload.code, label="location")
    location = Location(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(location)
    await session.flush()
    await _record(
        session,
        user,
        action=AuditAction.CREATE,
        entity_type="location",
        entity_id=location.id,
        summary=f"Created location {location.code} in state {location.state_code}",
        changes=payload.model_dump(mode="json"),
        request=request,
    )
    await session.commit()
    await session.refresh(location)
    return location


async def update_location(
    session: AsyncSession,
    user: User,
    location_id: uuid.UUID,
    payload: LocationUpdate,
    *,
    request: Request | None,
) -> Location:
    location = await _get(session, user, Location, location_id, "Location")
    changes = payload.model_dump(exclude_unset=True)
    _no_nulls(changes, "legal_entity_id", "name", "code", "state_code", "timezone", "is_active")
    if changes.get("legal_entity_id"):
        await _entity_in_tenant(session, user, changes["legal_entity_id"])
    if changes.get("code") and changes["code"] != location.code:
        await _code_free(
            session, user, Location, changes["code"], label="location", exclude=location.id
        )
    diff = _diff(location, changes)
    for key, value in changes.items():
        setattr(location, key, value)
    summary = f"Updated location {location.code}"
    if "state_code" in diff:
        summary += (
            f"; state changed {diff['state_code']['from']} → {diff['state_code']['to']}, "
            "which changes Professional Tax and the holiday calendar for everyone here"
        )
    await _record(
        session,
        user,
        action=AuditAction.UPDATE,
        entity_type="location",
        entity_id=location.id,
        summary=summary,
        changes=diff,
        request=request,
    )
    await session.commit()
    await session.refresh(location)
    return location


# ═══════════════════════════════════════════════════════════════════════════
# Departments
# ═══════════════════════════════════════════════════════════════════════════


async def _check_head(session: AsyncSession, user: User, employee_id: uuid.UUID | None) -> None:
    if employee_id is None:
        return
    found = await session.scalar(
        tenant_scoped(
            select(Employee.id).where(Employee.id == employee_id, Employee.deleted_at.is_(None)),
            Employee,
            user,
        )
    )
    if found is None:
        raise _unprocessable("The department head must be an active employee in this workspace.")


async def _check_parent(
    session: AsyncSession, user: User, parent_id: uuid.UUID | None, *, department_id=None
) -> None:
    """The parent exists here, and choosing it does not close a loop."""
    current = parent_id
    for _ in range(MAX_DEPTH):
        if current is None:
            return
        if department_id is not None and current == department_id:
            raise _unprocessable(
                "That parent would put the department inside itself. Pick a department "
                "that is not below this one."
            )
        row = await session.scalar(
            tenant_scoped(select(Department).where(Department.id == current), Department, user)
        )
        if row is None:
            raise _unprocessable("The parent department is not part of this workspace.")
        current = row.parent_id
    raise _unprocessable("The department tree is deeper than allowed, or already loops.")


async def create_department(
    session: AsyncSession, user: User, payload: DepartmentCreate, *, request: Request | None
) -> Department:
    await _code_free(session, user, Department, payload.code, label="department")
    await _check_parent(session, user, payload.parent_id)
    await _check_head(session, user, payload.head_employee_id)
    department = Department(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(department)
    await session.flush()
    await _record(
        session,
        user,
        action=AuditAction.CREATE,
        entity_type="department",
        entity_id=department.id,
        summary=f"Created department {department.code}",
        changes=payload.model_dump(mode="json"),
        request=request,
    )
    await session.commit()
    await session.refresh(department)
    return department


async def update_department(
    session: AsyncSession,
    user: User,
    department_id: uuid.UUID,
    payload: DepartmentUpdate,
    *,
    request: Request | None,
) -> Department:
    department = await _get(session, user, Department, department_id, "Department")
    changes = payload.model_dump(exclude_unset=True)
    _no_nulls(changes, "name", "code", "is_active")
    if changes.get("code") and changes["code"] != department.code:
        await _code_free(
            session, user, Department, changes["code"], label="department", exclude=department.id
        )
    if "parent_id" in changes:
        await _check_parent(session, user, changes["parent_id"], department_id=department.id)
    if "head_employee_id" in changes:
        await _check_head(session, user, changes["head_employee_id"])
    diff = _diff(department, changes)
    for key, value in changes.items():
        setattr(department, key, value)
    await _record(
        session,
        user,
        action=AuditAction.UPDATE,
        entity_type="department",
        entity_id=department.id,
        summary=f"Updated department {department.code}",
        changes=diff,
        request=request,
    )
    await session.commit()
    await session.refresh(department)
    return department
