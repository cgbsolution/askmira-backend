"""The DB-facing half of the PIP and increment flow.

The trail assessment, the band arithmetic, the budget maths, the equity
comparison and the promotion plan all live in the pure modules beside this. An
invariant checks the isolation.

═══════════════════════════════════════════════════════════════════════════════
`apply_promotion` DRIVES SLICE 24, IT DOES NOT REIMPLEMENT IT
═══════════════════════════════════════════════════════════════════════════════

The atomic chain a promotion needs - employee record, salary revision, position
assignment, letter, applied together or not at all - **already exists**, in
`services.lifecycle.effect_event`, wrapped in a savepoint and tested by slice
24. Building a second one here would give the product two promotion paths, and
the one that drifts is always the newer one.

What was missing was the front of it: refusing before anything starts, raising
the event from an *approved increment recommendation*, and recording which
recommendation produced which event so a half-applied promotion is attributable
to a decision rather than merely visible.

═══════════════════════════════════════════════════════════════════════════════
THE EQUITY SNAPSHOT IS STORED, NOT RECOMPUTED
═══════════════════════════════════════════════════════════════════════════════

The comparison group changes as people join and leave. The useful record is what
the manager was actually shown at the moment they chose the number, so it is
written onto the recommendation.

**What is and is not wired**, stated once here:

- The cascade creates the salary revision and **payroll is not re-run**; arrears
  are flagged with their window and the arrears engine from slice 12 computes
  them when a run happens.
- **No PDF is rendered here** and nothing emails it; the letter is whatever the
  cascade issues.
- An unsuccessful PIP writes the exit linkage; **it does not start an exit
  workflow** by itself.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.performance_flow import (
    EmployeeInputStateEnum,
    IncrementCycle,
    IncrementCycleStatusEnum,
    IncrementMatrixRow,
    IncrementRecommendation,
    MilestoneStatusEnum,
    OverageModeEnum,
    Pip,
    PipCheckIn,
    PipMilestone,
    PipStatusEnum,
    PromotionRecord,
    RecommendationStatusEnum,
)
from app.models.user import User
from app.services import audit
from app.services.performance import increments as inc_engine
from app.services.performance import pip as pip_engine
from app.services.performance import promotion as promo_engine

__all__ = [
    "apply_promotion",
    "budget_view",
    "close_pip",
    "create_increment_cycle",
    "decide_recommendation",
    "evidence_pack",
    "open_pip",
    "performance_posture",
    "recommend_increment",
    "record_check_in",
    "release_letters",
]


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _d(value: Any) -> Decimal:
    return Decimal(str(value if value is not None else 0))


# ══════════════════════════════════════════════════════════════════════════════
# PIP
# ══════════════════════════════════════════════════════════════════════════════


async def open_pip(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    start_date: date,
    end_date: date,
    reason: str,
    expectations: str,
    support_provided: str,
    milestones: list[dict[str, Any]],
    appraisal_id: uuid.UUID | None = None,
    request: Request | None = None,
) -> Pip:
    """Open a plan, refusing the shapes that cannot be defended later.

    Every refusal here is a thing employment counsel would ask about first: no
    written expectations, no support recorded, no measurable milestone.
    """
    try:
        pip_engine.PipFacts(
            pip_id="pending",
            employee_id=str(employee_id),
            initiated_by=str(user.id),
            start_date=start_date,
            end_date=end_date,
            reason=reason,
            expectations=expectations,
            support_provided=support_provided,
            status=pip_engine.PipStatus.DRAFT,
        )
    except pip_engine.PipError as exc:
        raise _bad_request(str(exc)) from exc

    if not milestones:
        raise _bad_request(
            "A plan needs at least one measurable milestone. Without one there "
            "is nothing the employee could have met, and nothing they can be "
            "said to have failed."
        )

    row = Pip(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        initiated_by_user_id=user.id,
        appraisal_id=appraisal_id,
        start_date=start_date,
        end_date=end_date,
        reason=reason,
        expectations=expectations,
        support_provided=support_provided,
        status=PipStatusEnum.ACTIVE,
    )
    session.add(row)
    await session.flush()

    for spec in milestones:
        try:
            pip_engine.Milestone(
                milestone_id="pending",
                description=str(spec["description"]),
                target_date=spec["target_date"],
                measurable_criteria=str(spec.get("measurable_criteria", "")),
            )
        except pip_engine.PipError as exc:
            raise _bad_request(str(exc)) from exc
        session.add(
            PipMilestone(
                tenant_id=user.tenant_id,
                pip_id=row.id,
                description=str(spec["description"]),
                target_date=spec["target_date"],
                measurable_criteria=str(spec["measurable_criteria"]),
            )
        )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="pip",
        entity_id=row.id,
        actor=user,
        summary=(
            f"PIP opened {start_date.isoformat()} to {end_date.isoformat()} "
            f"with {len(milestones)} milestone(s)."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def record_check_in(
    session: AsyncSession,
    user: User,
    *,
    pip_id: uuid.UUID,
    scheduled_for: date,
    held_on: date,
    manager_comments: str,
    employee_input_state: EmployeeInputStateEnum,
    employee_comments: str = "",
    decline_reason: str = "",
    milestone_id: uuid.UUID | None = None,
    request: Request | None = None,
) -> PipCheckIn:
    """Record a conversation, with the employee's own account or a reasoned note.

    The engine refuses a check-in the employee was never asked about. A trail of
    manager observations with no employee response is one side of a conversation
    and its evidential value is close to zero.
    """
    plan = (
        await session.execute(
            tenant_scoped(select(Pip).where(Pip.id == pip_id), Pip, user)
        )
    ).scalar_one_or_none()
    if plan is None:
        raise _not_found("No such PIP in this tenant.")

    try:
        pip_engine.record_check_in(
            check_in_id="pending",
            scheduled_for=scheduled_for,
            held_on=held_on,
            manager_comments=manager_comments,
            employee_comments=employee_comments,
            employee_input_state=pip_engine.EmployeeInputState(
                employee_input_state.value
            ),
            decline_reason=decline_reason,
        )
    except pip_engine.PipError as exc:
        raise _bad_request(str(exc)) from exc

    row = PipCheckIn(
        tenant_id=user.tenant_id,
        pip_id=pip_id,
        milestone_id=milestone_id,
        scheduled_for=scheduled_for,
        held_on=held_on,
        manager_comments=manager_comments,
        employee_comments=employee_comments or None,
        employee_input_state=employee_input_state,
        decline_reason=decline_reason or None,
        recorded_by_user_id=user.id,
        employee_comment_at=(
            datetime.now(UTC)
            if employee_input_state is EmployeeInputStateEnum.GIVEN
            else None
        ),
    )
    session.add(row)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="pip_check_in",
        entity_id=row.id,
        actor=user,
        summary=(
            f"PIP check-in held {held_on.isoformat()}; employee input "
            f"{employee_input_state.value}."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def _engine_pip(
    session: AsyncSession, user: User, row: Pip
) -> pip_engine.PipFacts:
    milestones = list(
        (
            await session.execute(
                tenant_scoped(
                    select(PipMilestone).where(PipMilestone.pip_id == row.id),
                    PipMilestone,
                    user,
                )
            )
        ).scalars()
    )
    check_ins = list(
        (
            await session.execute(
                tenant_scoped(
                    select(PipCheckIn).where(PipCheckIn.pip_id == row.id),
                    PipCheckIn,
                    user,
                )
            )
        ).scalars()
    )
    return pip_engine.PipFacts(
        pip_id=str(row.id),
        employee_id=str(row.employee_id),
        initiated_by=str(row.initiated_by_user_id or ""),
        start_date=row.start_date,
        end_date=row.end_date,
        reason=row.reason,
        expectations=row.expectations,
        support_provided=row.support_provided,
        status=pip_engine.PipStatus(row.status.value),
        appraisal_id=str(row.appraisal_id) if row.appraisal_id else "",
        milestones=tuple(
            pip_engine.Milestone(
                milestone_id=str(m.id),
                description=m.description,
                target_date=m.target_date,
                measurable_criteria=m.measurable_criteria,
                met=(
                    None
                    if m.status is MilestoneStatusEnum.PENDING
                    else m.status is MilestoneStatusEnum.MET
                ),
            )
            for m in milestones
        ),
        check_ins=tuple(
            pip_engine.CheckIn(
                check_in_id=str(c.id),
                scheduled_for=c.scheduled_for,
                held_on=c.held_on,
                manager_comments=c.manager_comments or "",
                employee_comments=c.employee_comments or "",
                employee_input_state=pip_engine.EmployeeInputState(
                    c.employee_input_state.value
                ),
                decline_reason=c.decline_reason or "",
                milestone_id=str(c.milestone_id) if c.milestone_id else "",
            )
            for c in check_ins
        ),
    )


async def close_pip(
    session: AsyncSession,
    user: User,
    *,
    pip_id: uuid.UUID,
    outcome: str,
    today: date,
    reason: str = "",
    new_end_date: date | None = None,
    acknowledge_weak_trail: bool = False,
    request: Request | None = None,
) -> dict[str, Any]:
    """Record an outcome, refusing an exit route on an unevidenced plan.

    The gaps as they stood are frozen onto the row. Recomputing them later would
    describe a plan that has since been edited, and the useful record is what
    the trail looked like when somebody decided.
    """
    row = (
        await session.execute(
            tenant_scoped(select(Pip).where(Pip.id == pip_id), Pip, user)
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found("No such PIP in this tenant.")

    facts = await _engine_pip(session, user, row)
    try:
        result = pip_engine.close_pip(
            facts,
            outcome=pip_engine.PipOutcome(outcome),
            today=today,
            reason=reason,
            new_end_date=new_end_date,
            acknowledge_weak_trail=acknowledge_weak_trail,
        )
    except pip_engine.PipError as exc:
        raise _bad_request(str(exc)) from exc
    except ValueError as exc:
        raise _bad_request(f"{outcome!r} is not a PIP outcome.") from exc

    row.status = PipStatusEnum(result.new_status.value)
    row.outcome_reason = reason or None
    row.outcome_recorded_on = today
    row.trail_gaps_at_outcome = list(result.trail.gaps)
    row.weak_trail_acknowledged = (
        acknowledge_weak_trail and not result.trail.is_defensible
    )
    if result.new_end_date is not None:
        row.extensions = [
            *list(row.extensions or []),
            {
                "from": row.end_date.isoformat(),
                "to": result.new_end_date.isoformat(),
                "reason": reason,
                "at": today.isoformat(),
            },
        ]
        row.end_date = result.new_end_date

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="pip",
        entity_id=row.id,
        actor=user,
        summary=f"PIP {result.new_status.value}: {reason}".strip(),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return result.as_dict()


async def evidence_pack(
    session: AsyncSession, user: User, *, pip_id: uuid.UUID, today: date
) -> dict[str, Any]:
    """Everything an employment adviser would ask for, in one payload."""
    row = (
        await session.execute(
            tenant_scoped(select(Pip).where(Pip.id == pip_id), Pip, user)
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found("No such PIP in this tenant.")
    pack = pip_engine.evidence_pack(
        await _engine_pip(session, user, row), today=today
    )
    pack["weak_trail_acknowledged"] = row.weak_trail_acknowledged
    pack["trail_gaps_at_outcome"] = list(row.trail_gaps_at_outcome or [])
    return pack


# ══════════════════════════════════════════════════════════════════════════════
# Increments
# ══════════════════════════════════════════════════════════════════════════════


async def create_increment_cycle(
    session: AsyncSession,
    user: User,
    *,
    name: str,
    effective_from: date,
    budget_total: Decimal,
    bands: list[dict[str, Any]],
    budget_by_department: dict[str, Any] | None = None,
    overage_mode: OverageModeEnum = OverageModeEnum.ESCALATE,
    letter_release_on: date | None = None,
    linked_appraisal_cycle_id: uuid.UUID | None = None,
    request: Request | None = None,
) -> IncrementCycle:
    if not bands:
        raise _bad_request(
            "A cycle needs an increment matrix. Without one every rating "
            "resolves to no band and the manager has no anchor at all."
        )

    row = IncrementCycle(
        tenant_id=user.tenant_id,
        name=name,
        effective_from=effective_from,
        budget_total=_d(budget_total),
        budget_by_department={
            str(k): str(v) for k, v in (budget_by_department or {}).items()
        },
        status=IncrementCycleStatusEnum.OPEN,
        overage_mode=overage_mode,
        letter_release_on=letter_release_on,
        linked_appraisal_cycle_id=linked_appraisal_cycle_id,
    )
    session.add(row)
    await session.flush()

    for spec in bands:
        try:
            inc_engine.IncrementBand(
                rating_label=str(spec["rating_label"]),
                min_percentage=_d(spec["min_percentage"]),
                max_percentage=_d(spec["max_percentage"]),
                recommended_percentage=_d(spec["recommended_percentage"]),
            )
        except inc_engine.IncrementError as exc:
            raise _bad_request(str(exc)) from exc
        session.add(
            IncrementMatrixRow(
                tenant_id=user.tenant_id,
                cycle_id=row.id,
                rating_label=str(spec["rating_label"]),
                min_percentage=_d(spec["min_percentage"]),
                max_percentage=_d(spec["max_percentage"]),
                recommended_percentage=_d(spec["recommended_percentage"]),
            )
        )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="increment_cycle",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Increment cycle {name!r} opened with {len(bands)} band(s), budget "
            f"{budget_total}, letters releasing "
            f"{letter_release_on.isoformat() if letter_release_on else 'unset'}."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def _bands(
    session: AsyncSession, user: User, cycle_id: uuid.UUID
) -> list[inc_engine.IncrementBand]:
    rows = list(
        (
            await session.execute(
                tenant_scoped(
                    select(IncrementMatrixRow).where(
                        IncrementMatrixRow.cycle_id == cycle_id
                    ),
                    IncrementMatrixRow,
                    user,
                )
            )
        ).scalars()
    )
    return [
        inc_engine.IncrementBand(
            rating_label=r.rating_label,
            min_percentage=_d(r.min_percentage),
            max_percentage=_d(r.max_percentage),
            recommended_percentage=_d(r.recommended_percentage),
        )
        for r in rows
    ]


async def _consumed(
    session: AsyncSession,
    user: User,
    cycle_id: uuid.UUID,
    *,
    department_id: uuid.UUID | None,
    exclude_employee_id: uuid.UUID | None = None,
) -> tuple[Decimal, Decimal]:
    """What this cycle has already committed, in total and in one department.

    Cancelled and rejected recommendations do not consume budget: a rejected
    proposal is not spend, and counting it would make a department appear full
    of money it never allocated.
    """
    stmt = tenant_scoped(
        select(IncrementRecommendation)
        .where(IncrementRecommendation.cycle_id == cycle_id)
        .where(
            IncrementRecommendation.status != RecommendationStatusEnum.REJECTED
        ),
        IncrementRecommendation,
        user,
    )
    rows = list((await session.execute(stmt)).scalars())
    if exclude_employee_id is not None:
        rows = [r for r in rows if r.employee_id != exclude_employee_id]
    total = sum((_d(r.increase_amount) for r in rows), Decimal("0"))
    dept = sum(
        (
            _d(r.increase_amount)
            for r in rows
            if department_id is not None and r.department_id == department_id
        ),
        Decimal("0"),
    )
    return total, dept


async def budget_view(
    session: AsyncSession,
    user: User,
    *,
    cycle_id: uuid.UUID,
    department_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """What is left, for a manager who is still allocating.

    Called before a recommendation is entered rather than after it is rejected.
    A manager who allocates twelve increments blind and is told the department
    is 8% over redoes all twelve at 11pm, and the second pass is a proportional
    haircut rather than a re-think.
    """
    cycle = (
        await session.execute(
            tenant_scoped(
                select(IncrementCycle).where(IncrementCycle.id == cycle_id),
                IncrementCycle,
                user,
            )
        )
    ).scalar_one_or_none()
    if cycle is None:
        raise _not_found("No such increment cycle in this tenant.")

    total_used, dept_used = await _consumed(
        session, user, cycle_id, department_id=department_id
    )
    dept_budget = _d(
        (cycle.budget_by_department or {}).get(str(department_id), 0)
        if department_id
        else 0
    )
    return inc_engine.headroom(
        department_allocated=dept_budget,
        department_consumed=dept_used,
        total_allocated=_d(cycle.budget_total),
        total_consumed=total_used,
        department_key=str(department_id or "unassigned"),
        mode=inc_engine.OverageMode(cycle.overage_mode.value),
    )


async def recommend_increment(
    session: AsyncSession,
    user: User,
    *,
    cycle_id: uuid.UUID,
    employee_id: uuid.UUID,
    rating_label: str,
    current_ctc: Decimal,
    percentage: Decimal,
    justification: str | None = None,
    promotion_to_grade: str | None = None,
    band_midpoint: Decimal | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Record a recommendation, with the budget and the equity view in the reply.

    Both are returned rather than left to a second call: the manager is choosing
    the number now, and a parity comparison that arrives in a report next March
    cannot change it.
    """
    cycle = (
        await session.execute(
            tenant_scoped(
                select(IncrementCycle).where(IncrementCycle.id == cycle_id),
                IncrementCycle,
                user,
            )
        )
    ).scalar_one_or_none()
    if cycle is None:
        raise _not_found("No such increment cycle in this tenant.")
    if cycle.status not in (
        IncrementCycleStatusEnum.OPEN,
        IncrementCycleStatusEnum.APPROVALS,
    ):
        raise _bad_request(f"This cycle is {cycle.status.value}.")

    employee = (
        await session.execute(
            tenant_scoped(
                select(Employee).where(Employee.id == employee_id), Employee, user
            )
        )
    ).scalar_one_or_none()
    if employee is None:
        raise _not_found("No such employee in this tenant.")

    try:
        proposal = inc_engine.recommend(
            employee_id=str(employee_id),
            rating_label=rating_label,
            current_ctc=_d(current_ctc),
            percentage=_d(percentage),
            bands=await _bands(session, user, cycle_id),
            justification=justification or "",
        )
    except inc_engine.IncrementError as exc:
        raise _bad_request(str(exc)) from exc

    if proposal.requires_justification:
        raise _bad_request(proposal.message)

    increase = proposal.recommended_ctc - proposal.current_ctc
    total_used, dept_used = await _consumed(
        session,
        user,
        cycle_id,
        department_id=employee.department_id,
        exclude_employee_id=employee_id,
    )
    dept_budget = _d(
        (cycle.budget_by_department or {}).get(str(employee.department_id), 0)
    )
    budget = inc_engine.consume(
        increase=increase,
        department_allocated=dept_budget,
        department_consumed=dept_used,
        total_allocated=_d(cycle.budget_total),
        total_consumed=total_used,
        department_key=str(employee.department_id or "unassigned"),
        mode=inc_engine.OverageMode(cycle.overage_mode.value),
    )
    if not budget.permitted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=budget.message
        )

    peers = list(
        (
            await session.execute(
                tenant_scoped(
                    select(Employee)
                    .where(Employee.grade == employee.grade)
                    .where(Employee.location_id == employee.location_id),
                    Employee,
                    user,
                )
            )
        ).scalars()
    )
    peer_ctcs = {
        str(r.employee_id): _d(r.current_ctc)
        for r in (
            await session.execute(
                tenant_scoped(
                    select(IncrementRecommendation).where(
                        IncrementRecommendation.cycle_id == cycle_id
                    ),
                    IncrementRecommendation,
                    user,
                )
            )
        ).scalars()
    }
    equity = inc_engine.equity_snapshot(
        employee_id=str(employee_id),
        proposed_ctc=proposal.recommended_ctc,
        peers=[
            inc_engine.PeerFacts(
                employee_id=str(p.id),
                ctc=peer_ctcs.get(str(p.id), Decimal("0")),
                grade=p.grade or "",
                role=p.designation or "",
                location=str(p.location_id or ""),
                gender=(p.gender.value if p.gender else ""),
            )
            for p in peers
            if peer_ctcs.get(str(p.id), Decimal("0")) > 0
        ],
        employee_gender=(employee.gender.value if employee.gender else ""),
        band_midpoint=band_midpoint,
    )

    existing = (
        await session.execute(
            tenant_scoped(
                select(IncrementRecommendation)
                .where(IncrementRecommendation.cycle_id == cycle_id)
                .where(IncrementRecommendation.employee_id == employee_id),
                IncrementRecommendation,
                user,
            )
        )
    ).scalar_one_or_none()
    row = existing or IncrementRecommendation(
        tenant_id=user.tenant_id,
        cycle_id=cycle_id,
        employee_id=employee_id,
    )
    row.department_id = employee.department_id
    row.current_ctc = proposal.current_ctc
    row.rating_label = rating_label
    row.recommended_percentage = proposal.percentage
    row.recommended_ctc = proposal.recommended_ctc
    row.increase_amount = increase
    row.within_band = proposal.within_band
    row.justification = justification
    row.promotion_to_grade = promotion_to_grade
    row.requires_escalation = budget.requires_escalation
    row.overage_amount = budget.overage
    row.equity_snapshot = equity
    row.status = (
        RecommendationStatusEnum.ESCALATED
        if budget.requires_escalation
        else RecommendationStatusEnum.SUBMITTED
    )
    if existing is None:
        session.add(row)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="increment_recommendation",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Increment recommended: {proposal.percentage}% "
            f"({proposal.current_ctc} to {proposal.recommended_ctc})"
            + (f", promotion to {promotion_to_grade}" if promotion_to_grade else "")
            + "."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)

    return {
        "recommendation_id": str(row.id),
        "proposal": proposal.as_dict(),
        "budget": budget.as_dict(),
        "equity": equity,
        "status": row.status.value,
    }


