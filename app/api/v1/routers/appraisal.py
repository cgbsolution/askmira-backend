"""The appraisal chain (slice 15): cycles, reviews, delegation, operations.

Thin: parse, gate, delegate, serialise. Who may see or act on whose review is
decided in the services, through `app.core.permissions`.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, status

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.appraisal import AppraisalStatus, CycleStatus
from app.schemas.performance import (
    AcknowledgeRequest,
    AppraisalRead,
    CycleCreate,
    CycleRead,
    CycleUpdate,
    DelegationCreate,
    ReleaseRequest,
    ReminderRequest,
    ReopenRequest,
    ReturnRequest,
    ScorePreviewRequest,
    StageRead,
    SubmitRequest,
)
from app.services.appraisal import operations, review
from app.services.appraisal import service as appraisal_service

router = APIRouter(tags=["appraisal"])

HR_ONLY = [Depends(require_roles(*permissions.HR_ROLES))]
MANAGERS = [Depends(require_roles(*permissions.MANAGER_ROLES))]


# ── Cycles ──────────────────────────────────────────────────────────────────


@router.post(
    "/appraisal/cycles",
    response_model=CycleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def create_cycle(
    payload: CycleCreate, user: CurrentUser, session: DbSession, request: Request
) -> CycleRead:
    cycle = await appraisal_service.create_cycle(session, user, payload, request=request)
    await session.commit()
    await session.refresh(cycle)
    return CycleRead.model_validate(cycle)


@router.get("/appraisal/cycles", dependencies=MANAGERS)
async def list_cycles(
    user: CurrentUser, session: DbSession, cycle_status: CycleStatus | None = None
) -> list[dict[str, Any]]:
    return await appraisal_service.list_cycles(session, user, cycle_status=cycle_status)


@router.get("/appraisal/cycles/{cycle_id}", dependencies=HR_ONLY)
async def cycle_detail(
    cycle_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    return await appraisal_service.cycle_detail(session, user, cycle_id)


@router.patch("/appraisal/cycles/{cycle_id}", response_model=CycleRead, dependencies=HR_ONLY)
async def update_cycle(
    cycle_id: uuid.UUID,
    payload: CycleUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> CycleRead:
    cycle = await appraisal_service.update_cycle(session, user, cycle_id, payload, request=request)
    await session.commit()
    await session.refresh(cycle)
    return CycleRead.model_validate(cycle)


@router.get(
    "/appraisal/cycles/{cycle_id}/stages",
    response_model=list[StageRead],
    dependencies=MANAGERS,
)
async def list_stages(
    cycle_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[StageRead]:
    await appraisal_service._cycle(session, user, cycle_id)
    stages = await appraisal_service._stages(session, user, cycle_id)
    return [StageRead.model_validate(row) for row in stages]


@router.get("/appraisal/cycles/{cycle_id}/progress", dependencies=MANAGERS)
async def progress(cycle_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """By stage, status, department and manager — a manager sees their own line."""
    return await appraisal_service.cycle_progress(session, user, cycle_id)


@router.post("/appraisal/cycles/{cycle_id}/reminders", dependencies=HR_ONLY)
async def remind(
    cycle_id: uuid.UUID,
    payload: ReminderRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    result = await operations.send_stage_reminders(
        session, user, cycle_id=cycle_id, stage_id=payload.stage_id, request=request
    )
    await session.commit()
    return result


@router.post("/appraisal/cycles/{cycle_id}/escalate-overdue", dependencies=HR_ONLY)
async def escalate(
    cycle_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    result = await operations.escalate_overdue(session, user, cycle_id=cycle_id, request=request)
    await session.commit()
    return result


@router.post("/appraisal/sweep", dependencies=HR_ONLY)
async def sweep(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """The scheduled job, run by hand until a scheduler exists."""
    return await operations.run_performance_sweep(session, user)


# ── Reviews ─────────────────────────────────────────────────────────────────


@router.get("/appraisal/my")
async def my_reviews(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    return await review.my_reviews(session, user)


@router.get("/appraisal/team")
async def team(
    user: CurrentUser, session: DbSession, cycle_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    return await review.team_overview(session, user, cycle_id=cycle_id)


@router.get("/appraisal/appraisals")
async def list_appraisals(
    user: CurrentUser,
    session: DbSession,
    cycle_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
    appraisal_status: AppraisalStatus | None = None,
) -> list[dict[str, Any]]:
    return await appraisal_service.list_appraisals(
        session,
        user,
        cycle_id=cycle_id,
        employee_id=employee_id,
        appraisal_status=appraisal_status,
    )


@router.get("/appraisal/appraisals/{appraisal_id}")
async def review_detail(
    appraisal_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    return await review.review_detail(session, user, appraisal_id)


@router.post("/appraisal/appraisals/{appraisal_id}/score-preview")
async def score_preview(
    appraisal_id: uuid.UUID,
    payload: ScorePreviewRequest,
    user: CurrentUser,
    session: DbSession,
) -> dict[str, Any]:
    return await review.score_preview(
        session, user, appraisal_id=appraisal_id, ratings=payload.ratings
    )


@router.post("/appraisal/appraisals/{appraisal_id}/draft")
async def save_draft(
    appraisal_id: uuid.UUID,
    payload: SubmitRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    entry = await appraisal_service.save_draft(
        session,
        user,
        appraisal_id=appraisal_id,
        ratings=payload.ratings,
        comments=payload.comments,
        request=request,
    )
    await session.commit()
    return {"entry_id": str(entry.id), "saved": True}


def _read(appraisal: Any) -> AppraisalRead:
    return AppraisalRead.model_validate(appraisal)


@router.post("/appraisal/appraisals/{appraisal_id}/submit", response_model=AppraisalRead)
async def submit(
    appraisal_id: uuid.UUID,
    payload: SubmitRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AppraisalRead:
    appraisal = await appraisal_service.submit_stage(
        session,
        user,
        appraisal_id=appraisal_id,
        ratings=payload.ratings,
        comments=payload.comments,
        request=request,
    )
    await session.commit()
    await session.refresh(appraisal)
    return _read(appraisal)


@router.post("/appraisal/appraisals/{appraisal_id}/return", response_model=AppraisalRead)
async def send_back(
    appraisal_id: uuid.UUID,
    payload: ReturnRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AppraisalRead:
    appraisal = await appraisal_service.return_stage(
        session,
        user,
        appraisal_id=appraisal_id,
        target_stage_id=payload.target_stage_id,
        reason=payload.reason,
        request=request,
    )
    await session.commit()
    await session.refresh(appraisal)
    return _read(appraisal)


@router.post(
    "/appraisal/appraisals/{appraisal_id}/release",
    response_model=AppraisalRead,
    dependencies=HR_ONLY,
)
async def release_to_employee(
    appraisal_id: uuid.UUID,
    payload: ReleaseRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AppraisalRead:
    appraisal = await appraisal_service.release(
        session, user, appraisal_id=appraisal_id, final_rating=payload.final_rating, request=request
    )
    await session.commit()
    await session.refresh(appraisal)
    return _read(appraisal)


@router.post("/appraisal/appraisals/{appraisal_id}/acknowledge", response_model=AppraisalRead)
async def acknowledge(
    appraisal_id: uuid.UUID,
    payload: AcknowledgeRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AppraisalRead:
    """The employee's own response, agreeing or not. Disagreement still closes."""
    appraisal = await appraisal_service.acknowledge(
        session,
        user,
        appraisal_id=appraisal_id,
        acknowledgement=payload.acknowledgement,
        objection=payload.objection,
        request=request,
    )
    await session.commit()
    await session.refresh(appraisal)
    return _read(appraisal)


