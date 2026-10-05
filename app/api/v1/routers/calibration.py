"""Calibration policies, sessions and the change record (slice 17).

Managers reach the session endpoints only for sessions they facilitate or sit
in; that check, and "nobody calibrates themselves", live in the service.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, status

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.schemas.performance import (
    CalibrationReopenRequest,
    ChangeRead,
    ChangeRequest,
    PolicyCreate,
    PolicyRead,
    SessionCreate,
    SessionRead,
    SessionReopenRequest,
)
from app.services.appraisal import calibration_service

router = APIRouter(tags=["calibration"])

HR_ONLY = [Depends(require_roles(*permissions.HR_ROLES))]
ADMIN_ONLY = [Depends(require_roles(UserRole.ADMIN))]
ROOM = [Depends(require_roles(*permissions.HR_ROLES, line_managers=True))]


@router.post(
    "/calibration/policies",
    response_model=PolicyRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def create_policy(
    payload: PolicyCreate, user: CurrentUser, session: DbSession, request: Request
) -> PolicyRead:
    policy = await calibration_service.create_policy(session, user, payload, request=request)
    await session.commit()
    return PolicyRead.model_validate(
        await calibration_service.get_policy(session, user, policy.cycle_id)
    )


@router.get("/calibration/policies", response_model=PolicyRead | None, dependencies=ROOM)
async def get_policy(
    cycle_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> PolicyRead | None:
    row = await calibration_service.get_policy(session, user, cycle_id)
    return PolicyRead.model_validate(row) if row else None


@router.get("/calibration/cycles/{cycle_id}/distribution", dependencies=HR_ONLY)
async def distribution(
    cycle_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Actual against target for every scope, and what blocks closure."""
    return await calibration_service.cycle_distribution(session, user, cycle_id)


@router.post(
    "/calibration/sessions",
    response_model=SessionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def create_session(
    payload: SessionCreate, user: CurrentUser, session: DbSession, request: Request
) -> SessionRead:
    row = await calibration_service.create_session(session, user, payload, request=request)
    await session.commit()
    rows = await calibration_service.list_sessions(session, user, cycle_id=row.cycle_id)
    return SessionRead.model_validate(next(r for r in rows if r["id"] == row.id))


@router.get("/calibration/sessions", response_model=list[SessionRead], dependencies=ROOM)
async def list_sessions(
    user: CurrentUser, session: DbSession, cycle_id: uuid.UUID | None = None
) -> list[SessionRead]:
    rows = await calibration_service.list_sessions(session, user, cycle_id=cycle_id)
    return [SessionRead.model_validate(r) for r in rows]


@router.post("/calibration/sessions/{session_id}/start", dependencies=ROOM)
async def start_session(
    session_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    row = await calibration_service.set_session_status(
        session, user, session_id=session_id, action="start", request=request
    )
    await session.commit()
    return {"session_id": str(row.id), "status": row.status.value}


@router.post("/calibration/sessions/{session_id}/cancel", dependencies=HR_ONLY)
async def cancel_session(
    session_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    row = await calibration_service.set_session_status(
        session, user, session_id=session_id, action="cancel", request=request
    )
    await session.commit()
    return {"session_id": str(row.id), "status": row.status.value}


@router.get("/calibration/sessions/{session_id}/workspace", dependencies=ROOM)
async def workspace(session_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """The grid, the live distribution, outliers and peers, in one call."""
    return await calibration_service.session_workspace(session, user, session_id)


@router.get("/calibration/sessions/{session_id}/evidence/{appraisal_id}", dependencies=ROOM)
async def person_evidence(
    session_id: uuid.UUID, appraisal_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Goals, recognition and review entries for one person, inside the room."""
    return await calibration_service.person_evidence(
        session, user, session_id=session_id, appraisal_id=appraisal_id
    )


@router.post(
    "/calibration/sessions/{session_id}/changes",
    response_model=ChangeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=ROOM,
)
async def change_rating(
    session_id: uuid.UUID,
    payload: ChangeRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ChangeRead:
    """Change one rating, with a justification. Nothing is anonymous."""
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
    dependencies=ROOM,
)
async def list_changes(
    session_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[ChangeRead]:
    rows = await calibration_service.list_changes(session, user, session_id)
    return [ChangeRead.model_validate(row) for row in rows]


@router.post("/calibration/sessions/{session_id}/lock", dependencies=HR_ONLY)
async def lock(
    session_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Write the agreed ratings back to the appraisals."""
    result = await calibration_service.lock_session(
        session, user, session_id=session_id, request=request
    )
    await session.commit()
    return result


@router.post("/calibration/sessions/{session_id}/reopen", dependencies=ADMIN_ONLY)
async def reopen_session(
    session_id: uuid.UUID,
    payload: SessionReopenRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    row = await calibration_service.reopen_session(
        session, user, session_id=session_id, reason=payload.reason, request=request
    )
    await session.commit()
    return {"session_id": str(row.id), "status": row.status.value}


@router.post("/calibration/appraisals/{appraisal_id}/reopen", dependencies=ADMIN_ONLY)
async def reopen(
    appraisal_id: uuid.UUID,
    payload: CalibrationReopenRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Reopen a calibrated appraisal, under HR override. The history stays."""
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
