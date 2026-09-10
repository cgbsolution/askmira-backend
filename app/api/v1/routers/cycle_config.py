"""Cycle configuration: assignment rules, activation, cloning, probation (slice 18)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.appraisal import AppraisalStage
from app.models.cycle_config import (
    AutoAdvanceBehaviour,
    ProbationOutcome,
    ProbationReview,
    TemplateAssignmentRule,
)
from app.models.employee import Employee
from app.models.user import UserRole
from app.services.appraisal import assignment
from app.services.appraisal import cycle_config as cycle_service

router = APIRouter(tags=["cycle-config"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)


# ── Contracts ───────────────────────────────────────────────────────────────


class RuleCreate(BaseModel):
    """One template assignment rule.

    `conditions` accepts only the keys the engine understands. An unrecognised
    key is refused rather than silently never matching — a rule that can never
    fire looks exactly like a rule that is not needed.
    """

    cycle_id: uuid.UUID
    priority: int = Field(ge=1, le=1000)
    template_id: uuid.UUID
    conditions: dict[str, Any] = Field(default_factory=dict)
    label: str | None = Field(default=None, max_length=128)


class RuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cycle_id: uuid.UUID
    priority: int
    template_id: uuid.UUID
    conditions: dict[str, Any]
    label: str | None


class StageWindowUpdate(BaseModel):
    open_date: date | None = None
    close_date: date | None = None
    due_date: date | None = None
    grace_days: int = Field(default=0, ge=0, le=90)
    # No default here on purpose: what happens when a window closes is a
    # decision, and a silent default is how a cycle ends up skipping stages
    # nobody agreed to skip.
    auto_advance: AutoAdvanceBehaviour


class CloneRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    period_start: date
    period_end: date


class CloseRequest(BaseModel):
    acknowledge_incomplete: bool = False


class ProbationCreate(BaseModel):
    employee_id: uuid.UUID
    probation_end_date: date
    review_due_date: date


class ProbationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    probation_end_date: date
    review_due_date: date
    outcome: ProbationOutcome | None
    outcome_recorded_at: datetime | None
    extended_to: date | None
    extends_review_id: uuid.UUID | None
    notes: str | None


class ProbationOutcomeRequest(BaseModel):
    outcome: ProbationOutcome
    new_probation_end_date: date | None = None
    notes: str | None = None


# ── Assignment rules ────────────────────────────────────────────────────────


@router.post(
    "/cycle-config/rules",
    response_model=RuleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_rule(
    payload: RuleCreate, user: CurrentUser, session: DbSession
) -> RuleRead:
    unknown = set(payload.conditions) - assignment.KNOWN_CONDITIONS
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"These conditions are not understood: {sorted(unknown)}. A rule with an "
                f"unrecognised condition can never match, which looks exactly like a "
                f"rule that is not needed. Available: "
                f"{sorted(assignment.KNOWN_CONDITIONS)}."
            ),
        )

    rule = TemplateAssignmentRule(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return RuleRead.model_validate(rule)


@router.get(
    "/cycle-config/rules",
    response_model=list[RuleRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_rules(
    cycle_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[RuleRead]:
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
    return [RuleRead.model_validate(row) for row in rows]


@router.get(
    "/cycle-config/cycles/{cycle_id}/assignment-preview",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def assignment_preview(
    cycle_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Who resolves to which template, before activating.

    The unresolved list is the one to read: those employees would get no
    appraisal at all.
    """
    cycle = await cycle_service._cycle(session, user, cycle_id)
    rules = await cycle_service._rules(session, user, cycle_id)
    population = await cycle_service._eligible_population(session, user, cycle)

    resolved, unresolved = assignment.resolve_all(
        population=[facts for _, facts in population],
        rules=rules,
        as_of=cycle.period_end,
    )
    return {
        "eligible": len(population),
        "resolved": len(resolved),
        "unresolved": [r.as_dict() for r in unresolved],
        "by_template": cycle_service._count_by_template(resolved),
    }


# ── Stage windows ───────────────────────────────────────────────────────────


