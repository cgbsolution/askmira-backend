"""PIPs, increment cycles and promotions (slice 35).

- `POST /performance/pips/{id}/check-ins` requires the employee's own account or
  a reasoned record that they declined, and is never recorded by the person on
  the plan.
- `POST /performance/recommendations` returns the budget headroom and the
  equity comparison with the recommendation, takes the current CTC from the
  salary assignment and the rating from the linked review.
- Dates that gate a decision (letter release, promotion effect, PIP outcome)
  are the server's today, never a date the caller supplies.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request, status

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.performance_flow import PipStatusEnum
from app.models.user import UserRole
from app.schemas.performance import (
    CheckInIn,
    ClosePipIn,
    DecisionIn,
    EmployeeStatementIn,
    IncrementCycleIn,
    MilestoneAssessIn,
    PipIn,
    PromotionIn,
    RecommendationIn,
    ReleaseIn,
)
from app.services.performance import service as performance
from app.services.performance import views

router = APIRouter(tags=["performance-flow"])

HR_ONLY = [Depends(require_roles(*permissions.HR_ROLES))]
ADMIN_ONLY = [Depends(require_roles(UserRole.ADMIN))]
MANAGERS = [Depends(require_roles(*permissions.HR_ROLES, line_managers=True))]


@router.get("/performance/posture")
async def posture(user: CurrentUser) -> dict[str, Any]:
    """What this slice enforces, and what it does not."""
    return performance.performance_posture()


@router.get("/performance/people")
async def people(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Your reporting line (everybody for HR), for the pickers on these screens."""
    return await views.people(session, user)


# ── PIP ─────────────────────────────────────────────────────────────────────


@router.get("/performance/pips")
async def list_pips(
    user: CurrentUser, session: DbSession, pip_status: PipStatusEnum | None = None
) -> list[dict[str, Any]]:
    return await views.list_pips(session, user, pip_status=pip_status)


@router.post("/performance/pips", status_code=status.HTTP_201_CREATED)
async def open_pip(
    body: PipIn, session: DbSession, user: CurrentUser, request: Request
) -> dict[str, Any]:
    """Open a plan for somebody in your line. Never for yourself."""
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


@router.get("/performance/pips/{pip_id}")
async def pip_detail(pip_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return await views.pip_detail(session, user, pip_id)


@router.post("/performance/pips/{pip_id}/check-ins", status_code=201)
async def record_check_in(
    pip_id: uuid.UUID,
    body: CheckInIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
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


@router.post("/performance/pips/{pip_id}/milestones/{milestone_id}/assess")
async def assess_milestone(
    pip_id: uuid.UUID,
    milestone_id: uuid.UUID,
    body: MilestoneAssessIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    return await views.assess_milestone(
        session,
        user,
        pip_id=pip_id,
        milestone_id=milestone_id,
        milestone_status=body.status,
        note=body.note,
        request=request,
    )


@router.post("/performance/pips/{pip_id}/statements", status_code=201)
async def add_statement(
    pip_id: uuid.UUID,
    body: EmployeeStatementIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """The employee's own account, in their own words."""
    return await views.add_employee_statement(
        session,
        user,
        pip_id=pip_id,
        statement=body.statement,
        check_in_id=body.check_in_id,
        request=request,
    )


@router.post("/performance/pips/{pip_id}/close", dependencies=HR_ONLY)
async def close_pip(
    pip_id: uuid.UUID,
    body: ClosePipIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    return await performance.close_pip(
        session,
        user,
        pip_id=pip_id,
        outcome=body.outcome,
        today=date.today(),
        reason=body.reason,
        new_end_date=body.new_end_date,
        acknowledge_weak_trail=body.acknowledge_weak_trail,
        request=request,
    )


@router.get("/performance/pips/{pip_id}/evidence", dependencies=HR_ONLY)
async def evidence_pack(pip_id: uuid.UUID, session: DbSession, user: CurrentUser) -> dict[str, Any]:
    """Everything an employment adviser would ask for, in one payload."""
    return await performance.evidence_pack(session, user, pip_id=pip_id, today=date.today())


# ── Increment cycle ─────────────────────────────────────────────────────────


@router.post("/performance/cycles", status_code=status.HTTP_201_CREATED, dependencies=ADMIN_ONLY)
async def create_cycle(
    body: IncrementCycleIn, session: DbSession, user: CurrentUser, request: Request
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
    return {"cycle_id": str(row.id), "name": row.name, "status": row.status.value}


@router.get("/performance/cycles", dependencies=MANAGERS)
async def list_increment_cycles(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    return await views.list_increment_cycles(session, user)


@router.get("/performance/cycles/{cycle_id}", dependencies=MANAGERS)
async def increment_cycle(
    cycle_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    return await views.increment_cycle_detail(session, user, cycle_id)


@router.get("/performance/cycles/{cycle_id}/candidates", dependencies=MANAGERS)
async def candidates(cycle_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """Your reporting line (everybody for HR) with rating, band, pay and status."""
    return await views.increment_candidates(session, user, cycle_id)


@router.get("/performance/cycles/{cycle_id}/budget")
async def budget_view(
    cycle_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    department_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """What is left, for a manager who is still allocating."""
    permissions.require_role(
        user,
        permissions.HR_ROLES,
        line_managers=True,
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


@router.post("/performance/recommendations/{recommendation_id}/decision", dependencies=HR_ONLY)
async def decide_recommendation(
    recommendation_id: uuid.UUID,
    body: DecisionIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    row = await performance.decide_recommendation(
        session,
        user,
        recommendation_id=recommendation_id,
        approve=body.approve,
        note=body.note,
        request=request,
    )
    return {"recommendation_id": str(row.id), "status": row.status.value}


# ── Promotion and release ───────────────────────────────────────────────────


@router.post("/performance/promotions", status_code=status.HTTP_201_CREATED, dependencies=HR_ONLY)
async def apply_promotion(
    body: PromotionIn, session: DbSession, user: CurrentUser, request: Request
) -> dict[str, Any]:
    """Apply an approved promotion through the lifecycle cascade."""
    return await performance.apply_promotion(
        session,
        user,
        recommendation_id=body.recommendation_id,
        to_position_id=body.to_position_id,
        effective_from=body.effective_from,
        today=date.today(),
        request=request,
    )


@router.post("/performance/cycles/{cycle_id}/release-letters", dependencies=ADMIN_ONLY)
async def release_letters(
    cycle_id: uuid.UUID,
    body: ReleaseIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Release the cycle's letters together, on the controlled date."""
    return await performance.release_letters(
        session,
        user,
        cycle_id=cycle_id,
        today=date.today(),
        force_partial=body.force_partial,
        request=request,
    )
