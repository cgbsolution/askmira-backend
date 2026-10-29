"""TDS for a year that began on another payroll.

A company that moves to AskMira in September paid April to August on its old
payroll. The run must count those months as income — at the salary structure —
or it sees part of a year, finds it under the rebate limit and deducts nothing.
Their tax was deducted on the old payroll; it is taken at the year's steady
rate, so September onwards deducts a twelfth of the year's tax, not the whole
year again. The fixture's rule set is a flat 10% with no deductions: 100,000 a
month taxable, 1,200,000 a year, 120,000 tax, 10,000 a month.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org import LegalEntity, Location
from app.models.payroll import Payslip, SalaryStructure
from app.models.tenant import Tenant
from app.models.user import UserRole
from app.schemas.payroll import EmployeeSalaryCreate
from app.services.payroll import run as run_service
from app.services.payroll import service as salary_service
from app.services.payroll.year_pay import month_span, year_pay
from tests.integration.conftest import requires_db
from tests.integration.test_payroll_run_flow import (  # noqa: F401  (fixture)
    _approved_rules,
    _lock,
    _open_run,
    _person,
    org,
)

pytestmark = requires_db

FY_START, FY_END = date(2026, 4, 1), date(2027, 3, 31)


async def _computed(session: AsyncSession, org, month: int):
    session.add(_lock(org["tenant"], org["maker"], 2026, month))
    await session.commit()
    run = await _open_run(session, org, month)
    return await run_service.compute_run(session, org["maker"], run.id)


async def _slip(session: AsyncSession, run_id, employee_id) -> Payslip:
    slip = await session.scalar(
        select(Payslip).where(Payslip.run_id == run_id, Payslip.employee_id == employee_id)
    )
    assert slip is not None
    return slip


async def _structure_of(session: AsyncSession, tenant: Tenant) -> SalaryStructure:
    structure = await session.scalar(
        select(SalaryStructure).where(SalaryStructure.tenant_id == tenant.id)
    )
    assert structure is not None
    return structure


async def test_months_paid_on_the_old_payroll_count_as_income(session: AsyncSession, org) -> None:
    await _approved_rules(session, org)
    run = await _computed(session, org, 9)

    slip = await _slip(session, run.id, org["meera_emp"].id)
    working = slip.tds_working
    projection = working["projection"]
    assert projection["earlier_months_from_salary_structure"] == 5
    assert Decimal(projection["earlier_months_taxable_pay"]) == Decimal("500000.00")
    # The whole year is income: 120,000 tax, not the 70,000 of seven months.
    assert Decimal(working["annual_liability"]) == Decimal("120000.00")
    # April-August's tax taken at the steady rate: five twelfths.
    assert Decimal(working["tds_assumed_deducted_by_previous_payroll"]) == Decimal("50000.00")
    assert Decimal(str(slip.tds)) == Decimal("10000.00")

    notices = [n for n in run.input_notices if n.get("key") == "tds_earlier_months"]
    assert len(notices) == 1
    message = notices[0]["message"]
    assert message.startswith("Apr-Aug 2026 was paid on your previous payroll for")
    assert "MEERA" in message
    assert "off by the difference" in message


async def test_a_month_paid_here_is_not_counted_twice(session: AsyncSession, org) -> None:
    await _approved_rules(session, org)
    august = await _computed(session, org, 8)
    aug_slip = await _slip(session, august.id, org["meera_emp"].id)
    assert aug_slip.tds_working["projection"]["earlier_months_from_salary_structure"] == 4
    assert Decimal(str(aug_slip.tds)) == Decimal("10000.00")
    await run_service.approve_run(session, org["checker"], august.id)

    september = await _computed(session, org, 9)
    slip = await _slip(session, september.id, org["meera_emp"].id)
    # AskMira began in August: April-July only, and August from its payslip.
    assert slip.tds_working["projection"]["earlier_months_from_salary_structure"] == 4
    # (120,000 - 40,000 assumed - 10,000 deducted in August) over seven months.
    assert Decimal(str(slip.tds)) == Decimal("10000.00")


async def test_a_month_after_the_company_started_here_is_not_assumed(
    session: AsyncSession, org
) -> None:
    """A gap after the company's first AskMira run was not paid on another payroll."""
    await _approved_rules(session, org)
    april = await _computed(session, org, 4)
    await run_service.approve_run(session, org["checker"], april.id)

    september = await _computed(session, org, 9)
    slip = await _slip(session, september.id, org["meera_emp"].id)
    assert slip.tds_working["projection"]["earlier_months_from_salary_structure"] == 0
    assert not [n for n in september.input_notices if n.get("key") == "tds_earlier_months"]


