"""PIPs, increment cycles and promotions (slice 35).

Three endpoints carry the weight:

- `POST /performance/pips/{id}/check-ins` requires the employee's own account or
  a reasoned record that they declined. Never asking is refused, because a trail
  of manager observations with no employee response is one side of a
  conversation.
- `POST /performance/recommendations` returns the **budget headroom and the
  equity comparison in the same response** as the recommendation. A parity view
  that arrives in a report next March cannot change the number being chosen now.
- `POST /performance/cycles/{id}/release-letters` refuses before the controlled
  date and while approvals are outstanding.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.performance_flow import EmployeeInputStateEnum, OverageModeEnum
from app.models.user import UserRole
from app.services.performance import service as performance

router = APIRouter(tags=["performance-flow"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)


# ── Contracts ───────────────────────────────────────────────────────────────


class MilestoneIn(BaseModel):
    description: str = Field(min_length=1, max_length=2000)
    target_date: date
    #: Required. "Improve communication" is not a standard anybody can meet or
    #: fail, and a plan built from them cannot be defended.
    measurable_criteria: str = Field(min_length=1, max_length=2000)


class PipIn(BaseModel):
    employee_id: uuid.UUID
    start_date: date
    end_date: date
    reason: str = Field(min_length=1, max_length=4000)
    expectations: str = Field(min_length=1, max_length=8000)
    #: Required. An improvement plan the employer contributed nothing to is a
    #: notice period with paperwork.
    support_provided: str = Field(min_length=1, max_length=8000)
    milestones: list[MilestoneIn] = Field(min_length=1)
    appraisal_id: uuid.UUID | None = None


class CheckInIn(BaseModel):
    scheduled_for: date
    held_on: date
    manager_comments: str = Field(min_length=1, max_length=8000)
    #: `not_asked` is refused by the engine. `declined` needs a reason.
    employee_input_state: EmployeeInputStateEnum
    employee_comments: str = Field(default="", max_length=8000)
    decline_reason: str = Field(default="", max_length=2000)
    milestone_id: uuid.UUID | None = None


class ClosePipIn(BaseModel):
    outcome: str = Field(pattern="^(successful|extended|unsuccessful|abandoned)$")
    today: date
    reason: str = Field(default="", max_length=4000)
    new_end_date: date | None = None
    #: Proceeding to an exit on a plan with gaps. A deliberate act, recorded.
    acknowledge_weak_trail: bool = False


class BandIn(BaseModel):
    rating_label: str = Field(min_length=1, max_length=64)
    min_percentage: Decimal = Field(ge=0, le=200)
    max_percentage: Decimal = Field(ge=0, le=200)
    recommended_percentage: Decimal = Field(ge=0, le=200)


class CycleIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    effective_from: date
    #: The annual cost of the *rises*, not the payroll.
    budget_total: Decimal = Field(ge=0)
    bands: list[BandIn] = Field(min_length=1)
    budget_by_department: dict[str, Decimal] = Field(default_factory=dict)
    overage_mode: OverageModeEnum = OverageModeEnum.ESCALATE
    letter_release_on: date | None = None
    linked_appraisal_cycle_id: uuid.UUID | None = None


class RecommendationIn(BaseModel):
    cycle_id: uuid.UUID
    employee_id: uuid.UUID
    rating_label: str = Field(min_length=1, max_length=64)
    current_ctc: Decimal = Field(gt=0)
    percentage: Decimal = Field(ge=0, le=200)
    justification: str | None = Field(default=None, max_length=4000)
    promotion_to_grade: str | None = Field(default=None, max_length=32)
    band_midpoint: Decimal | None = Field(default=None, gt=0)


class DecisionIn(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=2000)


class PromotionIn(BaseModel):
    recommendation_id: uuid.UUID
    to_position_id: uuid.UUID | None = None
    effective_from: date
    today: date


class ReleaseIn(BaseModel):
    today: date
    #: A cycle occasionally has one genuinely stuck approval. A deliberate act
    #: with the outstanding names recorded, not a default.
    force_partial: bool = False


# ── Posture ─────────────────────────────────────────────────────────────────


@router.get("/performance/posture")
async def posture(user: CurrentUser) -> dict[str, Any]:
    """What this slice enforces, and what it does not."""
    return performance.performance_posture()


# ── PIP ─────────────────────────────────────────────────────────────────────


@router.post("/performance/pips", status_code=status.HTTP_201_CREATED)
async def open_pip(
    body: PipIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Open a plan, refusing the shapes that cannot be defended later."""
    row = await performance.open_pip(
        session,
        user,
        employee_id=body.employee_id,
        start_date=body.start_date,
        end_date=body.end_date,
        reason=body.reason,
        expectations=body.expectations,
        support_provided=body.support_provided,
        milestones=[m.model_dump() for m in body.milestones],
        appraisal_id=body.appraisal_id,
        request=request,
    )
    return {
        "pip_id": str(row.id),
        "status": row.status.value,
        "start_date": row.start_date.isoformat(),
        "end_date": row.end_date.isoformat(),
    }


