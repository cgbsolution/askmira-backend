"""Slice 35 — PIP and increment flow against a real database.

The checklist gates:

- PIP trail is complete enough to hand to employment counsel
- employee contemporaneous input captured at every check-in
- budget visible to managers during allocation
- promotion applies atomically across all four systems
- letters release on a controlled date, not piecemeal
- pay-equity indicator surfaces at decision time
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.employee import Employee, EmploymentStatus, Gender
from app.models.performance_flow import (
    EmployeeInputStateEnum,
    IncrementCycle,
    MilestoneStatusEnum,
    OverageModeEnum,
    Pip,
    PipMilestone,
    PipStatusEnum,
    PromotionRecord,
    RecommendationStatusEnum,
)
from app.services.performance import service as performance
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

D = Decimal
TODAY = date(2026, 12, 1)


# ── Helpers ─────────────────────────────────────────────────────────────────


async def make_employee(
    session, tenant, legal_entity, location, code, *, grade="M3", gender=None
):
    person = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=code,
        last_name="Test",
        work_email=f"{code.lower()}@acme.test",
        legal_entity_id=legal_entity.id,
        location_id=location.id,
        status=EmploymentStatus.ACTIVE,
        date_of_joining=date(2024, 1, 1),
        grade=grade,
        gender=gender,
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


async def open_plan(session, hr_user, employee, **kw):
    body = {
        "employee_id": employee.id,
        "start_date": date(2026, 9, 1),
        "end_date": date(2026, 11, 30),
        "reason": "Sustained shortfall against agreed targets since May.",
        "expectations": "Close twelve tickets a week and clear the backlog.",
        "support_provided": "Weekly coaching with the team lead; a new runbook.",
        "milestones": [
            {
                "description": "Close twelve tickets a week",
                "target_date": date(2026, 10, 1),
                "measurable_criteria": "12 or more closed for four weeks running",
            }
        ],
    }
    body.update(kw)
    return await performance.open_pip(session, hr_user, **body)


async def add_check_in(session, hr_user, plan, day, **kw):
    body = {
        "pip_id": plan.id,
        "scheduled_for": day,
        "held_on": day,
        "manager_comments": "Reviewed the ticket numbers and two escalations.",
        "employee_input_state": EmployeeInputStateEnum.GIVEN,
        "employee_comments": "I disagree about the Q2 target but will work on it.",
    }
    body.update(kw)
    return await performance.record_check_in(session, hr_user, **body)


async def give_salary(session, tenant, hr_user, employee, ctc="1200000"):
    """A promotion revises a salary, so there has to be one to revise.

    Built the way slice 24's own tests build it - a real structure with real
    components, assigned through the payroll service - because a promotion that
    works against a hand-made row and not against a real assignment is a
    promotion that works in the test and not in the product.
    """
    from decimal import Decimal as Dec

    from app.models.payroll import (
        CalculationType,
        ComponentType,
        SalaryComponent,
        SalaryStructure,
        SalaryStructureComponent,
    )
    from app.schemas.payroll import EmployeeSalaryCreate
    from app.services.payroll import service as payroll_service

    suffix = uuid.uuid4().hex[:4].upper()
    basic = SalaryComponent(
        tenant_id=tenant.id,
        code=f"BASIC{suffix}",
        name="Basic",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.PERCENTAGE_OF,
        base_component_code="CTC",
        value=Dec(40),
        display_order=1,
    )
    special = SalaryComponent(
        tenant_id=tenant.id,
        code=f"SPECIAL{suffix}",
        name="Special",
        component_type=ComponentType.EARNING,
        calculation_type=CalculationType.BALANCE,
        display_order=9,
    )
    session.add_all([basic, special])
    await session.flush()

    structure = SalaryStructure(
        tenant_id=tenant.id,
        name=f"Standard {suffix}",
        effective_from=date(2025, 1, 1),
        applicable_grades=[],
        applicable_locations=[],
    )
    for index, component in enumerate((basic, special)):
        structure.components.append(
            SalaryStructureComponent(
                tenant_id=tenant.id,
                component_id=component.id,
                component=component,
                display_order=index,
            )
        )
    session.add(structure)
    await session.commit()

    await payroll_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id,
            ctc_annual=Dec(ctc),
            effective_from=date(2025, 4, 1),
        ),
    )
    return structure


async def open_cycle(session, hr_user, **kw):
    body = {
        "name": f"FY27 increments {uuid.uuid4().hex[:6]}",
        "effective_from": date(2027, 4, 1),
        "budget_total": D("5000000"),
        "bands": [
            {
                "rating_label": "Exceeds",
                "min_percentage": D("8"),
                "max_percentage": D("14"),
                "recommended_percentage": D("11"),
            },
            {
                "rating_label": "Meets",
                "min_percentage": D("4"),
                "max_percentage": D("8"),
                "recommended_percentage": D("6"),
            },
        ],
        "letter_release_on": date(2027, 3, 25),
    }
    body.update(kw)
    return await performance.create_increment_cycle(session, hr_user, **body)


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the employee's contemporaneous input at every check-in
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_check_in_captures_both_sides(session, hr_user, employee):
    plan = await open_plan(session, hr_user, employee)
    row = await add_check_in(session, hr_user, plan, date(2026, 9, 7))
    assert row.employee_input_state is EmployeeInputStateEnum.GIVEN
    assert row.manager_comments
    assert row.employee_comments
    assert row.employee_comment_at is not None


async def test_a_check_in_the_employee_was_never_asked_about_is_refused(
    session, hr_user, employee
):
    """A trail of manager observations with no employee response is one side of
    a conversation."""
    plan = await open_plan(session, hr_user, employee)
    with pytest.raises(HTTPException) as exc:
        await add_check_in(
            session,
            hr_user,
            plan,
            date(2026, 9, 7),
            employee_input_state=EmployeeInputStateEnum.NOT_ASKED,
            employee_comments="",
        )
    assert "one side of a conversation" in exc.value.detail


async def test_a_bare_declined_is_refused(session, hr_user, employee):
    plan = await open_plan(session, hr_user, employee)
    with pytest.raises(HTTPException):
        await add_check_in(
            session,
            hr_user,
            plan,
            date(2026, 9, 7),
            employee_input_state=EmployeeInputStateEnum.DECLINED,
            employee_comments="",
        )


async def test_a_reasoned_decline_is_recorded(session, hr_user, employee):
    """Declining is a real thing people do; forcing a manager to type something
    on the employee's behalf would be worse."""
    plan = await open_plan(session, hr_user, employee)
    row = await add_check_in(
        session,
        hr_user,
        plan,
        date(2026, 9, 7),
        employee_input_state=EmployeeInputStateEnum.DECLINED,
        employee_comments="",
        decline_reason="Said she would prefer to respond in writing later.",
    )
    assert row.decline_reason
    assert row.employee_comment_at is None


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the trail is complete enough for employment counsel
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_plan_with_no_support_recorded_is_refused(
    session, hr_user, employee
):
    """An improvement plan the employer contributed nothing to is a notice
    period with paperwork."""
    with pytest.raises(HTTPException) as exc:
        await open_plan(session, hr_user, employee, support_provided="   ")
    assert "notice period with paperwork" in exc.value.detail


