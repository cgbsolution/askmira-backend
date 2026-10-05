"""Cycle configuration: assignment rules, windows, launch, clone, close, probation
(slice 18)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request, status

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.schemas.performance import (
    CloneRequest,
    CloseRequest,
    ProbationCreate,
    ProbationOutcomeRequest,
    ProbationRead,
    ProbationScheduleRequest,
    RuleCreate,
    RuleRead,
    StageWindowUpdate,
)
from app.services.appraisal import cycle_config as cycle_service

router = APIRouter(tags=["cycle-config"])

HR_ONLY = [Depends(require_roles(*permissions.HR_ROLES))]
ADMIN_ONLY = [Depends(require_roles(UserRole.ADMIN))]


# ── Assignment rules ────────────────────────────────────────────────────────


@router.post(
    "/cycle-config/rules",
    response_model=RuleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def create_rule(
    payload: RuleCreate, user: CurrentUser, session: DbSession, request: Request
) -> RuleRead:
    rule = await cycle_service.create_rule(session, user, payload, request=request)
    await session.commit()
    await session.refresh(rule)
    return RuleRead.model_validate(rule)


@router.get("/cycle-config/rules", response_model=list[RuleRead], dependencies=HR_ONLY)
async def list_rules(cycle_id: uuid.UUID, user: CurrentUser, session: DbSession) -> list[RuleRead]:
    return [
        RuleRead.model_validate(r) for r in await cycle_service.list_rules(session, user, cycle_id)
    ]


@router.delete(
    "/cycle-config/rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=HR_ONLY,
)
async def delete_rule(
    rule_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await cycle_service.delete_rule(session, user, rule_id, request=request)
    await session.commit()


@router.get("/cycle-config/cycles/{cycle_id}/assignment-preview", dependencies=HR_ONLY)
async def assignment_preview(
    cycle_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Who resolves to which template, before launching."""
    return await cycle_service.assignment_preview(session, user, cycle_id)


# ── Stage windows ───────────────────────────────────────────────────────────


@router.patch("/cycle-config/stages/{stage_id}/window", dependencies=HR_ONLY)
async def set_stage_window(
    stage_id: uuid.UUID,
    payload: StageWindowUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    result = await cycle_service.set_stage_window(session, user, stage_id, payload, request=request)
    await session.commit()
    return result


# ── Lifecycle ───────────────────────────────────────────────────────────────


@router.post("/cycle-config/cycles/{cycle_id}/launch", dependencies=ADMIN_ONLY)
async def launch(
    cycle_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Validate the rules (where there are any) and create every appraisal."""
    result = await cycle_service.launch(session, user, cycle_id=cycle_id, request=request)
    await session.commit()
    return result


@router.post("/cycle-config/cycles/{cycle_id}/auto-advance", dependencies=HR_ONLY)
async def auto_advance(
    cycle_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
    as_of: date | None = None,
) -> dict[str, Any]:
    result = await cycle_service.run_auto_advance(
        session, user, cycle_id=cycle_id, as_of=as_of, request=request
    )
    await session.commit()
    return result


@router.post(
    "/cycle-config/cycles/{cycle_id}/clone",
    status_code=status.HTTP_201_CREATED,
    dependencies=ADMIN_ONLY,
)
async def clone(
    cycle_id: uuid.UUID,
    payload: CloneRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
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


@router.post("/cycle-config/cycles/{cycle_id}/close", dependencies=ADMIN_ONLY)
async def close(
    cycle_id: uuid.UUID,
    payload: CloseRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Blocked while an ENFORCED calibration is out of tolerance."""
    result = await cycle_service.close_cycle(
        session,
        user,
        cycle_id=cycle_id,
        acknowledge_incomplete=payload.acknowledge_incomplete,
        release_completed=payload.release_completed,
        request=request,
    )
    await session.commit()
    return result


# ── Probation ───────────────────────────────────────────────────────────────


@router.post(
    "/cycle-config/probation-reviews",
    response_model=ProbationRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def create_probation_review(
    payload: ProbationCreate, user: CurrentUser, session: DbSession, request: Request
) -> ProbationRead:
    review = await cycle_service.create_probation_review(session, user, payload, request=request)
    await session.commit()
    await session.refresh(review)
    return ProbationRead.model_validate(review, from_attributes=True)


@router.post("/cycle-config/probation-reviews/schedule-due", dependencies=HR_ONLY)
async def schedule_due(
    payload: ProbationScheduleRequest, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Schedule reviews for probations ending soon, from the employee record."""
    result = await cycle_service.schedule_due_probation_reviews(
        session,
        user,
        horizon_days=payload.horizon_days,
        lead_days=payload.lead_days,
        request=request,
    )
    await session.commit()
    return result


@router.get(
    "/cycle-config/probation-reviews",
    response_model=list[ProbationRead],
    dependencies=HR_ONLY,
)
async def list_probation_reviews(
    user: CurrentUser,
    session: DbSession,
    due_before: date | None = None,
    pending_only: bool = True,
) -> list[ProbationRead]:
    rows = await cycle_service.list_probation_reviews(
        session, user, due_before=due_before, pending_only=pending_only
    )
    return [ProbationRead.model_validate(r) for r in rows]


@router.post(
    "/cycle-config/probation-reviews/{review_id}/outcome",
    response_model=ProbationRead,
    dependencies=ADMIN_ONLY,
)
async def record_outcome(
    review_id: uuid.UUID,
    payload: ProbationOutcomeRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ProbationRead:
    """Confirm, extend, or terminate. An extension schedules the next review."""
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
    return ProbationRead.model_validate(review, from_attributes=True)
