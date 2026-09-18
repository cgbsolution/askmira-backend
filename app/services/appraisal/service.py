"""Appraisal cycle persistence: launch, submit, return, release, reopen.

The progression rules live in `progression.py` and are exercised there. Who may
act on which step lives in `access.py`. This module is the database side of
both, plus the things that only make sense against real records:

- **Bulk launch.** A cycle is launched for hundreds of people at once, and
  eligibility is evaluated per employee against the cycle's rule.
- **Scores are computed here, never accepted from a screen.** Where the cycle
  has an appraisal template the step's score is composed from measured goals
  and the ratings on the step (`review.compute_score`).
- **Release and acknowledgement**, including acknowledging with an objection.
- **Reopening after close**, with the downstream impact recorded at the time —
  after the fact nobody can reconstruct what the rating had already fed.
- **The completion dashboard**, by stage, department and manager.

Every stage transition raises a notification in the same transaction as the
change and its audit row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
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
from app.models.calibration import CalibrationPolicy, DistributionTarget
from app.models.competency import AppraisalTemplate, CompetencyTemplate, RatingScale
from app.models.cycle_config import TemplateAssignmentRule
from app.models.employee import Employee, EmploymentStatus
from app.models.org import Department
from app.models.performance_flow import (
    IncrementCycle,
    IncrementRecommendation,
    RecommendationStatusEnum,
)
from app.models.user import User
from app.schemas.performance import CycleCreate, CycleUpdate, DelegationCreate
from app.services import audit
from app.services.appraisal import access, operations, progression, review, scoring
from app.services.performance import notices

__all__ = [
    "acknowledge",
    "create_cycle",
    "create_delegation",
    "cycle_detail",
    "cycle_progress",
    "launch_cycle",
    "list_appraisals",
    "list_cycles",
    "list_delegations",
    "release",
    "reopen",
    "return_stage",
    "revoke_delegation",
    "save_draft",
    "submit_stage",
    "update_cycle",
]

DONE_STATUSES = (AppraisalStatus.COMPLETED, AppraisalStatus.RELEASED, AppraisalStatus.ACKNOWLEDGED)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


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


async def _stages(session: AsyncSession, user: User, cycle_id: uuid.UUID) -> list[AppraisalStage]:
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


async def _appraisal(session: AsyncSession, user: User, appraisal_id: uuid.UUID) -> Appraisal:
    appraisal = await session.scalar(
        tenant_scoped(select(Appraisal).where(Appraisal.id == appraisal_id), Appraisal, user)
    )
    if appraisal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appraisal not found")
    return appraisal


async def _employee(session: AsyncSession, user: User, employee_id: uuid.UUID) -> Employee | None:
    return await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )


# ═══════════════════════════════════════════════════════════════════════════
# Cycle configuration (the chain itself)
# ═══════════════════════════════════════════════════════════════════════════


def _check_stage_sequences(stages: list[Any]) -> None:
    sequences = [stage.sequence for stage in stages]
    if len(set(sequences)) != len(sequences):
        raise _unprocessable(
            "Two stages share a sequence number, so the order the chain runs in would be undefined."
        )


async def create_cycle(
    session: AsyncSession, user: User, payload: CycleCreate, *, request: Request | None = None
) -> AppraisalCycle:
    if payload.period_end < payload.period_start:
        raise _unprocessable("The review period ends before it starts.")
    _check_stage_sequences(payload.stages)

    duplicate = await session.scalar(
        tenant_scoped(
            select(AppraisalCycle.id).where(
                AppraisalCycle.name == payload.name,
                AppraisalCycle.period_start == payload.period_start,
            ),
            AppraisalCycle,
            user,
        )
    )
    if duplicate is not None:
        raise _conflict("A cycle with this name and start date already exists.")

    cycle = AppraisalCycle(
        tenant_id=user.tenant_id,
        name=payload.name,
        cycle_type=payload.cycle_type,
        period_start=payload.period_start,
        period_end=payload.period_end,
        eligibility_rule=payload.eligibility_rule,
        escalation_after_days=payload.escalation_after_days,
    )
    session.add(cycle)
    await session.flush()
    for stage in payload.stages:
        session.add(
            AppraisalStage(tenant_id=user.tenant_id, cycle_id=cycle.id, **stage.model_dump())
        )
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="appraisal_cycle",
        entity_id=cycle.id,
        summary=f"Created cycle '{cycle.name}' with {len(payload.stages)} stage(s)",
        changes={"cycle_type": payload.cycle_type.value, "stages": len(payload.stages)},
        request=request,
    )
    return cycle


async def update_cycle(
    session: AsyncSession,
    user: User,
    cycle_id: uuid.UUID,
    payload: CycleUpdate,
    *,
    request: Request | None = None,
) -> AppraisalCycle:
    """Change a draft cycle. A launched cycle's chain is not rewritten under people."""
    permissions.require_hr(user, detail="Cycles are configured by HR.")
    cycle = await _cycle(session, user, cycle_id)
    if cycle.status is not CycleStatus.DRAFT:
        raise _conflict(
            f"This cycle is '{cycle.status.value}'. Only a draft cycle's configuration can "
            "change; clone it to start a new one."
        )
    changes: dict[str, Any] = {}
    for field in (
        "name",
        "period_start",
        "period_end",
        "eligibility_rule",
        "escalation_after_days",
    ):
        value = getattr(payload, field)
        if value is not None and value != getattr(cycle, field):
            changes[field] = {"from": str(getattr(cycle, field)), "to": str(value)}
            setattr(cycle, field, value)
    if cycle.period_end < cycle.period_start:
        raise _unprocessable("The review period ends before it starts.")

    if payload.stages is not None:
        _check_stage_sequences(payload.stages)
        for stage in await _stages(session, user, cycle.id):
            await session.delete(stage)
        await session.flush()
        for stage in payload.stages:
            session.add(
                AppraisalStage(tenant_id=user.tenant_id, cycle_id=cycle.id, **stage.model_dump())
            )
        changes["stages"] = len(payload.stages)

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="appraisal_cycle",
        entity_id=cycle.id,
        summary=f"Updated draft cycle '{cycle.name}'",
        changes=changes,
        request=request,
    )
    return cycle


