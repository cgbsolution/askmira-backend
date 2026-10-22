"""Arrears reach the returns.

An arrear payslip deducts the employee's PF and ESI arrears. Until they are on a
return they are deducted and never remitted, so: the PF arrear ECR for the month
the arrears were paid with, and ESI arrears on that month's ESIC file.
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import AttendancePeriodLock
from app.models.employee import Employee
from app.models.filings import FilingStatus, ReturnType
from app.models.org import LegalEntity
from app.models.payroll import (
    EmployeePfElection,
    PayrollRuleSet,
    PayrollRun,
    PayrollRunStatus,
    Payslip,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.services.arrears import service as arrears_service
from app.services.filings import service as filing_service
from app.services.payroll import statutory_service
from tests.integration.conftest import requires_db
from tests.integration.test_arrears_flow import (
    _revise,
    _salary_from,
    _second_admin,
    _structure,
)

pytestmark = requires_db

PF_RULES = {
    "wage_ceiling": 15000,
    "employee_rate": 12,
    "employer_rate": 12,
    "eps_wage_ceiling": 15000,
    "edli_wage_ceiling": 15000,
    "eps_rate": 8.33,
    "apply_wage_ceiling": True,
}
ESI_RULES = {
    "wage_threshold": 21000,
    "employee_rate": "0.75",
    "employer_rate": "3.25",
    "contribution_periods": [
        {"start_month": 4, "end_month": 9, "label": "APR-SEP"},
        {"start_month": 10, "end_month": 3, "label": "OCT-MAR"},
    ],
}


async def _rules(session: AsyncSession, tenant: Tenant, *, esi: bool = False) -> PayrollRuleSet:
    pf = PayrollRuleSet(
        tenant_id=tenant.id,
        rule_type="pf",
        jurisdiction="IN",
        effective_from=date(2026, 1, 1),
        rules=PF_RULES,
        approved_at=datetime.now(UTC),
    )
    session.add(pf)
    esi_rules = None
    if esi:
        esi_rules = PayrollRuleSet(
            tenant_id=tenant.id,
            rule_type="esi",
            jurisdiction="IN",
            effective_from=date(2026, 1, 1),
            rules=ESI_RULES,
            approved_at=datetime.now(UTC),
        )
        session.add(esi_rules)
    await session.commit()
    return esi_rules or pf


def _items(basic: str, special: str, pf: str, esi: tuple[str, str] | None = None) -> list[dict]:
    rows = [
        {"code": "BASIC", "name": "Basic", "type": "earning", "amount": basic},
        {"code": "SPECIAL", "name": "Special allowance", "type": "earning", "amount": special},
        {"code": "PF_EMPLOYEE", "name": "PF (employee)", "type": "deduction", "amount": pf},
        {
            "code": "PF_EMPLOYER",
            "name": "PF (employer)",
            "type": "employer_contribution",
            "amount": pf,
        },
    ]
    if esi:
        rows += [
            {"code": "ESI_EMPLOYEE", "name": "ESI (employee)", "type": "deduction",
             "amount": esi[0]},
            {"code": "ESI_EMPLOYER", "name": "ESI (employer)",
             "type": "employer_contribution", "amount": esi[1]},
        ]
    return rows


async def _paid_month(
    session: AsyncSession,
    tenant: Tenant,
    legal_entity: LegalEntity,
    employee: Employee,
    month: int,
    *,
    basic: str,
    special: str,
    pf: str,
    esi: tuple[str, str] | None = None,
    status: PayrollRunStatus = PayrollRunStatus.PAID,
    lock: bool = True,
) -> PayrollRun:
    """A paid month with one payslip, its wage bases and line items as payroll writes them."""
    last = calendar.monthrange(2026, month)[1]
    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=legal_entity.id,
        period_year=2026,
        period_month=month,
        effective_date=date(2026, month, last),
        status=status,
    )
    session.add(run)
    await session.flush()
    gross = Decimal(basic) + Decimal(special)
    session.add(
        Payslip(
            tenant_id=tenant.id,
            run_id=run.id,
            employee_id=employee.id,
            working_days=Decimal(last),
            paid_days=Decimal(last),
            lop_days=Decimal(0),
            gross_earnings=gross,
            net_pay=gross,
            pf_wage_base=Decimal(basic),
            esi_wage_base=gross if esi else Decimal(0),
            pf_employee=Decimal(pf),
            pf_employer=Decimal(pf),
            esi_employee=Decimal(esi[0]) if esi else Decimal(0),
            esi_employer=Decimal(esi[1]) if esi else Decimal(0),
            line_items=_items(basic, special, pf, esi),
        )
    )
    if lock:
        session.add(
            AttendancePeriodLock(
                tenant_id=tenant.id,
                period_start=date(2026, 1, 1),
                period_end=date(2099, 12, 31),
                locked_at=datetime.now(UTC),
            )
        )
    await session.commit()
    return run


async def _identified(session: AsyncSession, employee: Employee) -> Employee:
    employee.uan = "100123456789"
    employee.esi_number = "3112345678"
    await session.commit()
    return employee


async def _paid_with_october(
    session, tenant, hr_user, legal_entity, employee, salary, effective: date, **october
):
    """Revise from `effective`, approve, and pay the arrears with October's run."""
    batch = await _revise(session, hr_user, legal_entity, employee, salary, effective)
    approver = await _second_admin(session, tenant)
    await arrears_service.approve_batch(
        session, approver, batch_id=batch.id, acknowledge_filings=True
    )
    run = await _paid_month(
        session, tenant, legal_entity, employee, 10, status=PayrollRunStatus.APPROVED,
        lock=False, **october,
    )
    await arrears_service.mark_arrears_paid(
        session, approver, batch_id=batch.id, payroll_run_id=run.id
    )
    await session.commit()
    return batch


