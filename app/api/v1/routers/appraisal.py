"""The appraisal chain (slice 15)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
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
from app.models.audit import AuditAction
from app.models.user import UserRole
from app.services import audit
from app.services.appraisal import service as appraisal_service

router = APIRouter(tags=["appraisal"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)
# A stage owner is usually a manager; the employee owns their self-appraisal.
PARTICIPANT_ROLES = (*HR_ROLES, UserRole.MANAGER, UserRole.EMPLOYEE)


# ── Contracts ───────────────────────────────────────────────────────────────


class StageCreate(BaseModel):
    sequence: int = Field(ge=1, le=20)
    role: StageRole
    label: str = Field(min_length=1, max_length=128)
    due_date: date | None = None
    is_mandatory: bool = True
    can_reopen: bool = False
    sees_previous_entries: bool = True
    # Usually false: manager comments visible mid-cycle change what managers
    # write, which is not the same as changing what they think.
    visible_to_employee_before_release: bool = False


class CycleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    cycle_type: CycleType
    period_start: date
    period_end: date
    eligibility_rule: dict[str, Any] = Field(default_factory=dict)
    escalation_after_days: int = Field(default=7, ge=1, le=90)
    stages: list[StageCreate] = Field(min_length=1)


class CycleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    cycle_type: CycleType
    status: CycleStatus
    period_start: date
    period_end: date
    eligibility_rule: dict[str, Any]
    escalation_after_days: int
    launched_at: datetime | None
    closed_at: datetime | None


class StageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sequence: int
    role: StageRole
    label: str
    due_date: date | None
    is_mandatory: bool
    sees_previous_entries: bool
    visible_to_employee_before_release: bool


class AppraisalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cycle_id: uuid.UUID
    employee_id: uuid.UUID
    current_stage_id: uuid.UUID | None
    status: AppraisalStatus
    final_rating: str | None
    final_score: Decimal | None
    completed_at: datetime | None
    released_at: datetime | None
    acknowledged_at: datetime | None
    acknowledgement_type: AcknowledgementType | None
    objection: str | None
    reopened_count: int


class EntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    stage_id: uuid.UUID
    actor_user_id: uuid.UUID | None
    delegated_from_user_id: uuid.UUID | None
    is_escalated: bool
    ratings: dict[str, Any]
    comments: str | None
    score: Decimal | None
    submitted_at: datetime | None
    returned_to_stage_id: uuid.UUID | None
    returned_reason: str | None


class SubmitRequest(BaseModel):
    ratings: dict[str, Any] = Field(default_factory=dict)
    comments: str | None = None
    score: Decimal | None = Field(default=None, ge=0)


class ReturnRequest(BaseModel):
    target_stage_id: uuid.UUID
    reason: str = Field(min_length=1)


class ReleaseRequest(BaseModel):
    final_rating: str | None = Field(default=None, max_length=64)


class AcknowledgeRequest(BaseModel):
    """Disagreement is a valid close.

    Forcing agreement as the only way to acknowledge is how an appraisal
    dispute becomes an employment dispute.
    """

    acknowledgement: AcknowledgementType
    objection: str | None = None


class ReopenRequest(BaseModel):
    reason: str = Field(min_length=1)
    downstream_impact: list[dict[str, Any]] = Field(default_factory=list)
    acknowledge_impact: bool = False


class DelegationCreate(BaseModel):
    cycle_id: uuid.UUID
    from_user_id: uuid.UUID
    to_user_id: uuid.UUID
    effective_from: date
    # Required, and bounded: a standing delegation with no end date is how a
    # manager who returned from leave three months ago still is not rating
    # anyone.
    effective_to: date
    reason: str | None = None


# ── Cycles ──────────────────────────────────────────────────────────────────


@router.post(
    "/appraisal/cycles",
    response_model=CycleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_cycle(
    payload: CycleCreate, user: CurrentUser, session: DbSession
) -> CycleRead:
    if payload.period_end < payload.period_start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The review period ends before it starts.",
        )

    sequences = [stage.sequence for stage in payload.stages]
    if len(set(sequences)) != len(sequences):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Two stages share a sequence number, so the order the chain runs in "
                "would be undefined."
            ),
        )

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
            AppraisalStage(
                tenant_id=user.tenant_id, cycle_id=cycle.id, **stage.model_dump()
            )
        )

    await session.commit()
    await session.refresh(cycle)
    return CycleRead.model_validate(cycle)


@router.get(
    "/appraisal/cycles",
    response_model=list[CycleRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_cycles(
    user: CurrentUser, session: DbSession, cycle_status: CycleStatus | None = None
) -> list[CycleRead]:
    stmt = select(AppraisalCycle).order_by(AppraisalCycle.period_start.desc())
    if cycle_status is not None:
        stmt = stmt.where(AppraisalCycle.status == cycle_status)
    rows = (await session.scalars(tenant_scoped(stmt, AppraisalCycle, user))).all()
    return [CycleRead.model_validate(row) for row in rows]


@router.get(
    "/appraisal/cycles/{cycle_id}/stages",
    response_model=list[StageRead],
    dependencies=[Depends(require_roles(*PARTICIPANT_ROLES))],
)
async def list_stages(
    cycle_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[StageRead]:
    rows = (
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
    return [StageRead.model_validate(row) for row in rows]


@router.post(
    "/appraisal/cycles/{cycle_id}/launch",
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def launch(
    cycle_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Create an appraisal for every eligible employee.

    The response says who was excluded and why — "why is Anita not in this
    cycle" is asked on every launch.
    """
    summary = await appraisal_service.launch_cycle(
        session, user, cycle_id=cycle_id, request=request
    )
    await session.commit()
    return summary