async def list_cycles(
    session: AsyncSession, user: User, *, cycle_status: CycleStatus | None = None
) -> list[dict[str, Any]]:
    stmt = tenant_scoped(select(AppraisalCycle), AppraisalCycle, user).order_by(
        AppraisalCycle.period_start.desc()
    )
    if cycle_status is not None:
        stmt = stmt.where(AppraisalCycle.status == cycle_status)
    cycles = list((await session.scalars(stmt)).all())
    if not cycles:
        return []
    ids = [c.id for c in cycles]
    counts: dict[uuid.UUID, dict[str, int]] = {cid: {"total": 0, "completed": 0} for cid in ids}
    for cycle_id, appraisal_status in (
        await session.execute(
            tenant_scoped(
                select(Appraisal.cycle_id, Appraisal.status).where(Appraisal.cycle_id.in_(ids)),
                Appraisal,
                user,
            )
        )
    ).all():
        counts[cycle_id]["total"] += 1
        if appraisal_status in DONE_STATUSES:
            counts[cycle_id]["completed"] += 1
    templated = set(
        (
            await session.scalars(
                tenant_scoped(
                    select(AppraisalTemplate.cycle_id).where(AppraisalTemplate.cycle_id.in_(ids)),
                    AppraisalTemplate,
                    user,
                )
            )
        ).all()
    )
    return [
        {
            "id": str(c.id),
            "name": c.name,
            "cycle_type": c.cycle_type.value,
            "status": c.status.value,
            "period_start": c.period_start.isoformat(),
            "period_end": c.period_end.isoformat(),
            "escalation_after_days": c.escalation_after_days,
            "launched_at": c.launched_at.isoformat() if c.launched_at else None,
            "closed_at": c.closed_at.isoformat() if c.closed_at else None,
            "appraisals": counts[c.id]["total"],
            "completed": counts[c.id]["completed"],
            "has_template": c.id in templated,
        }
        for c in cycles
    ]


async def cycle_detail(session: AsyncSession, user: User, cycle_id: uuid.UUID) -> dict[str, Any]:
    """The whole configuration of one cycle, for the cycle screen."""
    permissions.require_hr(user, detail="Cycle configuration is visible to HR.")
    cycle = await _cycle(session, user, cycle_id)
    stages = await _stages(session, user, cycle_id)
    template = await session.scalar(
        tenant_scoped(
            select(AppraisalTemplate).where(AppraisalTemplate.cycle_id == cycle_id),
            AppraisalTemplate,
            user,
        )
    )
    template_payload = None
    if template is not None:
        scale_name = await session.scalar(
            tenant_scoped(
                select(RatingScale.name).where(RatingScale.id == template.rating_scale_id),
                RatingScale,
                user,
            )
        )
        competency_template_name = None
        if template.competency_template_id:
            competency_template_name = await session.scalar(
                tenant_scoped(
                    select(CompetencyTemplate.name).where(
                        CompetencyTemplate.id == template.competency_template_id
                    ),
                    CompetencyTemplate,
                    user,
                )
            )
        template_payload = {
            "id": str(template.id),
            "rating_scale_id": str(template.rating_scale_id),
            "rating_scale_name": scale_name,
            "competency_template_id": (
                str(template.competency_template_id) if template.competency_template_id else None
            ),
            "competency_template_name": competency_template_name,
            "goal_weight": str(template.goal_weight),
            "competency_weight": str(template.competency_weight),
            "values_weight": str(template.values_weight),
            "score_precision": template.score_precision,
            "bands": template.score_bands or [],
        }
    rules = (
        await session.scalars(
            tenant_scoped(
                select(TemplateAssignmentRule)
                .where(TemplateAssignmentRule.cycle_id == cycle_id)
                .order_by(TemplateAssignmentRule.priority),
                TemplateAssignmentRule,
                user,
            )
        )
    ).all()
    policy = await session.scalar(
        tenant_scoped(
            select(CalibrationPolicy).where(CalibrationPolicy.cycle_id == cycle_id),
            CalibrationPolicy,
            user,
        )
    )
    targets = []
    if policy is not None:
        targets = (
            await session.scalars(
                tenant_scoped(
                    select(DistributionTarget).where(DistributionTarget.policy_id == policy.id),
                    DistributionTarget,
                    user,
                )
            )
        ).all()
    summary = next(
        (row for row in await list_cycles(session, user) if row["id"] == str(cycle.id)), None
    )
    return {
        "cycle": summary,
        "eligibility_rule": cycle.eligibility_rule or {},
        "stages": [
            {
                "id": str(s.id),
                "sequence": s.sequence,
                "role": s.role.value,
                "label": s.label,
                "due_date": s.due_date.isoformat() if s.due_date else None,
                "open_date": s.open_date.isoformat() if s.open_date else None,
                "close_date": s.close_date.isoformat() if s.close_date else None,
                "grace_days": s.grace_days,
                "auto_advance": s.auto_advance.value,
                "is_mandatory": s.is_mandatory,
                "can_reopen": s.can_reopen,
                "sees_previous_entries": s.sees_previous_entries,
                "visible_to_employee_before_release": s.visible_to_employee_before_release,
            }
            for s in stages
        ],
        "template": template_payload,
        "rules": [
            {
                "id": str(r.id),
                "priority": r.priority,
                "template_id": str(r.template_id),
                "conditions": r.conditions or {},
                "label": r.label,
                "is_default": not r.conditions,
            }
            for r in rules
        ],
        "policy": (
            {
                "id": str(policy.id),
                "mode": policy.mode.value,
                "scope": policy.scope.value,
                "min_group_size": policy.min_group_size,
                "disclose_calibration_to_employee": policy.disclose_calibration_to_employee,
                "targets": [
                    {
                        "rating_value": t.rating_value,
                        "target_percent": str(t.target_percent),
                        "tolerance_percent": str(t.tolerance_percent),
                    }
                    for t in targets
                ],
            }
            if policy
            else None
        ),
    }


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
                f"{months} month(s) of service against the {minimum} this cycle requires."
            )

    return True, "Eligible."