async def test_a_run_from_april_has_no_earlier_months(session: AsyncSession, org) -> None:
    await _approved_rules(session, org)
    run = await _computed(session, org, 4)
    slip = await _slip(session, run.id, org["meera_emp"].id)
    assert slip.tds_working["projection"]["earlier_months_from_salary_structure"] == 0
    assert Decimal(slip.tds_working["tds_assumed_deducted_by_previous_payroll"]) == 0
    assert Decimal(str(slip.tds)) == Decimal("10000.00")
    assert not [n for n in run.input_notices if n.get("key") == "tds_earlier_months"]


async def test_a_salary_loaded_from_a_later_revision_covers_the_earlier_months(
    session: AsyncSession,
    org,
    tenant: Tenant,
    legal_entity: LegalEntity,
    location: Location,
) -> None:
    """The salary master holds only September's revision: April-August use it, and say so."""
    await _approved_rules(session, org)
    asha, _ = await _person(
        session, tenant, legal_entity, location, code="ASHA", role=UserRole.EMPLOYEE
    )
    await salary_service.assign_salary(
        session,
        org["maker"],
        asha.id,
        EmployeeSalaryCreate(
            structure_id=(await _structure_of(session, tenant)).id,
            ctc_annual=Decimal("1200000"),
            effective_from=date(2026, 9, 1),
        ),
    )

    run = await _computed(session, org, 9)
    slip = await _slip(session, run.id, asha.id)
    assert slip.tds_working["projection"]["earlier_months_from_salary_structure"] == 5
    assert Decimal(str(slip.tds)) == Decimal("10000.00")
    assumed = [
        n["message"]
        for n in run.input_notices
        if n.get("key") == "tds_earlier_months" and n["message"].startswith("ASHA:")
    ]
    assert assumed == [
        "ASHA: No salary is recorded for Apr-Aug 2026: counted at the salary from "
        "01 Sep 2026. Add the earlier salary in the salary master if it was different."
    ]

    # The employee's own projection reads the year the same way.
    pay = await year_pay(session, org["maker"], asha.id, FY_START, FY_END)
    assert pay.months_projected == 12
    assert pay.taxable_salary == Decimal("1200000")


async def test_a_mid_year_joiner_counts_only_months_worked(
    session: AsyncSession,
    org,
    tenant: Tenant,
    legal_entity: LegalEntity,
    location: Location,
) -> None:
    await _approved_rules(session, org)
    joiner, _ = await _person(
        session, tenant, legal_entity, location, code="JOINER", role=UserRole.EMPLOYEE
    )
    joiner.date_of_joining = date(2026, 7, 16)
    await session.commit()
    await salary_service.assign_salary(
        session,
        org["maker"],
        joiner.id,
        EmployeeSalaryCreate(
            structure_id=(await _structure_of(session, tenant)).id,
            ctc_annual=Decimal("1200000"),
            effective_from=date(2026, 7, 16),
        ),
    )

    run = await _computed(session, org, 9)
    slip = await _slip(session, run.id, joiner.id)
    working = slip.tds_working
    projection = working["projection"]
    # 16 of July's 31 days, and August. Nothing before joining, nothing assumed.
    assert projection["earlier_months_from_salary_structure"] == 2
    assert Decimal(projection["earlier_months_taxable_pay"]) == Decimal("151612.90")
    # 851,612.90 a year, 85,161.29 tax; July and August are two of nine months.
    assert Decimal(working["annual_liability"]) == Decimal("85161.29")
    assert Decimal(working["tds_assumed_deducted_by_previous_payroll"]) == Decimal("18924.73")
    assert Decimal(str(slip.tds)) == Decimal("9462.00")
    assert not [n for n in run.input_notices if n.get("message", "").startswith("JOINER:")]


def test_month_span() -> None:
    assert month_span([(2026, 4), (2026, 5), (2026, 8)]) == "Apr-Aug 2026"
    assert month_span([(2026, 9)]) == "Sep 2026"
    assert month_span([(2026, 12), (2027, 1)]) == "Dec 2026-Jan 2027"