@router.post(
    "/appraisal/appraisals/{appraisal_id}/reopen",
    response_model=AppraisalRead,
    dependencies=[Depends(require_roles(*permissions.HR_ROLES - {permissions.UserRole.HR_OPS}))],
)
async def reopen(
    appraisal_id: uuid.UUID,
    payload: ReopenRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AppraisalRead:
    appraisal = await appraisal_service.reopen(
        session,
        user,
        appraisal_id=appraisal_id,
        reason=payload.reason,
        downstream_impact=payload.downstream_impact,
        acknowledge_impact=payload.acknowledge_impact,
        request=request,
    )
    await session.commit()
    await session.refresh(appraisal)
    return _read(appraisal)


# ── Delegation ──────────────────────────────────────────────────────────────


@router.post("/appraisal/delegations", status_code=status.HTTP_201_CREATED, dependencies=MANAGERS)
async def delegate(
    payload: DelegationCreate, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Hand your own review steps to somebody while away. HR may do it for anyone."""
    delegation = await appraisal_service.create_delegation(session, user, payload, request=request)
    await session.commit()
    return {"id": str(delegation.id), "effective_to": str(delegation.effective_to)}


@router.get("/appraisal/delegations", dependencies=MANAGERS)
async def list_delegations(
    user: CurrentUser, session: DbSession, cycle_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    return await appraisal_service.list_delegations(session, user, cycle_id=cycle_id)


@router.post("/appraisal/delegations/{delegation_id}/revoke", dependencies=MANAGERS)
async def revoke_delegation(
    delegation_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    delegation = await appraisal_service.revoke_delegation(
        session, user, delegation_id, request=request
    )
    await session.commit()
    return {"id": str(delegation.id), "active": delegation.active}