async def test_pf_arrears_paid_with_october_are_filed_on_its_arrear_ecr(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """Below the ceiling, a raise moves every arrear column.

    May's basic 7,600 becomes 8,800: 1,200 more EPF, EPS and EDLI wages, 144 more
    from the employee and the employer, of which the pension takes 733 - 633.
    """
    await _identified(session, employee)
    await _rules(session, tenant)
    await _paid_month(
        session, tenant, legal_entity, employee, 5, basic="7600.00", special="11400.00", pf="912.00"
    )
    structure = await _structure(session, tenant)
    salary = await _salary_from(session, hr_user, employee, structure, "264000", date(2026, 5, 1))
    await _paid_with_october(
        session, tenant, hr_user, legal_entity, employee, salary, date(2026, 5, 1),
        basic="8800.00", special="13200.00", pf="1056.00",
    )

    generated = await filing_service.generate_arrear_ecr(
        session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=10
    )
    await session.commit()

    assert generated.content == (
        "100123456789#~#Anita Sharma#~#1200#~#1200#~#1200#~#144#~#44#~#100\r\n"
    )
    assert generated.record.return_type is ReturnType.PF_ECR_ARREAR
    assert generated.record.status is FilingStatus.GENERATED
    assert generated.record.total_amount == Decimal("288.00")


async def test_on_actual_wages_the_arrear_raises_epf_wages_but_not_eps(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    await _identified(session, employee)
    await _rules(session, tenant)
    session.add(
        EmployeePfElection(
            tenant_id=tenant.id,
            employee_id=employee.id,
            effective_from=date(2026, 1, 1),
            pf_on_actual_wage=True,
        )
    )
    await session.commit()
    await _paid_month(
        session, tenant, legal_entity, employee, 3, basic="40000.00", special="60000.00",
        pf="4800.00",
    )
    structure = await _structure(session, tenant)
    salary = await _salary_from(session, hr_user, employee, structure, "1500000", date(2026, 3, 1))
    await _paid_with_october(
        session, tenant, hr_user, legal_entity, employee, salary, date(2026, 3, 1),
        basic="50000.00", special="75000.00", pf="6000.00",
    )

    generated = await filing_service.generate_arrear_ecr(
        session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=10
    )
    assert generated.content == (
        "100123456789#~#Anita Sharma#~#10000#~#0#~#0#~#1200#~#1200#~#0\r\n"
    ), "EPS and EDLI wages stay at their ceilings; the whole employer arrear is EPF"


async def test_a_raise_above_the_ceiling_leaves_nothing_for_the_arrear_ecr(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    await _identified(session, employee)
    await _rules(session, tenant)
    await _paid_month(
        session, tenant, legal_entity, employee, 3, basic="40000.00", special="60000.00",
        pf="1800.00",
    )
    structure = await _structure(session, tenant)
    salary = await _salary_from(session, hr_user, employee, structure, "1500000", date(2026, 3, 1))
    await _paid_with_october(
        session, tenant, hr_user, legal_entity, employee, salary, date(2026, 3, 1),
        basic="50000.00", special="75000.00", pf="1800.00",
    )

    with pytest.raises(filing_service.FilingBlockedError) as exc:
        await filing_service.generate_arrear_ecr(
            session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=10
        )
    await session.commit()
    assert [e.code for e in exc.value.result.blocking] == ["NOTHING_TO_FILE"]
    assert exc.value.record.status is FilingStatus.BLOCKED


async def test_a_month_that_paid_no_arrears_has_no_arrear_ecr(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    from fastapi import HTTPException

    await _rules(session, tenant)
    await _paid_month(
        session, tenant, legal_entity, employee, 10, basic="7600.00", special="11400.00",
        pf="912.00", status=PayrollRunStatus.APPROVED, lock=False,
    )
    with pytest.raises(HTTPException) as exc:
        await filing_service.generate_arrear_ecr(
            session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=10
        )
    assert exc.value.status_code == 409
    assert "No PF arrears were paid" in exc.value.detail


async def test_esi_arrears_join_the_esic_return_of_the_month_they_are_paid_in(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    """May's ESI wages go from 19,000 to 22,000; October's return carries the 3,000."""
    await _identified(session, employee)
    esi = await _rules(session, tenant, esi=True)
    await statutory_service.open_esi_coverage_period(
        session,
        hr_user,
        employee_id=employee.id,
        on_date=date(2026, 4, 1),
        esi_wage_base=Decimal("19000"),
        esi_rules=esi,
    )
    await session.commit()
    await _paid_month(
        session, tenant, legal_entity, employee, 5, basic="7600.00", special="11400.00",
        pf="912.00", esi=("143.00", "618.00"),
    )
    structure = await _structure(session, tenant)
    salary = await _salary_from(session, hr_user, employee, structure, "264000", date(2026, 5, 1))
    await _paid_with_october(
        session, tenant, hr_user, legal_entity, employee, salary, date(2026, 5, 1),
        basic="8000.00", special="12000.00", pf="960.00", esi=("150.00", "650.00"),
    )

    generated = await filing_service.generate_esic(
        session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=10
    )
    line = generated.content.split("\r\n")[1].split(",")
    assert line[0] == "3112345678"
    assert line[3] == "23000", "October's 20,000 plus May's 3,000 of arrear ESI wages"
    assert "ARREARS_INCLUDED" in {e.code for e in generated.result.warnings}


async def test_esi_arrears_for_an_ip_not_on_the_months_file_are_said_not_dropped(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    legal_entity: LegalEntity,
    employee: Employee,
) -> None:
    await _identified(session, employee)
    esi = await _rules(session, tenant, esi=True)
    await statutory_service.open_esi_coverage_period(
        session,
        hr_user,
        employee_id=employee.id,
        on_date=date(2026, 4, 1),
        esi_wage_base=Decimal("19000"),
        esi_rules=esi,
    )
    await session.commit()
    await _paid_month(
        session, tenant, legal_entity, employee, 5, basic="7600.00", special="11400.00",
        pf="912.00", esi=("143.00", "618.00"),
    )
    structure = await _structure(session, tenant)
    salary = await _salary_from(session, hr_user, employee, structure, "264000", date(2026, 5, 1))
    # By October the IP is out of coverage: no ESI on October's payslip.
    await _paid_with_october(
        session, tenant, hr_user, legal_entity, employee, salary, date(2026, 5, 1),
        basic="8800.00", special="13200.00", pf="1056.00",
    )

    generated = await filing_service.generate_esic(
        session, hr_user, legal_entity_id=legal_entity.id, year=2026, month=10
    )
    notes = [e for e in generated.result.warnings if e.code == "ARREAR_NOT_ON_FILE"]
    assert len(notes) == 1
    assert "Deposit them on the ESIC portal separately" in notes[0].message
