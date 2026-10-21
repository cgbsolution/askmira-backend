"""Releasing an increment cycle makes the increments true.

The approved CTC becomes a new effective-dated salary, the increment letter is
issued against it, the revision is recorded, and a second release cannot apply
anything twice. A promotion is left to the lifecycle cascade that owns it.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.arrears import RevisionType, SalaryRevision
from app.models.documents import IssuedLetter
from app.models.payroll import EmployeeSalary
from app.models.performance_flow import IncrementRecommendation
from app.services.performance import service as performance
from tests.integration.conftest import requires_db
from tests.integration.test_performance_flow import (
    approved_promotion,
    give_salary,
    make_employee,
    open_cycle,
)

pytestmark = requires_db


async def _approved_increment(session, hr_user, employee, cycle, percentage="6"):
    out = await performance.recommend_increment(
        session,
        hr_user,
        cycle_id=cycle.id,
        employee_id=employee.id,
        rating_label="Meets",
        current_ctc=Decimal("1200000"),
        percentage=Decimal(percentage),
    )
    recommendation_id = uuid.UUID(out["recommendation_id"])
    await performance.decide_recommendation(
        session, hr_user, recommendation_id=recommendation_id, approve=True, note="Approved."
    )
    return recommendation_id


async def test_release_assigns_the_salary_issues_the_letter_and_records_the_revision(
    session, hr_user, tenant, legal_entity, location
) -> None:
    person = await make_employee(session, tenant, legal_entity, location, "INC1")
    await give_salary(session, tenant, hr_user, person)
    cycle = await open_cycle(session, hr_user)  # effective 1 Apr 2027, letters 25 Mar
    recommendation_id = await _approved_increment(session, hr_user, person, cycle)

    result = await performance.release_letters(
        session, hr_user, cycle_id=cycle.id, today=date(2027, 3, 25)
    )
    assert result["applied"]["applied"] == 1
    assert result["applied"]["arrear_batches"] == 0, "nothing was paid at the old rate yet"

    salaries = (
        await session.scalars(
            select(EmployeeSalary)
            .where(EmployeeSalary.employee_id == person.id)
            .order_by(EmployeeSalary.effective_from)
        )
    ).all()
    assert len(salaries) == 2
    assert salaries[1].effective_from == date(2027, 4, 1)
    assert Decimal(str(salaries[0].ctc_annual)) == Decimal("1200000")
    assert salaries[0].effective_to == date(2027, 3, 31)
    assert Decimal(str(salaries[1].ctc_annual)) == Decimal("1272000")

    rec = await session.scalar(
        select(IncrementRecommendation).where(IncrementRecommendation.id == recommendation_id)
    )
    assert rec.salary_id == salaries[1].id and rec.applied_at is not None

    letter = await session.scalar(select(IssuedLetter).where(IssuedLetter.id == rec.letter_id))
    assert letter is not None and letter.employee_id == person.id
    assert "1,272,000" in letter.merged_html or "12,72,000" in letter.merged_html
    assert "6.00%" in letter.merged_html

    revision = await session.scalar(
        select(SalaryRevision).where(SalaryRevision.employee_id == person.id)
    )
    assert revision.revision_type is RevisionType.INCREMENT
    assert revision.new_salary_id == salaries[1].id

    # Released once; nothing applies twice.
    with pytest.raises(HTTPException):
        await performance.release_letters(
            session, hr_user, cycle_id=cycle.id, today=date(2027, 3, 25)
        )
    count = len(
        (
            await session.scalars(
                select(EmployeeSalary).where(EmployeeSalary.employee_id == person.id)
            )
        ).all()
    )
    assert count == 2


async def test_a_promotion_is_left_to_the_lifecycle_cascade(
    session, hr_user, tenant, legal_entity, location
) -> None:
    person = await make_employee(session, tenant, legal_entity, location, "INC2")
    await give_salary(session, tenant, hr_user, person)
    cycle = await open_cycle(session, hr_user)
    await approved_promotion(session, hr_user, person, cycle)

    result = await performance.release_letters(
        session, hr_user, cycle_id=cycle.id, today=date(2027, 3, 25)
    )
    assert result["applied"]["applied"] == 0
    salaries = (
        await session.scalars(select(EmployeeSalary).where(EmployeeSalary.employee_id == person.id))
    ).all()
    assert len(salaries) == 1, "the promotion path revises the salary, not the release"


async def test_an_increment_approved_after_release_is_applied_on_approval(
    session, hr_user, tenant, legal_entity, location
) -> None:
    first = await make_employee(session, tenant, legal_entity, location, "INC3")
    late = await make_employee(session, tenant, legal_entity, location, "INC4")
    await give_salary(session, tenant, hr_user, first)
    await give_salary(session, tenant, hr_user, late)
    cycle = await open_cycle(session, hr_user)
    await _approved_increment(session, hr_user, first, cycle)
    pending = await performance.recommend_increment(
        session,
        hr_user,
        cycle_id=cycle.id,
        employee_id=late.id,
        rating_label="Meets",
        current_ctc=Decimal("1200000"),
        percentage=Decimal("5"),
    )
    await performance.release_letters(
        session, hr_user, cycle_id=cycle.id, today=date(2027, 3, 25), force_partial=True
    )
    await performance.decide_recommendation(
        session,
        hr_user,
        recommendation_id=uuid.UUID(pending["recommendation_id"]),
        approve=True,
        note="Late approval.",
    )
    salaries = (
        await session.scalars(
            select(EmployeeSalary)
            .where(EmployeeSalary.employee_id == late.id)
            .order_by(EmployeeSalary.effective_from)
        )
    ).all()
    ctcs = [Decimal(str(s.ctc_annual)) for s in salaries]
    assert ctcs == [Decimal("1200000"), Decimal("1260000")]