@router.post("/performance/pips/{pip_id}/check-ins", status_code=201)
async def record_check_in(
    pip_id: uuid.UUID,
    body: CheckInIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Record a conversation, with both sides of it.

    Returns 400 where the employee was never asked, or declined with no reason
    recorded. Their contemporaneous account is the part with evidential value.
    """
    row = await performance.record_check_in(
        session,
        user,
        pip_id=pip_id,
        scheduled_for=body.scheduled_for,
        held_on=body.held_on,
        manager_comments=body.manager_comments,
        employee_input_state=body.employee_input_state,
        employee_comments=body.employee_comments,
        decline_reason=body.decline_reason,
        milestone_id=body.milestone_id,
        request=request,
    )
    return {
        "check_in_id": str(row.id),
        "held_on": row.held_on.isoformat() if row.held_on else None,
        "employee_input_state": row.employee_input_state.value,
    }


@router.post("/performance/pips/{pip_id}/close")
async def close_pip(
    pip_id: uuid.UUID,
    body: ClosePipIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Record an outcome.

    An unsuccessful outcome on a plan whose trail has gaps returns 400 listing
    them, unless `acknowledge_weak_trail` is set — which is recorded, because
    falsifying a check-in to get past the check would be far worse.
    """
    return await performance.close_pip(
        session,
        user,
        pip_id=pip_id,
        outcome=body.outcome,
        today=body.today,
        reason=body.reason,
        new_end_date=body.new_end_date,
        acknowledge_weak_trail=body.acknowledge_weak_trail,
        request=request,
    )


@router.get("/performance/pips/{pip_id}/evidence")
async def evidence_pack(
    pip_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    today: date | None = None,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Everything an employment adviser would ask for, in one payload.

    The check-ins are reproduced in full rather than summarised: a précis of
    somebody's contemporaneous account is not their account.
    """
    return await performance.evidence_pack(
        session, user, pip_id=pip_id, today=today or date.today()
    )


# ── Increment cycle ─────────────────────────────────────────────────────────


@router.post("/performance/cycles", status_code=status.HTTP_201_CREATED)
async def create_cycle(
    body: CycleIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    row = await performance.create_increment_cycle(
        session,
        user,
        name=body.name,
        effective_from=body.effective_from,
        budget_total=body.budget_total,
        bands=[b.model_dump() for b in body.bands],
        budget_by_department=body.budget_by_department,
        overage_mode=body.overage_mode,
        letter_release_on=body.letter_release_on,
        linked_appraisal_cycle_id=body.linked_appraisal_cycle_id,
        request=request,
    )
    return {
        "cycle_id": str(row.id),
        "name": row.name,
        "status": row.status.value,
        "overage_mode": row.overage_mode.value,
        "letter_release_on": (
            row.letter_release_on.isoformat() if row.letter_release_on else None
        ),
    }


@router.get("/performance/cycles/{cycle_id}/budget")
async def budget_view(
    cycle_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    department_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """What is left, for a manager who is still allocating.

    Not role-gated to HR: the manager doing the allocating is the person who
    needs it, and allocating blind is what this exists to prevent.
    """
    permissions.require_role(
        user,
        permissions.MANAGER_ROLES,
        detail="Increment budgets are visible to managers and HR.",
    )
    return await performance.budget_view(
        session, user, cycle_id=cycle_id, department_id=department_id
    )


@router.post("/performance/recommendations", status_code=201)
async def recommend_increment(
    body: RecommendationIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Record a recommendation, with the budget and the equity view in the reply.

    Returns 409 when a hard-stop budget would be exceeded, and 400 when the
    percentage is outside its rating band with no written justification.
    """
    await permissions.require_manager_or_hr(
        session,
        user,
        body.employee_id,
        detail="Increments are recommended by the employee's manager or HR.",
    )
    return await performance.recommend_increment(
        session,
        user,
        cycle_id=body.cycle_id,
        employee_id=body.employee_id,
        rating_label=body.rating_label,
        current_ctc=body.current_ctc,
        percentage=body.percentage,
        justification=body.justification,
        promotion_to_grade=body.promotion_to_grade,
        band_midpoint=body.band_midpoint,
        request=request,
    )


@router.post("/performance/recommendations/{recommendation_id}/decision")
async def decide_recommendation(
    recommendation_id: uuid.UUID,
    body: DecisionIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    row = await performance.decide_recommendation(
        session,
        user,
        recommendation_id=recommendation_id,
        approve=body.approve,
        note=body.note,
        request=request,
    )
    return {
        "recommendation_id": str(row.id),
        "status": row.status.value,
        "approvals": row.approvals,
    }


# ── Promotion and release ───────────────────────────────────────────────────


@router.post("/performance/promotions", status_code=status.HTTP_201_CREATED)
async def apply_promotion(
    body: PromotionIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Apply an approved promotion through the lifecycle cascade.

    Grade, salary revision, position and letter together or none of them — the
    cascade from slice 24, driven from the recommendation rather than
    reimplemented. A future-dated promotion is raised and approved and left for
    the due sweep.
    """
    return await performance.apply_promotion(
        session,
        user,
        recommendation_id=body.recommendation_id,
        to_position_id=body.to_position_id,
        effective_from=body.effective_from,
        today=body.today,
        request=request,
    )


@router.post("/performance/cycles/{cycle_id}/release-letters")
async def release_letters(
    cycle_id: uuid.UUID,
    body: ReleaseIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Release the cycle's letters, all together, on the controlled date.

    Refuses before the date and while approvals are outstanding. Releasing the
    approved ones "for now" is exactly the piecemeal release the controlled date
    exists to prevent.
    """
    return await performance.release_letters(
        session,
        user,
        cycle_id=cycle_id,
        today=body.today,
        force_partial=body.force_partial,
        request=request,
    )
