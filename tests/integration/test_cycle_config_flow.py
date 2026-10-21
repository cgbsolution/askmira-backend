"""Slice 18 gates that need a real database.

The assignment engine is proved in `tests/appraisal/`. What only a database
settles: that activation genuinely refuses when someone is unresolved, that a
clone reproduces a full configuration, that auto-advance does what the stage was
configured to do, and that closure is blocked by an enforced calibration.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appraisal import (
    Appraisal,
    AppraisalCycle,
    AppraisalStage,
    AppraisalStatus,
    CycleStatus,
    CycleType,
    StageRole,
)
from app.models.calibration import (
    CalibrationMode,
    CalibrationPolicy,
    CalibrationScope,
    DistributionTarget,
)
from app.models.competency import CompetencyTemplate
from app.models.cycle_config import (
    AutoAdvanceBehaviour,
    ProbationOutcome,
    ProbationReview,
    TemplateAssignmentRule,
)
from app.models.employee import Employee, EmploymentStatus
from app.models.org import Department, LegalEntity
from app.models.tenant import Tenant
from app.models.user import User
from app.services.appraisal import cycle_config as cycle_service
from tests.integration.conftest import requires_db

pytestmark = requires_db


async def _template(session: AsyncSession, tenant: Tenant, name: str) -> CompetencyTemplate:
    template = CompetencyTemplate(tenant_id=tenant.id, name=name)
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return template


async def _cycle(
    session: AsyncSession, tenant: Tenant, *, name: str = "FY26 review"
) -> AppraisalCycle:
    cycle = AppraisalCycle(
        tenant_id=tenant.id,
        name=name,
        cycle_type=CycleType.ANNUAL,
        period_start=date(2026, 4, 1),
        period_end=date(2027, 3, 31),
    )
    session.add(cycle)
    await session.flush()
    for sequence, (role, label) in enumerate(
        [(StageRole.SELF, "Self appraisal"), (StageRole.MANAGER, "Manager review")],
        start=1,
    ):
        session.add(
            AppraisalStage(
                tenant_id=tenant.id,
                cycle_id=cycle.id,
                sequence=sequence,
                role=role,
                label=label,
            )
        )
    await session.commit()
    await session.refresh(cycle)
    return cycle


async def _rule(
    session: AsyncSession,
    tenant: Tenant,
    cycle: AppraisalCycle,
    template: CompetencyTemplate,
    *,
    priority: int,
    conditions: dict | None = None,
    label: str = "",
) -> TemplateAssignmentRule:
    rule = TemplateAssignmentRule(
        tenant_id=tenant.id,
        cycle_id=cycle.id,
        priority=priority,
        template_id=template.id,
        conditions=conditions or {},
        label=label,
    )
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return rule


# ═══════════════════════════════════════════════════════════════════════════
# Activation — the gate
# ═══════════════════════════════════════════════════════════════════════════


async def test_activation_refuses_when_an_employee_resolves_to_no_template(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate.

    At activation this is a configuration problem somebody fixes in a minute.
    At launch it is an employee with no appraisal who finds out months later.
    """
    cycle = await _cycle(session, tenant)
    senior = await _template(session, tenant, "Senior")
    await _rule(
        session,
        tenant,
        cycle,
        senior,
        priority=10,
        conditions={"grades": ["L5"]},
        label="Senior grades",
    )
    employee.grade = "L3"
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await cycle_service.activate_cycle(session, hr_user, cycle_id=cycle.id)

    assert exc.value.status_code == 422
    assert "No default template rule" in exc.value.detail

    await session.refresh(cycle)
    assert cycle.status is CycleStatus.DRAFT, "the cycle did not activate"


