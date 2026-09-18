"""Employee master service (FRD 5.1).

Every query goes through `tenant_scoped` — that is the isolation rule, and its
absence in a query is the bug to look for in review.

Two further rules live here:

- **Every reference is checked against the tenant.** A department, location,
  legal entity or manager id from another workspace is refused, not stored — a
  foreign key only proves the row exists somewhere.
- **Nobody edits their own record through HR's path.** An HR admin changes
  their own contact details through self-service like everybody else; their
  own grade, status or manager is changed by somebody else.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import CustomFieldDefinition, Employee
from app.models.org import Department, LegalEntity, Location
from app.models.user import User
from app.schemas.employee import EmployeeCreate, EmployeeSelfUpdate, EmployeeUpdate
from app.services import audit

#: A reporting chain longer than this is a loop or a data error.
MAX_CHAIN = 25

_NOT_NULL = ("first_name", "work_email", "employment_type", "status")


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


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
        raise _unprocessable(f"Unknown custom field(s): {', '.join(sorted(unknown))}")

    missing = [
        d.label for d in definitions if d.is_required and values.get(d.key) in (None, "", [])
    ]
    if missing:
        raise _unprocessable(f"Required field(s) missing: {', '.join(missing)}")


async def _validate_references(
    session: AsyncSession,
    user: User,
    values: dict[str, Any],
    *,
    employee_id: uuid.UUID | None = None,
) -> None:
    """Every id the record points at belongs to this workspace, and no loop forms."""
    checks: tuple[tuple[str, type, str], ...] = (
        ("legal_entity_id", LegalEntity, "legal entity"),
        ("location_id", Location, "location"),
        ("department_id", Department, "department"),
    )
    for key, model, label in checks:
        value = values.get(key)
        if value is None:
            continue
        found = await session.scalar(
            tenant_scoped(select(model.id).where(model.id == value), model, user)
        )
        if found is None:
            raise _unprocessable(f"That {label} is not part of this workspace.")

    manager_id = values.get("manager_id")
    if manager_id is None:
        return
    if employee_id is not None and manager_id == employee_id:
        raise _unprocessable("An employee cannot report to themselves")

    current: uuid.UUID | None = manager_id
    for _ in range(MAX_CHAIN):
        if current is None:
            return
        row = await session.scalar(
            tenant_scoped(select(Employee).where(Employee.id == current), Employee, user)
        )
        if row is None:
            raise _unprocessable("That manager is not an employee in this workspace.")
        if current == manager_id and row.deleted_at is not None:
            raise _unprocessable("That manager's record has been deactivated.")
        if employee_id is not None and row.manager_id == employee_id:
            raise _unprocessable(
                "That manager already reports, directly or indirectly, to this employee. "
                "Reporting lines cannot loop."
            )
        current = row.manager_id
    raise _unprocessable("The reporting chain above that manager is too long, or loops.")


async def list_employees(
    session: AsyncSession,
    user: User,
    *,
    search: str | None = None,
    department_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    manager_id: uuid.UUID | None = None,
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
    if manager_id:
        stmt = stmt.where(Employee.manager_id == manager_id)
    if status_filter:
        stmt = stmt.where(Employee.status == status_filter)
    if search and search.strip():
        term = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                Employee.first_name.ilike(term),
                Employee.last_name.ilike(term),
                Employee.display_name.ilike(term),
                Employee.work_email.ilike(term),
                Employee.employee_code.ilike(term),
                Employee.designation.ilike(term),
            )
        )

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (
        await session.scalars(
            stmt.order_by(Employee.first_name, Employee.last_name, Employee.id)
            .offset(offset)
            .limit(limit)
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


async def get_own_employee(session: AsyncSession, user: User) -> Employee:
    """The caller's own record, from the token's link — never from the request."""
    permissions.own_employee_id(user)
    return await get_employee(session, user, user.employee_id)


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

    await _validate_references(session, user, payload.model_dump())
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


def _diff(employee: Employee, changes: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Field-level before/after, so the audit trail shows what actually moved."""
    return {
        key: {"from": str(getattr(employee, key)), "to": str(value)}
        for key, value in changes.items()
        if getattr(employee, key) != value
    }


async def update_employee(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    payload: EmployeeUpdate,
    request: Request | None = None,
) -> Employee:
    """HR's edit of somebody else's record."""
    if user.employee_id is not None and employee_id == user.employee_id:
        raise permissions.forbidden(
            "You cannot edit your own record through HR. Change your contact details on "
            "your profile; anything else on your record is changed by another HR admin."
        )
    employee = await get_employee(session, user, employee_id)
    changes = payload.model_dump(exclude_unset=True)

    for key in _NOT_NULL:
        if key in changes and changes[key] is None:
            raise _unprocessable(f"'{key}' cannot be cleared.")

    if "custom_fields" in changes:
        if changes["custom_fields"] is None:
            changes["custom_fields"] = {}
        await _validate_custom_fields(session, user, changes["custom_fields"])

    await _validate_references(session, user, changes, employee_id=employee.id)

    joining = changes.get("date_of_joining", employee.date_of_joining)
    probation_end = changes.get("probation_end_date", employee.probation_end_date)
    exit_date = changes.get("date_of_exit", employee.date_of_exit)
    if joining and probation_end and probation_end < joining:
        raise _unprocessable("Probation cannot end before the date of joining")
    if joining and exit_date and exit_date < joining:
        raise _unprocessable("The date of exit cannot be before the date of joining")

    diff = _diff(employee, changes)
    for key, value in changes.items():
        setattr(employee, key, value)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="employee",
        entity_id=employee.id,
        actor=user,
        changes=diff,
        summary=f"Updated employee {employee.employee_code}",
        request=request,
    )
    await session.commit()
    await session.refresh(employee)
    return employee


async def update_own_contact(
    session: AsyncSession,
    user: User,
    payload: EmployeeSelfUpdate,
    request: Request | None = None,
) -> Employee:
    """Self-service: the caller's own display name and personal contact details."""
    permissions.own_employee_id(user)
    employee = await get_employee(session, user, user.employee_id)
    changes = payload.model_dump(exclude_unset=True)
    diff = _diff(employee, changes)
    for key, value in changes.items():
        setattr(employee, key, value)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="employee",
        entity_id=employee.id,
        actor=user,
        changes=diff,
        summary=f"{employee.employee_code} updated their own contact details",
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
    if user.employee_id is not None and employee_id == user.employee_id:
        raise permissions.forbidden("You cannot deactivate your own record.")
    employee = await get_employee(session, user, employee_id)
    if employee.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Employee {employee.employee_code} is already deactivated",
        )
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
