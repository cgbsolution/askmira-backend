"""Group benefits: enrol in the window, change cover on a life event, files for the insurer."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.benefits import BenefitKind, LifeEventKind
from app.models.employee import Employee
from app.models.payroll_inputs import AdjustmentStatus, PayrollAdjustment
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.benefits import EnrolIn, LifeEventIn, Member, PlanCreate, TopUp
from app.services import benefits
from tests.integration.conftest import as_caller, requires_db

pytestmark = requires_db

TODAY = date(2026, 4, 10)


async def _login(session: AsyncSession, tenant: Tenant, employee: Employee) -> User:
    user = User(
        tenant_id=tenant.id, email="anita.bn@acme-demo.com", password_hash="x", full_name="Anita",
        role=UserRole.EMPLOYEE, is_active=True, employee_id=employee.id,
    )
    session.add(user)
    await session.commit()
    return await as_caller(session, user)


async def _plan(session: AsyncSession, hr_user: User) -> uuid.UUID:
    plan = await benefits.create_plan(
        session, hr_user,
        PlanCreate(
            name="Group health", kind=BenefitKind.HEALTH, insurer="Acme Insurance",
            tpa="Acme TPA", policy_number="GH-2026", policy_start=date(2026, 4, 1),
            policy_end=date(2027, 3, 31), sum_insured=Decimal("500000"),
            covers=["spouse", "child"], max_children=2,
            employer_premium_per_member=Decimal("8000"),
            top_ups=[TopUp(code="TOP5", name="+5 lakh", sum_insured=Decimal("500000"),
                           employee_premium_annual=Decimal("6000"))],
            enrolment_opens=date(2026, 4, 1), enrolment_closes=date(2026, 4, 30),
        ),
        request=None,
    )
    return uuid.UUID(plan["id"])


async def test_enrolment_life_event_and_insurer_files(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    plan_id = await _plan(session, hr_user)
    me = await _login(session, tenant, employee)

    with pytest.raises(HTTPException) as parent:
        await benefits.enrol(
            session, me, plan_id,
            EnrolIn(members=[Member(name="Father", relationship="parent")]), today=TODAY,
        )
    assert "does not cover a parent" in parent.value.detail

    enrolled = await benefits.enrol(
        session, me, plan_id,
        EnrolIn(members=[Member(name="Ravi", relationship="spouse")], top_up_code="TOP5"),
        today=TODAY,
    )
    assert enrolled["status"] == "enrolled" and enrolled["employee_premium_annual"] == "6000.00"
    deduction = await session.scalar(select(PayrollAdjustment))
    assert deduction is not None
    assert deduction.amount == Decimal("500.00") and deduction.status is AdjustmentStatus.PENDING

    # Re-enrolling without the top-up cancels that deduction.
    await benefits.enrol(
        session, me, plan_id,
        EnrolIn(members=[Member(name="Ravi", relationship="spouse")]), today=TODAY,
    )
    await session.refresh(deduction)
    assert deduction.status is AdjustmentStatus.CANCELLED

    # After the window: a birth within 30 days adds the child once HR approves.
    later = date(2026, 8, 20)
    with pytest.raises(HTTPException):
        await benefits.enrol(session, me, plan_id, EnrolIn(), today=later)
    with pytest.raises(HTTPException):
        await benefits.raise_life_event(
            session, me,
            LifeEventIn(plan_id=plan_id, kind=LifeEventKind.BIRTH,
                        event_date=later - timedelta(days=45),
                        add_members=[Member(name="Baby", relationship="child")]),
            today=later,
        )
    event = await benefits.raise_life_event(
        session, me,
        LifeEventIn(plan_id=plan_id, kind=LifeEventKind.BIRTH, event_date=later - timedelta(days=3),
                    add_members=[Member(name="Baby", relationship="child")]),
        today=later,
    )
    pending = await benefits.list_life_events(session, hr_user)
    assert [p["id"] for p in pending] == [event["id"]]
    await benefits.decide_life_event(
        session, hr_user, uuid.UUID(event["id"]), approve=True, note=None
    )

    census = list(csv.DictReader(io.StringIO(await benefits.census_csv(session, hr_user, plan_id))))
    assert sorted(row["Relationship"] for row in census) == ["child", "self", "spouse"]
    endorsements = list(
        csv.DictReader(
            io.StringIO(
                await benefits.endorsements_csv(session, hr_user, plan_id, since=date(2026, 1, 1))
            )
        )
    )
    assert [(r["Change"], r["Member name"]) for r in endorsements] == [
        ("Addition", "Ravi"), ("Addition", "Baby"),
    ]


async def test_insurer_files_are_hr_only_and_the_open_window_shows(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    plan_id = await _plan(session, hr_user)
    me = await _login(session, tenant, employee)
    with pytest.raises(HTTPException):
        await benefits.census_csv(session, me, plan_id)
    mine = await benefits.my_benefits(session, me, today=TODAY)
    assert mine[0]["window_open"] is True and mine[0]["enrolment"] is None
