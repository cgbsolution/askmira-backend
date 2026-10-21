"""ESOP grants: a vesting schedule that adds up, worth at fair value, exercise taxed in payroll."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.esop import EsopGrant
from app.models.org import LegalEntity
from app.models.payroll import PayrollRun, PayrollRunStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services import esop
from app.services.payroll.inputs import EsopPerquisiteProvider, RunScope
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db


def test_pre_cliff_tranches_vest_together_on_the_cliff() -> None:
    grant = EsopGrant(
        units=1000,
        vesting_start=date(2026, 1, 1),
        cliff_months=12,
        vesting_months=48,
        frequency_months=3,
    )
    schedule = esop.vesting_schedule(grant)
    assert schedule[0] == {"date": date(2027, 1, 1), "units": 250}
    assert schedule[-1]["date"] == date(2030, 1, 1)
    assert sum(r["units"] for r in schedule) == 1000
    assert len(schedule) == 13

    odd = EsopGrant(
        units=100,
        vesting_start=date(2026, 1, 31),
        cliff_months=10,
        vesting_months=12,
        frequency_months=3,
    )
    rows = esop.vesting_schedule(odd)
    # 3, 6 and 9 months fall before a 10-month cliff and vest on it; month-end is kept.
    assert rows[0] == {"date": date(2026, 11, 30), "units": 75}
    assert rows[1] == {"date": date(2027, 1, 31), "units": 25}


async def test_grant_value_exercise_and_payroll_perquisite(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    legal_entity: LegalEntity,
) -> None:
    plan = await esop.create_plan(
        session, hr_user, esop.PlanIn(name="ESOP 2026", pool_units=10_000), request=None
    )
    plan_id = uuid.UUID(plan["id"])
    with pytest.raises(HTTPException):
        await esop.create_grant(
            session,
            hr_user,
            plan_id,
            esop.GrantIn(
                employee_id=employee.id,
                grant_date=date(2026, 1, 1),
                units=20_000,
                exercise_price=Decimal("10"),
            ),
            request=None,
        )
    grant = await esop.create_grant(
        session,
        hr_user,
        plan_id,
        esop.GrantIn(
            employee_id=employee.id,
            grant_date=date(2026, 1, 1),
            units=1000,
            exercise_price=Decimal("10"),
        ),
        request=None,
    )
    grant_id = uuid.UUID(grant["id"])
    await esop.add_valuation(
        session,
        hr_user,
        plan_id,
        esop.ValuationIn(
            effective_from=date(2026, 1, 1),
            fmv_per_unit=Decimal("40"),
            source="Valuer report, Dec 2025",
        ),
        request=None,
    )

    me = User(
        tenant_id=tenant.id,
        email="anita.esop@acme-demo.com",
        password_hash="x",
        full_name="Anita",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(me)
    await session.commit()
    me = await as_caller(session, me)
    mine = await esop.portfolio(session, me, employee.id, on=date(2027, 4, 1))
    held = mine["grants"][0]
    # The cliff (250) and one quarter (62) have vested.
    assert held["vested"] == 312 and held["unvested"] == 688
    assert held["value_vested_unexercised"] == "9360.00"

    # What-if: at ₹100 a share the spread is 90; 312 vested now, 1000 in the end.
    what_if = await esop.projection(
        session, me, employee.id, price_per_unit=Decimal("100"), on=date(2027, 4, 1)
    )
    assert what_if["value_now"] == "28080.00"
    assert what_if["value_when_fully_vested"] == "90000.00"
    assert what_if["timeline"][0]["value"] == "28080.00"
    assert what_if["timeline"][-1]["value"] == "90000.00"
    below = await esop.projection(
        session, me, employee.id, price_per_unit=Decimal("5"), on=date(2027, 4, 1)
    )
    assert below["value_when_fully_vested"] == "0.00", "under water is worth nothing, not less"

    with pytest.raises(HTTPException):
        await esop.record_exercise(
            session,
            hr_user,
            grant_id,
            esop.ExerciseIn(exercise_date=date(2027, 4, 1), units=400),
            request=None,
        )
    exercised = await esop.record_exercise(
        session,
        hr_user,
        grant_id,
        esop.ExerciseIn(exercise_date=date(2027, 4, 1), units=100),
        request=None,
    )
    assert exercised["perquisite_value"] == "3000.00"

    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=legal_entity.id,
        period_year=2027,
        period_month=4,
        effective_date=date(2027, 4, 30),
        status=PayrollRunStatus.DRAFT,
    )
    session.add(run)
    await session.commit()
    scope = RunScope(
        run=run,
        period_start=date(2027, 4, 1),
        period_end=date(2027, 4, 30),
        period="2027-04",
        employee_ids=frozenset({employee.id}),
        fy_start=date(2027, 4, 1),
        fy_end=date(2028, 3, 31),
    )
    output = await EsopPerquisiteProvider().collect(session, hr_user, scope)
    lines = output.lines[employee.id]
    assert [(line.code, line.taxable_amount) for line in lines] == [
        ("ESOP_PERQUISITE", Decimal("3000.00"))
    ]
