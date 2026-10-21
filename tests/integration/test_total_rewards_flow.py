"""The total-rewards statement, read from the salary and payslips on record."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee, EmploymentStatus
from app.models.org import LegalEntity
from app.models.payroll import (
    ComponentType,
    EmployeeSalary,
    EmployeeSalaryComponent,
    PayrollRun,
    PayrollRunStatus,
    Payslip,
    SalaryStructure,
)
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.payroll import total_rewards
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db


async def test_the_statement_adds_up_ctc_and_what_was_paid(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
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
        ctc_annual=Decimal("1200000"),
        effective_from=date(2026, 4, 1),
    )
    session.add(salary)
    await session.flush()
    for code, kind, annual in (
        ("BASIC", ComponentType.EARNING, "480000"),
        ("SPECIAL", ComponentType.EARNING, "662400"),
        ("PF_ER", ComponentType.EMPLOYER_CONTRIBUTION, "57600"),
        ("PF_EE", ComponentType.DEDUCTION, "57600"),
    ):
        session.add(
            EmployeeSalaryComponent(
                tenant_id=tenant.id,
                employee_salary_id=salary.id,
                component_code=code,
                component_name=code.title(),
                component_type=kind,
                annual_amount=Decimal(annual),
                monthly_amount=Decimal(annual) / 12,
            )
        )
    for month, status in ((4, PayrollRunStatus.PAID), (5, PayrollRunStatus.DRAFT)):
        run = PayrollRun(
            tenant_id=tenant.id,
            legal_entity_id=legal_entity.id,
            period_year=2026,
            period_month=month,
            effective_date=date(2026, month, 28),
            status=status,
        )
        session.add(run)
        await session.flush()
        session.add(
            Payslip(
                tenant_id=tenant.id,
                run_id=run.id,
                employee_id=employee.id,
                gross_earnings=Decimal("95200"),
                net_pay=Decimal("85000"),
                pf_employer=Decimal("4800"),
                esi_employer=Decimal("0"),
            )
        )
    await session.commit()

    result = await total_rewards.statement(session, hr_user, employee.id, on=date(2026, 6, 1))
    assert result["financial_year"] == "2026-27"
    assert result["ctc"]["annual"] == "1200000.00"
    assert result["ctc"]["earnings"] == "1142400.00"
    assert result["ctc"]["employer_contributions"] == "57600.00"
    assert all(c["type"] != "deduction" for c in result["ctc"]["components"])
    # Only the paid run counts; the draft does not.
    assert result["paid_this_year"]["payslips"] == 1
    assert result["paid_this_year"]["gross_earnings"] == "95200.00"
    assert result["not_included"]


async def test_only_the_person_and_hr_read_it(
    session: AsyncSession, tenant: Tenant, legal_entity: LegalEntity, employee: Employee
) -> None:
    other = Employee(
        tenant_id=tenant.id,
        employee_code="OTH",
        first_name="Other",
        work_email="other@acme-demo.com",
        legal_entity_id=legal_entity.id,
        status=EmploymentStatus.ACTIVE,
    )
    session.add(other)
    await session.flush()
    login = User(
        tenant_id=tenant.id,
        email="other.tr@acme-demo.com",
        password_hash="x",
        full_name="Other",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=other.id,
    )
    session.add(login)
    await session.commit()
    login = await as_caller(session, login)

    own = await total_rewards.statement(session, login, other.id)
    assert own["ctc"]["annual"] is None
    with pytest.raises(HTTPException):
        await total_rewards.statement(session, login, employee.id)