async def test_a_plan_with_a_vague_milestone_is_refused(
    session, hr_user, employee
):
    with pytest.raises(HTTPException) as exc:
        await open_plan(
            session,
            hr_user,
            employee,
            milestones=[
                {
                    "description": "Improve communication",
                    "target_date": date(2026, 10, 1),
                    "measurable_criteria": "  ",
                }
            ],
        )
    assert "cannot be defended" in exc.value.detail


async def test_a_plan_with_no_milestones_is_refused(session, hr_user, employee):
    with pytest.raises(HTTPException) as exc:
        await open_plan(session, hr_user, employee, milestones=[])
    assert "nothing they can be said to have failed" in exc.value.detail


async def test_an_unsuccessful_outcome_on_a_thin_trail_is_refused(
    session, hr_user, employee
):
    """An exit built on a plan that cannot be evidenced is the situation this
    slice exists to prevent."""
    plan = await open_plan(session, hr_user, employee)
    await add_check_in(session, hr_user, plan, date(2026, 9, 7))
    with pytest.raises(HTTPException) as exc:
        await performance.close_pip(
            session,
            hr_user,
            pip_id=plan.id,
            outcome="unsuccessful",
            today=TODAY,
            reason="Targets not met.",
        )
    assert "routes to an exit" in exc.value.detail


