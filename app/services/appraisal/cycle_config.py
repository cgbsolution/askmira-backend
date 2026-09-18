"""Cycle configuration: rules, activation, auto-advance, cloning, closure, probation.

Four decisions worth stating.

**Activation validates that every eligible employee resolves to a template,
then launches.** Checked here, when it is a configuration problem somebody fixes
in a minute — not later, when it is an employee with no appraisal who finds out
months afterwards that they were never reviewed. Each appraisal records the
template its person resolved to.

**Auto-advance on stage close is configured, never accidental.** A cycle that
stalls forever because one manager is on leave is a real failure mode, and so
is a cycle that silently skipped the manager review for forty people. Both are
choices; the configuration says which, and the skip is recorded either way.

**Cloning copies the configuration, not the people.** Cycle setup is annual and
painful; the stages, rules and template survive, and the appraisals do not.

**Closure is gated on the things that make a rating final** — mandatory stages
done, and calibration inside tolerance where the policy enforces it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.appraisal import (
    Appraisal,
    AppraisalCycle,
    AppraisalStage,
    AppraisalStatus,
    CycleStatus,
)
from app.models.audit import AuditAction
from app.models.calibration import CalibrationMode, CalibrationPolicy
from app.models.competency import AppraisalTemplate, CompetencyTemplate
from app.models.cycle_config import (
    AutoAdvanceBehaviour,
    ProbationOutcome,
    ProbationReview,
    TemplateAssignmentRule,
)
from app.models.employee import Employee, EmploymentStatus
from app.models.user import User
from app.schemas.performance import ProbationCreate, RuleCreate, StageWindowUpdate
from app.services import audit
from app.services.appraisal import assignment, calibration_service, operations
from app.services.appraisal import service as appraisal_service
from app.services.performance import notices

__all__ = [
    "activate_cycle",
    "assignment_preview",
    "clone_cycle",
    "close_cycle",
    "create_probation_review",
    "create_rule",
    "delete_rule",
    "launch",
    "list_probation_reviews",
    "list_rules",
    "record_probation_outcome",
    "run_auto_advance",
    "schedule_due_probation_reviews",
    "set_stage_window",
]


def _unprocessable(detail: Any) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


async def _cycle(session: AsyncSession, user: User, cycle_id: uuid.UUID) -> AppraisalCycle:
    cycle = await session.scalar(
        tenant_scoped(
            select(AppraisalCycle).where(AppraisalCycle.id == cycle_id),
            AppraisalCycle,
            user,
        )
    )
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found")
    return cycle


async def _rules(session: AsyncSession, user: User, cycle_id: uuid.UUID) -> list[assignment.Rule]:
    rows = (
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
    return [
        assignment.Rule(
            rule_id=str(row.id),
            priority=row.priority,
            template_id=str(row.template_id),
            conditions=row.conditions or {},
            label=row.label or "",
        )
        for row in rows
    ]


async def _eligible_population(
    session: AsyncSession, user: User, cycle: AppraisalCycle
) -> list[tuple[Employee, assignment.EmployeeFacts]]:
    employees = (
        await session.scalars(
            tenant_scoped(select(Employee).where(Employee.deleted_at.is_(None)), Employee, user)
        )
    ).all()

    out = []
    for employee in employees:
        eligible, _ = appraisal_service.is_eligible(
            employee, cycle.eligibility_rule, cycle.period_end
        )
        if not eligible:
            continue
        out.append(
            (
                employee,
                assignment.EmployeeFacts(
                    employee_id=str(employee.id),
                    employee_code=employee.employee_code,
                    grade=employee.grade,
                    department_id=str(employee.department_id) if employee.department_id else None,
                    location_id=str(employee.location_id) if employee.location_id else None,
                    employment_type=(
                        employee.employment_type.value if employee.employment_type else None
                    ),
                    date_of_joining=employee.date_of_joining,
                ),
            )
        )
    return out


def _count_by_template(results: list[assignment.AssignmentResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        if result.template_id:
            counts[result.template_id] = counts.get(result.template_id, 0) + 1
    return counts


# ═══════════════════════════════════════════════════════════════════════════
# Assignment rules
# ═══════════════════════════════════════════════════════════════════════════


async def create_rule(
    session: AsyncSession, user: User, payload: RuleCreate, *, request: Request | None = None
) -> TemplateAssignmentRule:
    """One template assignment rule, on a draft cycle.

    An unrecognised condition is refused rather than silently never matching —
    a rule that can never fire looks exactly like a rule that is not needed.
    """
    permissions.require_hr(user, detail="Assignment rules are configured by HR.")
    unknown = set(payload.conditions) - assignment.KNOWN_CONDITIONS
    if unknown:
        raise _unprocessable(
            f"These conditions are not understood: {sorted(unknown)}. A rule with an "
            "unrecognised condition can never match, which looks exactly like a rule that is "
            f"not needed. Available: {sorted(assignment.KNOWN_CONDITIONS)}."
        )
    cycle = await _cycle(session, user, payload.cycle_id)
    if cycle.status is not CycleStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Rules change only while the cycle is a draft; people are already assigned.",
        )
    template = await session.scalar(
        tenant_scoped(
            select(CompetencyTemplate.id).where(CompetencyTemplate.id == payload.template_id),
            CompetencyTemplate,
            user,
        )
    )
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")
    clash = await session.scalar(
        tenant_scoped(
            select(TemplateAssignmentRule.id).where(
                TemplateAssignmentRule.cycle_id == payload.cycle_id,
                TemplateAssignmentRule.priority == payload.priority,
            ),
            TemplateAssignmentRule,
            user,
        )
    )
    if clash is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Another rule already uses priority {payload.priority}. Two rules at one "
                "priority would make the winner depend on row order."
            ),
        )

    rule = TemplateAssignmentRule(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(rule)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="template_assignment_rule",
        entity_id=rule.id,
        summary=(
            f"Added rule '{payload.label or 'unnamed'}' at priority {payload.priority}"
            + (" (default)" if not payload.conditions else "")
        ),
        changes=payload.model_dump(mode="json"),
        request=request,
    )
    return rule


async def delete_rule(
    session: AsyncSession, user: User, rule_id: uuid.UUID, *, request: Request | None = None
) -> None:
    permissions.require_hr(user, detail="Assignment rules are configured by HR.")
    rule = await session.scalar(
        tenant_scoped(
            select(TemplateAssignmentRule).where(TemplateAssignmentRule.id == rule_id),
            TemplateAssignmentRule,
            user,
        )
    )
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")
    cycle = await _cycle(session, user, rule.cycle_id)
    if cycle.status is not CycleStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Rules change only while the cycle is a draft.",
        )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.DELETE,
        entity_type="template_assignment_rule",
        entity_id=rule.id,
        summary=f"Removed rule '{rule.label or 'unnamed'}' at priority {rule.priority}",
        changes={"priority": rule.priority, "conditions": rule.conditions},
        request=request,
    )
    await session.delete(rule)
    await session.flush()


async def list_rules(
    session: AsyncSession, user: User, cycle_id: uuid.UUID
) -> list[TemplateAssignmentRule]:
    await _cycle(session, user, cycle_id)
    return list(
        (
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
    )


async def assignment_preview(
    session: AsyncSession, user: User, cycle_id: uuid.UUID
) -> dict[str, Any]:
    """Who resolves to which template, before activating."""
    cycle = await _cycle(session, user, cycle_id)
    rules = await _rules(session, user, cycle_id)
    population = await _eligible_population(session, user, cycle)
    resolved, unresolved = assignment.resolve_all(
        population=[facts for _, facts in population],
        rules=rules,
        as_of=cycle.period_end,
    )
    return {
        "eligible": len(population),
        "resolved": len(resolved),
        "unresolved": [r.as_dict() for r in unresolved],
        "by_template": _count_by_template(resolved),
        "rules": len(rules),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Stage windows
# ═══════════════════════════════════════════════════════════════════════════

AUTO_ADVANCE_NOTES = {
    AutoAdvanceBehaviour.ESCALATE: (
        "Unfinished appraisals stay on this stage and escalate. Nothing is skipped; "
        "somebody else is asked to do it."
    ),
    AutoAdvanceBehaviour.ADVANCE: (
        "Unfinished appraisals move on with this stage recorded as never filled in. The "
        "cycle completes; the record says the stage was skipped."
    ),
    AutoAdvanceBehaviour.HOLD: (
        "Unfinished appraisals stay put and wait for a human. Right if somebody will look; "
        "a stalled cycle if nobody does."
    ),
}


async def set_stage_window(
    session: AsyncSession,
    user: User,
    stage_id: uuid.UUID,
    payload: StageWindowUpdate,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """When a stage opens and closes, and what happens if it does not finish."""
    permissions.require_hr(user, detail="Stage windows are configured by HR.")
    stage = await session.scalar(
        tenant_scoped(
            select(AppraisalStage).where(AppraisalStage.id == stage_id), AppraisalStage, user
        )
    )
    if stage is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stage not found")
    if payload.open_date and payload.close_date and payload.close_date < payload.open_date:
        raise _unprocessable("The stage closes before it opens.")

    before = {
        "open_date": str(stage.open_date),
        "close_date": str(stage.close_date),
        "due_date": str(stage.due_date),
        "grace_days": stage.grace_days,
        "auto_advance": stage.auto_advance.value,
    }
    stage.open_date = payload.open_date
    stage.close_date = payload.close_date
    stage.due_date = payload.due_date
    stage.grace_days = payload.grace_days
    stage.auto_advance = payload.auto_advance
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="appraisal_stage",
        entity_id=stage.id,
        summary=f"Set the window for '{stage.label}' ({payload.auto_advance.value} on close)",
        changes={"before": before, "after": payload.model_dump(mode="json")},
        request=request,
    )
    return {
        "stage_id": str(stage.id),
        "auto_advance": stage.auto_advance.value,
        "note": AUTO_ADVANCE_NOTES[stage.auto_advance],
    }


# ═══════════════════════════════════════════════════════════════════════════
# Activation and launch
# ═══════════════════════════════════════════════════════════════════════════


async def activate_cycle(
    session: AsyncSession,
    user: User,
    *,
    cycle_id: uuid.UUID,
    request: Request | None = None,
) -> dict[str, Any]:
    """Validate the assignment rules, then launch with each person's template.

    Refuses if any eligible employee cannot be resolved to a template. That is
    the whole point of having an activation step: afterwards the same problem is
    a person with no appraisal, discovered when they ask.
    """
    cycle = await _cycle(session, user, cycle_id)
    if cycle.status is not CycleStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This cycle is already '{cycle.status.value}'.",
        )

    stages = (
        await session.scalars(
            tenant_scoped(
                select(AppraisalStage).where(AppraisalStage.cycle_id == cycle_id),
                AppraisalStage,
                user,
            )
        )
    ).all()
    if not stages:
        raise _unprocessable("This cycle has no stages, so there is no chain to run.")

    rules = await _rules(session, user, cycle_id)
    try:
        assignment.validate_rule_set(rules)
    except assignment.AssignmentError as exc:
        raise _unprocessable(str(exc)) from exc

    population = await _eligible_population(session, user, cycle)
    resolved, unresolved = assignment.resolve_all(
        population=[facts for _, facts in population],
        rules=rules,
        as_of=cycle.period_end,
    )

    if unresolved:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": (
                    f"{len(unresolved)} eligible employee(s) do not resolve to a "
                    "template. They would get no appraisal at all, and would not find "
                    "out until they asked why they were not reviewed."
                ),
                "unresolved": [r.as_dict() for r in unresolved[:20]],
            },
        )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE,
        entity_type="appraisal_cycle",
        entity_id=cycle.id,
        summary=(
            f"Activated '{cycle.name}': {len(resolved)} employee(s) resolved to a "
            f"template across {len(rules)} rule(s)"
        ),
        changes={"resolved": len(resolved), "rules": len(rules)},
        request=request,
    )
    launched = await appraisal_service.launch_cycle(
        session,
        user,
        cycle_id=cycle.id,
        assignments={r.employee_id: r.template_id for r in resolved if r.template_id},
        request=request,
    )
    return {
        **launched,
        "resolved": len(resolved),
        "by_template": _count_by_template(resolved),
    }


async def launch(
    session: AsyncSession, user: User, *, cycle_id: uuid.UUID, request: Request | None = None
) -> dict[str, Any]:
    """The one launch button: through the rules where the cycle has any."""
    permissions.require_hr(user, detail="Cycles are launched by HR.")
    if await _rules(session, user, cycle_id):
        return await activate_cycle(session, user, cycle_id=cycle_id, request=request)
    return await appraisal_service.launch_cycle(session, user, cycle_id=cycle_id, request=request)


# ═══════════════════════════════════════════════════════════════════════════
# Auto-advance
# ═══════════════════════════════════════════════════════════════════════════


async def run_auto_advance(
    session: AsyncSession,
    user: User,
    *,
    cycle_id: uuid.UUID,
    as_of: date | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Deal with appraisals sitting on a stage whose window has closed.

    What happens is configured per stage, and both options are real choices:

    - ESCALATE keeps the appraisal on the stage and raises it to the next level
      up. Nothing is skipped; somebody else is asked to do it.
    - ADVANCE moves it on with the stage recorded as skipped. The cycle
      finishes; the record says a stage was never filled in.
    - HOLD does nothing, which is the right answer for a tenant that wants a
      human to decide every time.
    """
    cycle = await _cycle(session, user, cycle_id)
    today = as_of or date.today()

    stages = (
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
    overdue_stages = [
        s
        for s in stages
        if s.close_date is not None and s.close_date + timedelta(days=s.grace_days) < today
    ]
    if not overdue_stages:
        return {"advanced": 0, "escalated": 0, "held": 0, "note": "No stage windows have closed."}

    definitions = appraisal_service._definitions(list(stages))
    advanced = escalated = held = 0
    actions: list[dict[str, str]] = []

    for stage in overdue_stages:
        stuck = (
            await session.execute(
                tenant_scoped(
                    select(Appraisal, Employee)
                    .join(Employee, Employee.id == Appraisal.employee_id)
                    .where(
                        Appraisal.cycle_id == cycle_id,
                        Appraisal.current_stage_id == stage.id,
                        Appraisal.status.notin_(
                            [
                                AppraisalStatus.COMPLETED,
                                AppraisalStatus.RELEASED,
                                AppraisalStatus.ACKNOWLEDGED,
                            ]
                        ),
                    ),
                    Appraisal,
                    user,
                )
            )
        ).all()

        for appraisal, employee in stuck:
            if stage.auto_advance is AutoAdvanceBehaviour.HOLD:
                held += 1
                continue

            if stage.auto_advance is AutoAdvanceBehaviour.ESCALATE:
                escalated += 1
                if appraisal.escalated_at is None:
                    await operations.escalate_appraisal(
                        session,
                        user,
                        cycle=cycle,
                        appraisal=appraisal,
                        stage=stage,
                        employee=employee,
                        days_overdue=(today - (stage.close_date or today)).days,
                    )
                actions.append(
                    {
                        "appraisal_id": str(appraisal.id),
                        "stage": stage.label,
                        "action": "escalated",
                        "reason": (
                            f"'{stage.label}' closed on {stage.close_date} with no "
                            "submission. Escalated rather than skipped — the stage still "
                            "needs doing, by somebody."
                        ),
                    }
                )
                continue

            move = appraisal_service.progression.next_stage(
                stages=definitions,
                current_stage_id=str(stage.id),
                completed_stage_ids=set(),
            )
            appraisal.current_stage_id = uuid.UUID(move.to_stage_id) if move.to_stage_id else None
            if move.is_complete:
                appraisal.status = AppraisalStatus.COMPLETED
                appraisal.completed_at = datetime.now(UTC)
            advanced += 1
            actions.append(
                {
                    "appraisal_id": str(appraisal.id),
                    "stage": stage.label,
                    "action": "advanced_with_stage_skipped",
                    "reason": (
                        f"'{stage.label}' closed on {stage.close_date} with no "
                        "submission and is configured to advance. The stage is recorded "
                        "as never filled in."
                    ),
                }
            )

    await session.flush()
    if actions:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            action=AuditAction.UPDATE,
            entity_type="appraisal_cycle",
            entity_id=cycle.id,
            summary=(
                f"Auto-advance on '{cycle.name}': {advanced} advanced with a stage "
                f"skipped, {escalated} escalated, {held} held"
            ),
            changes={"advanced": advanced, "escalated": escalated, "held": held},
            request=request,
        )

    return {
        "advanced": advanced,
        "escalated": escalated,
        "held": held,
        "actions": actions,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Cloning
# ═══════════════════════════════════════════════════════════════════════════


async def clone_cycle(
    session: AsyncSession,
    user: User,
    *,
    source_cycle_id: uuid.UUID,
    name: str,
    period_start: date,
    period_end: date,
    request: Request | None = None,
) -> dict[str, Any]:
    """Copy a cycle's configuration into a new draft.

    Stages, assignment rules, the appraisal template and the calibration policy
    come across. The appraisals do not — a clone is a new review of the same
    shape, not a copy of last year's answers.
    """
    source = await _cycle(session, user, source_cycle_id)
    if period_end < period_start:
        raise _unprocessable("The review period ends before it starts.")

    clone = AppraisalCycle(
        tenant_id=user.tenant_id,
        name=name,
        cycle_type=source.cycle_type,
        status=CycleStatus.DRAFT,
        period_start=period_start,
        period_end=period_end,
        eligibility_rule=dict(source.eligibility_rule or {}),
        escalation_after_days=source.escalation_after_days,
    )
    session.add(clone)
    await session.flush()

    stages = (
        await session.scalars(
            tenant_scoped(
                select(AppraisalStage)
                .where(AppraisalStage.cycle_id == source_cycle_id)
                .order_by(AppraisalStage.sequence),
                AppraisalStage,
                user,
            )
        )
    ).all()
    for stage in stages:
        copy = AppraisalStage(
            tenant_id=user.tenant_id,
            cycle_id=clone.id,
            sequence=stage.sequence,
            role=stage.role,
            label=stage.label,
            # Dates are deliberately not copied: last year's due dates on this
            # year's cycle would all be in the past, and auto-advance would
            # fire on everything the moment it was activated.
            due_date=None,
            open_date=None,
            close_date=None,
            is_mandatory=stage.is_mandatory,
            can_reopen=stage.can_reopen,
            sees_previous_entries=stage.sees_previous_entries,
            visible_to_employee_before_release=stage.visible_to_employee_before_release,
            auto_advance=stage.auto_advance,
            grace_days=stage.grace_days,
        )
        session.add(copy)
    await session.flush()

    rules = (
        await session.scalars(
            tenant_scoped(
                select(TemplateAssignmentRule).where(
                    TemplateAssignmentRule.cycle_id == source_cycle_id
                ),
                TemplateAssignmentRule,
                user,
            )
        )
    ).all()
    for rule in rules:
        session.add(
            TemplateAssignmentRule(
                tenant_id=user.tenant_id,
                cycle_id=clone.id,
                priority=rule.priority,
                template_id=rule.template_id,
                conditions=dict(rule.conditions or {}),
                label=rule.label,
            )
        )

    template = await session.scalar(
        tenant_scoped(
            select(AppraisalTemplate).where(AppraisalTemplate.cycle_id == source_cycle_id),
            AppraisalTemplate,
            user,
        )
    )
    if template is not None:
        session.add(
            AppraisalTemplate(
                tenant_id=user.tenant_id,
                cycle_id=clone.id,
                rating_scale_id=template.rating_scale_id,
                competency_template_id=template.competency_template_id,
                goal_weight=template.goal_weight,
                competency_weight=template.competency_weight,
                values_weight=template.values_weight,
                score_precision=template.score_precision,
                score_bands=list(template.score_bands or []),
            )
        )

    policy_copied = await calibration_service.clone_policy(
        session, user, source_cycle_id=source_cycle_id, target_cycle_id=clone.id
    )

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="appraisal_cycle",
        entity_id=clone.id,
        summary=(
            f"Cloned '{source.name}' into '{name}': {len(stages)} stage(s), "
            f"{len(rules)} rule(s), template {'copied' if template else 'absent'}"
        ),
        changes={
            "source_cycle_id": str(source_cycle_id),
            "stages": len(stages),
            "rules": len(rules),
            "calibration_policy_copied": policy_copied,
        },
        request=request,
    )
    return {
        "cycle_id": str(clone.id),
        "stages": len(stages),
        "rules": len(rules),
        "template_copied": template is not None,
        "calibration_policy_copied": policy_copied,
        "note": (
            "Stage dates were not copied — last year's dates on this year's cycle "
            "would all be in the past, and auto-advance would fire on everything."
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Closure
# ═══════════════════════════════════════════════════════════════════════════


async def close_cycle(
    session: AsyncSession,
    user: User,
    *,
    cycle_id: uuid.UUID,
    acknowledge_incomplete: bool = False,
    release_completed: bool = False,
    request: Request | None = None,
) -> dict[str, Any]:
    """Close the cycle, gated on the things that make a rating final.

    Two gates, and the calibration one is not acknowledgeable: under ENFORCED
    the client has said a distribution outside tolerance is not acceptable, and
    a checkbox that overrides their own policy makes the policy decorative.
    With `release_completed`, closure publishes every completed rating to its
    employee.
    """
    cycle = await _cycle(session, user, cycle_id)
    if cycle.status is CycleStatus.CLOSED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This cycle is already closed."
        )

    incomplete = (
        await session.scalars(
            tenant_scoped(
                select(Appraisal).where(
                    Appraisal.cycle_id == cycle_id,
                    Appraisal.status.notin_(
                        [
                            AppraisalStatus.COMPLETED,
                            AppraisalStatus.RELEASED,
                            AppraisalStatus.ACKNOWLEDGED,
                        ]
                    ),
                ),
                Appraisal,
                user,
            )
        )
    ).all()

    # ── The calibration gate ────────────────────────────────────────────────
    policy = await session.scalar(
        tenant_scoped(
            select(CalibrationPolicy).where(CalibrationPolicy.cycle_id == cycle_id),
            CalibrationPolicy,
            user,
        )
    )
    blockers: list[dict[str, Any]] = []
    if policy is not None and policy.mode is CalibrationMode.ENFORCED:
        distribution = await calibration_service.cycle_distribution(session, user, cycle_id)
        blockers = distribution["blockers"]

    if blockers:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    f"{len(blockers)} scope(s) are outside the enforced distribution "
                    "tolerance. This cycle's policy is ENFORCED, so closure is blocked "
                    "until they are calibrated — that is what the client chose the mode "
                    "for."
                ),
                "blockers": blockers,
            },
        )

    if incomplete and not acknowledge_incomplete:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    f"{len(incomplete)} appraisal(s) have not completed their stages. "
                    "Closing now leaves them permanently unfinished; acknowledge to "
                    "proceed anyway."
                ),
                "incomplete_count": len(incomplete),
            },
        )

    released: list[Appraisal] = []
    if release_completed:
        released = list(
            (
                await session.scalars(
                    tenant_scoped(
                        select(Appraisal).where(
                            Appraisal.cycle_id == cycle_id,
                            Appraisal.status == AppraisalStatus.COMPLETED,
                        ),
                        Appraisal,
                        user,
                    )
                )
            ).all()
        )
        now = datetime.now(UTC)
        for appraisal in released:
            appraisal.status = AppraisalStatus.RELEASED
            appraisal.released_at = now

    cycle.status = CycleStatus.CLOSED
    cycle.closed_at = datetime.now(UTC)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE,
        entity_type="appraisal_cycle",
        entity_id=cycle.id,
        summary=(
            f"Closed '{cycle.name}'"
            + (f", acknowledging {len(incomplete)} unfinished appraisal(s)" if incomplete else "")
            + (f", releasing {len(released)} rating(s)" if released else "")
        ),
        changes={"incomplete_acknowledged": len(incomplete), "released": len(released)},
        request=request,
    )
    if released:
        recipients = await notices.user_ids_for_employees(
            session, user, [a.employee_id for a in released]
        )
        await notices.send(
            session,
            tenant_id=user.tenant_id,
            event_key="performance.review.released",
            recipient_user_ids=recipients.values(),
            payload={"cycle_name": cycle.name},
        )
    return {
        "cycle_id": str(cycle.id),
        "closed_at": cycle.closed_at.isoformat(),
        "incomplete_acknowledged": len(incomplete),
        "released": len(released),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Probation
# ═══════════════════════════════════════════════════════════════════════════


async def _employee(session: AsyncSession, user: User, employee_id: uuid.UUID) -> Employee:
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    return employee


async def _notify_manager_of_probation(
    session: AsyncSession, user: User, employee: Employee, review: ProbationReview
) -> None:
    if employee.manager_id is None:
        return
    recipients = await notices.user_ids_for_employees(session, user, [employee.manager_id])
    await notices.send(
        session,
        tenant_id=user.tenant_id,
        event_key="performance.probation.due",
        recipient_user_ids=recipients.values(),
        payload={
            "employee_name": employee.full_name,
            "probation_end_date": review.probation_end_date.isoformat(),
            "review_due_date": review.review_due_date.isoformat(),
        },
    )


async def create_probation_review(
    session: AsyncSession, user: User, payload: ProbationCreate, *, request: Request | None = None
) -> ProbationReview:
    """Schedule a probation review on one employee's own clock."""
    employee = await _employee(session, user, payload.employee_id)
    if employee.id == user.employee_id:
        raise permissions.forbidden("You cannot schedule your own probation review.")
    if payload.review_due_date > payload.probation_end_date:
        raise _unprocessable(
            "The review is due after probation ends. The conversation has to happen "
            "before the deadline, not after it."
        )
    review = ProbationReview(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(review)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="probation_review",
        entity_id=review.id,
        summary=f"Scheduled a probation review for {employee.full_name}",
        changes=payload.model_dump(mode="json"),
        request=request,
    )
    await _notify_manager_of_probation(session, user, employee, review)
    return review


async def list_probation_reviews(
    session: AsyncSession,
    user: User,
    *,
    due_before: date | None = None,
    pending_only: bool = True,
) -> list[dict[str, Any]]:
    """Reviews falling due, on each employee's own clock rather than a calendar cycle."""
    stmt = tenant_scoped(
        select(ProbationReview, Employee).join(
            Employee, Employee.id == ProbationReview.employee_id
        ),
        ProbationReview,
        user,
    ).order_by(ProbationReview.review_due_date)
    if pending_only:
        stmt = stmt.where(ProbationReview.outcome.is_(None))
    if due_before is not None:
        stmt = stmt.where(ProbationReview.review_due_date <= due_before)
    return [
        {
            "id": review.id,
            "employee_id": review.employee_id,
            "employee_name": employee.full_name,
            "probation_end_date": review.probation_end_date,
            "review_due_date": review.review_due_date,
            "outcome": review.outcome,
            "outcome_recorded_at": review.outcome_recorded_at,
            "extended_to": review.extended_to,
            "extends_review_id": review.extends_review_id,
            "notes": review.notes,
        }
        for review, employee in (await session.execute(stmt)).all()
    ]


async def schedule_due_probation_reviews(
    session: AsyncSession,
    user: User,
    *,
    as_of: date | None = None,
    horizon_days: int = 30,
    lead_days: int = 7,
    request: Request | None = None,
) -> dict[str, Any]:
    """Create a review for every probation ending soon that has none pending.

    Triggered from the probation end date on the employee record — slice 18's
    "per employee, not on the calendar". Idempotent: an employee with a pending
    review for that end date is skipped.
    """
    permissions.require_hr(user, detail="Probation reviews are scheduled by HR.")
    today = as_of or date.today()
    horizon = today + timedelta(days=horizon_days)
    employees = (
        await session.scalars(
            tenant_scoped(
                select(Employee).where(
                    Employee.deleted_at.is_(None),
                    Employee.status.notin_([EmploymentStatus.EXITED, EmploymentStatus.PRE_JOINING]),
                    Employee.probation_end_date.isnot(None),
                    Employee.probation_end_date >= today,
                    Employee.probation_end_date <= horizon,
                ),
                Employee,
                user,
            )
        )
    ).all()
    pending = {
        (employee_id, end)
        for employee_id, end in (
            await session.execute(
                tenant_scoped(
                    select(ProbationReview.employee_id, ProbationReview.probation_end_date).where(
                        ProbationReview.outcome.is_(None)
                    ),
                    ProbationReview,
                    user,
                )
            )
        ).all()
    }

    scheduled: list[str] = []
    for employee in employees:
        if employee.probation_end_date is None:
            continue
        if (employee.id, employee.probation_end_date) in pending:
            continue
        review = ProbationReview(
            tenant_id=user.tenant_id,
            employee_id=employee.id,
            probation_end_date=employee.probation_end_date,
            review_due_date=max(today, employee.probation_end_date - timedelta(days=lead_days)),
        )
        session.add(review)
        await session.flush()
        await _notify_manager_of_probation(session, user, employee, review)
        scheduled.append(employee.employee_code)

    if scheduled:
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            actor=user,
            action=AuditAction.CREATE,
            entity_type="probation_review",
            summary=(
                f"Scheduled {len(scheduled)} probation review(s) for probations ending by "
                f"{horizon.isoformat()}"
            ),
            changes={"employees": scheduled, "horizon": horizon.isoformat()},
            request=request,
        )
    return {"scheduled": len(scheduled), "employees": scheduled, "horizon": horizon.isoformat()}


