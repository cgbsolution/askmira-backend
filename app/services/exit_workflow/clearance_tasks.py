"""Clearance items waiting on the caller, whatever their login role.

A clearance item can name its owner — the IT admin who collects the laptop, the
librarian — and that person often signs in as a plain employee. The checklist
used to be reachable only through the exits screen, which is for managers and
HR, so a named owner could be assigned an item and never be able to sign it
off. This lists what waits on the caller: items that name them, and, for a line
manager, the reporting-manager items of people leaving their team.

Who may *sign* an item off is still `operations.can_update_item`; this module
only finds the items.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.employee import Employee
from app.models.exit_workflow import (
    ClearanceItem,
    ClearanceOwnerRole,
    ClearanceStatus,
    ExitRecord,
    ExitStatus,
)
from app.models.user import User, UserRole

__all__ = ["inbox_items", "my_tasks"]

_CLOSED = (ExitStatus.CANCELLED, ExitStatus.SETTLED)


async def _rows(
    session: AsyncSession, user: User
) -> list[tuple[ClearanceItem, ExitRecord, Employee]]:
    mine = and_(
        ClearanceItem.owner_role == ClearanceOwnerRole.NAMED_USER.value,
        ClearanceItem.owner_user_id == user.id,
    )
    conditions = [mine]
    if user.role is UserRole.MANAGER:
        team = await permissions.reporting_line(session, user)
        if team:
            conditions.append(
                and_(
                    ClearanceItem.owner_role == ClearanceOwnerRole.REPORTING_MANAGER.value,
                    ExitRecord.employee_id.in_(team),
                )
            )
    stmt = tenant_scoped(
        select(ClearanceItem, ExitRecord, Employee)
        .join(ExitRecord, ExitRecord.id == ClearanceItem.exit_record_id)
        .join(Employee, Employee.id == ExitRecord.employee_id)
        .where(
            ClearanceItem.status == ClearanceStatus.PENDING,
            ExitRecord.status.notin_(_CLOSED),
            or_(*conditions),
        )
        .order_by(ExitRecord.created_at, ClearanceItem.department, ClearanceItem.created_at),
        ClearanceItem,
        user,
    )
    # Never your own exit: somebody else signs that off.
    if user.employee_id is not None:
        stmt = stmt.where(ExitRecord.employee_id != user.employee_id)
    return [tuple(row) for row in (await session.execute(stmt)).all()]  # type: ignore[misc]


async def my_tasks(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Pending clearance items that wait on the caller, oldest exit first."""
    return [
        {
            "item_id": item.id,
            "exit_id": record.id,
            "employee_id": employee.id,
            "employee_name": employee.full_name,
            "employee_code": employee.employee_code,
            "photo_url": employee.photo_url,
            "last_working_day": record.approved_last_working_day
            or record.requested_last_working_day,
            "department": item.department,
            "description": item.description,
            "is_mandatory": item.is_mandatory,
            "owner_role": item.owner_role,
            "created_at": item.created_at,
        }
        for item, record, employee in await _rows(session, user)
    ]


async def inbox_items(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """The same tasks, for the approvals inbox."""
    return [
        {
            "id": item.id,
            "title": f"Clearance: {item.department}, {item.description}",
            "subject_name": employee.full_name,
            "detail": "Mandatory" if item.is_mandatory else "Optional",
            "submitted_at": item.created_at,
            "due_on": record.approved_last_working_day or record.requested_last_working_day,
        }
        for item, record, employee in await _rows(session, user)
    ]
