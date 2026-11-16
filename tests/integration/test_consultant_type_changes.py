"""Switching to and from consultant is guarded."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.employee import ApproverRoute, EmploymentType
from app.models.payroll import EmployeeSalary, SalaryStructure
from app.models.timesheets import Timesheet, TimesheetStatus
from app.schemas.employee import EmployeeCreate, EmployeeUpdate
from app.services import employee as employee_service
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db


def _person(code: str, kind: EmploymentType = EmploymentType.FULL_TIME) -> EmployeeCreate:
    extra = (
        {"timesheet_approver_route": ApproverRoute.INTERNAL}
        if kind is EmploymentType.CONSULTANT
        else {}
    )
    return EmployeeCreate(
        employee_code=code,
        first_name="Pat",
        work_email=f"{code.lower()}-{uuid.uuid4().hex[:6]}@acme.example.com",
        employment_type=kind,
        **extra,
    )


async def test_cannot_become_consultant_with_current_salary(session, tenant, hr_user) -> None:
    caller = await as_caller(session, hr_user)
    person = await employee_service.create_employee(session, caller, _person("T1"))
    structure = SalaryStructure(
        tenant_id=tenant.id, name="Standard", effective_from=date(2024, 1, 1)
    )
    session.add(structure)
    await session.flush()
    session.add(
        EmployeeSalary(
            tenant_id=tenant.id,
            employee_id=person.id,
            structure_id=structure.id,
            ctc_annual=Decimal("600000"),
            effective_from=date(2024, 4, 1),
        )
    )
    await session.commit()

    with pytest.raises(HTTPException) as refused:
        await employee_service.update_employee(
            session, caller, person.id, EmployeeUpdate(employment_type=EmploymentType.CONSULTANT)
        )
    assert refused.value.status_code == 409
    assert "End their salary first" in refused.value.detail


async def test_cannot_stop_being_consultant_with_unpaid_weeks(session, tenant, hr_user) -> None:
    caller = await as_caller(session, hr_user)
    person = await employee_service.create_employee(
        session, caller, _person("T2", EmploymentType.CONSULTANT)
    )
    week = Timesheet(
        tenant_id=tenant.id,
        employee_id=person.id,
        week_start=date(2026, 11, 2),
        status=TimesheetStatus.APPROVED,
    )
    session.add(week)
    await session.commit()

    with pytest.raises(HTTPException) as refused:
        await employee_service.update_employee(
            session, caller, person.id, EmployeeUpdate(employment_type=EmploymentType.FULL_TIME)
        )
    assert refused.value.status_code == 409
    assert "approved weeks not yet paid" in refused.value.detail


async def test_a_salary_ended_in_the_past_does_not_block_becoming_consultant(
    session, tenant, hr_user
) -> None:
    caller = await as_caller(session, hr_user)
    person = await employee_service.create_employee(session, caller, _person("T3"))
    structure = SalaryStructure(
        tenant_id=tenant.id, name="Standard", effective_from=date(2024, 1, 1)
    )
    session.add(structure)
    await session.flush()
    session.add(
        EmployeeSalary(
            tenant_id=tenant.id,
            employee_id=person.id,
            structure_id=structure.id,
            ctc_annual=Decimal("600000"),
            effective_from=date(2024, 4, 1),
            effective_to=date(2025, 3, 31),
        )
    )
    await session.commit()

    updated = await employee_service.update_employee(
        session,
        caller,
        person.id,
        EmployeeUpdate(
            employment_type=EmploymentType.CONSULTANT,
            timesheet_approver_route=ApproverRoute.INTERNAL,
        ),
    )
    assert updated.employment_type is EmploymentType.CONSULTANT