async def test_activation_succeeds_once_a_default_rule_exists(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    cycle = await _cycle(session, tenant)
    senior = await _template(session, tenant, "Senior")
    standard = await _template(session, tenant, "Standard")
    await _rule(
        session,
        tenant,
        cycle,
        senior,
        priority=10,
        conditions={"grades": ["L5"]},
        label="Senior grades",
    )
    await _rule(session, tenant, cycle, standard, priority=100, label="Everyone else")
    employee.grade = "L3"
    await session.commit()

    result = await cycle_service.activate_cycle(session, hr_user, cycle_id=cycle.id)
    await session.commit()

    assert result["resolved"] == 1
    assert result["by_template"] == {str(standard.id): 1}
    assert cycle.status is CycleStatus.ACTIVE


async def test_the_highest_priority_rule_wins_against_real_employees(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    legal_entity: LegalEntity,
) -> None:
    """The checklist gate: overlapping conditions resolve deterministically."""
    cycle = await _cycle(session, tenant)
    senior = await _template(session, tenant, "Senior")
    engineering = await _template(session, tenant, "Engineering")
    standard = await _template(session, tenant, "Standard")

    dept = Department(tenant_id=tenant.id, name="Engineering", code="ENG")
    session.add(dept)
    await session.flush()

    await _rule(
        session,
        tenant,
        cycle,
        senior,
        priority=10,
        conditions={"grades": ["L5"]},
        label="Senior",
    )
    await _rule(
        session,
        tenant,
        cycle,
        engineering,
        priority=20,
        conditions={"department_ids": [str(dept.id)]},
        label="Engineering",
    )
    await _rule(session, tenant, cycle, standard, priority=100, label="Default")

    # An L5 engineer matches both the senior and the engineering rule.
    employee.grade = "L5"
    employee.department_id = dept.id
    await session.commit()

    result = await cycle_service.activate_cycle(session, hr_user, cycle_id=cycle.id)
    await session.commit()

    assert result["by_template"] == {str(senior.id): 1}, "priority 10 beats priority 20"


async def test_a_cycle_with_no_stages_cannot_activate(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    cycle = AppraisalCycle(
        tenant_id=tenant.id,
        name="Empty",
        cycle_type=CycleType.OFF_CYCLE,
        period_start=date(2026, 4, 1),
        period_end=date(2027, 3, 31),
    )
    session.add(cycle)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await cycle_service.activate_cycle(session, hr_user, cycle_id=cycle.id)
    assert "no stages" in exc.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# Auto-advance
# ═══════════════════════════════════════════════════════════════════════════


async def _stuck(session, tenant, hr_user, employee, behaviour):
    cycle = await _cycle(session, tenant)
    standard = await _template(session, tenant, "Standard")
    await _rule(session, tenant, cycle, standard, priority=100, label="Default")

    stages = (
        await session.scalars(
            select(AppraisalStage)
            .where(AppraisalStage.cycle_id == cycle.id)
            .order_by(AppraisalStage.sequence)
        )
    ).all()
    stages[0].close_date = date(2026, 5, 31)
    stages[0].auto_advance = behaviour

    appraisal = Appraisal(
        tenant_id=tenant.id,
        cycle_id=cycle.id,
        employee_id=employee.id,
        current_stage_id=stages[0].id,
        status=AppraisalStatus.IN_PROGRESS,
    )
    session.add(appraisal)
    await session.commit()
    return cycle, stages, appraisal


async def test_advance_moves_the_appraisal_on_and_records_the_skip(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate: explicit and configured, not accidental."""
    cycle, stages, appraisal = await _stuck(
        session, tenant, hr_user, employee, AutoAdvanceBehaviour.ADVANCE
    )

    result = await cycle_service.run_auto_advance(
        session, hr_user, cycle_id=cycle.id, as_of=date(2026, 6, 15)
    )
    await session.commit()

    assert result["advanced"] == 1
    assert result["escalated"] == 0
    await session.refresh(appraisal)
    assert appraisal.current_stage_id == stages[1].id
    assert "recorded as never filled in" in result["actions"][0]["reason"]


async def test_escalate_keeps_the_appraisal_on_the_stage(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """Nothing is skipped; somebody else is asked to do it."""
    cycle, stages, appraisal = await _stuck(
        session, tenant, hr_user, employee, AutoAdvanceBehaviour.ESCALATE
    )

    result = await cycle_service.run_auto_advance(
        session, hr_user, cycle_id=cycle.id, as_of=date(2026, 6, 15)
    )
    await session.commit()

    assert result["escalated"] == 1
    assert result["advanced"] == 0
    await session.refresh(appraisal)
    assert appraisal.current_stage_id == stages[0].id, "it stays put"


async def test_hold_does_nothing(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    cycle, stages, appraisal = await _stuck(
        session, tenant, hr_user, employee, AutoAdvanceBehaviour.HOLD
    )

    result = await cycle_service.run_auto_advance(
        session, hr_user, cycle_id=cycle.id, as_of=date(2026, 6, 15)
    )
    await session.commit()

    assert result["held"] == 1
    await session.refresh(appraisal)
    assert appraisal.current_stage_id == stages[0].id


async def test_an_open_stage_window_is_left_alone(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    cycle, _, _ = await _stuck(session, tenant, hr_user, employee, AutoAdvanceBehaviour.ADVANCE)
    result = await cycle_service.run_auto_advance(
        session, hr_user, cycle_id=cycle.id, as_of=date(2026, 5, 1)
    )
    assert result["advanced"] == 0
    assert "No stage windows have closed" in result["note"]


# ═══════════════════════════════════════════════════════════════════════════
# Cloning
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_clone_reproduces_stages_and_rules_but_not_appraisals(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate.

    A clone is a new review of the same shape, not a copy of last year's
    answers.
    """
    cycle = await _cycle(session, tenant)
    senior = await _template(session, tenant, "Senior")
    standard = await _template(session, tenant, "Standard")
    await _rule(
        session,
        tenant,
        cycle,
        senior,
        priority=10,
        conditions={"grades": ["L5"]},
        label="Senior",
    )
    await _rule(session, tenant, cycle, standard, priority=100, label="Default")
    session.add(
        Appraisal(
            tenant_id=tenant.id,
            cycle_id=cycle.id,
            employee_id=employee.id,
            status=AppraisalStatus.COMPLETED,
            final_rating="Meets",
        )
    )
    await session.commit()

    result = await cycle_service.clone_cycle(
        session,
        hr_user,
        source_cycle_id=cycle.id,
        name="FY27 review",
        period_start=date(2027, 4, 1),
        period_end=date(2028, 3, 31),
    )
    await session.commit()

    clone_id = result["cycle_id"]
    assert result["stages"] == 2
    assert result["rules"] == 2

    stages = await session.scalar(
        select(func.count()).select_from(AppraisalStage).where(AppraisalStage.cycle_id == clone_id)
    )
    rules = await session.scalar(
        select(func.count())
        .select_from(TemplateAssignmentRule)
        .where(TemplateAssignmentRule.cycle_id == clone_id)
    )
    appraisals = await session.scalar(
        select(func.count()).select_from(Appraisal).where(Appraisal.cycle_id == clone_id)
    )
    assert stages == 2
    assert rules == 2
    assert appraisals == 0, "the people do not come across"

    clone = await session.get(AppraisalCycle, clone_id)
    assert clone.status is CycleStatus.DRAFT


async def test_a_clone_carries_no_stage_dates(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """Last year's dates are all in the past; auto-advance would fire on
    everything the moment the clone activated."""
    cycle = await _cycle(session, tenant)
    standard = await _template(session, tenant, "Standard")
    await _rule(session, tenant, cycle, standard, priority=100, label="Default")

    stages = (
        await session.scalars(select(AppraisalStage).where(AppraisalStage.cycle_id == cycle.id))
    ).all()
    for stage in stages:
        stage.due_date = date(2026, 5, 31)
        stage.close_date = date(2026, 6, 15)
    await session.commit()

    result = await cycle_service.clone_cycle(
        session,
        hr_user,
        source_cycle_id=cycle.id,
        name="FY27 review",
        period_start=date(2027, 4, 1),
        period_end=date(2028, 3, 31),
    )
    await session.commit()

    cloned = (
        await session.scalars(
            select(AppraisalStage).where(AppraisalStage.cycle_id == result["cycle_id"])
        )
    ).all()
    assert all(s.due_date is None and s.close_date is None for s in cloned)
    assert "would all be in the past" in result["note"]


async def test_a_clone_preserves_the_auto_advance_configuration(
    session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    """The dates are last year's; the *decision* about what happens when a
    window closes is not."""
    cycle = await _cycle(session, tenant)
    standard = await _template(session, tenant, "Standard")
    await _rule(session, tenant, cycle, standard, priority=100, label="Default")

    stages = (
        await session.scalars(
            select(AppraisalStage)
            .where(AppraisalStage.cycle_id == cycle.id)
            .order_by(AppraisalStage.sequence)
        )
    ).all()
    stages[0].auto_advance = AutoAdvanceBehaviour.ADVANCE
    await session.commit()

    result = await cycle_service.clone_cycle(
        session,
        hr_user,
        source_cycle_id=cycle.id,
        name="FY27 review",
        period_start=date(2027, 4, 1),
        period_end=date(2028, 3, 31),
    )
    await session.commit()

    cloned = (
        await session.scalars(
            select(AppraisalStage)
            .where(AppraisalStage.cycle_id == result["cycle_id"])
            .order_by(AppraisalStage.sequence)
        )
    ).all()
    assert cloned[0].auto_advance is AutoAdvanceBehaviour.ADVANCE


# ═══════════════════════════════════════════════════════════════════════════
# Closure
# ═══════════════════════════════════════════════════════════════════════════


async def test_closure_is_blocked_while_an_enforced_calibration_is_out(
    session: AsyncSession, tenant: Tenant, hr_user: User, legal_entity: LegalEntity
) -> None:
    """The checklist gate, including the calibration dependency.

    Not acknowledgeable: the client chose ENFORCED, and a checkbox that
    overrides their own policy makes the policy decorative.
    """
    cycle = await _cycle(session, tenant)
    policy = CalibrationPolicy(
        tenant_id=tenant.id,
        cycle_id=cycle.id,
        mode=CalibrationMode.ENFORCED,
        scope=CalibrationScope.DEPARTMENT,
        min_group_size=4,
    )
    session.add(policy)
    await session.flush()
    for rating, target, tolerance in [("Outstanding", 10, 2), ("Meets", 90, 10)]:
        session.add(
            DistributionTarget(
                tenant_id=tenant.id,
                policy_id=policy.id,
                rating_value=rating,
                target_percent=Decimal(target),
                tolerance_percent=Decimal(tolerance),
            )
        )

    dept = Department(tenant_id=tenant.id, name="Engineering", code="ENG")
    session.add(dept)
    await session.flush()

    # Everyone Outstanding: 100% against an 8-12% band.
    for index in range(5):
        person = Employee(
            tenant_id=tenant.id,
            employee_code=f"CLS{index:03d}",
            first_name="Person",
            last_name=str(index),
            work_email=f"cls{index}@acme.test",
            legal_entity_id=legal_entity.id,
            department_id=dept.id,
        )
        session.add(person)
        await session.flush()
        session.add(
            Appraisal(
                tenant_id=tenant.id,
                cycle_id=cycle.id,
                employee_id=person.id,
                status=AppraisalStatus.COMPLETED,
                final_rating="Outstanding",
            )
        )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await cycle_service.close_cycle(session, hr_user, cycle_id=cycle.id)

    assert exc.value.status_code == 409
    assert "outside the enforced distribution tolerance" in exc.value.detail["message"]
    assert exc.value.detail["blockers"]

    # And it is not acknowledgeable.
    with pytest.raises(HTTPException):
        await cycle_service.close_cycle(
            session, hr_user, cycle_id=cycle.id, acknowledge_incomplete=True
        )


async def test_incomplete_appraisals_block_closure_until_acknowledged(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    cycle = await _cycle(session, tenant)
    session.add(
        Appraisal(
            tenant_id=tenant.id,
            cycle_id=cycle.id,
            employee_id=employee.id,
            status=AppraisalStatus.IN_PROGRESS,
        )
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await cycle_service.close_cycle(session, hr_user, cycle_id=cycle.id)
    assert "permanently unfinished" in exc.value.detail["message"]

    result = await cycle_service.close_cycle(
        session, hr_user, cycle_id=cycle.id, acknowledge_incomplete=True
    )
    await session.commit()
    assert result["incomplete_acknowledged"] == 1
    assert cycle.status is CycleStatus.CLOSED


async def test_a_complete_cycle_closes_cleanly(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    cycle = await _cycle(session, tenant)
    session.add(
        Appraisal(
            tenant_id=tenant.id,
            cycle_id=cycle.id,
            employee_id=employee.id,
            status=AppraisalStatus.ACKNOWLEDGED,
            final_rating="Meets",
        )
    )
    await session.commit()

    result = await cycle_service.close_cycle(session, hr_user, cycle_id=cycle.id)
    await session.commit()
    assert result["incomplete_acknowledged"] == 0
    assert cycle.status is CycleStatus.CLOSED


# ═══════════════════════════════════════════════════════════════════════════
# Probation — on its own clock
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_probation_review_is_due_on_the_employees_own_date(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    legal_entity: LegalEntity,
) -> None:
    """The checklist gate: triggered per employee, not on the calendar."""
    other = Employee(
        tenant_id=tenant.id,
        employee_code="PRB002",
        first_name="Later",
        last_name="Joiner",
        work_email="later@acme.test",
        legal_entity_id=legal_entity.id,
    )
    session.add(other)
    await session.flush()

    session.add_all(
        [
            ProbationReview(
                tenant_id=tenant.id,
                employee_id=employee.id,
                probation_end_date=date(2026, 7, 1),
                review_due_date=date(2026, 6, 15),
            ),
            ProbationReview(
                tenant_id=tenant.id,
                employee_id=other.id,
                probation_end_date=date(2026, 11, 1),
                review_due_date=date(2026, 10, 15),
            ),
        ]
    )
    await session.commit()

    due_by_june = (
        await session.scalars(
            select(ProbationReview).where(
                ProbationReview.review_due_date <= date(2026, 6, 30),
                ProbationReview.outcome.is_(None),
            )
        )
    ).all()
    assert len(due_by_june) == 1, "each runs on its own clock"
    assert due_by_june[0].employee_id == employee.id


async def test_confirming_stamps_the_employee_record(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """`EmploymentStatus` has no PROBATION member — a probationer is stored as
    ACTIVE with a `probation_end_date`, so the confirmation date is what the
    outcome actually writes back."""
    employee.status = EmploymentStatus.ON_LEAVE
    review = ProbationReview(
        tenant_id=tenant.id,
        employee_id=employee.id,
        probation_end_date=date(2026, 7, 1),
        review_due_date=date(2026, 6, 15),
    )
    session.add(review)
    await session.commit()

    result = await cycle_service.record_probation_outcome(
        session,
        hr_user,
        review_id=review.id,
        outcome=ProbationOutcome.CONFIRM,
        notes="Confirmed after a strong first three months.",
    )
    await session.commit()

    assert result.outcome is ProbationOutcome.CONFIRM
    assert result.outcome_recorded_at is not None
    assert result.outcome_recorded_by_user_id == hr_user.id
    await session.refresh(employee)
    assert employee.status is EmploymentStatus.ACTIVE
    assert employee.probation_end_date == date(2026, 7, 1)


async def test_extending_schedules_the_next_review_automatically(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate.

    An extension with no follow-up is how someone stays on probation
    indefinitely because the second conversation never got diarised.
    """
    review = ProbationReview(
        tenant_id=tenant.id,
        employee_id=employee.id,
        probation_end_date=date(2026, 7, 1),
        review_due_date=date(2026, 6, 15),
    )
    session.add(review)
    await session.commit()

    await cycle_service.record_probation_outcome(
        session,
        hr_user,
        review_id=review.id,
        outcome=ProbationOutcome.EXTEND,
        new_probation_end_date=date(2026, 10, 1),
        notes="Needs another quarter on the delivery goal.",
    )
    await session.commit()

    follow_ups = (
        await session.scalars(
            select(ProbationReview).where(ProbationReview.extends_review_id == review.id)
        )
    ).all()
    assert len(follow_ups) == 1
    assert follow_ups[0].probation_end_date == date(2026, 10, 1)
    assert follow_ups[0].outcome is None

    await session.refresh(employee)
    assert employee.probation_end_date == date(2026, 10, 1)


async def test_extending_without_a_new_date_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    review = ProbationReview(
        tenant_id=tenant.id,
        employee_id=employee.id,
        probation_end_date=date(2026, 7, 1),
        review_due_date=date(2026, 6, 15),
    )
    session.add(review)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await cycle_service.record_probation_outcome(
            session, hr_user, review_id=review.id, outcome=ProbationOutcome.EXTEND
        )
    assert "stays on probation with nothing scheduled to end it" in exc.value.detail


async def test_a_review_concludes_only_once(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    review = ProbationReview(
        tenant_id=tenant.id,
        employee_id=employee.id,
        probation_end_date=date(2026, 7, 1),
        review_due_date=date(2026, 6, 15),
    )
    session.add(review)
    await session.commit()

    await cycle_service.record_probation_outcome(
        session, hr_user, review_id=review.id, outcome=ProbationOutcome.CONFIRM
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await cycle_service.record_probation_outcome(
            session, hr_user, review_id=review.id, outcome=ProbationOutcome.TERMINATE
        )
    assert "already concluded" in exc.value.detail


async def test_cycle_configuration_is_tenant_isolated(
    session: AsyncSession, tenant: Tenant, hr_user: User, other_hr_user: User
) -> None:
    cycle = await _cycle(session, tenant)
    with pytest.raises(HTTPException) as exc:
        await cycle_service.activate_cycle(session, other_hr_user, cycle_id=cycle.id)
    assert exc.value.status_code == 404