@router.get(
    "/appraisal/cycles/{cycle_id}/progress",
    dependencies=[Depends(require_roles(*HR_ROLES, UserRole.MANAGER))],
)
async def progress(
    cycle_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Where the cycle stands, by stage and status.

    Without it HR chases everyone instead of the four people who are late.
    """
    return await appraisal_service.cycle_progress(session, user, cycle_id)


# ── Appraisals ──────────────────────────────────────────────────────────────


@router.get(
    "/appraisal/appraisals",
    response_model=list[AppraisalRead],
    dependencies=[Depends(require_roles(*PARTICIPANT_ROLES))],
)
async def list_appraisals(
    user: CurrentUser,
    session: DbSession,
    cycle_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
    appraisal_status: AppraisalStatus | None = None,
) -> list[AppraisalRead]:
    stmt = select(Appraisal)
    if cycle_id is not None:
        stmt = stmt.where(Appraisal.cycle_id == cycle_id)
    if employee_id is not None:
        stmt = stmt.where(Appraisal.employee_id == employee_id)
    if appraisal_status is not None:
        stmt = stmt.where(Appraisal.status == appraisal_status)
    rows = (await session.scalars(tenant_scoped(stmt, Appraisal, user))).all()
    return [AppraisalRead.model_validate(row) for row in rows]


@router.get(
    "/appraisal/appraisals/{appraisal_id}/entries",
    response_model=list[EntryRead],
    dependencies=[Depends(require_roles(*PARTICIPANT_ROLES))],
)
async def list_entries(
    appraisal_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[EntryRead]:
    """The full history, returns included.

    An appraisal that reads as though it went straight through when it went
    back twice is not a record of what happened.
    """
    rows = (
        await session.scalars(
            tenant_scoped(
                select(AppraisalStageEntry)
                .where(AppraisalStageEntry.appraisal_id == appraisal_id)
                .order_by(AppraisalStageEntry.created_at),
                AppraisalStageEntry,
                user,
            )
        )
    ).all()
    return [EntryRead.model_validate(row) for row in rows]


@router.post(
    "/appraisal/appraisals/{appraisal_id}/submit",
    response_model=AppraisalRead,
    dependencies=[Depends(require_roles(*PARTICIPANT_ROLES))],
)
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
        score=float(payload.score) if payload.score is not None else None,
        request=request,
    )
    await session.commit()
    await session.refresh(appraisal)
    return AppraisalRead.model_validate(appraisal)


@router.post(
    "/appraisal/appraisals/{appraisal_id}/return",
    response_model=AppraisalRead,
    dependencies=[Depends(require_roles(*PARTICIPANT_ROLES))],
)
async def send_back(
    appraisal_id: uuid.UUID,
    payload: ReturnRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AppraisalRead:
    """Send back to an earlier stage. An ordinary move, not an error path."""
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
    return AppraisalRead.model_validate(appraisal)


@router.post(
    "/appraisal/appraisals/{appraisal_id}/release",
    response_model=AppraisalRead,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def release_to_employee(
    appraisal_id: uuid.UUID,
    payload: ReleaseRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AppraisalRead:
    appraisal = await appraisal_service.release(
        session,
        user,
        appraisal_id=appraisal_id,
        final_rating=payload.final_rating,
        request=request,
    )
    await session.commit()
    await session.refresh(appraisal)
    return AppraisalRead.model_validate(appraisal)


@router.post(
    "/appraisal/appraisals/{appraisal_id}/acknowledge",
    response_model=AppraisalRead,
    dependencies=[Depends(require_roles(*PARTICIPANT_ROLES))],
)
async def acknowledge(
    appraisal_id: uuid.UUID,
    payload: AcknowledgeRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AppraisalRead:
    """Acknowledge, agreeing or not.

    An employee who disagrees still closes the cycle; their written objection
    routes to HR and stays on the record.
    """
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
    return AppraisalRead.model_validate(appraisal)


@router.post(
    "/appraisal/appraisals/{appraisal_id}/reopen",
    response_model=AppraisalRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def reopen(
    appraisal_id: uuid.UUID,
    payload: ReopenRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AppraisalRead:
    """Reopen a closed appraisal.

    Where the rating has already fed an increment, this refuses until the
    impact is acknowledged — reopening does not unwind what was decided on it.
    """
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
    return AppraisalRead.model_validate(appraisal)


# ── Delegation ──────────────────────────────────────────────────────────────


@router.post(
    "/appraisal/delegations",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES, UserRole.MANAGER))],
)
async def delegate(
    payload: DelegationCreate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Hand a stage to someone else while away.

    Time-boxed on purpose. The original owner stays on every entry the
    delegate submits.
    """
    if payload.effective_to < payload.effective_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The delegation ends before it starts.",
        )
    if payload.from_user_id == payload.to_user_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Delegating to yourself changes nothing.",
        )

    delegation = StageDelegation(tenant_id=user.tenant_id, **payload.model_dump())
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
            f"Delegated appraisal stages from {payload.from_user_id} to "
            f"{payload.to_user_id} until {payload.effective_to}"
        ),
        changes={
            "from_user_id": str(payload.from_user_id),
            "to_user_id": str(payload.to_user_id),
            "effective_to": str(payload.effective_to),
            "reason": payload.reason,
        },
        request=request,
    )
    await session.commit()
    return {"id": str(delegation.id), "effective_to": str(payload.effective_to)}