@router.patch(
    "/cycle-config/stages/{stage_id}/window",
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def set_stage_window(
    stage_id: uuid.UUID,
    payload: StageWindowUpdate,
    user: CurrentUser,
    session: DbSession,
) -> dict[str, Any]:
    """When a stage opens and closes, and what happens if it does not finish."""
    stage = await session.scalar(
        tenant_scoped(
            select(AppraisalStage).where(AppraisalStage.id == stage_id),
            AppraisalStage,
            user,
        )
    )
    if stage is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stage not found")

    if (
        payload.open_date
        and payload.close_date
        and payload.close_date < payload.open_date
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The stage closes before it opens.",
        )

    stage.open_date = payload.open_date
    stage.close_date = payload.close_date
    stage.due_date = payload.due_date
    stage.grace_days = payload.grace_days
    stage.auto_advance = payload.auto_advance
    await session.commit()

    return {
        "stage_id": str(stage.id),
        "auto_advance": stage.auto_advance.value,
        "note": {
            AutoAdvanceBehaviour.ESCALATE: (
                "Unfinished appraisals stay on this stage and escalate. Nothing is "
                "skipped; somebody else is asked to do it."
            ),
            AutoAdvanceBehaviour.ADVANCE: (
                "Unfinished appraisals move on with this stage recorded as never "
                "filled in. The cycle completes; the record says the stage was skipped."
            ),
            AutoAdvanceBehaviour.HOLD: (
                "Unfinished appraisals stay put and wait for a human. Right if somebody "
                "will look; a stalled cycle if nobody does."
            ),
        }[stage.auto_advance],
    }


# ── Lifecycle ───────────────────────────────────────────────────────────────


@router.post(
    "/cycle-config/cycles/{cycle_id}/activate",
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def activate(
    cycle_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Validate the configuration and make the cycle live.

    Refuses if any eligible employee resolves to no template — at activation
    that is a configuration problem; at launch it is a person with no
    appraisal.
    """
    result = await cycle_service.activate_cycle(
        session, user, cycle_id=cycle_id, request=request
    )
    await session.commit()
    return result


@router.post(
    "/cycle-config/cycles/{cycle_id}/auto-advance",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def auto_advance(
    cycle_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Deal with appraisals sitting on a closed stage, per configuration."""
    result = await cycle_service.run_auto_advance(
        session, user, cycle_id=cycle_id, as_of=as_of, request=request
    )
    await session.commit()
    return result


@router.post(
    "/cycle-config/cycles/{cycle_id}/clone",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def clone(
    cycle_id: uuid.UUID,
    payload: CloneRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Copy a cycle's stages, rules and template into a new draft."""
    result = await cycle_service.clone_cycle(
        session,
        user,
        source_cycle_id=cycle_id,
        name=payload.name,
        period_start=payload.period_start,
        period_end=payload.period_end,
        request=request,
    )
    await session.commit()
    return result


@router.post(
    "/cycle-config/cycles/{cycle_id}/close",
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def close(
    cycle_id: uuid.UUID,
    payload: CloseRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Close the cycle.

    Blocked while an ENFORCED calibration policy is out of tolerance — that
    gate is not acknowledgeable, because the client chose the mode.
    """
    result = await cycle_service.close_cycle(
        session,
        user,
        cycle_id=cycle_id,
        acknowledge_incomplete=payload.acknowledge_incomplete,
        request=request,
    )
    await session.commit()
    return result


# ── Probation ───────────────────────────────────────────────────────────────


@router.post(
    "/cycle-config/probation-reviews",
    response_model=ProbationRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def create_probation_review(
    payload: ProbationCreate, user: CurrentUser, session: DbSession
) -> ProbationRead:
    """Schedule a probation review on one employee's own clock."""
    employee = await session.scalar(
        tenant_scoped(
            select(Employee).where(Employee.id == payload.employee_id), Employee, user
        )
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if payload.review_due_date > payload.probation_end_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "The review is due after probation ends. The conversation has to happen "
                "before the deadline, not after it."
            ),
        )

    review = ProbationReview(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(review)
    await session.commit()
    await session.refresh(review)
    return ProbationRead.model_validate(review)


@router.get(
    "/cycle-config/probation-reviews",
    response_model=list[ProbationRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_probation_reviews(
    user: CurrentUser,
    session: DbSession,
    due_before: date | None = None,
    pending_only: bool = True,
) -> list[ProbationRead]:
    """Reviews falling due, on each employee's own clock rather than a
    calendar cycle."""
    stmt = select(ProbationReview).order_by(ProbationReview.review_due_date)
    if pending_only:
        stmt = stmt.where(ProbationReview.outcome.is_(None))
    if due_before is not None:
        stmt = stmt.where(ProbationReview.review_due_date <= due_before)
    rows = (await session.scalars(tenant_scoped(stmt, ProbationReview, user))).all()
    return [ProbationRead.model_validate(row) for row in rows]


@router.post(
    "/cycle-config/probation-reviews/{review_id}/outcome",
    response_model=ProbationRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def record_outcome(
    review_id: uuid.UUID,
    payload: ProbationOutcomeRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ProbationRead:
    """Confirm, extend, or terminate.

    An extension schedules the next review automatically — an extension with
    no follow-up is how someone stays on probation indefinitely because the
    second conversation never got diarised.
    """
    review = await cycle_service.record_probation_outcome(
        session,
        user,
        review_id=review_id,
        outcome=payload.outcome,
        new_probation_end_date=payload.new_probation_end_date,
        notes=payload.notes,
        request=request,
    )
    await session.commit()
    await session.refresh(review)
    return ProbationRead.model_validate(review)
