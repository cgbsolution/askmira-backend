"""Salary master: a SALES INCENTIVE inside the CTC, paid when earned.

An offer reads "Annual fixed salary 12,00,000 + variable component (sales
incentive) 4,00,000 = CTC 16,00,000": Basic 50,000, HRA 20,000, Special 24,346
a month (gross 94,346), PF 3,000 at the Rs 25,000 ceiling, insurance 239 (take
home 91,107). The incentive counts in CTC TOTAL and nowhere else: not in gross,
take home, a payslip or the TDS projection. It is paid through Adjustments.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.employee import Employee
from app.models.payroll import (
    PAID_WHEN_EARNED_CODES,
    EmployeeSalary,
    PayrollRuleSet,
    SalaryComponent,
    SalaryStructure,
    WageGroup,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.payroll import EmployeeSalaryCreate, SalaryComponentAmountInput
from app.services.payroll import salary_master, standard_setup
from app.services.payroll import service as salary_service
from app.services.payroll.engine import PayrollInput, compute_payroll
from tests.integration.conftest import requires_db

pytestmark = requires_db

PF25 = {
    "wage_ceiling": 25000,
    "employee_rate": 12,
    "employer_rate": 12,
    "eps_wage_ceiling": 25000,
    "edli_wage_ceiling": 25000,
    "eps_rate": 8.33,
    "apply_wage_ceiling": True,
    "contribution_rounding": "nearest_rupee",
}

#: The offer letter, typed month-wise as HR does, the incentive a year.
OFFER = [
    ("BASIC", "50000", "monthly"),
    ("HRA", "20000", "monthly"),
    ("SPECIAL", "24346", "monthly"),
    ("INSURANCE", "239", "monthly"),
    ("SALES_INCENTIVE", "400000", "annual"),
]


@pytest.fixture
async def client(session: AsyncSession) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_session] = override
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
    )
    return {"Authorization": f"Bearer {token}"}


async def _setup(session: AsyncSession, tenant: Tenant, user: User) -> SalaryStructure:
    await standard_setup.install_standard(session, user)
    session.add(
        PayrollRuleSet(
            tenant_id=tenant.id,
            rule_type="pf",
            jurisdiction="IN",
            effective_from=date(2020, 1, 1),
            rules=PF25,
            approved_at=datetime.now(UTC),
        )
    )
    await session.commit()
    structure = await session.scalar(select(SalaryStructure).where(SalaryStructure.active))
    assert structure is not None
    return structure


async def _assign(session, user, employee, structure, offer=OFFER):
    return await salary_service.assign_salary(
        session,
        user,
        employee.id,
        EmployeeSalaryCreate(
            entry="manual",
            structure_id=structure.id,
            ctc_amount=Decimal("1600000"),
            ctc_basis="annual",
            effective_from=date(2026, 11, 23),
            components=[
                SalaryComponentAmountInput(component_code=c, amount=Decimal(a), basis=b)
                for c, a, b in offer
            ],
        ),
    )


async def test_the_standard_set_has_a_sales_incentive_in_variable_earning(
    session: AsyncSession, hr_user: User
) -> None:
    await standard_setup.install_standard(session, hr_user)
    row = await session.scalar(
        select(SalaryComponent).where(SalaryComponent.code == "SALES_INCENTIVE")
    )
    assert row is not None and row.name == "SALES INCENTIVE"
    assert row.wage_group == WageGroup.VARIABLE_EARNING.value
    assert row.part_of_ctc and row.taxable and not row.counts_toward_pf
    assert "SALES_INCENTIVE" in PAID_WHEN_EARNED_CODES


async def test_the_incentive_is_in_the_ctc_but_not_in_gross_or_take_home(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    structure = await _setup(session, tenant, hr_user)
    await _assign(session, hr_user, employee, structure)

    [revision] = await salary_master.list_revisions(session, hr_user, employee.id)
    assert revision.ctc_annual == Decimal("1600000.00")
    assert revision.variable_annual == Decimal("400000.00")
    assert revision.fixed_ctc_monthly == Decimal("100000.00")
    assert revision.gross_monthly == Decimal("94346.00")
    assert revision.gross_annual == Decimal("1132152.00")
    assert revision.take_home is not None
    assert revision.take_home.monthly == Decimal("91107.00")  # 94,346 - PF 3,000 - 239
    incentive = next(c for c in revision.components if c.component_code == "SALES_INCENTIVE")
    assert incentive.annual_amount == Decimal("400000.00")


async def test_payroll_never_pays_the_incentive_as_a_monthly_line(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    structure = await _setup(session, tenant, hr_user)
    salary = await _assign(session, hr_user, employee, structure)
    loaded = await session.scalar(
        select(EmployeeSalary)
        .options(selectinload(EmployeeSalary.components))
        .where(EmployeeSalary.id == salary.id)
    )
    pf = await session.scalar(select(PayrollRuleSet).where(PayrollRuleSet.rule_type == "pf"))
    result = compute_payroll(
        PayrollInput(
            employee_salary=loaded,
            period_start=date(2026, 12, 1),
            period_end=date(2026, 12, 31),
            working_days=Decimal(31),
            rule_sets={"pf": pf},
        )
    )
    assert "SALES_INCENTIVE" not in {line.code for line in result.lines}
    assert result.gross_earnings == Decimal("94346.00")
    assert result.pf_employee == Decimal("3000.00")


async def test_without_an_incentive_nothing_changes_on_the_card(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    structure = await _setup(session, tenant, hr_user)
    await _assign(session, hr_user, employee, structure, OFFER[:-1])
    [revision] = await salary_master.list_revisions(session, hr_user, employee.id)
    assert revision.variable_annual is None and revision.fixed_ctc_monthly is None


async def test_amounts_over_the_ctc_with_the_incentive_are_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    structure = await _setup(session, tenant, hr_user)
    too_much = [*OFFER[:-1], ("SALES_INCENTIVE", "600000", "annual")]
    with pytest.raises(Exception) as caught:
        await _assign(session, hr_user, employee, structure, too_much)
    assert "more than CTC TOTAL" in str(getattr(caught.value, "detail", caught.value))


async def test_calculate_splits_only_the_fixed_part_of_the_ctc(
    client: httpx.AsyncClient, session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    structure = await _setup(session, tenant, hr_user)
    response = await client.post(
        f"/api/v1/salary-templates/{structure.id}/calculate",
        headers=_auth(hr_user),
        json={
            "ctc_amount": "1600000",
            "ctc_basis": "annual",
            "variable_amount": "400000",
            "variable_basis": "annual",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    lines = {line["component_code"]: line for line in body["lines"]}
    assert lines["BASIC"]["annual"] == "600000.00"  # 50% of the fixed 12,00,000
    assert body["ctc_annual"] == "1200000.00"
    assert "SALES_INCENTIVE" not in lines


async def test_calculate_refuses_an_incentive_that_is_the_whole_ctc(
    client: httpx.AsyncClient, session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    structure = await _setup(session, tenant, hr_user)
    response = await client.post(
        f"/api/v1/salary-templates/{structure.id}/calculate",
        headers=_auth(hr_user),
        json={
            "ctc_amount": "100000",
            "ctc_basis": "monthly",
            "variable_amount": "1200000",
            "variable_basis": "annual",
        },
    )
    assert response.status_code == 422
    assert "SALES INCENTIVE" in response.json()["detail"]
