"""Consultants: people paid hourly from approved timesheets, outside payroll."""

from __future__ import annotations

from app.models.employee import Employee, EmploymentType

#: Why a consultant cannot be given a salary.
CONSULTANT_SALARY_REFUSAL = "Consultants are paid through Consultant payouts, not a salary."


def is_consultant(employee: Employee) -> bool:
    """Whether this person is paid as a consultant. Always ask here, not by comparing the type."""
    return employee.employment_type is EmploymentType.CONSULTANT
