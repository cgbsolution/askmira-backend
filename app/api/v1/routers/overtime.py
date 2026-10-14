"""Overtime: policies, sync from attendance, and manager / HR decisions.

Thin: parse, gate, delegate to `app.services.attendance.overtime`, serialise.
Policies and sync are HR's; decisions are the line manager's or HR's; an
employee reads their own.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.payroll_inputs import OvertimeStatus
from app.models.user import UserRole
from app.schemas.payroll_inputs import (
    OvertimeBulkDecision,
    OvertimeDecision,
    OvertimeEntryRead,
    OvertimePolicyCreate,
    OvertimePolicyRead,
    OvertimePolicyUpdate,
    OvertimeSyncRequest,
    OvertimeSyncResult,
)
from app.services.attendance import overtime as overtime_service

HR_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
HR_ONLY = [Depends(require_roles(*HR_ROLES))]
MANAGERS_AND_HR = [Depends(require_roles(*HR_ROLES, line_managers=True))]

router = APIRouter(prefix="/attendance/overtime", tags=["attendance"])


@router.get("/policies", response_model=list[OvertimePolicyRead], dependencies=HR_ONLY)
async def list_policies(user: CurrentUser, session: DbSession) -> list[OvertimePolicyRead]:
    return await overtime_service.list_policies(session, user)


@router.post(
    "/policies",
    response_model=OvertimePolicyRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def create_policy(
    payload: OvertimePolicyCreate, user: CurrentUser, session: DbSession, request: Request
) -> OvertimePolicyRead:
    return await overtime_service.create_policy(session, user, payload, request=request)


@router.patch("/policies/{policy_id}", response_model=OvertimePolicyRead, dependencies=HR_ONLY)
async def update_policy(
    policy_id: uuid.UUID,
    payload: OvertimePolicyUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> OvertimePolicyRead:
    return await overtime_service.update_policy(
        session, user, policy_id, payload, request=request
    )


@router.post("/sync", response_model=OvertimeSyncResult, dependencies=HR_ONLY)
async def sync_overtime(
    payload: OvertimeSyncRequest, user: CurrentUser, session: DbSession, request: Request
) -> OvertimeSyncResult:
    return await overtime_service.sync(
        session, user, start=payload.start, end=payload.end, request=request
    )


@router.get("/mine", response_model=list[OvertimeEntryRead])
async def my_overtime(
    user: CurrentUser,
    session: DbSession,
    start: date | None = None,
    end: date | None = None,
) -> list[OvertimeEntryRead]:
    if user.employee_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This login has no employee record, so it has no overtime.",
        )
    return await overtime_service.list_entries(session, user, start=start, end=end, mine=True)


@router.get("", response_model=list[OvertimeEntryRead], dependencies=MANAGERS_AND_HR)
async def list_overtime(
    user: CurrentUser,
    session: DbSession,
    entry_status: OvertimeStatus | None = Query(default=None, alias="status"),
    start: date | None = None,
    end: date | None = None,
    employee_id: uuid.UUID | None = None,
) -> list[OvertimeEntryRead]:
    return await overtime_service.list_entries(
        session, user, entry_status=entry_status, start=start, end=end, employee_id=employee_id
    )


@router.post("/decide", response_model=list[OvertimeEntryRead], dependencies=MANAGERS_AND_HR)
async def decide_many(
    payload: OvertimeBulkDecision, user: CurrentUser, session: DbSession, request: Request
) -> list[OvertimeEntryRead]:
    return await overtime_service.decide_many(
        session,
        user,
        payload.entry_ids,
        approve_it=payload.approve,
        note=payload.note,
        request=request,
    )


@router.post("/{entry_id}/decide", response_model=OvertimeEntryRead, dependencies=MANAGERS_AND_HR)
async def decide(
    entry_id: uuid.UUID,
    payload: OvertimeDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> OvertimeEntryRead:
    return await overtime_service.decide(
        session,
        user,
        entry_id,
        approve_it=payload.approve,
        minutes=payload.minutes,
        note=payload.note,
        request=request,
    )
