"""Consultants: people paid hourly from approved hours entries, outside payroll."""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.models.employee import Employee, EmploymentType
from app.models.payroll_inputs import AdjustmentStatus, PayrollAdjustment
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


#: A payroll adjustment in these states may still be paid by a payroll run. A
#: one-time adjustment becomes COMPLETED once a run applies it.
ADJUSTMENT_MAY_STILL_PAY = (AdjustmentStatus.PENDING, AdjustmentStatus.APPROVED)


def timesheet_pay_waiting_in_payroll() -> ColumnElement[bool]:
    """PayrollAdjustment rows raised from a timesheet that a payroll run may still pay."""
    return and_(
        PayrollAdjustment.reference.like("timesheet:%"),
        PayrollAdjustment.status.in_(ADJUSTMENT_MAY_STILL_PAY),
    )
