"""Slice 15 gates that need a real database.

The progression rules are proved in `tests/appraisal/`. What only a database
settles: that a return leaves both entries in the history, that a delegated
entry names the original owner, that an objection closes the cycle without
agreement, and that the progress dashboard is right under partial completion.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appraisal import (
    AcknowledgementType,
    Appraisal,
    AppraisalCycle,
    AppraisalStage,
    AppraisalStageEntry,
    AppraisalStatus,
    CycleStatus,
    CycleType,
    StageDelegation,
    StageRole,
)
from app.models.employee import Employee, EmploymentStatus
from app.models.org import LegalEntity
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.appraisal import service as appraisal_service
from tests.integration.conftest import requires_db

pytestmark = requires_db


async def _cycle(session: AsyncSession, tenant: Tenant, **overrides) -> AppraisalCycle:
    base = {
        "name": "FY26 annual review",
        "cycle_type": CycleType.ANNUAL,
        "period_start": date(2026, 4, 1),
        "period_end": date(2027, 3, 31),
        "eligibility_rule": {},
    }
    base.update(overrides)
    cycle = AppraisalCycle(tenant_id=tenant.id, **base)
    session.add(cycle)
    await session.flush()

    for sequence, (role, label, mandatory) in enumerate(
        [
            (StageRole.SELF, "Self appraisal", True),
            (StageRole.MANAGER, "Manager review", True),
            (StageRole.REVIEWER, "Reviewer sign-off", False),
            (StageRole.HR, "HR closure", True),
        ],
        start=1,
    ):
        session.add(
            AppraisalStage(
                tenant_id=tenant.id,
                cycle_id=cycle.id,
                sequence=sequence,
                role=role,
                label=label,
                is_mandatory=mandatory,
            )
        )
    await session.commit()
    await session.refresh(cycle)
    return cycle


async def _stages(session: AsyncSession, cycle: AppraisalCycle) -> list[AppraisalStage]:
    return list(
        (
            await session.scalars(
                select(AppraisalStage)
                .where(AppraisalStage.cycle_id == cycle.id)
                .order_by(AppraisalStage.sequence)
            )
        ).all()
    )


async def _launched(
    session: AsyncSession, tenant: Tenant, hr_user: User, **overrides
) -> tuple[AppraisalCycle, dict]:
    cycle = await _cycle(session, tenant, **overrides)
    summary = await appraisal_service.launch_cycle(session, hr_user, cycle_id=cycle.id)
    await session.commit()
    return cycle, summary


async def _appraisal_for(
    session: AsyncSession, cycle: AppraisalCycle, employee: Employee
) -> Appraisal:
    appraisal = await session.scalar(
        select(Appraisal).where(
            Appraisal.cycle_id == cycle.id, Appraisal.employee_id == employee.id
        )
    )
    assert appraisal is not None
    return appraisal


async def _subject(session: AsyncSession, appraisal: Appraisal) -> User:
    """The login of the person the appraisal is about, created on first use.

    The self review and the acknowledgement are theirs alone, so the tests act
    as them for those steps.
    """
    employee = await session.get(Employee, appraisal.employee_id)
    assert employee is not None
    found = await session.scalar(select(User).where(User.employee_id == employee.id))
    if found is not None:
        return found
    person = User(
        tenant_id=employee.tenant_id,
        email=f"{employee.employee_code.lower()}@people.acme.test",
        password_hash="not-a-real-hash",
        full_name=employee.full_name,
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


async def _advance(session: AsyncSession, hr_user: User, *, appraisal_id, **kwargs):
    """Submit the current step as whoever owns it: the person for the self
    review, HR (stepping in for the line) for the rest."""
    appraisal = await session.get(Appraisal, appraisal_id)
    assert appraisal is not None
    stage = await session.get(AppraisalStage, appraisal.current_stage_id)
    actor = await _subject(session, appraisal) if stage.role is StageRole.SELF else hr_user
    return await appraisal_service.submit_stage(session, actor, appraisal_id=appraisal_id, **kwargs)


# ═══════════════════════════════════════════════════════════════════════════
# Launch and eligibility
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_launch_creates_an_appraisal_for_every_eligible_employee(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    cycle, summary = await _launched(session, tenant, hr_user)

    assert summary["included"] == 1
    assert cycle.status is CycleStatus.ACTIVE

    appraisal = await _appraisal_for(session, cycle, employee)
    stages = await _stages(session, cycle)
    assert appraisal.current_stage_id == stages[0].id
    assert appraisal.status is AppraisalStatus.NOT_STARTED


async def test_exclusions_are_reported_with_a_reason(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """ "Why is Anita not in this cycle" is asked on every launch."""
    employee.grade = "L3"
    await session.commit()

    _, summary = await _launched(
        session, tenant, hr_user, eligibility_rule={"grades": ["L5", "L6"]}
    )
    assert summary["included"] == 0
    assert summary["excluded"][0]["reason"].startswith("Grade 'L3' is not in scope")


async def test_an_exited_employee_is_excluded(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    employee.status = EmploymentStatus.EXITED
    await session.commit()

    _, summary = await _launched(session, tenant, hr_user)
    assert summary["included"] == 0
    assert "exited" in summary["excluded"][0]["reason"]


async def test_too_little_service_excludes_with_the_numbers(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    employee.date_of_joining = date(2027, 2, 1)
    await session.commit()

    _, summary = await _launched(
        session, tenant, hr_user, eligibility_rule={"min_service_months": 6}
    )
    assert summary["included"] == 0
    assert "month(s) of service against the 6" in summary["excluded"][0]["reason"]


async def test_an_employee_who_is_their_own_manager_is_flagged_at_launch(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate, surfaced when HR can still fix it.

    An appraisal chain with the subject as their own manager has no
    independent view in it.
    """
    employee.manager_id = employee.id
    await session.commit()

    _, summary = await _launched(session, tenant, hr_user)
    assert summary["conflicts"]
    assert summary["conflicts"][0]["employee_code"] == employee.employee_code
    assert "no independent view" in summary["conflicts"][0]["reason"]


