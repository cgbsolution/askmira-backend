"""Team leave balances: a manager's reporting line, or the whole company for HR.

The table a manager checks before planning a release and HR reaches for at the
month end, with its CSV. Every figure comes from `service.balance_lines`, the
computation behind the Time page cards, so a person's row here and their own
card cannot disagree.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from typing import Any

from fastapi import Request
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import LeaveType
from app.models.org import Department
from app.models.user import User
from app.schemas.leave import (
    TeamBalanceCell,
    TeamBalanceColumn,
    TeamBalanceRow,
    TeamBalancesRead,
)
from app.services import audit
from app.services.leave import service

EXPORT_COLUMNS = (
    "employee_code",
    "employee_name",
    "department",
    "designation",
    "leave_type",
    "leave_type_code",
    "year",
    "opening",
    "accrued",
    "carried_forward",
    "adjusted",
    "used",
    "encashed",
    "pending",
    "available",
)

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: Any) -> str:
    """A cell a spreadsheet shows rather than runs."""
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_PREFIXES) and not re.fullmatch(r"-\d+(\.\d+)?", text):
        return "'" + text
    return text


def _team(user: User, visible: set[uuid.UUID] | None) -> set[uuid.UUID] | None:
    """Whose balances the caller may list, from `permissions.visible_employee_ids`:
    `None` for everybody (HR), otherwise their reporting line. Your own balance
    is on your Time page, not here."""
    if visible is None:
        return None
    line = visible - ({user.employee_id} if user.employee_id else set())
    if not line:
        raise permissions.forbidden(
            "Team balances are for managers and HR. Your own balances are on the Time off page."
        )
    return line


def _people(
    user: User,
    scope: set[uuid.UUID] | None,
    *,
    q: str | None,
    department_id: uuid.UUID | None,
) -> Select[tuple[Employee]]:
    """Current employees in scope. Leavers are settled on the exit screen."""
    stmt = tenant_scoped(
        select(Employee).where(
            Employee.deleted_at.is_(None), Employee.status != EmploymentStatus.EXITED
        ),
        Employee,
        user,
    )
    if scope is not None:
        stmt = stmt.where(Employee.id.in_(scope))
    if department_id is not None:
        stmt = stmt.where(Employee.department_id == department_id)
    if q and q.strip():
        term = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Employee.first_name.ilike(term),
                # A full name ("Meera Nair") spans both columns.
                func.concat_ws(" ", Employee.first_name, Employee.last_name).ilike(term),
                Employee.last_name.ilike(term),
                Employee.display_name.ilike(term),
                Employee.employee_code.ilike(term),
            )
        )
    return stmt.order_by(Employee.first_name, Employee.last_name, Employee.id)


async def _department_names(
    session: AsyncSession, user: User, employees: list[Employee]
) -> dict[uuid.UUID, str]:
    ids = {e.department_id for e in employees if e.department_id is not None}
    if not ids:
        return {}
    rows = await session.execute(
        tenant_scoped(
            select(Department.id, Department.name).where(Department.id.in_(ids)), Department, user
        )
    )
    return {department_id: name for department_id, name in rows.all()}


def _name(employee: Employee) -> str:
    return employee.display_name or employee.full_name


async def team_balances(
    session: AsyncSession,
    user: User,
    *,
    year: int,
    q: str | None = None,
    department_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 25,
) -> TeamBalancesRead:
    scope = _team(user, await permissions.visible_employee_ids(session, user))
    stmt = _people(user, scope, q=q, department_id=department_id)
    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    employees = list(
        (await session.scalars(stmt.offset((page - 1) * page_size).limit(page_size))).all()
    )
    lines = await service.balance_lines(session, user, employees, year)
    departments = await _department_names(session, user, employees)
    leave_types = (
        await session.scalars(
            tenant_scoped(
                select(LeaveType).where(LeaveType.is_active.is_(True)).order_by(LeaveType.name),
                LeaveType,
                user,
            )
        )
    ).all()
    return TeamBalancesRead(
        year=year,
        leave_types=[
            TeamBalanceColumn(
                leave_type_id=lt.id,
                name=lt.name,
                code=lt.code,
                is_paid=lt.is_paid,
                is_statutory=lt.is_statutory,
                is_comp_off=lt.is_comp_off,
            )
            for lt in leave_types
        ],
        items=[
            TeamBalanceRow(
                employee_id=employee.id,
                employee_code=employee.employee_code,
                employee_name=_name(employee),
                photo_url=employee.photo_url,
                designation=employee.designation,
                department_name=departments.get(employee.department_id)
                if employee.department_id
                else None,
                balances=[
                    TeamBalanceCell(
                        leave_type_id=line.leave_type.id,
                        entitled=line.balance.opening
                        + line.balance.accrued
                        + line.balance.carried_forward
                        + line.balance.adjusted,
                        used=line.balance.used,
                        encashed=line.balance.encashed,
                        pending=line.balance.pending,
                        available=line.balance.available,
                    )
                    for line in lines[employee.id]
                ],
            )
            for employee in employees
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


async def export_csv(
    session: AsyncSession,
    user: User,
    *,
    year: int,
    q: str | None = None,
    department_id: uuid.UUID | None = None,
    request: Request | None = None,
) -> str:
    """Every matching person's balances, one row per leave type. Recorded as an export."""
    scope = _team(user, await permissions.visible_employee_ids(session, user))
    employees = list(
        (await session.scalars(_people(user, scope, q=q, department_id=department_id))).all()
    )
    lines = await service.balance_lines(session, user, employees, year)
    departments = await _department_names(session, user, employees)

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(EXPORT_COLUMNS)
    rows = 0
    for employee in employees:
        for line in lines[employee.id]:
            balance = line.balance
            writer.writerow(
                [
                    _cell(value)
                    for value in (
                        employee.employee_code,
                        _name(employee),
                        departments.get(employee.department_id) if employee.department_id else None,
                        employee.designation,
                        line.leave_type.name,
                        line.leave_type.code,
                        year,
                        # Two places throughout: a ledger column reads "8.00"
                        # and an untouched one plain "0" otherwise.
                        *(
                            f"{figure:.2f}"
                            for figure in (
                                balance.opening,
                                balance.accrued,
                                balance.carried_forward,
                                balance.adjusted,
                                balance.used,
                                balance.encashed,
                                balance.pending,
                                balance.available,
                            )
                        ),
                    )
                ]
            )
            rows += 1
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.EXPORT,
        entity_type="leave_balance",
        summary=f"Exported {year} leave balances for {len(employees)} people as CSV",
        changes={
            "year": year,
            "people": len(employees),
            "rows": rows,
            "q": q,
            "department_id": str(department_id) if department_id else None,
        },
        request=request,
    )
    return buffer.getvalue()