async def test_a_full_trail_permits_an_unsuccessful_outcome(
    session, hr_user, employee
):
    plan = await open_plan(session, hr_user, employee)
    for day in (date(2026, 9, 7), date(2026, 10, 5), date(2026, 11, 2)):
        await add_check_in(session, hr_user, plan, day)

    milestone = (
        await session.execute(
            select(PipMilestone).where(PipMilestone.pip_id == plan.id)
        )
    ).scalar_one()
    milestone.status = MilestoneStatusEnum.NOT_MET
    await session.commit()

    result = await performance.close_pip(
        session,
        hr_user,
        pip_id=plan.id,
        outcome="unsuccessful",
        today=TODAY,
        reason="Targets not met across three months of review.",
    )
    assert result["routes_to_exit"] is True
    assert result["trail"]["is_defensible"] is True


async def test_proceeding_on_a_thin_trail_is_recorded(session, hr_user, employee):
    """Falsifying a check-in to get past the check would be far worse than
    proceeding with the gap on the record."""
    plan = await open_plan(session, hr_user, employee)
    await add_check_in(session, hr_user, plan, date(2026, 9, 7))
    result = await performance.close_pip(
        session,
        hr_user,
        pip_id=plan.id,
        outcome="unsuccessful",
        today=TODAY,
        reason="Targets not met.",
        acknowledge_weak_trail=True,
    )
    assert result["routes_to_exit"] is True

    row = (await session.execute(select(Pip).where(Pip.id == plan.id))).scalar_one()
    assert row.weak_trail_acknowledged is True
    assert row.trail_gaps_at_outcome


async def test_the_trail_gaps_are_frozen_at_the_outcome(session, hr_user, employee):
    """Recomputing them later would describe a plan that has since been
    edited."""
    plan = await open_plan(session, hr_user, employee)
    await add_check_in(session, hr_user, plan, date(2026, 9, 7))
    await performance.close_pip(
        session,
        hr_user,
        pip_id=plan.id,
        outcome="unsuccessful",
        today=TODAY,
        reason="Targets not met.",
        acknowledge_weak_trail=True,
    )
    row = (await session.execute(select(Pip).where(Pip.id == plan.id))).scalar_one()
    frozen = list(row.trail_gaps_at_outcome)

    for day in (date(2026, 10, 5), date(2026, 11, 2)):
        await add_check_in(session, hr_user, plan, day)
    await session.refresh(row)
    assert list(row.trail_gaps_at_outcome) == frozen


async def test_a_successful_outcome_needs_no_acknowledgement(
    session, hr_user, employee
):
    """The asymmetry is deliberate: the employee keeps their job."""
    plan = await open_plan(session, hr_user, employee)
    result = await performance.close_pip(
        session, hr_user, pip_id=plan.id, outcome="successful", today=TODAY
    )
    assert result["new_status"] == "successful"
    assert result["routes_to_exit"] is False


async def test_an_extension_records_the_original_period(session, hr_user, employee):
    plan = await open_plan(session, hr_user, employee)
    await performance.close_pip(
        session,
        hr_user,
        pip_id=plan.id,
        outcome="extended",
        today=TODAY,
        reason="Two weeks lost to a project freeze.",
        new_end_date=date(2026, 12, 31),
    )
    row = (await session.execute(select(Pip).where(Pip.id == plan.id))).scalar_one()
    assert row.status is PipStatusEnum.EXTENDED
    assert row.end_date == date(2026, 12, 31)
    assert row.extensions[0]["from"] == "2026-11-30"


async def test_the_evidence_pack_carries_the_employees_words(
    session, hr_user, employee
):
    """A précis of somebody's contemporaneous account is not their account."""
    plan = await open_plan(session, hr_user, employee)
    await add_check_in(
        session,
        hr_user,
        plan,
        date(2026, 9, 7),
        employee_comments="The Q2 target was changed after I agreed it.",
    )
    pack = await performance.evidence_pack(
        session, hr_user, pip_id=plan.id, today=TODAY
    )
    assert (
        pack["check_ins"][0]["employee_comments"]
        == "The Q2 target was changed after I agreed it."
    )
    assert pack["support_provided"]
    assert pack["milestones"]


