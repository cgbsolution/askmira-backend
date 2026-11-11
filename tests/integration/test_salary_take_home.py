"""Take-home on the Salary master: on every revision, and live while HR types.

The figure must be what the first payslip pays, not earnings less what happens
to be typed: PF is worked out from the statutory rules even when its MANUAL row
is empty. The case is a real promotion letter's: Basic 3,09,996, HRA 1,24,008
and Special 1,47,684 a year, medical insurance 2,868 a year, PF at 12% of the
15,000 ceiling — 48,474 gross, 46,435 take-home.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.employee import Employee
from app.models.payroll import (
    CalculationType,
    ComponentType,
    EmployeeSalary,
    PayrollRuleSet,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.payroll import EmployeeSalaryCreate, SalaryComponentAmountInput
from app.services.payroll import salary_master
from app.services.payroll import service as salary_service
from tests.integration.conftest import requires_db

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

#: The promotion letter's figures, typed a year at a time as HR does.
TYPED = {"BASIC": "309996", "HRA": "124008", "SPECIAL": "147684", "INSURANCE": "2868"}


async def _setup(session: AsyncSession, tenant: Tenant, *, pf: bool = True) -> SalaryStructure:
    specs = [
        ("BASIC", "BASIC SALARY", ComponentType.EARNING, CalculationType.PERCENTAGE_OF, "CTC",
         Decimal(50), True, True),
        ("HRA", "HOUSE RENT ALLOWANCE", ComponentType.EARNING, CalculationType.PERCENTAGE_OF,
         "BASIC", Decimal(40), False, True),
        ("SPECIAL", "SPECIAL ALLOWANCE", ComponentType.EARNING, CalculationType.BALANCE, None,
         None, False, True),
        ("INSURANCE", "INSURANCE", ComponentType.DEDUCTION, CalculationType.FIXED, None,
         Decimal(0), False, False),
    ]  # fmt: skip
    components = [
        SalaryComponent(
            tenant_id=tenant.id,
            code=code,
            name=name,
            component_type=kind,
            calculation_type=calc,
            base_component_code=base,
            value=value,
            counts_toward_pf=counts_pf,
            part_of_ctc=in_ctc,
            display_order=order,
        )
        for order, (code, name, kind, calc, base, value, counts_pf, in_ctc) in enumerate(specs)
    ]
    session.add_all(components)
    await session.flush()
    structure = SalaryStructure(
        tenant_id=tenant.id,
        name="Standard CTC",
        effective_from=date(2020, 1, 1),
        applicable_grades=[],
        applicable_locations=[],
    )
    for order, component in enumerate(components):
        structure.components.append(
            SalaryStructureComponent(
                tenant_id=tenant.id, component_id=component.id, component=component,
                display_order=order,
            )
        )  # fmt: skip
    session.add(structure)
    if pf:
        session.add(
            PayrollRuleSet(
                tenant_id=tenant.id,
                rule_type="pf",
                jurisdiction="IN",
                effective_from=date(2020, 1, 1),
                rules=PF_RULES,
                approved_at=datetime.now(UTC),
            )
        )
    await session.commit()
    await session.refresh(structure)
    return structure


def _typed() -> list[SalaryComponentAmountInput]:
    return [
        SalaryComponentAmountInput(component_code=code, amount=Decimal(amount), basis="annual")
        for code, amount in TYPED.items()
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


def _preview_body(employee: Employee | None) -> dict:
    body: dict = {
        "entry": "manual",
        "ctc_amount": "51666.67",
        "ctc_basis": "monthly",
        "components": [
            {"component_code": code, "amount": amount, "basis": "annual"}
            for code, amount in TYPED.items()
        ],
    }
    if employee is not None:
        body["employee_id"] = str(employee.id)
        body["effective_from"] = "2026-04-01"
    return body


async def test_each_revision_carries_its_take_home_with_pf_worked_out(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    structure = await _setup(session, tenant)
    await salary_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            entry="manual",
            structure_id=structure.id,
            ctc_amount=Decimal("51666.67"),
            ctc_basis="monthly",
            effective_from=date(2026, 4, 1),
            components=_typed(),
        ),
    )

    [revision] = await salary_master.list_revisions(session, hr_user, employee.id)
    assert revision.gross_monthly == Decimal("48474.00")
    take_home = revision.take_home
    assert take_home is not None and take_home.note is None
    # 48,474 less PF 1,800 (no PF row typed: the rules work it out) less insurance 239.
    assert take_home.monthly == Decimal("46435.00")
    assert take_home.annual == Decimal("557220.00")
    # The statutory sections: PF on both sides at 12% of the 15,000 ceiling,
    # ESIC listed at nothing (gross is above its ceiling and no rules are set).
    employee_side = {line.code: line for line in take_home.employee}
    employer_side = {line.code: line for line in take_home.employer}
    assert list(employee_side) == ["PF_EMPLOYEE", "ESI_EMPLOYEE"]
    assert list(employer_side) == ["PF_EMPLOYER", "ESI_EMPLOYER"]
    assert employee_side["PF_EMPLOYEE"].monthly == Decimal("1800.00")
    assert employee_side["PF_EMPLOYEE"].annual == Decimal("21600.00")
    assert employee_side["PF_EMPLOYEE"].basis == "12% of PF wages ₹15,000"
    assert employer_side["PF_EMPLOYER"].monthly == Decimal("1800.00")
    assert employer_side["ESI_EMPLOYER"].monthly == Decimal("0.00")
    assert employer_side["ESI_EMPLOYER"].basis == (
        "Nothing due: no ESI rules set up (Payroll → Statutory rules)"
    )
    assert take_home.employee_total == Decimal("1800.00")
    assert take_home.employer_total == Decimal("1800.00")
    # PF first, as the letter lists it.
    assert [(d.code, d.monthly) for d in take_home.deductions] == [
        ("PF_EMPLOYEE", Decimal("1800.00")),
        ("INSURANCE", Decimal("239.00")),
    ]


async def test_a_typed_tds_is_taken_off_and_named(
    client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
) -> None:
    structure = await _setup(session, tenant)
    session.add(
        SalaryComponent(
            tenant_id=tenant.id,
            code="TDS_MANUAL",
            name="TDS MANUAL",
            component_type=ComponentType.DEDUCTION,
            calculation_type=CalculationType.FIXED,
            value=Decimal(0),
            part_of_ctc=False,
            display_order=20,
        )
    )
    await session.commit()
    body = _preview_body(employee)
    body["components"].append({"component_code": "TDS_MANUAL", "amount": "500", "basis": "monthly"})
    response = await client.post(
        f"/api/v1/salary-structures/{structure.id}/preview", headers=_auth(hr_user), json=body
    )
    assert response.status_code == 200, response.text
    take_home = response.json()["take_home"]
    assert take_home["monthly"] == "45935.00"
    assert take_home["annual"] == "551220.00"
    tds = take_home["deductions"][-1]
    assert (tds["code"], tds["monthly"], tds["annual"]) == ("TDS", "500.00", "6000.00")
    assert take_home["employee"][-1]["code"] == "TDS"


async def test_a_typed_pf_shows_as_typed_on_the_employee_side(
    client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
) -> None:
    structure = await _setup(session, tenant)
    session.add(
        SalaryComponent(
            tenant_id=tenant.id,
            code="PF_MANUAL",
            name="MPF CONTRIBUTION MANUAL",
            component_type=ComponentType.DEDUCTION,
            calculation_type=CalculationType.FIXED,
            value=Decimal(0),
            part_of_ctc=False,
            display_order=21,
        )
    )
    await session.commit()
    body = _preview_body(employee)
    body["components"].append({"component_code": "PF_MANUAL", "amount": "2000", "basis": "monthly"})
    response = await client.post(
        f"/api/v1/salary-structures/{structure.id}/preview", headers=_auth(hr_user), json=body
    )
    assert response.status_code == 200, response.text
    take_home = response.json()["take_home"]
    pf = take_home["employee"][0]
    assert (pf["code"], pf["monthly"]) == ("PF_EMPLOYEE", "2000.00")
    assert pf["basis"].startswith("Typed on the Salary master")
    assert take_home["monthly"] == "46235.00"


async def test_take_home_says_why_when_the_rules_are_missing(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    structure = await _setup(session, tenant, pf=False)
    await salary_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            entry="manual",
            structure_id=structure.id,
            ctc_amount=Decimal("51666.67"),
            ctc_basis="monthly",
            effective_from=date(2026, 4, 1),
            components=_typed(),
        ),
    )
    [revision] = await salary_master.list_revisions(session, hr_user, employee.id)
    assert revision.take_home is not None
    assert revision.take_home.monthly is None
    assert "Statutory rules" in (revision.take_home.note or "")


async def test_the_preview_shows_take_home_while_typing_and_writes_nothing(
    client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
) -> None:
    structure = await _setup(session, tenant)
    response = await client.post(
        f"/api/v1/salary-structures/{structure.id}/preview",
        headers=_auth(hr_user),
        json=_preview_body(employee),
    )
    assert response.status_code == 200, response.text
    take_home = response.json()["take_home"]
    assert take_home["monthly"] == "46435.00"
    assert {d["code"] for d in take_home["deductions"]} == {"PF_EMPLOYEE", "INSURANCE"}
    # The unsaved salary the engine ran on never reached the database.
    assert await session.scalar(select(func.count(EmployeeSalary.id))) == 0

    # Without an employee the preview is as before: no take-home.
    plain = await client.post(
        f"/api/v1/salary-structures/{structure.id}/preview",
        headers=_auth(hr_user),
        json=_preview_body(None),
    )
    assert plain.status_code == 200, plain.text
    assert plain.json()["take_home"] is None


async def test_the_preview_cannot_reach_another_tenants_employee(
    client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    other_hr_user: User,
    employee: Employee,
) -> None:
    await _setup(session, tenant)
    theirs = await _setup(session, await session.get(Tenant, other_hr_user.tenant_id))
    response = await client.post(
        f"/api/v1/salary-structures/{theirs.id}/preview",
        headers=_auth(other_hr_user),
        json=_preview_body(employee),
    )
    assert response.status_code == 404


async def test_esic_says_when_wages_are_above_its_ceiling_and_deducts_below_it(
    client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
) -> None:
    structure = await _setup(session, tenant)
    for component in structure.components:
        if component.component.code in ("BASIC", "HRA", "SPECIAL"):
            component.component.counts_toward_esi = True
    session.add(
        PayrollRuleSet(
            tenant_id=tenant.id,
            rule_type="esi",
            jurisdiction="IN",
            effective_from=date(2020, 1, 1),
            rules={
                "wage_threshold": 21000,
                "employee_rate": "0.75",
                "employer_rate": "3.25",
                "contribution_periods": [
                    {"start_month": 4, "end_month": 9, "label": "APR-SEP"},
                    {"start_month": 10, "end_month": 3, "label": "OCT-MAR"},
                ],
            },
            approved_at=datetime.now(UTC),
        )
    )
    await session.commit()
    url = f"/api/v1/salary-structures/{structure.id}/preview"

    above = await client.post(url, headers=_auth(hr_user), json=_preview_body(employee))
    assert above.status_code == 200, above.text
    esi = {line["code"]: line for line in above.json()["take_home"]["employer"]}["ESI_EMPLOYER"]
    assert esi["monthly"] == "0.00"
    assert esi["basis"] == "Nothing due: ESI wages ₹48,474 are above the ESI ceiling of ₹21,000"

    # 18,000 a month: within the ceiling, so both sides are deducted.
    body = _preview_body(employee)
    body["ctc_amount"] = "20000"
    body["components"] = [
        {"component_code": "BASIC", "amount": "120000", "basis": "annual"},
        {"component_code": "HRA", "amount": "48000", "basis": "annual"},
        {"component_code": "SPECIAL", "amount": "48000", "basis": "annual"},
    ]
    below = await client.post(url, headers=_auth(hr_user), json=body)
    assert below.status_code == 200, below.text
    take_home = below.json()["take_home"]
    employee_side = {line["code"]: line for line in take_home["employee"]}
    employer_side = {line["code"]: line for line in take_home["employer"]}
    assert employee_side["ESI_EMPLOYEE"]["monthly"] == "135.00"  # 0.75% of 18,000
    assert employer_side["ESI_EMPLOYER"]["monthly"] == "585.00"  # 3.25% of 18,000
    assert employee_side["PF_EMPLOYEE"]["monthly"] == "1200.00"  # 12% of basic 10,000
    # 18,000 less PF 1,200 and ESIC 135.
    assert take_home["monthly"] == "16665.00"
