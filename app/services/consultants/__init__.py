"""Consultants: people paid hourly from approved timesheets, outside payroll."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.models.employee import Employee, EmploymentType
from app.models.user import User

#: Why a consultant cannot be given a salary.
CONSULTANT_SALARY_REFUSAL = "Consultants are paid through Consultant payouts, not a salary."


def is_consultant(employee: Employee) -> bool:
    """Whether this person is paid as a consultant. Always ask here, not by comparing the type."""
    return employee.employment_type is EmploymentType.CONSULTANT


async def can_manage_consultant(session: AsyncSession, user: User, employee: Employee) -> bool:
    """Whether this caller may act on a consultant's timesheets: HR, or their internal approver.

    Never true for a person who is not a consultant, nor for the consultant themself.
    """
    if not is_consultant(employee) or employee.id == user.employee_id:
        return False
    return permissions.is_hr(user) or employee.internal_approver_user_id == user.id