# ══════════════════════════════════════════════════════════════════════════════
# Gate: budget visible to managers during allocation
# ══════════════════════════════════════════════════════════════════════════════


async def test_the_budget_view_reports_both_scopes(session, hr_user, employee):
    cycle = await open_cycle(session, hr_user)
    view = await performance.budget_view(
        session, hr_user, cycle_id=cycle.id, department_id=employee.department_id
    )
    assert "department" in view
    assert "company" in view
    assert view["company"]["allocated"] == "5000000.00"


async def test_a_recommendation_returns_the_budget_with_it(
    session, hr_user, employee
):
    """A manager who allocates blind and is rejected later redoes everything at
    11pm."""
    cycle = await open_cycle(session, hr_user)
    out = await performance.recommend_increment(
        session,
        hr_user,
        cycle_id=cycle.id,
        employee_id=employee.id,
        rating_label="Meets",
        current_ctc=D("1200000"),
        percentage=D("6"),
    )
    assert out["budget"]["permitted"] is True
    assert out["proposal"]["recommended_ctc"] == "1272000.00"


async def test_consumption_accumulates_across_recommendations(
    session, hr_user, tenant, legal_entity, location
):
    cycle = await open_cycle(session, hr_user, budget_total=D("200000"))
    people = [
        await make_employee(session, tenant, legal_entity, location, f"BUD{i}")
        for i in range(2)
    ]
    await performance.recommend_increment(
        session,
        hr_user,
        cycle_id=cycle.id,
        employee_id=people[0].id,
        rating_label="Meets",
        current_ctc=D("1000000"),
        percentage=D("6"),
    )
    view = await performance.budget_view(session, hr_user, cycle_id=cycle.id)
    assert view["company"]["consumed"] == "60000.00"
    assert view["company"]["remaining"] == "140000.00"


async def test_a_hard_stop_budget_refuses_the_recommendation(
    session, hr_user, employee
):
    cycle = await open_cycle(
        session,
        hr_user,
        budget_total=D("10000"),
        overage_mode=OverageModeEnum.BLOCK,
    )
    with pytest.raises(HTTPException) as exc:
        await performance.recommend_increment(
            session,
            hr_user,
            cycle_id=cycle.id,
            employee_id=employee.id,
            rating_label="Meets",
            current_ctc=D("1200000"),
            percentage=D("6"),
        )
    assert exc.value.status_code == 409
    assert "set to block" in exc.value.detail


async def test_an_escalate_budget_permits_and_flags(session, hr_user, employee):
    cycle = await open_cycle(
        session,
        hr_user,
        budget_total=D("10000"),
        overage_mode=OverageModeEnum.ESCALATE,
    )
    out = await performance.recommend_increment(
        session,
        hr_user,
        cycle_id=cycle.id,
        employee_id=employee.id,
        rating_label="Meets",
        current_ctc=D("1200000"),
        percentage=D("6"),
    )
    assert out["status"] == "escalated"
    assert out["budget"]["requires_escalation"] is True


async def test_outside_the_band_without_a_reason_is_refused(
    session, hr_user, employee
):
    cycle = await open_cycle(session, hr_user)
    with pytest.raises(HTTPException) as exc:
        await performance.recommend_increment(
            session,
            hr_user,
            cycle_id=cycle.id,
            employee_id=employee.id,
            rating_label="Meets",
            current_ctc=D("1200000"),
            percentage=D("20"),
        )
    assert "corrupts the rating data" in exc.value.detail


async def test_outside_the_band_with_a_reason_is_accepted(
    session, hr_user, employee
):
    cycle = await open_cycle(session, hr_user)
    out = await performance.recommend_increment(
        session,
        hr_user,
        cycle_id=cycle.id,
        employee_id=employee.id,
        rating_label="Meets",
        current_ctc=D("1200000"),
        percentage=D("20"),
        justification="Took over a failing project in March and turned it round.",
    )
    assert out["proposal"]["within_band"] is False
    assert out["status"] in {"submitted", "escalated"}


