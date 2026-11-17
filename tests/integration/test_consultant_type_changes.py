"""Switching to and from consultant is guarded."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.consultant_hours import ConsultantHoursEntry, HoursEntryStatus
from app.models.consultant_payouts import (
    ConsultantPayoutEntry,
    ConsultantPayoutLine,
    ConsultantPayoutRun,
    PayoutRunStatus,
)
from app.models.contractors import TdsSection
from app.models.employee import ApproverRoute, EmploymentType
from app.models.payroll import EmployeeSalary, SalaryStructure
from app.models.timesheets import Timesheet, TimesheetStatus
from app.schemas.employee import EmployeeCreate, EmployeeUpdate
from app.services import employee as employee_service
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db


def _approved_hours(tenant, person) -> ConsultantHoursEntry:
    return ConsultantHoursEntry(
        tenant_id=tenant.id,
        employee_id=person.id,
        period_start=date(2026, 11, 2),
        period_end=date(2026, 11, 8),
        total_billable_hours=Decimal("40"),
        status=HoursEntryStatus.APPROVED,
    )


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


async def test_type_change_guard_uses_unpaid_entries(session, tenant, hr_user) -> None:
    caller = await as_caller(session, hr_user)
    person = await employee_service.create_employee(
        session, caller, _person("T2", EmploymentType.CONSULTANT)
    )
    # An approved timesheet week is tracking only: it no longer holds anybody back.
    session.add(
        Timesheet(
            tenant_id=tenant.id,
            employee_id=person.id,
            week_start=date(2026, 10, 5),
            status=TimesheetStatus.APPROVED,
        )
    )
    session.add(_approved_hours(tenant, person))
    await session.commit()

    with pytest.raises(HTTPException) as refused:
        await employee_service.update_employee(
            session, caller, person.id, EmployeeUpdate(employment_type=EmploymentType.FULL_TIME)
        )
    assert refused.value.status_code == 409
    assert "approved hours not yet paid" in refused.value.detail


async def test_a_lone_approved_week_does_not_block_a_type_change(session, tenant, hr_user) -> None:
    caller = await as_caller(session, hr_user)
    person = await employee_service.create_employee(
        session, caller, _person("T6", EmploymentType.CONSULTANT)
    )
    session.add(
        Timesheet(
            tenant_id=tenant.id,
            employee_id=person.id,
            week_start=date(2026, 11, 2),
            status=TimesheetStatus.APPROVED,
        )
    )
    await session.commit()

    updated = await employee_service.update_employee(
        session, caller, person.id, EmployeeUpdate(employment_type=EmploymentType.FULL_TIME)
    )
    assert updated.employment_type is EmploymentType.FULL_TIME


async def test_can_stop_being_consultant_once_hours_are_paid(session, tenant, hr_user) -> None:
    caller = await as_caller(session, hr_user)
    person = await employee_service.create_employee(
        session, caller, _person("T4", EmploymentType.CONSULTANT)
    )
    entry = _approved_hours(tenant, person)
    run = ConsultantPayoutRun(
        tenant_id=tenant.id,
        cut_off=date(2026, 11, 8),
        pay_date=date(2026, 11, 10),
        status=PayoutRunStatus.PAID,
    )
    session.add_all([entry, run])
    await session.flush()
    line = ConsultantPayoutLine(
        tenant_id=tenant.id,
        run_id=run.id,
        employee_id=person.id,
        section=TdsSection.S194J_PROFESSIONAL,
    )
    session.add(line)
    await session.flush()
    session.add(
        ConsultantPayoutEntry(
            tenant_id=tenant.id, run_id=run.id, line_id=line.id, entry_id=entry.id
        )
    )
    await session.commit()

    updated = await employee_service.update_employee(
        session, caller, person.id, EmployeeUpdate(employment_type=EmploymentType.FULL_TIME)
    )
    assert updated.employment_type is EmploymentType.FULL_TIME


async def test_hours_in_an_unpaid_run_still_block(session, tenant, hr_user) -> None:
    caller = await as_caller(session, hr_user)
    person = await employee_service.create_employee(
        session, caller, _person("T5", EmploymentType.CONSULTANT)
    )
    entry = _approved_hours(tenant, person)
    run = ConsultantPayoutRun(
        tenant_id=tenant.id,
        cut_off=date(2026, 11, 8),
        pay_date=date(2026, 11, 10),
        status=PayoutRunStatus.APPROVED,
    )
    session.add_all([entry, run])
    await session.flush()
    line = ConsultantPayoutLine(
        tenant_id=tenant.id,
        run_id=run.id,
        employee_id=person.id,
        section=TdsSection.S194J_PROFESSIONAL,
    )
    session.add(line)
    await session.flush()
    session.add(
        ConsultantPayoutEntry(
            tenant_id=tenant.id, run_id=run.id, line_id=line.id, entry_id=entry.id
        )
    )
    await session.commit()

    with pytest.raises(HTTPException) as refused:
        await employee_service.update_employee(
            session, caller, person.id, EmployeeUpdate(employment_type=EmploymentType.FULL_TIME)
        )
    assert refused.value.status_code == 409


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


@pytest.mark.parametrize("adjustment_status", ["pending", "approved"])
async def test_cannot_become_consultant_with_timesheet_pay_waiting_in_payroll(
    session, tenant, hr_user, adjustment_status
) -> None:
    from app.models.payroll_inputs import (
        AdjustmentCategory,
        AdjustmentKind,
        AdjustmentRecurrence,
        AdjustmentStatus,
        PayrollAdjustment,
    )

    caller = await as_caller(session, hr_user)
    person = await employee_service.create_employee(session, caller, _person("T7"))
    session.add(
        PayrollAdjustment(
            tenant_id=tenant.id,
            employee_id=person.id,
            kind=AdjustmentKind.EARNING,
            category=AdjustmentCategory.OTHER_EARNING,
            name="Hours, week of 02 Nov 2026",
            amount=Decimal("9600"),
            recurrence=AdjustmentRecurrence.ONE_TIME,
            start_year=2026,
            start_month=11,
            reference=f"timesheet:{uuid.uuid4()}",
            status=AdjustmentStatus(adjustment_status),
            reason="Approved timesheet, 8 hours.",
            created_by_user_id=hr_user.id,
        )
    )
    await session.commit()

    with pytest.raises(HTTPException) as refused:
        await employee_service.update_employee(
            session, caller, person.id, EmployeeUpdate(employment_type=EmploymentType.CONSULTANT)
        )
    assert refused.value.status_code == 409
    assert "Payroll" in refused.value.detail


async def test_open_hours_entries_block_leaving_consultant(session, tenant, hr_user) -> None:
    caller = await as_caller(session, hr_user)
    person = await employee_service.create_employee(
        session, caller, _person("T9", EmploymentType.CONSULTANT)
    )
    entry = _approved_hours(tenant, person)
    entry.status = HoursEntryStatus.DRAFT
    session.add(entry)
    await session.commit()

    for open_status in (HoursEntryStatus.DRAFT, HoursEntryStatus.SUBMITTED):
        entry.status = open_status
        await session.commit()
        with pytest.raises(HTTPException) as refused:
            await employee_service.update_employee(
                session,
                caller,
                person.id,
                EmployeeUpdate(employment_type=EmploymentType.FULL_TIME),
            )
        assert refused.value.status_code == 409
        assert refused.value.detail == "Finish or reject their open hours entries first."

    # A rejected entry is not open: the change goes through.
    entry.status = HoursEntryStatus.REJECTED
    await session.commit()
    updated = await employee_service.update_employee(
        session, caller, person.id, EmployeeUpdate(employment_type=EmploymentType.FULL_TIME)
    )
    assert updated.employment_type is EmploymentType.FULL_TIME
