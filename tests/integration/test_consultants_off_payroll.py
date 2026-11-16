"""Consultants are never on the Salary master or the regular payroll run."""
# ruff: noqa: F811

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee, EmploymentType
from app.models.payroll import Payslip
from app.models.user import UserRole
from app.schemas.payroll import EmployeeSalaryCreate, EmployeeSalaryUpdate
from app.services.consultants import CONSULTANT_SALARY_REFUSAL
from app.services.payroll import run as run_service
from app.services.payroll import salary_import, salary_master
from app.services.payroll import service as salary_service
from tests.integration.conftest import requires_db
from tests.integration.test_payroll_run_flow import (  # noqa: F401  (fixture)
    _approved_rules,
    _lock,
    _open_run,
    _person,
    org,
)

pytestmark = requires_db


@pytest.fixture
async def consultant(session: AsyncSession, org, tenant, legal_entity, location):
    employee, _ = await _person(
        session, tenant, legal_entity, location, code="CNSLT1", role=UserRole.EMPLOYEE
    )
    employee.employment_type = EmploymentType.CONSULTANT
    await session.commit()
    return employee


def _salary(org, on: date = date(2026, 4, 1)) -> EmployeeSalaryCreate:
    return EmployeeSalaryCreate(
        structure_id=org["structure"].id, ctc_annual=Decimal("1200000"), effective_from=on
    )


async def test_salary_overview_leaves_out_consultants(
    session: AsyncSession, org, consultant
) -> None:
    rows, total = await salary_service.salary_overview(session, org["maker"])
    codes = {row.employee_code for row in rows}
    assert "MAKER" in codes
    assert consultant.employee_code not in codes
    assert total == len(rows)


async def test_assign_salary_refuses_consultant(session: AsyncSession, org, consultant) -> None:
    with pytest.raises(HTTPException) as refused:
        await salary_service.assign_salary(session, org["maker"], consultant.id, _salary(org))
    assert refused.value.status_code == 422
    assert refused.value.detail == CONSULTANT_SALARY_REFUSAL


async def test_assign_salaries_refuses_the_consultant_row_and_assigns_the_rest(
    session: AsyncSession, org, consultant
) -> None:
    full_timer = await session.scalar(select(Employee).where(Employee.employee_code == "MEERA"))
    batch = await salary_service.assign_salaries(
        session,
        org["maker"],
        [
            (consultant, _salary(org), True),
            (full_timer, _salary(org, date(2026, 8, 1)), True),
        ],
    )
    assert batch.refused == [("CNSLT1", CONSULTANT_SALARY_REFUSAL)]
    assert [salary.employee_id for salary in batch.saved] == [full_timer.id]


async def test_update_salary_refuses_consultant(session: AsyncSession, org, consultant) -> None:
    # Somebody who was on payroll before becoming a consultant keeps the old revision.
    consultant.employment_type = EmploymentType.FULL_TIME
    saved = await salary_service.assign_salary(session, org["maker"], consultant.id, _salary(org))
    consultant.employment_type = EmploymentType.CONSULTANT
    await session.commit()

    with pytest.raises(HTTPException) as refused:
        await salary_master.update_salary(
            session,
            org["maker"],
            consultant.id,
            saved.id,
            EmployeeSalaryUpdate(
                structure_id=org["structure"].id,
                ctc_annual=Decimal("1300000"),
                effective_from=date(2026, 4, 1),
            ),
        )
    assert refused.value.status_code == 422
    assert refused.value.detail == CONSULTANT_SALARY_REFUSAL


async def test_salary_import_refuses_consultant_rows(
    session: AsyncSession, org, consultant
) -> None:
    book = Workbook()
    sheet = book.active
    sheet.append(["Employee Code", "WEF", "Pay Basis", "CTC TOTAL"])
    sheet.append([consultant.employee_code, date(2026, 4, 1), "Yearly", 1200000])
    out = io.BytesIO()
    book.save(out)

    result = await salary_import.upload(
        session, org["maker"], out.getvalue(), "salaries.xlsx", apply=False
    )

    assert result.errors == 1
    assert result.rows[0].status == "error"
    assert result.rows[0].message == CONSULTANT_SALARY_REFUSAL


async def test_payroll_run_skips_consultants(session: AsyncSession, org, consultant) -> None:
    consultant.employment_type = EmploymentType.FULL_TIME
    await salary_service.assign_salary(session, org["maker"], consultant.id, _salary(org))
    consultant.employment_type = EmploymentType.CONSULTANT
    await session.commit()
    await _approved_rules(session, org)
    session.add(_lock(org["tenant"], org["maker"], 2026, 9))
    await session.commit()
    run = await _open_run(session, org, 9)

    await run_service.compute_run(session, org["maker"], run.id)

    slips = (await session.scalars(select(Payslip).where(Payslip.run_id == run.id))).all()
    assert slips
    assert consultant.id not in {slip.employee_id for slip in slips}