async def test_an_unmapped_rating_is_refused(session, hr_user, employee):
    cycle = await open_cycle(session, hr_user)
    with pytest.raises(HTTPException) as exc:
        await performance.recommend_increment(
            session,
            hr_user,
            cycle_id=cycle.id,
            employee_id=employee.id,
            rating_label="Outstanding",
            current_ctc=D("1200000"),
            percentage=D("10"),
        )
    assert "nobody can explain to the employee" in exc.value.detail


async def test_a_rejected_recommendation_frees_its_budget(
    session, hr_user, tenant, legal_entity, location
):
    """A rejected proposal is not spend."""
    cycle = await open_cycle(session, hr_user, budget_total=D("200000"))
    person = await make_employee(session, tenant, legal_entity, location, "REJ1")
    out = await performance.recommend_increment(
        session,
        hr_user,
        cycle_id=cycle.id,
        employee_id=person.id,
        rating_label="Meets",
        current_ctc=D("1000000"),
        percentage=D("6"),
    )
    await performance.decide_recommendation(
        session,
        hr_user,
        recommendation_id=uuid.UUID(out["recommendation_id"]),
        approve=False,
        note="Not this cycle.",
    )
    view = await performance.budget_view(session, hr_user, cycle_id=cycle.id)
    assert view["company"]["consumed"] == "0.00"


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the pay-equity indicator at decision time
# ══════════════════════════════════════════════════════════════════════════════


async def test_a_small_comparison_group_yields_no_gap_figure(
    session, hr_user, employee
):
    cycle = await open_cycle(session, hr_user)
    out = await performance.recommend_increment(
        session,
        hr_user,
        cycle_id=cycle.id,
        employee_id=employee.id,
        rating_label="Meets",
        current_ctc=D("1200000"),
        percentage=D("6"),
    )
    assert out["equity"]["gender_gap"] is None
    assert out["equity"]["parity"] is None


async def test_the_equity_view_appears_with_the_recommendation(
    session, hr_user, tenant, legal_entity, location
):
    """Surfacing it while the number is being chosen is the only version that
    changes the number."""
    cycle = await open_cycle(session, hr_user)
    people = []
    for i in range(12):
        people.append(
            await make_employee(
                session,
                tenant,
                legal_entity,
                location,
                f"EQ{i:02d}",
                gender=Gender.MALE if i % 2 else Gender.FEMALE,
            )
        )
    for i, person in enumerate(people[:-1]):
        await performance.recommend_increment(
            session,
            hr_user,
            cycle_id=cycle.id,
            employee_id=person.id,
            rating_label="Meets",
            current_ctc=D(str(1000000 + i * 50000)),
            percentage=D("6"),
        )
    out = await performance.recommend_increment(
        session,
        hr_user,
        cycle_id=cycle.id,
        employee_id=people[-1].id,
        rating_label="Meets",
        current_ctc=D("1150000"),
        percentage=D("6"),
        band_midpoint=D("1300000"),
    )
    assert out["equity"]["comparison_group_size"] >= 5
    assert out["equity"]["parity"] is not None
    assert out["equity"]["compa_ratio"]["reading"]


async def test_the_equity_snapshot_is_stored_on_the_row(
    session, hr_user, employee
):
    cycle = await open_cycle(session, hr_user)
    out = await performance.recommend_increment(
        session,
        hr_user,
        cycle_id=cycle.id,
        employee_id=employee.id,
        rating_label="Meets",
        current_ctc=D("1200000"),
        percentage=D("6"),
    )
    from app.models.performance_flow import IncrementRecommendation

    row = (
        await session.execute(
            select(IncrementRecommendation).where(
                IncrementRecommendation.id == uuid.UUID(out["recommendation_id"])
            )
        )
    ).scalar_one()
    assert row.equity_snapshot
    assert row.equity_snapshot["employee_id"] == str(employee.id)


# ══════════════════════════════════════════════════════════════════════════════
# Gate: the promotion applies atomically
# ══════════════════════════════════════════════════════════════════════════════


