"""Appraisal cycle persistence: launch, submit, return, release, reopen.

The progression rules live in `progression.py` and are exercised there. This
module is the database side of them, plus the four things that only make sense
against real records:

- **Bulk launch.** A cycle is launched for hundreds of people at once, and
  eligibility is evaluated per employee against the cycle's rule.
- **Release and acknowledgement**, including acknowledging with an objection.
- **Reopening after close**, with the downstream impact recorded at the time —
  after the fact nobody can reconstruct what the rating had already fed.
- **The completion dashboard**, which is what makes a cycle manageable at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.appraisal import (
    AcknowledgementType,
    Appraisal,
    AppraisalCycle,
    AppraisalStage,
    AppraisalStageEntry,
    AppraisalStatus,
    CycleStatus,
    StageDelegation,
    StageRole,
)
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.user import User
from app.services import audit
from app.services.appraisal import progression

__all__ = [
    "acknowledge",
    "cycle_progress",
    "launch_cycle",
    "release",
    "reopen",
    "return_stage",
    "submit_stage",
]


async def _cycle(session: AsyncSession, user: User, cycle_id: uuid.UUID) -> AppraisalCycle:
    cycle = await session.scalar(
        tenant_scoped(
            select(AppraisalCycle).where(AppraisalCycle.id == cycle_id),
            AppraisalCycle,
            user,
        )
    )
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Appraisal cycle not found"
        )
    return cycle


async def _stages(
    session: AsyncSession, user: User, cycle_id: uuid.UUID
) -> list[AppraisalStage]:
    """Queried, never read off `cycle.stages` — that relationship is lazily
    loaded and touching it in an async function raises MissingGreenlet."""
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(AppraisalStage)
                    .where(AppraisalStage.cycle_id == cycle_id)
                    .order_by(AppraisalStage.sequence),
                    AppraisalStage,
                    user,
                )
            )
        ).all()
    )


def _definitions(stages: list[AppraisalStage]) -> list[progression.StageDefinition]:
    return [
        progression.StageDefinition(
            stage_id=str(stage.id),
            sequence=stage.sequence,
            role=stage.role.value,
            label=stage.label,
            is_mandatory=stage.is_mandatory,
            sees_previous_entries=stage.sees_previous_entries,
            visible_to_employee_before_release=stage.visible_to_employee_before_release,
        )
        for stage in stages
    ]


async def _appraisal(
    session: AsyncSession, user: User, appraisal_id: uuid.UUID
) -> Appraisal:
    appraisal = await session.scalar(
        tenant_scoped(
            select(Appraisal).where(Appraisal.id == appraisal_id), Appraisal, user
        )
    )
    if appraisal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appraisal not found")
    return appraisal


# ═══════════════════════════════════════════════════════════════════════════
# Eligibility and launch
# ═══════════════════════════════════════════════════════════════════════════


def is_eligible(employee: Employee, rule: dict[str, Any], as_of: date) -> tuple[bool, str]:
    """Whether one employee is in the cycle.

    Held as data on the cycle so HR changes who is included without an
    engineering change. The reason is returned either way — "why is Anita not
    in this cycle" is asked on every launch.
    """
    if str(employee.id) in {str(x) for x in rule.get("exclude_employee_ids", [])}:
        return False, "Explicitly excluded from this cycle."

    if employee.status in (EmploymentStatus.EXITED, EmploymentStatus.PRE_JOINING):
        return False, f"Employment status is '{employee.status.value}'."

    grades = rule.get("grades")
    if grades and employee.grade not in grades:
        return False, f"Grade '{employee.grade}' is not in scope for this cycle."

    locations = rule.get("locations")
    if locations and str(employee.location_id) not in {str(x) for x in locations}:
        return False, "Work location is not in scope for this cycle."

    minimum = rule.get("min_service_months")
    if minimum and employee.date_of_joining:
        months = (as_of - employee.date_of_joining).days // 30
        if months < int(minimum):
            return False, (
                f"{months} month(s) of service against the {minimum} this cycle "
                "requires."
            )

    return True, "Eligible."


async def launch_cycle(
    session: AsyncSession,
    user: User,
    *,
    cycle_id: uuid.UUID,
    request: Request | None = None,
) -> dict[str, Any]:
    """Create an appraisal for every eligible employee.

    Returns a summary including who was excluded and why, because "why is Anita
    not in this cycle" is asked on every launch and the answer should not
    require a database query.
    """
    cycle = await _cycle(session, user, cycle_id)
    if cycle.status is not CycleStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This cycle is '{cycle.status.value}'. A launched cycle is not "
                "relaunched — add individual appraisals instead."
            ),
        )

    stages = await _stages(session, user, cycle_id)
    if not stages:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "This cycle has no stages, so there is no chain to run. Define the "
                "stages before launching."
            ),
        )

    employees = (
        await session.scalars(
            tenant_scoped(
                select(Employee).where(Employee.deleted_at.is_(None)), Employee, user
            )
        )
    ).all()

    first_stage = stages[0]
    included = 0
    excluded: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []

    for employee in employees:
        eligible, reason = is_eligible(employee, cycle.eligibility_rule, cycle.period_end)
        if not eligible:
            excluded.append({"employee_code": employee.employee_code, "reason": reason})
            continue

        session.add(
            Appraisal(
                tenant_id=user.tenant_id,
                cycle_id=cycle.id,
                employee_id=employee.id,
                current_stage_id=first_stage.id,
                status=AppraisalStatus.NOT_STARTED,
            )
        )
        included += 1

        # An employee who is their own manager produces a chain with no
        # independent view in it. Surfaced at launch, when HR can still fix it.
        manager_stage = next((s for s in stages if s.role is StageRole.MANAGER), None)
        if manager_stage is not None and employee.manager_id == employee.id:
            conflicts.append(
                {
                    "employee_code": employee.employee_code,
                    "reason": (
                        "This employee is recorded as their own manager, so the manager "
                        "stage has no independent view in it. Assign a reviewer."
                    ),
                }
            )

    cycle.status = CycleStatus.ACTIVE
    cycle.launched_at = datetime.now(UTC)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="appraisal_cycle",
        entity_id=cycle.id,
        summary=(
            f"Launched '{cycle.name}' for {included} employee(s); "
            f"{len(excluded)} excluded, {len(conflicts)} conflict(s) to resolve"
        ),
        changes={
            "included": included,
            "excluded": len(excluded),
            "conflicts": len(conflicts),
        },
        request=request,
    )

    return {
        "cycle_id": str(cycle.id),
        "included": included,
        "excluded": excluded,
        "conflicts": conflicts,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Stage transitions
# ═══════════════════════════════════════════════════════════════════════════


async def _delegate_for(
    session: AsyncSession, user: User, cycle_id: uuid.UUID, owner_id: uuid.UUID | None
) -> uuid.UUID | None:
    if owner_id is None:
        return None
    delegation = await session.scalar(
        tenant_scoped(
            select(StageDelegation).where(
                StageDelegation.cycle_id == cycle_id,
                StageDelegation.from_user_id == owner_id,
                StageDelegation.active.is_(True),
                StageDelegation.effective_from <= date.today(),
                StageDelegation.effective_to >= date.today(),
            ),
            StageDelegation,
            user,
        )
    )
    return delegation.to_user_id if delegation else None


async def submit_stage(
    session: AsyncSession,
    user: User,
    *,
    appraisal_id: uuid.UUID,
    ratings: dict[str, Any],
    comments: str | None = None,
    score: float | None = None,
    request: Request | None = None,
) -> Appraisal:
    """Fill in the current stage and advance."""
    appraisal = await _appraisal(session, user, appraisal_id)
    if appraisal.status in (AppraisalStatus.COMPLETED, AppraisalStatus.ACKNOWLEDGED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This appraisal is '{appraisal.status.value}'. Reopen it before "
                "changing anything."
            ),
        )
    if appraisal.current_stage_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This appraisal has no current stage.",
        )

    stages = await _stages(session, user, appraisal.cycle_id)
    current = next((s for s in stages if s.id == appraisal.current_stage_id), None)
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The current stage is not part of this cycle.",
        )

    delegate = await _delegate_for(session, user, appraisal.cycle_id, user.id)
    session.add(
        AppraisalStageEntry(
            tenant_id=user.tenant_id,
            appraisal_id=appraisal.id,
            stage_id=current.id,
            actor_user_id=user.id,
            delegated_from_user_id=delegate,
            ratings=ratings,
            comments=comments,
            score=score,
            submitted_at=datetime.now(UTC),
        )
    )

    completed = {
        str(row)
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(AppraisalStageEntry.stage_id).where(
                        AppraisalStageEntry.appraisal_id == appraisal.id,
                        AppraisalStageEntry.submitted_at.isnot(None),
                    ),
                    AppraisalStageEntry,
                    user,
                )
            )
        ).all()
    } | {str(current.id)}

    move = progression.next_stage(
        stages=_definitions(stages),
        current_stage_id=str(current.id),
        completed_stage_ids=completed,
    )

    if move.is_complete:
        appraisal.current_stage_id = None
        appraisal.status = AppraisalStatus.COMPLETED
        appraisal.completed_at = datetime.now(UTC)
        if score is not None:
            appraisal.final_score = score
    else:
        appraisal.current_stage_id = uuid.UUID(move.to_stage_id)
        appraisal.status = AppraisalStatus.IN_PROGRESS

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="appraisal",
        entity_id=appraisal.id,
        summary=(
            f"Submitted '{current.label}'. {move.reason}"
            + (f" Skipped: {', '.join(move.skipped)}." if move.skipped else "")
        ),
        changes={**move.as_dict(), "delegated_from": str(delegate) if delegate else None},
        request=request,
    )
    return appraisal


async def return_stage(
    session: AsyncSession,
    user: User,
    *,
    appraisal_id: uuid.UUID,
    target_stage_id: uuid.UUID,
    reason: str,
    request: Request | None = None,
) -> Appraisal:
    """Send the appraisal back, with a reason.

    A normal move, not an error path. The entry recording the return stays in
    the history — an appraisal that reads as though it went straight through
    when it went back twice is not a record of what happened.
    """
    appraisal = await _appraisal(session, user, appraisal_id)
    if appraisal.current_stage_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This appraisal has no current stage to return from.",
        )

    stages = await _stages(session, user, appraisal.cycle_id)
    try:
        move = progression.plan_return(
            stages=_definitions(stages),
            current_stage_id=str(appraisal.current_stage_id),
            target_stage_id=str(target_stage_id),
            reason=reason,
        )
    except progression.ProgressionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    session.add(
        AppraisalStageEntry(
            tenant_id=user.tenant_id,
            appraisal_id=appraisal.id,
            stage_id=appraisal.current_stage_id,
            actor_user_id=user.id,
            returned_to_stage_id=target_stage_id,
            returned_reason=move.reason,
            submitted_at=datetime.now(UTC),
        )
    )
    appraisal.current_stage_id = target_stage_id
    appraisal.status = AppraisalStatus.RETURNED
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.REJECT,
        entity_type="appraisal",
        entity_id=appraisal.id,
        summary=f"Returned to an earlier stage: {move.reason}",
        changes=move.as_dict(),
        request=request,
    )
    return appraisal


# ═══════════════════════════════════════════════════════════════════════════
# Release, acknowledgement, reopen
# ═══════════════════════════════════════════════════════════════════════════


async def release(
    session: AsyncSession,
    user: User,
    *,
    appraisal_id: uuid.UUID,
    final_rating: str | None = None,
    request: Request | None = None,
) -> Appraisal:
    """Show the completed appraisal to the employee."""
    appraisal = await _appraisal(session, user, appraisal_id)
    if appraisal.status is not AppraisalStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This appraisal is '{appraisal.status.value}'. Only a completed "
                "appraisal is released."
            ),
        )

    appraisal.status = AppraisalStatus.RELEASED
    appraisal.released_at = datetime.now(UTC)
    if final_rating:
        appraisal.final_rating = final_rating
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="appraisal",
        entity_id=appraisal.id,
        summary=f"Released to the employee with a rating of '{appraisal.final_rating}'",
        changes={"final_rating": appraisal.final_rating},
        request=request,
    )
    return appraisal


async def acknowledge(
    session: AsyncSession,
    user: User,
    *,
    appraisal_id: uuid.UUID,
    acknowledgement: AcknowledgementType,
    objection: str | None = None,
    request: Request | None = None,
) -> Appraisal:
    """The employee's response.

    Disagreement is a valid close. Forcing agreement as the only way to
    acknowledge is how an appraisal dispute becomes an employment dispute — the
    objection is recorded, routed to HR, and the cycle proceeds.
    """
    appraisal = await _appraisal(session, user, appraisal_id)
    if appraisal.status is not AppraisalStatus.RELEASED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This appraisal is '{appraisal.status.value}'. There is nothing "
                "released to acknowledge."
            ),
        )

    if acknowledgement is AcknowledgementType.DISAGREED and not (objection or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Disagreeing needs a written objection. It is your account of the year "
                "and it stays on the record — without it there is nothing for HR to "
                "look at."
            ),
        )

    appraisal.status = AppraisalStatus.ACKNOWLEDGED
    appraisal.acknowledged_at = datetime.now(UTC)
    appraisal.acknowledgement_type = acknowledgement
    if acknowledgement is AcknowledgementType.DISAGREED:
        appraisal.objection = objection
        appraisal.objection_routed_at = datetime.now(UTC)

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="appraisal",
        entity_id=appraisal.id,
        summary=(
            "Acknowledged with a written objection, routed to HR. The cycle is closed "
            "for this employee; the objection stands on the record."
            if acknowledgement is AcknowledgementType.DISAGREED
            else "Acknowledged and agreed."
        ),
        changes={"acknowledgement": acknowledgement.value, "has_objection": bool(objection)},
        request=request,
    )
    return appraisal


async def reopen(
    session: AsyncSession,
    user: User,
    *,
    appraisal_id: uuid.UUID,
    reason: str,
    downstream_impact: list[dict[str, Any]] | None = None,
    acknowledge_impact: bool = False,
    request: Request | None = None,
) -> Appraisal:
    """Reopen a closed appraisal.

    Where the rating has already fed something downstream — an increment
    granted on it — this refuses until the impact is explicitly acknowledged,
    and records what that impact was. After the fact nobody can reconstruct
    what the old rating had already been used for.
    """
    if not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Reopening a closed appraisal needs a reason.",
        )

    appraisal = await _appraisal(session, user, appraisal_id)
    if appraisal.status not in (
        AppraisalStatus.COMPLETED,
        AppraisalStatus.RELEASED,
        AppraisalStatus.ACKNOWLEDGED,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An appraisal in status '{appraisal.status.value}' is not closed.",
        )

    impact = downstream_impact or []
    if impact and not acknowledge_impact:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    "This rating has already fed downstream decisions. Reopening it does "
                    "not unwind them — they have to be revisited separately. Acknowledge "
                    "the impact to proceed."
                ),
                "downstream_impact": impact,
            },
        )

    stages = await _stages(session, user, appraisal.cycle_id)
    appraisal.status = AppraisalStatus.IN_PROGRESS
    appraisal.current_stage_id = stages[0].id if stages else None
    appraisal.completed_at = None
    appraisal.released_at = None
    appraisal.acknowledged_at = None
    appraisal.reopened_count += 1
    appraisal.reopen_reason = reason
    # Appended rather than replaced: a second reopen must not erase what the
    # first one disturbed.
    appraisal.downstream_impact = [*(appraisal.downstream_impact or []), *impact]
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="appraisal",
        entity_id=appraisal.id,
        summary=(
            f"Reopened (attempt {appraisal.reopened_count}): {reason}"
            + (
                f" — {len(impact)} downstream decision(s) already made on the old rating."
                if impact
                else ""
            )
        ),
        changes={"reason": reason, "downstream_impact": impact},
        request=request,
    )
    return appraisal


# ═══════════════════════════════════════════════════════════════════════════
# The completion dashboard
# ═══════════════════════════════════════════════════════════════════════════


async def cycle_progress(
    session: AsyncSession, user: User, cycle_id: uuid.UUID
) -> dict[str, Any]:
    """Where a cycle stands, by stage and by status.

    Without this a cycle of several hundred appraisals is unmanageable — HR
    cannot tell who is holding it up, and chases everyone instead of the four
    people who are late.
    """
    cycle = await _cycle(session, user, cycle_id)
    stages = await _stages(session, user, cycle_id)

    rows = (
        await session.execute(
            tenant_scoped(
                select(
                    Appraisal.status,
                    Appraisal.current_stage_id,
                    func.count(Appraisal.id),
                )
                .where(Appraisal.cycle_id == cycle_id)
                .group_by(Appraisal.status, Appraisal.current_stage_id),
                Appraisal,
                user,
            )
        )
    ).all()

    by_status: dict[str, int] = {}
    by_stage: dict[str, int] = {}
    total = 0
    for appraisal_status, stage_id, count in rows:
        total += count
        by_status[appraisal_status.value] = by_status.get(appraisal_status.value, 0) + count
        if stage_id is not None:
            label = next(
                (s.label for s in stages if s.id == stage_id), str(stage_id)
            )
            by_stage[label] = by_stage.get(label, 0) + count

    today = date.today()
    overdue = {
        stage.label: by_stage.get(stage.label, 0)
        for stage in stages
        if stage.due_date and stage.due_date < today and by_stage.get(stage.label, 0)
    }

    completed = by_status.get("completed", 0) + by_status.get(
        "acknowledged", 0
    ) + by_status.get("released", 0)

    return {
        "cycle": cycle.name,
        "status": cycle.status.value,
        "total": total,
        "completed": completed,
        "completion_percent": (
            round(completed / total * 100, 1) if total else 0.0
        ),
        "by_status": by_status,
        "pending_by_stage": by_stage,
        "overdue_by_stage": overdue,
    }
