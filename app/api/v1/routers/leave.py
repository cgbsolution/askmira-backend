"""Time-off endpoints (FRD 5.1, V1 build order step 3).

Thin by design: parse, gate, delegate to `app.services.leave.service`,
serialise. Who may see or decide whose leave is decided in the service, through
`app.core.permissions`, so the rule has one implementation.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query, Request, status

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.leave import LeaveRequestStatus
from app.schemas.common import Page
from app.schemas.leave import (
    AccrualRunRead,
    HolidayCreate,
    HolidayRead,
    LeaveBalanceRead,
    LeaveDecision,
    LeavePolicyCreate,
    LeavePolicyRead,
    LeavePreviewRead,
    LeaveRequestCreate,
    LeaveRequestRead,
    LeaveTypeCreate,
    LeaveTypeRead,
    WhoIsOutRead,
)
from app.services.leave import service as leave_service

router = APIRouter(prefix="/leave", tags=["leave"])

HR_ONLY = [Depends(require_roles(*permissions.HR_ROLES))]


# ── Catalogue ───────────────────────────────────────────────────────────────


@router.get("/types", response_model=list[LeaveTypeRead])
async def list_leave_types(user: CurrentUser, session: DbSession) -> list[LeaveTypeRead]:
    rows = await leave_service.list_types(session, user)
    return [LeaveTypeRead.model_validate(row) for row in rows]


@router.post(
    "/types",
    response_model=LeaveTypeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def create_leave_type(
    payload: LeaveTypeCreate, user: CurrentUser, session: DbSession, request: Request
) -> LeaveTypeRead:
    row = await leave_service.create_type(session, user, payload, request=request)
    return LeaveTypeRead.model_validate(row)


@router.get("/policies", response_model=list[LeavePolicyRead])
async def list_leave_policies(user: CurrentUser, session: DbSession) -> list[LeavePolicyRead]:
    rows = await leave_service.list_policies(session, user)
    return [LeavePolicyRead.model_validate(row) for row in rows]


@router.post(
    "/policies",
    response_model=LeavePolicyRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def create_leave_policy(
    payload: LeavePolicyCreate, user: CurrentUser, session: DbSession, request: Request
) -> LeavePolicyRead:
    row = await leave_service.create_policy(session, user, payload, request=request)
    return LeavePolicyRead.model_validate(row)


@router.get("/holidays", response_model=list[HolidayRead])
async def list_holidays(
    user: CurrentUser,
    session: DbSession,
    year: int = Query(default_factory=lambda: date.today().year),
    state_code: str | None = Query(default=None, max_length=4),
) -> list[HolidayRead]:
    """A year's holidays. A `state_code` returns that state's list plus the national ones."""
    rows = await leave_service.list_holidays(session, user, year=year, state_code=state_code)
    return [HolidayRead.model_validate(row) for row in rows]


@router.post(
    "/holidays",
    response_model=HolidayRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def create_holiday(
    payload: HolidayCreate, user: CurrentUser, session: DbSession, request: Request
) -> HolidayRead:
    row = await leave_service.create_holiday(session, user, payload, request=request)
    return HolidayRead.model_validate(row)


# ── Balances ────────────────────────────────────────────────────────────────


@router.get("/balances", response_model=list[LeaveBalanceRead])
async def list_balances(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
    year: int = Query(default_factory=lambda: date.today().year, ge=2000, le=2100),
) -> list[LeaveBalanceRead]:
    return await leave_service.balances(session, user, employee_id=employee_id, year=year)


@router.post("/accruals/run", response_model=AccrualRunRead, dependencies=HR_ONLY)
async def run_accruals(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    year: int = Query(default_factory=lambda: date.today().year, ge=2000, le=2100),
) -> AccrualRunRead:
    return await leave_service.run_accruals(session, user, year=year, request=request)


# ── Requests ────────────────────────────────────────────────────────────────


@router.post("/requests/preview", response_model=LeavePreviewRead)
async def preview_request(
    payload: LeaveRequestCreate, user: CurrentUser, session: DbSession
) -> LeavePreviewRead:
    return await leave_service.preview(session, user, payload)


@router.post(
    "/requests", response_model=LeaveRequestRead, status_code=status.HTTP_201_CREATED
)
async def create_request(
    payload: LeaveRequestCreate, user: CurrentUser, session: DbSession, request: Request
) -> LeaveRequestRead:
    return await leave_service.apply(session, user, payload, request=request)


@router.get("/requests", response_model=Page[LeaveRequestRead])
async def list_requests(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
    request_status: LeaveRequestStatus | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
) -> Page[LeaveRequestRead]:
    items, total = await leave_service.list_requests(
        session,
        user,
        employee_id=employee_id,
        request_status=request_status,
        page=page,
        page_size=page_size,
    )
    return Page[LeaveRequestRead](items=items, total=total, page=page, page_size=page_size)


@router.get("/approvals", response_model=list[LeaveRequestRead])
async def list_approvals(user: CurrentUser, session: DbSession) -> list[LeaveRequestRead]:
    return await leave_service.approvals_inbox(session, user)


@router.post("/requests/{request_id}/approve", response_model=LeaveRequestRead)
async def approve_request(
    request_id: uuid.UUID,
    payload: LeaveDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> LeaveRequestRead:
    return await leave_service.decide(
        session, user, request_id, approve=True, note=payload.note, request=request
    )


@router.post("/requests/{request_id}/reject", response_model=LeaveRequestRead)
async def reject_request(
    request_id: uuid.UUID,
    payload: LeaveDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> LeaveRequestRead:
    return await leave_service.decide(
        session, user, request_id, approve=False, note=payload.note, request=request
    )


@router.post("/requests/{request_id}/cancel", response_model=LeaveRequestRead)
async def cancel_request(
    request_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> LeaveRequestRead:
    return await leave_service.cancel(session, user, request_id, request=request)


@router.get("/who-is-out", response_model=list[WhoIsOutRead])
async def who_is_out(
    user: CurrentUser,
    session: DbSession,
    start: date,
    end: date,
) -> list[WhoIsOutRead]:
    return await leave_service.who_is_out(session, user, start=start, end=end)