async def approved_promotion(session, hr_user, employee, cycle, **kw):
    out = await performance.recommend_increment(
        session,
        hr_user,
        cycle_id=cycle.id,
        employee_id=employee.id,
        rating_label="Exceeds",
        current_ctc=D("1200000"),
        percentage=D("12"),
        promotion_to_grade="M4",
        **kw,
    )
    await performance.decide_recommendation(
        session,
        hr_user,
        recommendation_id=uuid.UUID(out["recommendation_id"]),
        approve=True,
        note="Approved.",
    )
    return uuid.UUID(out["recommendation_id"])


async def test_an_unapproved_recommendation_cannot_be_promoted(
    session, hr_user, employee
):
    cycle = await open_cycle(session, hr_user)
    out = await performance.recommend_increment(
        session,
        hr_user,
        cycle_id=cycle.id,
        employee_id=employee.id,
        rating_label="Exceeds",
        current_ctc=D("1200000"),
        percentage=D("12"),
        promotion_to_grade="M4",
    )
    with pytest.raises(HTTPException) as exc:
        await performance.apply_promotion(
            session,
            hr_user,
            recommendation_id=uuid.UUID(out["recommendation_id"]),
            to_position_id=None,
            effective_from=date(2027, 4, 1),
            today=date(2027, 4, 1),
        )
    assert "nobody signed off" in exc.value.detail


async def test_a_recommendation_with_no_target_grade_is_not_a_promotion(
    session, hr_user, employee
):
    cycle = await open_cycle(session, hr_user)
    out = await performance.recommend_increment(
        session,
        hr_user,
        cycle_id=cycle.id,
        employee_id=employee.id,
        rating_label="Meets",
        current_ctc=D("1200000"),
        percentage=D("6"),
    )
    await performance.decide_recommendation(
        session,
        hr_user,
        recommendation_id=uuid.UUID(out["recommendation_id"]),
        approve=True,
    )
    with pytest.raises(HTTPException) as exc:
        await performance.apply_promotion(
            session,
            hr_user,
            recommendation_id=uuid.UUID(out["recommendation_id"]),
            to_position_id=None,
            effective_from=date(2027, 4, 1),
            today=date(2027, 4, 1),
        )
    assert "it is an increment" in exc.value.detail


async def test_a_promotion_records_every_step_it_produced(
    session, hr_user, tenant, employee
):
    """A row with three of four filled is a promotion that did not complete."""
    await give_salary(session, tenant, hr_user, employee)
    cycle = await open_cycle(session, hr_user)
    recommendation_id = await approved_promotion(session, hr_user, employee, cycle)
    out = await performance.apply_promotion(
        session,
        hr_user,
        recommendation_id=recommendation_id,
        to_position_id=None,
        effective_from=date(2026, 12, 1),
        today=date(2026, 12, 1),
    )
    assert out["plan"]["is_complete"] is True

    row = (
        await session.execute(
            select(PromotionRecord).where(
                PromotionRecord.id == uuid.UUID(out["promotion_record_id"])
            )
        )
    ).scalar_one()
    assert row.lifecycle_event_id is not None
    assert row.employee_salary_id is not None
    assert row.to_grade == "M4"
    assert row.grade_applied is True


async def test_a_future_dated_promotion_is_not_applied_early(
    session, hr_user, tenant, employee
):
    """Effecting it early would change the record before the decision was due
    to apply."""
    await give_salary(session, tenant, hr_user, employee)
    grade_before = employee.grade
    cycle = await open_cycle(session, hr_user)
    recommendation_id = await approved_promotion(session, hr_user, employee, cycle)
    out = await performance.apply_promotion(
        session,
        hr_user,
        recommendation_id=recommendation_id,
        to_position_id=None,
        effective_from=date(2027, 4, 1),
        today=date(2026, 12, 1),
    )
    assert out["effected"] is None
    assert "not** applied yet" in out["note"]

    await session.refresh(employee)
    assert employee.grade == grade_before


async def test_a_promotion_cannot_be_applied_twice(
    session, hr_user, tenant, employee
):
    await give_salary(session, tenant, hr_user, employee)
    cycle = await open_cycle(session, hr_user)
    recommendation_id = await approved_promotion(session, hr_user, employee, cycle)
    await performance.apply_promotion(
        session,
        hr_user,
        recommendation_id=recommendation_id,
        to_position_id=None,
        effective_from=date(2026, 12, 1),
        today=date(2026, 12, 1),
    )
    with pytest.raises(HTTPException) as exc:
        await performance.apply_promotion(
            session,
            hr_user,
            recommendation_id=recommendation_id,
            to_position_id=None,
            effective_from=date(2026, 12, 1),
            today=date(2026, 12, 1),
        )
    assert "already been applied" in exc.value.detail


