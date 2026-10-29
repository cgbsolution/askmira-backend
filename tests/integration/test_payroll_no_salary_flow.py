"""Somebody with no salary is left off the run; everybody else is still paid.

A consultant on the books with no salary used to fail the whole month's run.
Now the run pays the rest and names who it left out. Adding their salary and
recomputing picks them up; once the run is approved, an off-cycle run pays
their month.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.org import LegalEntity, Location
from app.models.payroll import PayrollRunStatus, PayrollRunType, Payslip
from app.models.tenant import Tenant
from app.models.user import UserRole
from app.schemas.payroll import EmployeeSalaryCreate
from app.services.payroll import run as run_service
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
async def consultant(
    session: AsyncSession, org, tenant: Tenant, legal_entity: LegalEntity, location: Location
):
    employee, _ = await _person(
        session, tenant, legal_entity, location, code="CNST14", role=UserRole.EMPLOYEE
    )
    return employee


async def _give_salary(session: AsyncSession, org, employee) -> None:
    await salary_service.assign_salary(
        session,
        org["maker"],
        employee.id,
        EmployeeSalaryCreate(
            structure_id=org["structure"].id,
            ctc_annual=Decimal("1200000"),
            effective_from=date(2026, 4, 1),
        ),
    )


def _left_out(run) -> list[str]:
    return [n["message"] for n in run.input_notices if n.get("key") == "no_salary"]


async def test_the_run_pays_everybody_else_and_names_who_has_no_salary(
    session: AsyncSession, org, consultant
) -> None:
    await _approved_rules(session, org)
    session.add(_lock(org["tenant"], org["maker"], 2026, 9))
    await session.commit()
    run = await _open_run(session, org, 9)

    computed = await run_service.compute_run(session, org["maker"], run.id)

    assert computed.status is PayrollRunStatus.PENDING_REVIEW
    assert computed.error_log is None
    assert computed.employee_count == 4
    assert _left_out(computed) == [
        "CNST14 (Cnst14 Test) had no salary on 30 Sep 2026 when this run was computed, "
        "so it does not pay them."
    ]
    slips = (await session.scalars(select(Payslip).where(Payslip.run_id == run.id))).all()
    assert consultant.id not in {slip.employee_id for slip in slips}

    entry = await session.scalar(
        select(AuditLog)
        .where(AuditLog.entity_id == run.id)
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    )
    assert entry is not None
    assert entry.changes["left_out_no_salary"] == ["CNST14"]
    assert "1 left out with no salary" in entry.summary

    # Nothing about it blocks approval: the checker signs off the rest.
    approved = await run_service.approve_run(session, org["checker"], run.id)
    assert approved.status is PayrollRunStatus.APPROVED


async def test_a_recompute_after_their_salary_is_added_pays_them(
    session: AsyncSession, org, consultant
) -> None:
    await _approved_rules(session, org)
    session.add(_lock(org["tenant"], org["maker"], 2026, 9))
    await session.commit()
    run = await _open_run(session, org, 9)
    first = await run_service.compute_run(session, org["maker"], run.id)
    assert first.employee_count == 4

    await _give_salary(session, org, consultant)
    again = await run_service.compute_run(session, org["maker"], run.id)

    assert again.employee_count == 5
    assert _left_out(again) == []
    slip = await session.scalar(
        select(Payslip).where(Payslip.run_id == run.id, Payslip.employee_id == consultant.id)
    )
    assert slip is not None
    assert Decimal(str(slip.gross_earnings)) == Decimal("100000.00")


async def test_after_approval_an_off_cycle_run_pays_their_month(
    session: AsyncSession, org, consultant
) -> None:
    await _approved_rules(session, org)
    session.add(_lock(org["tenant"], org["maker"], 2026, 9))
    await session.commit()
    regular = await _open_run(session, org, 9)
    await run_service.compute_run(session, org["maker"], regular.id)
    await run_service.approve_run(session, org["checker"], regular.id)

    await _give_salary(session, org, consultant)
    off_cycle = await run_service.create_run(
        session,
        org["maker"],
        legal_entity_id=org["entity"].id,
        period_year=2026,
        period_month=9,
        run_type=PayrollRunType.OFF_CYCLE,
        employee_ids=[consultant.id],
        reason="Salary added after the September run was approved",
    )
    computed = await run_service.compute_run(session, org["maker"], off_cycle.id)

    assert computed.status is PayrollRunStatus.PENDING_REVIEW
    assert computed.employee_count == 1
    slip = await session.scalar(select(Payslip).where(Payslip.run_id == off_cycle.id))
    assert slip is not None and slip.employee_id == consultant.id
    # The whole month's salary, as a full payslip: the regular run paid none of it.
    assert slip.is_supplementary is False
    assert Decimal(str(slip.gross_earnings)) == Decimal("100000.00")
    assert Decimal(str(slip.pf_employee)) == Decimal("1800.00")


async def test_a_run_where_nobody_has_a_salary_still_fails(
    session: AsyncSession, org, consultant
) -> None:
    """An off-cycle run for the one person with no salary has nothing to pay."""
    await _approved_rules(session, org)
    session.add(_lock(org["tenant"], org["maker"], 2026, 9))
    await session.commit()
    regular = await _open_run(session, org, 9)
    await run_service.compute_run(session, org["maker"], regular.id)
    await run_service.approve_run(session, org["checker"], regular.id)

    off_cycle = await run_service.create_run(
        session,
        org["maker"],
        legal_entity_id=org["entity"].id,
        period_year=2026,
        period_month=9,
        run_type=PayrollRunType.OFF_CYCLE,
        employee_ids=[consultant.id],
        reason="Pay the consultant",
    )
    with pytest.raises(HTTPException) as exc:
        await run_service.compute_run(session, org["maker"], off_cycle.id)
    assert exc.value.status_code == 422
    assert "CNST14: no salary assigned for 2026-09-30" in exc.value.detail

    failed = await run_service.get_run(session, org["maker"], off_cycle.id)
    assert failed.status is PayrollRunStatus.FAILED
    assert failed.error_log == "CNST14: no salary assigned for 2026-09-30"