async def record_probation_outcome(
    session: AsyncSession,
    user: User,
    *,
    review_id: uuid.UUID,
    outcome: ProbationOutcome,
    new_probation_end_date: date | None = None,
    notes: str | None = None,
    request: Request | None = None,
) -> ProbationReview:
    """Confirm, extend, or terminate.

    EXTEND schedules the next review automatically. A probation extension with
    no follow-up review is how someone stays on probation indefinitely because
    the second conversation never got diarised.
    """
    review = await session.scalar(
        tenant_scoped(
            select(ProbationReview).where(ProbationReview.id == review_id),
            ProbationReview,
            user,
        )
    )
    if review is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Probation review not found"
        )
    if review.employee_id == user.employee_id:
        raise permissions.forbidden("You cannot record the outcome of your own probation.")
    if review.outcome is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This review already concluded as '{review.outcome.value}'.",
        )

    if outcome is ProbationOutcome.EXTEND and new_probation_end_date is None:
        raise _unprocessable(
            "An extension needs a new probation end date. Without one the employee "
            "stays on probation with nothing scheduled to end it."
        )
    if (
        outcome is ProbationOutcome.EXTEND
        and new_probation_end_date is not None
        and new_probation_end_date <= review.probation_end_date
    ):
        raise _unprocessable("An extension has to end after the current probation end date.")

    review.outcome = outcome
    review.outcome_recorded_at = datetime.now(UTC)
    review.outcome_recorded_by_user_id = user.id
    review.notes = notes

    follow_up: ProbationReview | None = None
    if outcome is ProbationOutcome.EXTEND and new_probation_end_date:
        review.extended_to = new_probation_end_date
        follow_up = ProbationReview(
            tenant_id=user.tenant_id,
            employee_id=review.employee_id,
            probation_end_date=new_probation_end_date,
            # Scheduled ahead of the end date so the conversation happens
            # before the deadline rather than after it.
            review_due_date=new_probation_end_date,
            extends_review_id=review.id,
        )
        session.add(follow_up)

    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == review.employee_id), Employee, user)
    )
    if employee is not None:
        if outcome is ProbationOutcome.CONFIRM:
            employee.status = EmploymentStatus.ACTIVE
            employee.probation_end_date = review.probation_end_date
        elif outcome is ProbationOutcome.EXTEND and new_probation_end_date:
            employee.probation_end_date = new_probation_end_date

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.APPROVE,
        entity_type="probation_review",
        entity_id=review.id,
        summary=(
            f"Probation outcome '{outcome.value}'"
            + (
                f", extended to {new_probation_end_date} with a follow-up review scheduled"
                if follow_up is not None
                else ""
            )
        ),
        changes={
            "outcome": outcome.value,
            "new_probation_end_date": (
                str(new_probation_end_date) if new_probation_end_date else None
            ),
            "follow_up_review_id": str(follow_up.id) if follow_up else None,
        },
        request=request,
    )
    if follow_up is not None and employee is not None:
        await _notify_manager_of_probation(session, user, employee, follow_up)
    return review
