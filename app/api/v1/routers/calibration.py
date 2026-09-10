"""Calibration policies, sessions and the change record (slice 17)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.calibration import (
    CalibrationChange,
    CalibrationMode,
    CalibrationPolicy,
    CalibrationScope,
    CalibrationSession,
    DistributionTarget,
    SessionStatus,
)
from app.models.user import UserRole
from app.services.appraisal import calibration_service

router = APIRouter(tags=["calibration"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)
# A facilitator is usually a senior manager, and participants are managers.
FACILITATOR_ROLES = (*HR_ROLES, UserRole.MANAGER)


# ── Contracts ───────────────────────────────────────────────────────────────


class TargetSpec(BaseModel):
    rating_value: str = Field(min_length=1, max_length=64)
    target_percent: Decimal = Field(ge=0, le=100)
    tolerance_percent: Decimal = Field(default=Decimal(0), ge=0, le=100)


class PolicyCreate(BaseModel):
    """How calibration runs for one cycle.

    All three modes are first class and none is presented as preferable — the
    client decided that before they bought the software.
    """

    cycle_id: uuid.UUID
    mode: CalibrationMode = CalibrationMode.ADVISORY
    scope: CalibrationScope = CalibrationScope.DEPARTMENT
    # Below this a group is reported but not enforced. A manager with three
    # reports cannot express a 10% target.
    min_group_size: int = Field(default=8, ge=1, le=1000)
    disclose_calibration_to_employee: bool = False
    targets: list[TargetSpec] = Field(default_factory=list)


class PolicyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cycle_id: uuid.UUID
    mode: CalibrationMode
    scope: CalibrationScope
    min_group_size: int
    disclose_calibration_to_employee: bool


class SessionCreate(BaseModel):
    cycle_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    scope: CalibrationScope
    scope_value: str = Field(min_length=1, max_length=128)
    facilitator_user_id: uuid.UUID | None = None
    # Who was in the room. "Who was present when my rating changed" is part of
    # the same question as "who changed it".
    participant_user_ids: list[uuid.UUID] = Field(default_factory=list)
    scheduled_at: datetime | None = None


class SessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cycle_id: uuid.UUID
    name: str
    scope: CalibrationScope
    scope_value: str
    status: SessionStatus
    facilitator_user_id: uuid.UUID | None
    scheduled_at: datetime | None
    locked_at: datetime | None


class ChangeRequest(BaseModel):
    appraisal_id: uuid.UUID
    new_rating: str = Field(min_length=1, max_length=64)
    new_score: Decimal | None = Field(default=None, ge=0)
    # Mandatory at the contract, and again in the service — the API must not be
    # able to bypass what the screen enforces.
    justification: str = Field(min_length=1)


class ChangeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    appraisal_id: uuid.UUID
    employee_id: uuid.UUID
    previous_rating: str | None
    new_rating: str
    previous_score: Decimal | None
    new_score: Decimal | None
    changed_by_user_id: uuid.UUID | None
    changed_at: datetime
    justification: str
    applied_on_lock: bool


class ReopenRequest(BaseModel):
    reason: str = Field(min_length=1)
    hr_override: bool = False


# ── Policies ────────────────────────────────────────────────────────────────


@router.post(
    "/calibration/policies",
    response_model=PolicyRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_policy(
    payload: PolicyCreate, user: CurrentUser, session: DbSession
) -> PolicyRead:
    if payload.mode is not CalibrationMode.NONE and not payload.targets:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"A '{payload.mode.value}' policy needs distribution targets — there is "
                "nothing to measure against without them."
            ),
        )

    total = sum(t.target_percent for t in payload.targets)
    if payload.targets and total != Decimal(100):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"The distribution targets sum to {total}%, not 100%. A target set that "
                "does not cover the population cannot be satisfied by any distribution."
            ),
        )

    existing = await session.scalar(
        tenant_scoped(
            select(CalibrationPolicy).where(
                CalibrationPolicy.cycle_id == payload.cycle_id
            ),
            CalibrationPolicy,
            user,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This cycle already has a calibration policy.",
        )

    policy = CalibrationPolicy(
        tenant_id=user.tenant_id,
        cycle_id=payload.cycle_id,
        mode=payload.mode,
        scope=payload.scope,
        min_group_size=payload.min_group_size,
        disclose_calibration_to_employee=payload.disclose_calibration_to_employee,
    )
    session.add(policy)
    await session.flush()

    for target in payload.targets:
        session.add(
            DistributionTarget(
                tenant_id=user.tenant_id, policy_id=policy.id, **target.model_dump()
            )
        )
    await session.commit()
    await session.refresh(policy)
    return PolicyRead.model_validate(policy)


@router.get(
    "/calibration/cycles/{cycle_id}/distribution",
    dependencies=[Depends(require_roles(*FACILITATOR_ROLES))],
)
async def distribution(
    cycle_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Actual against target for every scope, and what blocks closure.

    Both come from one query so "what does the distribution look like" and
    "can this cycle close" cannot give inconsistent answers.
    """
    return await calibration_service.cycle_distribution(session, user, cycle_id)


