"""Earned wage access: drawing earned pay early, approved and recovered in payroll."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import AttendanceDay, AttendanceStatus
from app.models.employee import Employee
from app.models.payroll import (
    ComponentType,
    EmployeeSalary,
    EmployeeSalaryComponent,
    SalaryStructure,
)
from app.models.payroll_inputs import AdjustmentStatus, PayrollAdjustment
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services import ewa
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db

TODAY = date(2026, 9, 11)


async def _setup(session: AsyncSession, tenant: Tenant, employee: Employee) -> User:
    structure = SalaryStructure(
        tenant_id=tenant.id,
        name="Standard",
        effective_from=date(2025, 1, 1),
        applicable_grades=[],
        applicable_locations=[],
    )
    session.add(structure)
    await session.flush()
    salary = EmployeeSalary(
        tenant_id=tenant.id,
        employee_id=employee.id,
        structure_id=structure.id,
        ctc_annual=Decimal("360000"),
        effective_from=date(2026, 4, 1),
    )
    session.add(salary)
    await session.flush()
    session.add(
        EmployeeSalaryComponent(
            tenant_id=tenant.id,
            employee_salary_id=salary.id,
            component_code="GROSS",
            component_name="Gross",
            component_type=ComponentType.EARNING,
            annual_amount=Decimal("360000"),
            monthly_amount=Decimal("30000"),
        )
    )
    # Ten days judged so far this month: nine present, one half day.
    for day in range(1, 11):
        session.add(
            AttendanceDay(
                tenant_id=tenant.id,
                employee_id=employee.id,
                attendance_date=date(2026, 9, day),
                status=AttendanceStatus.HALF_DAY if day == 5 else AttendanceStatus.PRESENT,
                payable_fraction=Decimal("0.5") if day == 5 else Decimal("1"),
                computed_at=datetime(2026, 9, day + 1, tzinfo=UTC),
            )
        )
    user = User(
        tenant_id=tenant.id,
        email="anita.ewa@acme-demo.com",
        password_hash="x",
        full_name="Anita",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    return await as_caller(session, user)


async def test_an_advance_is_capped_by_earned_pay_and_recovered_in_payroll(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    me = await _setup(session, tenant, employee)

    with pytest.raises(HTTPException) as off:
        await ewa.request_advance(session, me, Decimal("1000"), today=TODAY)
    assert off.value.status_code == 409
    await ewa.set_policy(
        session, hr_user, ewa.EwaPolicy(enabled=True, max_percent=Decimal("50")), request=None
    )

    position = await ewa.available_for(session, me, employee.id, today=TODAY)
    # 30,000 a month, 9.5 of 30 days paid so far: 9,500 earned; half of it may be drawn.
    assert position["basis"] == "attendance"
    assert position["earned_to_date"] == "9500.00"
    assert position["available"] == "4750.00"

    with pytest.raises(HTTPException):
        await ewa.request_advance(session, me, Decimal("5000"), today=TODAY)
    asked = await ewa.request_advance(session, me, Decimal("3000"), today=TODAY)
    after = await ewa.available_for(session, me, employee.id, today=TODAY)
    assert after["available"] == "1750.00" and after["requests_left"] == 1

    decided = await ewa.decide(session, hr_user, uuid.UUID(asked["id"]), approve=True, note=None)
    assert decided["status"] == "approved"
    deduction = await session.scalar(select(PayrollAdjustment))
    assert deduction is not None
    assert deduction.status is AdjustmentStatus.APPROVED
    assert deduction.amount == Decimal("3000.00")
    assert (deduction.start_year, deduction.start_month) == (2026, 9)
    assert deduction.created_by_user_id == me.id and deduction.decided_by_user_id == hr_user.id

    paid = await ewa.mark_disbursed(
        session, hr_user, uuid.UUID(asked["id"]), paid_on=TODAY, reference="UTR123"
    )
    assert paid["status"] == "disbursed"
    mine = await ewa.list_requests(session, me)
    assert [r["id"] for r in mine] == [asked["id"]]