async def launch_cycle(
    session: AsyncSession,
    user: User,
    *,
    cycle_id: uuid.UUID,
    assignments: dict[str, str] | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Create an appraisal for every eligible employee.

    `assignments` maps employee id to the competency template the assignment
    rules resolved them to (see `cycle_config.activate_cycle`). Without it,
    everybody is reviewed against the appraisal template's own competency
    template.

    Returns a summary including who was excluded and why, because "why is Anita
    not in this cycle" is asked on every launch and the answer should not
    require a database query.
    """
    cycle = await _cycle(session, user, cycle_id)
    if cycle.status is not CycleStatus.DRAFT:
        raise _conflict(
            f"This cycle is '{cycle.status.value}'. A launched cycle is not "
            "relaunched — add individual appraisals instead."
        )

    stages = await _stages(session, user, cycle_id)
    if not stages:
        raise _unprocessable(
            "This cycle has no stages, so there is no chain to run. Define the "
            "stages before launching."
        )

    template = await session.scalar(
        tenant_scoped(
            select(AppraisalTemplate).where(AppraisalTemplate.cycle_id == cycle_id),
            AppraisalTemplate,
            user,
        )
    )
    default_template_id = template.competency_template_id if template else None

    employees = (
        await session.scalars(
            tenant_scoped(select(Employee).where(Employee.deleted_at.is_(None)), Employee, user)
        )
    ).all()

    first_stage = stages[0]
    manager_stage = next((s for s in stages if s.role is StageRole.MANAGER), None)
    included: list[Employee] = []
    excluded: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []

    for employee in employees:
        eligible, reason = is_eligible(employee, cycle.eligibility_rule, cycle.period_end)
        if not eligible:
            excluded.append({"employee_code": employee.employee_code, "reason": reason})
            continue

        resolved = (assignments or {}).get(str(employee.id))
        session.add(
            Appraisal(
                tenant_id=user.tenant_id,
                cycle_id=cycle.id,
                employee_id=employee.id,
                current_stage_id=first_stage.id,
                status=AppraisalStatus.NOT_STARTED,
                competency_template_id=uuid.UUID(resolved) if resolved else default_template_id,
            )
        )
        included.append(employee)

        # An employee who is their own manager produces a chain with no
        # independent view in it. Surfaced at launch, when HR can still fix it.
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
            f"Launched '{cycle.name}' for {len(included)} employee(s); "
            f"{len(excluded)} excluded, {len(conflicts)} conflict(s) to resolve"
        ),
        changes={
            "included": len(included),
            "excluded": len(excluded),
            "conflicts": len(conflicts),
        },
        request=request,
    )

    # Tell whoever holds the first step. One event per recipient group, not
    # one per appraisal: a launch for five hundred people must not become a
    # thousand messages.
    if first_stage.role is StageRole.SELF:
        recipients = await notices.user_ids_for_employees(session, user, [e.id for e in included])
        await notices.send(
            session,
            tenant_id=user.tenant_id,
            event_key="performance.review.launched",
            recipient_user_ids=recipients.values(),
            payload={"cycle_name": cycle.name, "stage_label": first_stage.label},
        )
    else:
        managers = await notices.user_ids_for_employees(
            session, user, [e.manager_id for e in included if e.manager_id]
        )
        await notices.send(
            session,
            tenant_id=user.tenant_id,
            event_key="performance.review.reminder",
            recipient_user_ids=managers.values(),
            payload={
                "count": "new",
                "stage_label": first_stage.label,
                "cycle_name": cycle.name,
                "due_date": first_stage.due_date.isoformat() if first_stage.due_date else "not set",
            },
        )

    return {
        "cycle_id": str(cycle.id),
        "included": len(included),
        "excluded": excluded,
        "conflicts": conflicts,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Stage transitions
# ═══════════════════════════════════════════════════════════════════════════


async def _open_step(
    session: AsyncSession, user: User, appraisal: Appraisal
) -> tuple[AppraisalCycle, list[AppraisalStage], AppraisalStage]:
    if appraisal.status in DONE_STATUSES:
        raise _conflict(
            f"This appraisal is '{appraisal.status.value}'. Reopen it before changing anything."
        )
    if appraisal.current_stage_id is None:
        raise _conflict("This appraisal has no current stage.")
    cycle = await _cycle(session, user, appraisal.cycle_id)
    if cycle.status is CycleStatus.CLOSED:
        raise _conflict("This cycle is closed. A closed review changes only through an HR reopen.")
    stages = await _stages(session, user, appraisal.cycle_id)
    current = next((s for s in stages if s.id == appraisal.current_stage_id), None)
    if current is None:
        raise _conflict("The current stage is not part of this cycle.")
    return cycle, stages, current


async def _latest_scored_entry(
    session: AsyncSession, user: User, appraisal: Appraisal, stages: list[AppraisalStage]
) -> AppraisalStageEntry | None:
    non_self = [s.id for s in stages if s.role is not StageRole.SELF]
    if not non_self:
        return None
    return await session.scalar(
        tenant_scoped(
            select(AppraisalStageEntry)
            .where(
                AppraisalStageEntry.appraisal_id == appraisal.id,
                AppraisalStageEntry.stage_id.in_(non_self),
                AppraisalStageEntry.score.isnot(None),
                AppraisalStageEntry.submitted_at.isnot(None),
                AppraisalStageEntry.returned_to_stage_id.is_(None),
            )
            .order_by(AppraisalStageEntry.submitted_at.desc())
            .limit(1),
            AppraisalStageEntry,
            user,
        )
    )


async def _my_draft(
    session: AsyncSession, user: User, appraisal: Appraisal, stage: AppraisalStage
) -> AppraisalStageEntry | None:
    return await session.scalar(
        tenant_scoped(
            select(AppraisalStageEntry).where(
                AppraisalStageEntry.appraisal_id == appraisal.id,
                AppraisalStageEntry.stage_id == stage.id,
                AppraisalStageEntry.actor_user_id == user.id,
                AppraisalStageEntry.submitted_at.is_(None),
            ),
            AppraisalStageEntry,
            user,
        )
    )


async def save_draft(
    session: AsyncSession,
    user: User,
    *,
    appraisal_id: uuid.UUID,
    ratings: dict[str, Any],
    comments: str | None = None,
    request: Request | None = None,
) -> AppraisalStageEntry:
    """Keep work in progress on the current step without submitting it."""
    appraisal = await _appraisal(session, user, appraisal_id)
    _, stages, current = await _open_step(session, user, appraisal)
    actor = await access.stage_actor(session, user, appraisal, current, stages)
    if current.role is not StageRole.SELF and appraisal.employee_id == user.employee_id:
        raise permissions.forbidden("You cannot rate your own review.")
    review.parse_ratings(ratings)

    draft = await _my_draft(session, user, appraisal, current)
    if draft is None:
        draft = AppraisalStageEntry(
            tenant_id=user.tenant_id,
            appraisal_id=appraisal.id,
            stage_id=current.id,
            actor_user_id=user.id,
        )
        session.add(draft)
    draft.delegated_from_user_id = actor.delegated_from_user_id
    draft.is_escalated = actor.is_escalated
    draft.ratings = ratings
    draft.comments = comments
    if appraisal.status is AppraisalStatus.NOT_STARTED:
        appraisal.status = AppraisalStatus.IN_PROGRESS
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="appraisal",
        entity_id=appraisal.id,
        summary=f"Saved a draft of '{current.label}'",
        changes={"stage_id": str(current.id), "draft_entry_id": str(draft.id)},
        request=request,
    )
    return draft


async def submit_stage(
    session: AsyncSession,
    user: User,
    *,
    appraisal_id: uuid.UUID,
    ratings: dict[str, Any],
    comments: str | None = None,
    score: Decimal | float | None = None,
    request: Request | None = None,
) -> Appraisal:
    """Fill in the current stage and advance.

    `score` is honoured only for a cycle with no appraisal template — where
    there is one, the score is computed from measured goals and the ratings,
    never accepted from the caller.
    """
    appraisal = await _appraisal(session, user, appraisal_id)
    cycle, stages, current = await _open_step(session, user, appraisal)
    actor = await access.stage_actor(session, user, appraisal, current, stages)
    if current.role is not StageRole.SELF and appraisal.employee_id == user.employee_id:
        raise permissions.forbidden("You cannot rate your own review.")

    bundle = await review.load_bundle(session, user, appraisal, cycle)
    parsed = review.parse_ratings(ratings)
    stored_ratings: dict[str, Any] = ratings
    entry_score: Decimal | None = None
    computed: dict[str, Any] | None = None

    if bundle.template is None:
        entry_score = Decimal(str(score)) if score is not None else None
    else:
        stored_ratings = parsed.model_dump(mode="json")
        problems = review.validate_ratings(bundle, parsed)
        if problems:
            raise _unprocessable(" ".join(problems))
        if current.role is StageRole.SELF:
            computed = await review.compute_score(
                session, user, appraisal=appraisal, cycle=cycle, bundle=bundle, ratings=parsed
            )
            entry_score = Decimal(computed["score"]) if computed["score"] else None
        elif review.is_empty(parsed) and current.role is not StageRole.MANAGER:
            carried = await _latest_scored_entry(session, user, appraisal, stages)
            entry_score = Decimal(str(carried.score)) if carried is not None else None
        else:
            computed = await review.compute_score(
                session, user, appraisal=appraisal, cycle=cycle, bundle=bundle, ratings=parsed
            )
            if computed["problems"]:
                raise _unprocessable(
                    "These ratings cannot produce a score yet: " + " ".join(computed["problems"])
                )
            entry_score = Decimal(computed["score"])

    now = datetime.now(UTC)
    entry = await _my_draft(session, user, appraisal, current)
    if entry is None:
        entry = AppraisalStageEntry(
            tenant_id=user.tenant_id,
            appraisal_id=appraisal.id,
            stage_id=current.id,
            actor_user_id=user.id,
        )
        session.add(entry)
    entry.delegated_from_user_id = actor.delegated_from_user_id
    entry.is_escalated = actor.is_escalated
    entry.ratings = stored_ratings
    entry.comments = comments
    entry.score = entry_score
    entry.submitted_at = now
    await session.flush()

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

    next_stage: AppraisalStage | None = None
    if move.is_complete:
        appraisal.current_stage_id = None
        appraisal.status = AppraisalStatus.COMPLETED
        appraisal.completed_at = now
        if current.role is not StageRole.SELF and entry_score is not None:
            appraisal.final_score = entry_score
        elif bundle.template is not None:
            carried = await _latest_scored_entry(session, user, appraisal, stages)
            if carried is not None:
                appraisal.final_score = carried.score
        if bundle.template is not None and appraisal.final_score is not None:
            label = _band_label(bundle.template, Decimal(str(appraisal.final_score)))
            if label and not appraisal.final_rating:
                appraisal.final_rating = label
    else:
        next_stage = next((s for s in stages if str(s.id) == move.to_stage_id), None)
        appraisal.current_stage_id = uuid.UUID(move.to_stage_id)
        appraisal.status = AppraisalStatus.IN_PROGRESS
        appraisal.escalated_at = None
        appraisal.escalated_to_user_id = None

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
        changes={
            **move.as_dict(),
            "delegated_from": (
                str(actor.delegated_from_user_id) if actor.delegated_from_user_id else None
            ),
            "escalated": actor.is_escalated,
            "score": str(entry_score) if entry_score is not None else None,
            "goal_score": computed["goal"]["score"] if computed else None,
        },
        request=request,
    )

    if next_stage is not None:
        employee = await _employee(session, user, appraisal.employee_id)
        if employee is not None:
            owners = await operations.stage_owner_user_ids(
                session, user, appraisal=appraisal, stage=next_stage, employee=employee
            )
            await notices.send(
                session,
                tenant_id=user.tenant_id,
                event_key="performance.review.stage_open",
                recipient_user_ids=[uid for uid in owners if uid != user.id],
                payload={
                    "stage_label": next_stage.label,
                    "employee_name": employee.full_name,
                    "cycle_name": cycle.name,
                },
            )
    return appraisal


def _band_label(template: AppraisalTemplate, score: Decimal) -> str | None:
    bands = [
        scoring.Band(
            label=b["label"], lower=Decimal(str(b["lower"])), upper=Decimal(str(b["upper"]))
        )
        for b in (template.score_bands or [])
    ]
    if not bands:
        return None
    try:
        return scoring.map_to_band(score, bands).label
    except scoring.ScoringError:
        return None


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
        raise _conflict("This appraisal has no current stage to return from.")
    cycle, stages, current = await _open_step(session, user, appraisal)
    await access.stage_actor(session, user, appraisal, current, stages)
    if appraisal.employee_id == user.employee_id:
        raise permissions.forbidden("You cannot send your own review back.")

    try:
        move = progression.plan_return(
            stages=_definitions(stages),
            current_stage_id=str(appraisal.current_stage_id),
            target_stage_id=str(target_stage_id),
            reason=reason,
        )
    except progression.ProgressionError as exc:
        raise _unprocessable(str(exc)) from exc

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
    appraisal.escalated_at = None
    appraisal.escalated_to_user_id = None
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

    target = next((s for s in stages if s.id == target_stage_id), None)
    employee = await _employee(session, user, appraisal.employee_id)
    if target is not None and employee is not None:
        owners = await operations.stage_owner_user_ids(
            session, user, appraisal=appraisal, stage=target, employee=employee
        )
        await notices.send(
            session,
            tenant_id=user.tenant_id,
            event_key="performance.review.returned",
            recipient_user_ids=owners,
            payload={
                "employee_name": employee.full_name,
                "cycle_name": cycle.name,
                "stage_label": target.label,
                "reason": move.reason,
            },
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
    if appraisal.employee_id == user.employee_id:
        raise permissions.forbidden("You cannot release your own review.")
    if appraisal.status is not AppraisalStatus.COMPLETED:
        raise _conflict(
            f"This appraisal is '{appraisal.status.value}'. Only a completed appraisal is released."
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
    cycle = await _cycle(session, user, appraisal.cycle_id)
    recipients = await notices.user_ids_for_employees(session, user, [appraisal.employee_id])
    await notices.send(
        session,
        tenant_id=user.tenant_id,
        event_key="performance.review.released",
        recipient_user_ids=recipients.values(),
        payload={"cycle_name": cycle.name},
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
    """The employee's response — theirs alone.

    Disagreement is a valid close. Forcing agreement as the only way to
    acknowledge is how an appraisal dispute becomes an employment dispute — the
    objection is recorded, routed to HR, and the cycle proceeds.
    """
    appraisal = await _appraisal(session, user, appraisal_id)
    permissions.require_self(
        user,
        appraisal.employee_id,
        detail=(
            "Only the person reviewed can acknowledge their review. An acknowledgement "
            "recorded by somebody else is not an acknowledgement."
        ),
    )
    if appraisal.status is not AppraisalStatus.RELEASED:
        raise _conflict(
            f"This appraisal is '{appraisal.status.value}'. There is nothing "
            "released to acknowledge."
        )

    if acknowledgement is AcknowledgementType.DISAGREED and not (objection or "").strip():
        raise _unprocessable(
            "Disagreeing needs a written objection. It is your account of the year "
            "and it stays on the record — without it there is nothing for HR to "
            "look at."
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
    if acknowledgement is AcknowledgementType.DISAGREED:
        cycle = await _cycle(session, user, appraisal.cycle_id)
        employee = await _employee(session, user, appraisal.employee_id)
        await notices.send(
            session,
            tenant_id=user.tenant_id,
            event_key="performance.review.objection",
            recipient_user_ids=await notices.hr_user_ids(session, user),
            payload={
                "employee_name": employee.full_name if employee else "An employee",
                "cycle_name": cycle.name,
            },
        )
    return appraisal


async def _detected_impact(
    session: AsyncSession, user: User, appraisal: Appraisal
) -> list[dict[str, Any]]:
    """Increment recommendations already made on this review's rating."""
    rows = (
        await session.execute(
            tenant_scoped(
                select(IncrementRecommendation, IncrementCycle.name)
                .join(IncrementCycle, IncrementCycle.id == IncrementRecommendation.cycle_id)
                .where(
                    IncrementRecommendation.employee_id == appraisal.employee_id,
                    IncrementCycle.linked_appraisal_cycle_id == appraisal.cycle_id,
                    IncrementRecommendation.status != RecommendationStatusEnum.REJECTED,
                ),
                IncrementRecommendation,
                user,
            )
        )
    ).all()
    return [
        {
            "type": "increment_recommendation",
            "reference": str(rec.id),
            "detail": (
                f"{rec.recommended_percentage}% recommended in '{cycle_name}' on a rating of "
                f"'{rec.rating_label}' ({rec.status.value})."
            ),
        }
        for rec, cycle_name in rows
    ]


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
    recommended on it — this refuses until the impact is explicitly
    acknowledged, and records what that impact was. The increment link is
    detected here rather than trusted to the caller to mention.
    """
    if not reason.strip():
        raise _unprocessable("Reopening a closed appraisal needs a reason.")

    appraisal = await _appraisal(session, user, appraisal_id)
    if appraisal.employee_id == user.employee_id:
        raise permissions.forbidden("You cannot reopen your own review.")
    if appraisal.status not in DONE_STATUSES:
        raise _conflict(f"An appraisal in status '{appraisal.status.value}' is not closed.")

    impact = list(downstream_impact or [])
    known = {str(item.get("reference")) for item in impact}
    impact.extend(
        item
        for item in await _detected_impact(session, user, appraisal)
        if item["reference"] not in known
    )
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
# Lists and the completion dashboard
# ═══════════════════════════════════════════════════════════════════════════


async def list_appraisals(
    session: AsyncSession,
    user: User,
    *,
    cycle_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
    appraisal_status: AppraisalStatus | None = None,
) -> list[dict[str, Any]]:
    """Appraisals the caller may see: their own, their reporting line's, or all for HR."""
    visible = await permissions.visible_employee_ids(session, user)
    stmt = tenant_scoped(
        select(Appraisal, Employee).join(Employee, Employee.id == Appraisal.employee_id),
        Appraisal,
        user,
    )
    if visible is not None:
        if employee_id is not None and employee_id not in visible:
            raise permissions.forbidden(
                "You can see reviews for yourself and for people in your reporting line."
            )
        stmt = stmt.where(Appraisal.employee_id.in_(visible))
    if cycle_id is not None:
        stmt = stmt.where(Appraisal.cycle_id == cycle_id)
    if employee_id is not None:
        stmt = stmt.where(Appraisal.employee_id == employee_id)
    if appraisal_status is not None:
        stmt = stmt.where(Appraisal.status == appraisal_status)
    rows = (await session.execute(stmt.order_by(Employee.first_name))).all()
    return [
        {
            "id": str(a.id),
            "cycle_id": str(a.cycle_id),
            "employee_id": str(e.id),
            "employee_name": e.full_name,
            "employee_code": e.employee_code,
            "status": a.status.value,
            "current_stage_id": str(a.current_stage_id) if a.current_stage_id else None,
            "final_rating": (
                a.final_rating
                if a.employee_id != user.employee_id
                or a.status in (AppraisalStatus.RELEASED, AppraisalStatus.ACKNOWLEDGED)
                else None
            ),
            "final_score": (
                str(a.final_score)
                if a.final_score is not None
                and (
                    a.employee_id != user.employee_id
                    or a.status in (AppraisalStatus.RELEASED, AppraisalStatus.ACKNOWLEDGED)
                )
                else None
            ),
            "reopened_count": a.reopened_count,
        }
        for a, e in rows
    ]


def _bucket() -> dict[str, int]:
    return {"total": 0, "completed": 0, "pending": 0, "overdue": 0}


def _with_percent(bucket: dict[str, Any]) -> dict[str, Any]:
    total = bucket["total"]
    bucket["completion_percent"] = round(bucket["completed"] / total * 100, 1) if total else 0.0
    return bucket


async def cycle_progress(session: AsyncSession, user: User, cycle_id: uuid.UUID) -> dict[str, Any]:
    """Where a cycle stands, by stage, status, department and manager.

    A manager sees their own reporting line; HR sees everybody. Without this a
    cycle of several hundred appraisals is unmanageable — HR cannot tell who is
    holding it up, and chases everyone instead of the four people who are late.
    """
    cycle = await _cycle(session, user, cycle_id)
    stages = await _stages(session, user, cycle_id)
    stage_by_id = {s.id: s for s in stages}
    visible = await permissions.visible_employee_ids(session, user)

    stmt = tenant_scoped(
        select(Appraisal, Employee)
        .join(Employee, Employee.id == Appraisal.employee_id)
        .where(Appraisal.cycle_id == cycle_id),
        Appraisal,
        user,
    )
    if visible is not None:
        stmt = stmt.where(Appraisal.employee_id.in_(visible - {user.employee_id}))
    rows = (await session.execute(stmt)).all()

    department_ids = {e.department_id for _, e in rows if e.department_id}
    manager_ids = {e.manager_id for _, e in rows if e.manager_id}
    departments: dict[uuid.UUID, str] = {}
    managers: dict[uuid.UUID, str] = {}
    if department_ids:
        departments = {
            d.id: d.name
            for d in (
                await session.scalars(
                    tenant_scoped(
                        select(Department).where(Department.id.in_(department_ids)),
                        Department,
                        user,
                    )
                )
            ).all()
        }
    if manager_ids:
        managers = {
            m.id: m.full_name
            for m in (
                await session.scalars(
                    tenant_scoped(
                        select(Employee).where(Employee.id.in_(manager_ids)), Employee, user
                    )
                )
            ).all()
        }

    today = date.today()
    by_status: dict[str, int] = {}
    by_stage: dict[str, int] = {}
    overdue_by_stage: dict[str, int] = {}
    by_department: dict[str, dict[str, Any]] = {}
    by_manager: dict[str, dict[str, Any]] = {}
    completed = 0

    for appraisal, employee in rows:
        by_status[appraisal.status.value] = by_status.get(appraisal.status.value, 0) + 1
        done = appraisal.status in DONE_STATUSES
        completed += int(done)
        stage = stage_by_id.get(appraisal.current_stage_id) if appraisal.current_stage_id else None
        overdue = bool(stage and stage.due_date and stage.due_date < today and not done)
        if stage is not None:
            by_stage[stage.label] = by_stage.get(stage.label, 0) + 1
            if overdue:
                overdue_by_stage[stage.label] = overdue_by_stage.get(stage.label, 0) + 1

        dept_key = str(employee.department_id) if employee.department_id else "unassigned"
        dept = by_department.setdefault(
            dept_key,
            {
                "department_id": str(employee.department_id) if employee.department_id else None,
                "department": departments.get(employee.department_id, "No department")
                if employee.department_id
                else "No department",
                **_bucket(),
            },
        )
        mgr_key = str(employee.manager_id) if employee.manager_id else "none"
        mgr = by_manager.setdefault(
            mgr_key,
            {
                "manager_id": str(employee.manager_id) if employee.manager_id else None,
                "manager_name": managers.get(employee.manager_id, "No manager")
                if employee.manager_id
                else "No manager",
                **_bucket(),
            },
        )
        for bucket in (dept, mgr):
            bucket["total"] += 1
            bucket["completed"] += int(done)
            bucket["pending"] += int(not done)
            bucket["overdue"] += int(overdue)

    total = len(rows)
    return {
        "cycle": cycle.name,
        "cycle_id": str(cycle.id),
        "status": cycle.status.value,
        "total": total,
        "completed": completed,
        "completion_percent": round(completed / total * 100, 1) if total else 0.0,
        "by_status": by_status,
        "pending_by_stage": by_stage,
        "overdue_by_stage": overdue_by_stage,
        "by_department": sorted(
            (_with_percent(b) for b in by_department.values()),
            key=lambda b: (b["completion_percent"], b["department"]),
        ),
        "by_manager": sorted(
            (_with_percent(b) for b in by_manager.values()),
            key=lambda b: (b["completion_percent"], b["manager_name"]),
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Delegation
# ═══════════════════════════════════════════════════════════════════════════


async def create_delegation(
    session: AsyncSession,
    user: User,
    payload: DelegationCreate,
    *,
    request: Request | None = None,
) -> StageDelegation:
    """Hand one's own review steps to somebody else while away.

    A manager delegates their own steps; only HR delegates somebody else's.
    Time-boxed on purpose, and the original owner stays on every entry the
    delegate submits.
    """
    await _cycle(session, user, payload.cycle_id)
    from_user_id = payload.from_user_id or user.id
    if from_user_id != user.id:
        permissions.require_hr(user, detail="You can only delegate your own review steps.")
    if payload.effective_to < payload.effective_from:
        raise _unprocessable("The delegation ends before it starts.")
    if from_user_id == payload.to_user_id:
        raise _unprocessable("Delegating to yourself changes nothing.")

    people = {
        row.id: row
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(User).where(
                        User.id.in_([from_user_id, payload.to_user_id]),
                        User.is_active.is_(True),
                    ),
                    User,
                    user,
                )
            )
        ).all()
    }
    if from_user_id not in people or payload.to_user_id not in people:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    delegation = StageDelegation(
        tenant_id=user.tenant_id,
        cycle_id=payload.cycle_id,
        from_user_id=from_user_id,
        to_user_id=payload.to_user_id,
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
        reason=payload.reason,
    )
    session.add(delegation)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="stage_delegation",
        entity_id=delegation.id,
        summary=(
            f"Delegated review steps from {people[from_user_id].full_name} to "
            f"{people[payload.to_user_id].full_name} until {payload.effective_to}"
        ),
        changes={
            "from_user_id": str(from_user_id),
            "to_user_id": str(payload.to_user_id),
            "effective_to": str(payload.effective_to),
            "reason": payload.reason,
        },
        request=request,
    )
    return delegation


