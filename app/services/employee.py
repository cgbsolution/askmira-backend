"""Employee master service (FRD 5.1).

Every query goes through `tenant_scoped` — that is the isolation rule, and its
absence in a query is the bug to look for in review.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import CustomFieldDefinition, Employee
from app.models.user import User
from app.schemas.employee import EmployeeCreate, EmployeeUpdate
from app.services import audit


async def _validate_custom_fields(
    session: AsyncSession, user: User, values: dict
) -> None:
    """Reject unknown keys and missing required fields (FS-CHR-01).

    A required field blocking an incomplete save is an acceptance criterion,
    not a nicety — an employee record missing a statutory identifier surfaces
    as a payroll failure weeks later.
    """
    stmt = tenant_scoped(
        select(CustomFieldDefinition).where(
            CustomFieldDefinition.entity == "employee",
            CustomFieldDefinition.is_active.is_(True),
        ),
        CustomFieldDefinition,
        user,
    )
    definitions = (await session.scalars(stmt)).all()
    known = {d.key for d in definitions}

    unknown = set(values) - known
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown custom field(s): {', '.join(sorted(unknown))}",
        )

    missing = [
        d.label for d in definitions if d.is_required and values.get(d.key) in (None, "", [])
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Required field(s) missing: {', '.join(missing)}",
        )


async def list_employees(
    session: AsyncSession,
    user: User,
    *,
    search: str | None = None,
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    status_filter: str | None = None,
    include_deleted: bool = False,
    offset: int = 0,
    limit: int = 25,
) -> tuple[list[Employee], int]:
    """Return one page of employees plus the unpaginated total."""
    stmt = tenant_scoped(select(Employee), Employee, user)

    if not include_deleted:
        stmt = stmt.where(Employee.deleted_at.is_(None))
    if department_id:
        stmt = stmt.where(Employee.department_id == department_id)
    if location_id:
        stmt = stmt.where(Employee.location_id == location_id)
    if status_filter:
        stmt = stmt.where(Employee.status == status_filter)
    if search:
        term = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                Employee.first_name.ilike(term),
                Employee.last_name.ilike(term),
                Employee.display_name.ilike(term),
                Employee.work_email.ilike(term),
                Employee.employee_code.ilike(term),
            )
        )

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (
        await session.scalars(
            stmt.order_by(Employee.first_name, Employee.last_name).offset(offset).limit(limit)
        )
    ).all()
    return list(rows), total


async def get_employee(session: AsyncSession, user: User, employee_id: uuid.UUID) -> Employee:
    """Fetch one employee, or 404.

    A record in another tenant returns 404, not 403 — a 403 would confirm that
    the id exists somewhere.
    """
    stmt = tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    employee = await session.scalar(stmt)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    return employee


async def create_employee(
    session: AsyncSession, user: User, payload: EmployeeCreate, request: Request | None = None
) -> Employee:
    duplicate = await session.scalar(
        tenant_scoped(
            select(Employee.id).where(Employee.employee_code == payload.employee_code),
            Employee,
            user,
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Employee code {payload.employee_code} is already in use",
        )

    await _validate_custom_fields(session, user, payload.custom_fields)

    employee = Employee(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(employee)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="employee",
        entity_id=employee.id,
        actor=user,
        changes=payload.model_dump(mode="json"),
        summary=f"Created employee {employee.employee_code}",
        request=request,
    )
    await session.commit()
    await session.refresh(employee)
    return employee


async def update_employee(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    payload: EmployeeUpdate,
    request: Request | None = None,
) -> Employee:
    employee = await get_employee(session, user, employee_id)
    changes = payload.model_dump(exclude_unset=True)

    if "custom_fields" in changes and changes["custom_fields"] is not None:
        await _validate_custom_fields(session, user, changes["custom_fields"])

    if changes.get("manager_id") == employee.id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="An employee cannot report to themselves",
        )

    # Field-level before/after, so the audit trail shows what actually moved
    # rather than just that an update happened.
    diff = {
        key: {"from": getattr(employee, key), "to": value}
        for key, value in changes.items()
        if getattr(employee, key) != value
    }
    for key, value in changes.items():
        setattr(employee, key, value)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="employee",
        entity_id=employee.id,
        actor=user,
        changes={k: {"from": str(v["from"]), "to": str(v["to"])} for k, v in diff.items()},
        summary=f"Updated employee {employee.employee_code}",
        request=request,
    )
    await session.commit()
    await session.refresh(employee)
    return employee


async def deactivate_employee(
    session: AsyncSession, user: User, employee_id: uuid.UUID, request: Request | None = None
) -> None:
    """Soft-delete.

    Payroll runs and audit entries reference employees historically, so a hard
    delete would orphan records that must stay reproducible.
    """
    from datetime import UTC, datetime

    employee = await get_employee(session, user, employee_id)
    employee.deleted_at = datetime.now(UTC)
    employee.is_active = False

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="employee",
        entity_id=employee.id,
        actor=user,
        summary=f"Deactivated employee {employee.employee_code}",
        request=request,
    )
    await session.commit()