async def decide_recommendation(
    session: AsyncSession,
    user: User,
    *,
    recommendation_id: uuid.UUID,
    approve: bool,
    note: str | None = None,
    request: Request | None = None,
) -> IncrementRecommendation:
    row = (
        await session.execute(
            tenant_scoped(
                select(IncrementRecommendation).where(
                    IncrementRecommendation.id == recommendation_id
                ),
                IncrementRecommendation,
                user,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found("No such recommendation in this tenant.")
    if row.status not in (
        RecommendationStatusEnum.SUBMITTED,
        RecommendationStatusEnum.ESCALATED,
    ):
        raise _bad_request(f"This recommendation is {row.status.value}.")

    row.status = (
        RecommendationStatusEnum.APPROVED
        if approve
        else RecommendationStatusEnum.REJECTED
    )
    row.approvals = [
        *list(row.approvals or []),
        {
            "user_id": str(user.id),
            "decision": "approved" if approve else "rejected",
            "at": datetime.now(UTC).isoformat(),
            "note": note or "",
        },
    ]
    row.decided_by_user_id = user.id
    row.decided_at = datetime.now(UTC)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="increment_recommendation",
        entity_id=row.id,
        actor=user,
        summary=f"Increment {row.status.value}: {note or ''}".strip(),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


# ══════════════════════════════════════════════════════════════════════════════
# Promotion and release
# ══════════════════════════════════════════════════════════════════════════════


async def apply_promotion(
    session: AsyncSession,
    user: User,
    *,
    recommendation_id: uuid.UUID,
    to_position_id: uuid.UUID | None,
    effective_from: date,
    today: date,
    request: Request | None = None,
) -> dict[str, Any]:
    """Drive the lifecycle cascade from an approved recommendation.

    The atomic chain - employee record, salary revision, position assignment,
    letter - **already exists**, in `services.lifecycle.effect_event`, wrapped
    in a savepoint and tested by slice 24. Building a second one here would give
    the product two promotion paths that drift apart, and the one that drifts is
    always the newer one.

    So this function's job is the part that was missing: refusing before
    anything starts, raising the event from the approved recommendation, and
    recording which recommendation produced which event so a half-applied
    promotion is attributable to a decision.

    A future-dated promotion is raised and approved and **not effected** - the
    due sweep effects it on the day. Effecting it early would change the record
    before the decision was due to apply.
    """
    from app.models.lifecycle import LifecycleEventType
    from app.services.lifecycle import service as lifecycle

    recommendation = (
        await session.execute(
            tenant_scoped(
                select(IncrementRecommendation).where(
                    IncrementRecommendation.id == recommendation_id
                ),
                IncrementRecommendation,
                user,
            )
        )
    ).scalar_one_or_none()
    if recommendation is None:
        raise _not_found("No such recommendation in this tenant.")
    if recommendation.status is not RecommendationStatusEnum.APPROVED:
        raise _bad_request(
            f"This recommendation is {recommendation.status.value}. A promotion "
            "applies an approved decision; applying an unapproved one puts the "
            "employee on a new grade nobody signed off."
        )
    if not recommendation.promotion_to_grade:
        raise _bad_request(
            "This recommendation has no target grade, so it is an increment "
            "rather than a promotion. Calling it a promotion would misstate the "
            "record and the headcount plan."
        )

    existing = (
        await session.execute(
            tenant_scoped(
                select(PromotionRecord).where(
                    PromotionRecord.recommendation_id == recommendation_id
                ),
                PromotionRecord,
                user,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise _bad_request(
            "This recommendation has already been applied as a promotion. "
            "Applying it twice would raise a second grade change and a second "
            "salary revision from one decision."
        )

    employee = (
        await session.execute(
            tenant_scoped(
                select(Employee).where(Employee.id == recommendation.employee_id),
                Employee,
                user,
            )
        )
    ).scalar_one_or_none()
    if employee is None:
        raise _not_found("No such employee in this tenant.")

    # The cascade revises salary through payroll's own path, which needs a
    # current salary assignment to revise. Without one the cascade raises and
    # rolls back cleanly - correct, but invisible: the manager gets a 409 about
    # a savepoint. Refusing here says what to do instead, which is this
    # module's own stated principle.
    from app.models.payroll import EmployeeSalary

    current_salary = (
        await session.execute(
            tenant_scoped(
                select(EmployeeSalary)
                .where(EmployeeSalary.employee_id == employee.id)
                .where(EmployeeSalary.effective_to.is_(None)),
                EmployeeSalary,
                user,
            )
        )
    ).scalars().first()
    if current_salary is None:
        raise _bad_request(
            "This employee has no current salary assignment, so a promotion has "
            "nothing to revise. Assign a salary structure first - the cascade "
            "would otherwise refuse halfway through and roll back, which is "
            "correct but tells the manager nothing they can act on."
        )

    try:
        plan = promo_engine.plan_promotion(
            employee_id=str(employee.id),
            from_grade=employee.grade or "",
            to_grade=recommendation.promotion_to_grade,
            from_position_id=str(employee.id),
            to_position_id=str(to_position_id or employee.id),
            current_ctc=_d(recommendation.current_ctc),
            new_ctc=_d(recommendation.recommended_ctc),
            effective_from=effective_from,
            today=today,
        )
    except promo_engine.PromotionError as exc:
        raise _bad_request(str(exc)) from exc

    new_values: dict[str, Any] = {"grade": plan.to_grade}
    if to_position_id is not None:
        new_values["position_id"] = str(to_position_id)

    event = await lifecycle.raise_event(
        session,
        user,
        employee_id=employee.id,
        event_type=LifecycleEventType.PROMOTION,
        effective_from=effective_from,
        new_values=new_values,
        reason=(
            f"Promotion from {plan.from_grade or 'ungraded'} to {plan.to_grade} "
            f"under increment cycle {recommendation.cycle_id}."
        ),
        new_ctc_annual=plan.new_ctc,
        today=today,
        request=request,
    )
    await lifecycle.approve_event(
        session,
        user,
        event_id=event.id,
        approve=True,
        note="Approved increment recommendation.",
        today=today,
        request=request,
    )

    effected: dict[str, Any] | None = None
    if effective_from <= today:
        effected = await lifecycle.effect_event(
            session, user, event_id=event.id, on=today, request=request
        )

    # Flush before recording the ids. The cascade creates the salary revision
    # without committing - it owns the transaction - and `PromotionRecord` holds
    # a plain foreign key to it rather than a relationship, so SQLAlchemy has no
    # dependency to order the inserts by. Without this the promotion record is
    # written first and the foreign key points at a row that does not exist yet.
    await session.flush()
    await session.refresh(event)
    record = PromotionRecord(
        tenant_id=user.tenant_id,
        employee_id=employee.id,
        recommendation_id=recommendation.id,
        from_grade=plan.from_grade or None,
        to_grade=plan.to_grade,
        to_position_id=to_position_id,
        current_ctc=plan.current_ctc,
        new_ctc=plan.new_ctc,
        effective_from=effective_from,
        employee_salary_id=event.salary_revision_id,
        lifecycle_event_id=event.id,
        issued_letter_id=event.letter_id,
        grade_applied=effected is not None,
        is_backdated=plan.is_backdated,
        arrears_from=plan.arrears_from,
        applied_at=datetime.now(UTC),
        applied_by_user_id=user.id,
    )
    session.add(record)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="promotion_record",
        entity_id=record.id,
        actor=user,
        summary=plan.message,
        request=request,
    )
    await session.commit()
    await session.refresh(record)

    return {
        "promotion_record_id": str(record.id),
        "lifecycle_event_id": str(event.id),
        "plan": plan.as_dict(),
        "arrears": promo_engine.arrears_window(
            effective_from=effective_from, today=today
        ),
        "effected": effected,
        "note": (
            "Applied through the lifecycle cascade, which is one savepoint: "
            "employee record, salary revision, position and letter together or "
            "none of them."
            if effected
            else f"Raised and approved, effective {effective_from.isoformat()}. "
            "It is **not** applied yet - the due sweep effects it on the day, "
            "because effecting it early would change the record before the "
            "decision was due to apply."
        ),
    }


async def release_letters(
    session: AsyncSession,
    user: User,
    *,
    cycle_id: uuid.UUID,
    today: date,
    force_partial: bool = False,
    request: Request | None = None,
) -> dict[str, Any]:
    """Release the cycle's letters, all together, on the controlled date.

    Refuses before the date and refuses while approvals are outstanding.
    Releasing the approved ones "for now" is exactly the piecemeal release the
    controlled date exists to prevent.
    """
    cycle = (
        await session.execute(
            tenant_scoped(
                select(IncrementCycle).where(IncrementCycle.id == cycle_id),
                IncrementCycle,
                user,
            )
        )
    ).scalar_one_or_none()
    if cycle is None:
        raise _not_found("No such increment cycle in this tenant.")
    if cycle.letter_release_on is None:
        raise _bad_request(
            "This cycle has no letter release date. Without one the letters go "
            "out as each approval lands, and the first person to hear is "
            "whoever has the fastest approver."
        )
    if cycle.letters_released_at is not None:
        raise _bad_request(
            "This cycle's letters have already been released. Releasing again "
            "would send a second copy to everybody who already has one."
        )

    rows = list(
        (
            await session.execute(
                tenant_scoped(
                    select(IncrementRecommendation).where(
                        IncrementRecommendation.cycle_id == cycle_id
                    ),
                    IncrementRecommendation,
                    user,
                )
            )
        ).scalars()
    )
    approved = [
        str(r.employee_id)
        for r in rows
        if r.status is RecommendationStatusEnum.APPROVED
    ]
    pending = [
        str(r.employee_id)
        for r in rows
        if r.status
        in (RecommendationStatusEnum.SUBMITTED, RecommendationStatusEnum.ESCALATED)
    ]

    decision = promo_engine.release_plan(
        release_on=cycle.letter_release_on,
        today=today,
        approved_employee_ids=approved,
        pending_employee_ids=pending,
        force_partial=force_partial,
    )
    if not decision.permitted:
        raise _bad_request(decision.message)

    cycle.letters_released_at = datetime.now(UTC)
    cycle.status = IncrementCycleStatusEnum.RELEASED
    cycle.partial_release_acknowledged = bool(pending)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="increment_cycle",
        entity_id=cycle.id,
        actor=user,
        summary=decision.message,
        request=request,
    )
    await session.commit()
    return decision.as_dict()


def performance_posture() -> dict[str, Any]:
    """What this slice enforces, and what it does not."""
    return {
        "enforced": [
            "A PIP needs written expectations, recorded support and at least "
            "one measurable milestone.",
            "A check-in needs the employee's own account, or a reasoned record "
            "that they declined. Never asking is refused.",
            "An unsuccessful outcome on an unevidenced plan is refused unless "
            "somebody explicitly acknowledges the gaps, which is recorded.",
            "The trail gaps are frozen onto the plan when the outcome is "
            "recorded, not recomputed later.",
            "A recommendation outside its rating band needs a written "
            "justification; it is not silently refused.",
            "Budget headroom is returned before a recommendation is entered and "
            "again with it, for both department and company.",
            "The equity comparison is captured at the moment the number is "
            "chosen and stored on the recommendation.",
            "A promotion is applied through the lifecycle cascade of slice "
            "24 - employee record, salary revision, position and letter in "
            "one savepoint - rather than through a second promotion path.",
            "A future-dated promotion is raised and approved and not "
            "effected; the due sweep applies it on the day.",
            "A recommendation cannot be applied as a promotion twice.",
            "A promotion is refused before it starts where the employee has "
            "no salary assignment to revise, rather than failing halfway "
            "through the cascade.",
            "Letters release together on the cycle's controlled date; a partial "
            "release is a deliberate act with the outstanding names recorded.",
        ],
        "not_built": [
            "Payroll is not re-run on approval. The lifecycle cascade "
            "creates the salary revision and arrears are flagged with their "
            "window for the next run to pick up.",
            "No PDF is rendered and nothing is emailed here. The letter is "
            "whatever the lifecycle cascade issues.",
            "An unsuccessful PIP records the exit linkage; it does not start an "
            "exit workflow by itself.",
            "No approval routing matrix. A recommendation has one decision, and "
            "escalation is a flag rather than a different approver chain.",
            "Comparison groups are grade and location only. Role, tenure and "
            "performance history are not part of the parity view, and a real "
            "pay-equity analysis would include them.",
            "Nothing schedules check-ins or reminds anybody they are due.",
        ],
    }