async def list_delegations(
    session: AsyncSession, user: User, *, cycle_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    stmt = tenant_scoped(select(StageDelegation), StageDelegation, user).order_by(
        StageDelegation.effective_from.desc()
    )
    if not permissions.is_hr(user):
        stmt = stmt.where(
            (StageDelegation.from_user_id == user.id) | (StageDelegation.to_user_id == user.id)
        )
    if cycle_id is not None:
        stmt = stmt.where(StageDelegation.cycle_id == cycle_id)
    rows = (await session.scalars(stmt)).all()
    user_ids = {r.from_user_id for r in rows} | {r.to_user_id for r in rows}
    names: dict[uuid.UUID, str] = {}
    if user_ids:
        names = {
            u.id: u.full_name
            for u in (
                await session.scalars(
                    tenant_scoped(select(User).where(User.id.in_(user_ids)), User, user)
                )
            ).all()
        }
    today = date.today()
    return [
        {
            "id": str(r.id),
            "cycle_id": str(r.cycle_id),
            "from_user_id": str(r.from_user_id),
            "from_name": names.get(r.from_user_id),
            "to_user_id": str(r.to_user_id),
            "to_name": names.get(r.to_user_id),
            "effective_from": r.effective_from.isoformat(),
            "effective_to": r.effective_to.isoformat(),
            "reason": r.reason,
            "active": r.active,
            "in_effect": r.active and r.effective_from <= today <= r.effective_to,
        }
        for r in rows
    ]


async def revoke_delegation(
    session: AsyncSession,
    user: User,
    delegation_id: uuid.UUID,
    *,
    request: Request | None = None,
) -> StageDelegation:
    delegation = await session.scalar(
        tenant_scoped(
            select(StageDelegation).where(StageDelegation.id == delegation_id),
            StageDelegation,
            user,
        )
    )
    if delegation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delegation not found")
    if delegation.from_user_id != user.id:
        permissions.require_hr(user, detail="Only the person who delegated, or HR, can end it.")
    if not delegation.active:
        raise _conflict("This delegation has already ended.")
    delegation.active = False
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.PERMISSION_CHANGE,
        entity_type="stage_delegation",
        entity_id=delegation.id,
        summary="Ended a review delegation",
        changes={"active": False},
        request=request,
    )
    return delegation