async def test_a_backdated_promotion_flags_arrears(
    session, hr_user, tenant, employee
):
    """A revision effective in April and approved in December owes April
    onward."""
    await give_salary(session, tenant, hr_user, employee)
    cycle = await open_cycle(session, hr_user)
    recommendation_id = await approved_promotion(session, hr_user, employee, cycle)
    out = await performance.apply_promotion(
        session,
        hr_user,
        recommendation_id=recommendation_id,
        to_position_id=None,
        effective_from=date(2026, 4, 1),
        today=date(2026, 12, 1),
    )
    assert out["arrears"]["arrears_due"] is True
    assert out["arrears"]["from"] == "2026-04-01"
    assert out["arrears"]["months_spanned"] == 8


# ══════════════════════════════════════════════════════════════════════════════
# Gate: letters release on a controlled date
# ══════════════════════════════════════════════════════════════════════════════


async def test_letters_do_not_release_before_the_date(session, hr_user, employee):
    cycle = await open_cycle(session, hr_user, letter_release_on=date(2027, 3, 25))
    await approved_promotion(session, hr_user, employee, cycle)
    with pytest.raises(HTTPException) as exc:
        await performance.release_letters(
            session, hr_user, cycle_id=cycle.id, today=date(2027, 3, 1)
        )
    assert "release on 2027-03-25" in exc.value.detail


async def test_letters_do_not_release_while_approvals_are_outstanding(
    session, hr_user, tenant, legal_entity, location
):
    cycle = await open_cycle(session, hr_user, letter_release_on=date(2027, 3, 25))
    first = await make_employee(session, tenant, legal_entity, location, "LTR1")
    second = await make_employee(session, tenant, legal_entity, location, "LTR2")
    await approved_promotion(session, hr_user, first, cycle)
    await performance.recommend_increment(
        session,
        hr_user,
        cycle_id=cycle.id,
        employee_id=second.id,
        rating_label="Meets",
        current_ctc=D("1000000"),
        percentage=D("6"),
    )
    with pytest.raises(HTTPException) as exc:
        await performance.release_letters(
            session, hr_user, cycle_id=cycle.id, today=date(2027, 3, 25)
        )
    assert "piecemeal release" in exc.value.detail


async def test_a_complete_cycle_releases_together(session, hr_user, employee):
    cycle = await open_cycle(session, hr_user, letter_release_on=date(2027, 3, 25))
    await approved_promotion(session, hr_user, employee, cycle)
    result = await performance.release_letters(
        session, hr_user, cycle_id=cycle.id, today=date(2027, 3, 25)
    )
    assert result["permitted"] is True
    assert result["letter_count"] == 1

    row = (
        await session.execute(
            select(IncrementCycle).where(IncrementCycle.id == cycle.id)
        )
    ).scalar_one()
    assert row.letters_released_at is not None


async def test_a_partial_release_is_recorded(
    session, hr_user, tenant, legal_entity, location
):
    cycle = await open_cycle(session, hr_user, letter_release_on=date(2027, 3, 25))
    first = await make_employee(session, tenant, legal_entity, location, "PRT1")
    second = await make_employee(session, tenant, legal_entity, location, "PRT2")
    await approved_promotion(session, hr_user, first, cycle)
    await performance.recommend_increment(
        session,
        hr_user,
        cycle_id=cycle.id,
        employee_id=second.id,
        rating_label="Meets",
        current_ctc=D("1000000"),
        percentage=D("6"),
    )
    result = await performance.release_letters(
        session,
        hr_user,
        cycle_id=cycle.id,
        today=date(2027, 3, 25),
        force_partial=True,
    )
    assert result["permitted"] is True
    assert result["outstanding_approvals"] == [str(second.id)]

    row = (
        await session.execute(
            select(IncrementCycle).where(IncrementCycle.id == cycle.id)
        )
    ).scalar_one()
    assert row.partial_release_acknowledged is True