# ── Sessions ────────────────────────────────────────────────────────────────


@router.post(
    "/calibration/sessions",
    response_model=SessionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def create_session(
    payload: SessionCreate, user: CurrentUser, session: DbSession
) -> SessionRead:
    row = CalibrationSession(
        tenant_id=user.tenant_id,
        cycle_id=payload.cycle_id,
        name=payload.name,
        scope=payload.scope,
        scope_value=payload.scope_value,
        facilitator_user_id=payload.facilitator_user_id,
        participant_user_ids=[str(u) for u in payload.participant_user_ids],
        scheduled_at=payload.scheduled_at,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return SessionRead.model_validate(row)


@router.get(
    "/calibration/sessions",
    response_model=list[SessionRead],
    dependencies=[Depends(require_roles(*FACILITATOR_ROLES))],
)
async def list_sessions(
    user: CurrentUser, session: DbSession, cycle_id: uuid.UUID | None = None
) -> list[SessionRead]:
    stmt = select(CalibrationSession).order_by(CalibrationSession.created_at.desc())
    if cycle_id is not None:
        stmt = stmt.where(CalibrationSession.cycle_id == cycle_id)
    rows = (await session.scalars(tenant_scoped(stmt, CalibrationSession, user))).all()
    return [SessionRead.model_validate(row) for row in rows]


@router.get(
    "/calibration/sessions/{session_id}/workspace",
    dependencies=[Depends(require_roles(*FACILITATOR_ROLES))],
)
async def workspace(
    session_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """The grid, the live distribution, and peer scores by grade.

    Assembled server-side in one call: a screen that fetches these separately
    shows a distribution that disagrees with the rows above it for a moment
    after every change, which is exactly the moment a facilitator is looking
    at it.
    """
    return await calibration_service.session_workspace(session, user, session_id)


@router.post(
    "/calibration/sessions/{session_id}/changes",
    response_model=ChangeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*FACILITATOR_ROLES))],
)
async def change_rating(
    session_id: uuid.UUID,
    payload: ChangeRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ChangeRead:
    """Change one rating, with a justification.

    Nothing is anonymous. If the rating is disputed months later, this row is
    what the organisation answers with.
    """
    change = await calibration_service.apply_change(
        session,
        user,
        session_id=session_id,
        appraisal_id=payload.appraisal_id,
        new_rating=payload.new_rating,
        new_score=payload.new_score,
        justification=payload.justification,
        request=request,
    )
    await session.commit()
    await session.refresh(change)
    return ChangeRead.model_validate(change)


@router.get(
    "/calibration/sessions/{session_id}/changes",
    response_model=list[ChangeRead],
    dependencies=[Depends(require_roles(*FACILITATOR_ROLES))],
)
async def list_changes(
    session_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[ChangeRead]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(CalibrationChange)
                .where(CalibrationChange.session_id == session_id)
                .order_by(CalibrationChange.changed_at),
                CalibrationChange,
                user,
            )
        )
    ).all()
    return [ChangeRead.model_validate(row) for row in rows]


@router.get(
    "/calibration/appraisals/{appraisal_id}/history",
    response_model=list[ChangeRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def appraisal_history(
    appraisal_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[ChangeRead]:
    """Every calibration change to one appraisal.

    The answer to "who changed my rating, when, and why" — which is the
    question this whole slice exists to be able to answer.
    """
    rows = (
        await session.scalars(
            tenant_scoped(
                select(CalibrationChange)
                .where(CalibrationChange.appraisal_id == appraisal_id)
                .order_by(CalibrationChange.changed_at),
                CalibrationChange,
                user,
            )
        )
    ).all()
    return [ChangeRead.model_validate(row) for row in rows]


@router.post(
    "/calibration/sessions/{session_id}/lock",
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def lock(
    session_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Write the agreed ratings back to the appraisals."""
    result = await calibration_service.lock_session(
        session, user, session_id=session_id, request=request
    )
    await session.commit()
    return result


@router.post(
    "/calibration/appraisals/{appraisal_id}/reopen",
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def reopen(
    appraisal_id: uuid.UUID,
    payload: ReopenRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Reopen a calibrated appraisal, under HR override.

    The calibration history stays on the record — reopening adds to it rather
    than erasing the session.
    """
    appraisal = await calibration_service.reopen_calibrated(
        session,
        user,
        appraisal_id=appraisal_id,
        reason=payload.reason,
        hr_override=payload.hr_override,
        request=request,
    )
    await session.commit()
    return {
        "appraisal_id": str(appraisal.id),
        "reopened_count": appraisal.reopened_count,
        "reason": appraisal.reopen_reason,
    }