async def test_a_launched_cycle_is_not_relaunched(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    cycle, _ = await _launched(session, tenant, hr_user)
    with pytest.raises(HTTPException) as exc:
        await appraisal_service.launch_cycle(session, hr_user, cycle_id=cycle.id)
    assert exc.value.status_code == 409


async def test_a_cycle_with_no_stages_cannot_launch(
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
        await appraisal_service.launch_cycle(session, hr_user, cycle_id=cycle.id)
    assert "no stages" in exc.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# Progression
# ═══════════════════════════════════════════════════════════════════════════


async def test_stages_advance_in_sequence_and_skip_the_optional_one(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    cycle, _ = await _launched(session, tenant, hr_user)
    appraisal = await _appraisal_for(session, cycle, employee)
    stages = await _stages(session, cycle)

    await _advance(
        session,
        hr_user,
        appraisal_id=appraisal.id,
        ratings={"overall": 4},
    )
    await session.commit()
    assert appraisal.current_stage_id == stages[1].id

    await _advance(
        session,
        hr_user,
        appraisal_id=appraisal.id,
        ratings={"overall": 4},
    )
    await session.commit()
    assert appraisal.current_stage_id == stages[3].id, "the optional reviewer is skipped"


async def test_the_chain_completes_after_the_last_stage(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    cycle, _ = await _launched(session, tenant, hr_user)
    appraisal = await _appraisal_for(session, cycle, employee)

    for _ in range(3):
        await _advance(
            session, hr_user, appraisal_id=appraisal.id, ratings={"overall": 4}, score=4.0
        )
        await session.commit()

    assert appraisal.status is AppraisalStatus.COMPLETED
    assert appraisal.current_stage_id is None
    assert appraisal.completed_at is not None


# ═══════════════════════════════════════════════════════════════════════════
# Returns — the gate
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_return_moves_back_and_leaves_both_entries_in_the_history(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate, end to end.

    An appraisal that reads as though it went straight through when it went
    back twice is not a record of what happened.
    """
    cycle, _ = await _launched(session, tenant, hr_user)
    appraisal = await _appraisal_for(session, cycle, employee)
    stages = await _stages(session, cycle)

    await _advance(
        session,
        hr_user,
        appraisal_id=appraisal.id,
        ratings={"overall": 3},
    )
    await session.commit()

    await appraisal_service.return_stage(
        session,
        hr_user,
        appraisal_id=appraisal.id,
        target_stage_id=stages[0].id,
        reason="Please add specific examples against the delivery goal.",
    )
    await session.commit()

    assert appraisal.current_stage_id == stages[0].id
    assert appraisal.status is AppraisalStatus.RETURNED

    entries = (
        await session.scalars(
            select(AppraisalStageEntry)
            .where(AppraisalStageEntry.appraisal_id == appraisal.id)
            .order_by(AppraisalStageEntry.created_at)
        )
    ).all()
    assert len(entries) == 2, "the original submission and the return both survive"

    returned = entries[-1]
    assert returned.returned_to_stage_id == stages[0].id
    assert "specific examples" in returned.returned_reason


async def test_a_return_without_a_reason_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    cycle, _ = await _launched(session, tenant, hr_user)
    appraisal = await _appraisal_for(session, cycle, employee)
    stages = await _stages(session, cycle)

    await _advance(
        session,
        hr_user,
        appraisal_id=appraisal.id,
        ratings={},
    )
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await appraisal_service.return_stage(
            session,
            hr_user,
            appraisal_id=appraisal.id,
            target_stage_id=stages[0].id,
            reason="  ",
        )
    assert "needs a reason" in exc.value.detail


async def test_returning_forwards_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    cycle, _ = await _launched(session, tenant, hr_user)
    appraisal = await _appraisal_for(session, cycle, employee)
    stages = await _stages(session, cycle)
    await _advance(session, hr_user, appraisal_id=appraisal.id, ratings={})
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await appraisal_service.return_stage(
            session,
            hr_user,
            appraisal_id=appraisal.id,
            target_stage_id=stages[2].id,
            reason="Skip ahead.",
        )
    assert "goes backwards" in exc.value.detail


async def test_the_person_cannot_send_their_own_review_back(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    cycle, _ = await _launched(session, tenant, hr_user)
    appraisal = await _appraisal_for(session, cycle, employee)
    stages = await _stages(session, cycle)
    await _advance(session, hr_user, appraisal_id=appraisal.id, ratings={})
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await appraisal_service.return_stage(
            session,
            await _subject(session, appraisal),
            appraisal_id=appraisal.id,
            target_stage_id=stages[0].id,
            reason="I want to redo it.",
        )
    assert exc.value.status_code == 403


async def test_hr_cannot_write_somebodys_self_review(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    cycle, _ = await _launched(session, tenant, hr_user)
    appraisal = await _appraisal_for(session, cycle, employee)
    with pytest.raises(HTTPException) as exc:
        await appraisal_service.submit_stage(
            session, hr_user, appraisal_id=appraisal.id, ratings={}
        )
    assert exc.value.status_code == 403


async def test_hr_cannot_acknowledge_for_somebody(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    _, appraisal = await _completed(session, tenant, hr_user, employee)
    await appraisal_service.release(session, hr_user, appraisal_id=appraisal.id)
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await appraisal_service.acknowledge(
            session,
            hr_user,
            appraisal_id=appraisal.id,
            acknowledgement=AcknowledgementType.AGREED,
        )
    assert exc.value.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# Delegation
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_delegated_entry_names_both_the_delegate_and_the_original(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate.

    "Why did someone outside my reporting line rate me" has to be answerable:
    the delegate is the actor, the person who delegated is on the entry.
    """
    cover = User(
        tenant_id=tenant.id,
        email="cover@acme.test",
        password_hash="not-a-real-hash",
        full_name="Priya Cover",
        role=UserRole.MANAGER,
        is_active=True,
    )
    session.add(cover)
    await session.flush()

    cycle, _ = await _launched(session, tenant, hr_user)
    session.add(
        StageDelegation(
            tenant_id=tenant.id,
            cycle_id=cycle.id,
            from_user_id=hr_user.id,
            to_user_id=cover.id,
            effective_from=date.today(),
            effective_to=date(2099, 12, 31),
            reason="On leave for three weeks.",
        )
    )
    await session.commit()

    appraisal = await _appraisal_for(session, cycle, employee)
    await _advance(session, hr_user, appraisal_id=appraisal.id, ratings={})
    await session.commit()
    await appraisal_service.submit_stage(
        session,
        cover,
        appraisal_id=appraisal.id,
        ratings={"overall": 4},
    )
    await session.commit()

    stages = await _stages(session, cycle)
    entry = await session.scalar(
        select(AppraisalStageEntry).where(
            AppraisalStageEntry.appraisal_id == appraisal.id,
            AppraisalStageEntry.stage_id == stages[1].id,
        )
    )
    assert entry is not None
    assert entry.actor_user_id == cover.id
    assert entry.delegated_from_user_id == hr_user.id, "the original owner is recorded"
    assert entry.is_escalated is False


async def test_a_manager_without_a_delegation_cannot_act(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    stranger = User(
        tenant_id=tenant.id,
        email="stranger@acme.test",
        password_hash="not-a-real-hash",
        full_name="Not The Manager",
        role=UserRole.MANAGER,
        is_active=True,
    )
    session.add(stranger)
    await session.commit()
    cycle, _ = await _launched(session, tenant, hr_user)
    appraisal = await _appraisal_for(session, cycle, employee)
    await _advance(session, hr_user, appraisal_id=appraisal.id, ratings={})
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await appraisal_service.submit_stage(
            session, stranger, appraisal_id=appraisal.id, ratings={}
        )
    assert exc.value.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# Release, acknowledgement, objection
# ═══════════════════════════════════════════════════════════════════════════


async def _completed(session, tenant, hr_user, employee):
    cycle, _ = await _launched(session, tenant, hr_user)
    appraisal = await _appraisal_for(session, cycle, employee)
    for _ in range(3):
        await _advance(
            session, hr_user, appraisal_id=appraisal.id, ratings={"overall": 4}, score=4.0
        )
    await session.commit()
    return cycle, appraisal


async def test_an_employee_closes_the_cycle_while_disagreeing(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate.

    Forcing agreement as the only way to close is how an appraisal dispute
    becomes an employment dispute.
    """
    _, appraisal = await _completed(session, tenant, hr_user, employee)
    await appraisal_service.release(
        session, hr_user, appraisal_id=appraisal.id, final_rating="Meets expectations"
    )
    await session.commit()

    result = await appraisal_service.acknowledge(
        session,
        await _subject(session, appraisal),
        appraisal_id=appraisal.id,
        acknowledgement=AcknowledgementType.DISAGREED,
        objection="The delivery goal was reassigned in October and is not mine.",
    )
    await session.commit()

    assert result.status is AppraisalStatus.ACKNOWLEDGED, "the cycle still closes"
    assert result.acknowledgement_type is AcknowledgementType.DISAGREED
    assert "reassigned in October" in result.objection
    assert result.objection_routed_at is not None, "and it routes to HR"


async def test_disagreeing_without_an_objection_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """Without the written account there is nothing for HR to look at."""
    _, appraisal = await _completed(session, tenant, hr_user, employee)
    await appraisal_service.release(session, hr_user, appraisal_id=appraisal.id)
    await session.commit()

    with pytest.raises(HTTPException) as exc:
        await appraisal_service.acknowledge(
            session,
            await _subject(session, appraisal),
            appraisal_id=appraisal.id,
            acknowledgement=AcknowledgementType.DISAGREED,
        )
    assert "written objection" in exc.value.detail


async def test_agreeing_needs_no_objection(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    _, appraisal = await _completed(session, tenant, hr_user, employee)
    await appraisal_service.release(session, hr_user, appraisal_id=appraisal.id)
    result = await appraisal_service.acknowledge(
        session,
        await _subject(session, appraisal),
        appraisal_id=appraisal.id,
        acknowledgement=AcknowledgementType.AGREED,
    )
    await session.commit()
    assert result.status is AppraisalStatus.ACKNOWLEDGED
    assert result.objection is None


async def test_an_unreleased_appraisal_cannot_be_acknowledged(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    _, appraisal = await _completed(session, tenant, hr_user, employee)
    with pytest.raises(HTTPException) as exc:
        await appraisal_service.acknowledge(
            session,
            await _subject(session, appraisal),
            appraisal_id=appraisal.id,
            acknowledgement=AcknowledgementType.AGREED,
        )
    assert "nothing released to acknowledge" in exc.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# Reopening
# ═══════════════════════════════════════════════════════════════════════════


async def test_reopening_with_downstream_impact_warns_before_proceeding(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """The checklist gate.

    Reopening does not unwind an increment already granted on the old rating —
    that has to be revisited separately, and somebody has to know it exists.
    """
    _, appraisal = await _completed(session, tenant, hr_user, employee)
    impact = [
        {
            "type": "increment",
            "reference": "INC-2027-0412",
            "detail": "8% increment granted on a rating of 'Exceeds expectations'.",
        }
    ]

    with pytest.raises(HTTPException) as exc:
        await appraisal_service.reopen(
            session,
            hr_user,
            appraisal_id=appraisal.id,
            reason="The reviewer stage was skipped in error.",
            downstream_impact=impact,
        )
    assert exc.value.status_code == 409
    assert "does not unwind them" in exc.value.detail["message"]
    assert exc.value.detail["downstream_impact"] == impact

    reopened = await appraisal_service.reopen(
        session,
        hr_user,
        appraisal_id=appraisal.id,
        reason="The reviewer stage was skipped in error.",
        downstream_impact=impact,
        acknowledge_impact=True,
    )
    await session.commit()

    assert reopened.status is AppraisalStatus.IN_PROGRESS
    assert reopened.reopened_count == 1
    assert reopened.downstream_impact == impact


async def test_a_second_reopen_does_not_erase_the_first_impact(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    _, appraisal = await _completed(session, tenant, hr_user, employee)
    await appraisal_service.reopen(
        session,
        hr_user,
        appraisal_id=appraisal.id,
        reason="First.",
        downstream_impact=[{"type": "increment", "reference": "A"}],
        acknowledge_impact=True,
    )
    await session.commit()

    for _ in range(3):
        await _advance(session, hr_user, appraisal_id=appraisal.id, ratings={}, score=3.0)
    await session.commit()

    await appraisal_service.reopen(
        session,
        hr_user,
        appraisal_id=appraisal.id,
        reason="Second.",
        downstream_impact=[{"type": "bonus", "reference": "B"}],
        acknowledge_impact=True,
    )
    await session.commit()

    references = {item["reference"] for item in appraisal.downstream_impact}
    assert references == {"A", "B"}
    assert appraisal.reopened_count == 2


async def test_reopening_without_a_reason_is_refused(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    _, appraisal = await _completed(session, tenant, hr_user, employee)
    with pytest.raises(HTTPException) as exc:
        await appraisal_service.reopen(session, hr_user, appraisal_id=appraisal.id, reason="  ")
    assert "needs a reason" in exc.value.detail


# ═══════════════════════════════════════════════════════════════════════════
# The dashboard
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_progress_dashboard_is_accurate_under_partial_completion(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    legal_entity: LegalEntity,
) -> None:
    """The checklist gate.

    Without an accurate dashboard HR chases everyone instead of the few people
    who are late.
    """
    for index in range(4):
        session.add(
            Employee(
                tenant_id=tenant.id,
                employee_code=f"REV{index:03d}",
                first_name="Person",
                last_name=str(index),
                work_email=f"rev{index}@acme.test",
                legal_entity_id=legal_entity.id,
            )
        )
    await session.commit()

    cycle, summary = await _launched(session, tenant, hr_user)
    assert summary["included"] == 5

    appraisals = (
        await session.scalars(select(Appraisal).where(Appraisal.cycle_id == cycle.id))
    ).all()

    # Two move one stage on; one completes entirely.
    for appraisal in appraisals[:2]:
        await _advance(
            session,
            hr_user,
            appraisal_id=appraisal.id,
            ratings={},
        )
    for _ in range(3):
        await _advance(session, hr_user, appraisal_id=appraisals[2].id, ratings={}, score=4.0)
    await session.commit()

    progress = await appraisal_service.cycle_progress(session, hr_user, cycle.id)

    assert progress["total"] == 5
    assert progress["completed"] == 1
    assert progress["completion_percent"] == 20.0
    assert progress["by_status"]["not_started"] == 2
    assert progress["by_status"]["in_progress"] == 2
    assert progress["pending_by_stage"]["Self appraisal"] == 2
    assert progress["pending_by_stage"]["Manager review"] == 2


async def test_overdue_stages_are_flagged_on_the_dashboard(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    cycle, _ = await _launched(session, tenant, hr_user)
    stages = await _stages(session, cycle)
    stages[0].due_date = date(2020, 1, 1)
    await session.commit()

    progress = await appraisal_service.cycle_progress(session, hr_user, cycle.id)
    assert progress["overdue_by_stage"]["Self appraisal"] == 1


async def test_appraisals_are_tenant_isolated(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    other_hr_user: User,
    employee: Employee,
) -> None:
    cycle, _ = await _launched(session, tenant, hr_user)
    with pytest.raises(HTTPException) as exc:
        await appraisal_service.cycle_progress(session, other_hr_user, cycle.id)
    assert exc.value.status_code == 404