async def test_a_cycle_cannot_release_twice(session, hr_user, employee):
    cycle = await open_cycle(session, hr_user, letter_release_on=date(2027, 3, 25))
    await approved_promotion(session, hr_user, employee, cycle)
    await performance.release_letters(
        session, hr_user, cycle_id=cycle.id, today=date(2027, 3, 25)
    )
    with pytest.raises(HTTPException) as exc:
        await performance.release_letters(
            session, hr_user, cycle_id=cycle.id, today=date(2027, 3, 25)
        )
    assert "second copy" in exc.value.detail


async def test_a_cycle_with_no_release_date_is_refused(session, hr_user, employee):
    cycle = await open_cycle(session, hr_user, letter_release_on=None)
    await approved_promotion(session, hr_user, employee, cycle)
    with pytest.raises(HTTPException) as exc:
        await performance.release_letters(
            session, hr_user, cycle_id=cycle.id, today=date(2027, 3, 25)
        )
    assert "fastest approver" in exc.value.detail


# ══════════════════════════════════════════════════════════════════════════════
# Tenant isolation
# ══════════════════════════════════════════════════════════════════════════════


async def test_another_tenant_cannot_read_a_pip(
    session, hr_user, other_hr_user, employee
):
    plan = await open_plan(session, hr_user, employee)
    with pytest.raises(HTTPException) as exc:
        await performance.evidence_pack(
            session, other_hr_user, pip_id=plan.id, today=TODAY
        )
    assert exc.value.status_code == 404


async def test_another_tenant_cannot_see_this_cycles_budget(
    session, hr_user, other_hr_user
):
    cycle = await open_cycle(session, hr_user)
    with pytest.raises(HTTPException) as exc:
        await performance.budget_view(session, other_hr_user, cycle_id=cycle.id)
    assert exc.value.status_code == 404


async def test_another_tenant_cannot_decide_a_recommendation(
    session, hr_user, other_hr_user, employee
):
    cycle = await open_cycle(session, hr_user)
    out = await performance.recommend_increment(
        session,
        hr_user,
        cycle_id=cycle.id,
        employee_id=employee.id,
        rating_label="Meets",
        current_ctc=D("1200000"),
        percentage=D("6"),
    )
    with pytest.raises(HTTPException) as exc:
        await performance.decide_recommendation(
            session,
            other_hr_user,
            recommendation_id=uuid.UUID(out["recommendation_id"]),
            approve=True,
        )
    assert exc.value.status_code == 404


async def test_the_posture_says_payroll_is_not_re_run():
    posture = performance.performance_posture()
    joined = " ".join(posture["not_built"]).lower()
    assert "payroll is not re-run" in joined
    assert "grade and location" in joined


async def test_the_recommendation_status_reflects_escalation(
    session, hr_user, employee
):
    cycle = await open_cycle(
        session, hr_user, budget_total=D("1000"), overage_mode=OverageModeEnum.ESCALATE
    )
    out = await performance.recommend_increment(
        session,
        hr_user,
        cycle_id=cycle.id,
        employee_id=employee.id,
        rating_label="Meets",
        current_ctc=D("1200000"),
        percentage=D("6"),
    )
    from app.models.performance_flow import IncrementRecommendation

    row = (
        await session.execute(
            select(IncrementRecommendation).where(
                IncrementRecommendation.id == uuid.UUID(out["recommendation_id"])
            )
        )
    ).scalar_one()
    assert row.status is RecommendationStatusEnum.ESCALATED
    assert row.overage_amount > 0


async def test_a_promotion_with_no_salary_to_revise_is_refused_up_front(
    session, hr_user, employee
):
    """The cascade would refuse halfway through and roll back, which is correct
    but tells the manager nothing they can act on."""
    cycle = await open_cycle(session, hr_user)
    recommendation_id = await approved_promotion(session, hr_user, employee, cycle)
    with pytest.raises(HTTPException) as exc:
        await performance.apply_promotion(
            session,
            hr_user,
            recommendation_id=recommendation_id,
            to_position_id=None,
            effective_from=date(2026, 12, 1),
            today=date(2026, 12, 1),
        )
    assert "Assign a salary structure first" in exc.value.detail
