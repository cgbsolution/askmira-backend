"""Leave and time-off endpoints (FRD 5.1).

Read paths against real tables; the approval workflow lands in V1 build order
step 3. The shape of these responses is settled — the frontend can build
against them now.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, tenant_scoped
from app.models.leave import Holiday, LeaveRequest, LeaveRequestStatus, LeaveType
from app.schemas.common import Page

router = APIRouter(prefix="/leave", tags=["leave"])


class LeaveTypeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    code: str
    is_paid: bool
    allows_half_day: bool
    requires_document: bool
    is_statutory: bool


class HolidayRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    holiday_date: date
    state_code: str | None
    is_optional: bool


class LeaveRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    leave_type_id: uuid.UUID
    start_date: date
    end_date: date
    total_days: float
    is_half_day: bool
    reason: str | None
    status: LeaveRequestStatus


@router.get("/types", response_model=list[LeaveTypeRead])
async def list_leave_types(user: CurrentUser, session: DbSession) -> list[LeaveTypeRead]:
    stmt = tenant_scoped(
        select(LeaveType).where(LeaveType.is_active.is_(True)).order_by(LeaveType.name),
        LeaveType,
        user,
    )
    rows = (await session.scalars(stmt)).all()
    return [LeaveTypeRead.model_validate(row) for row in rows]


@router.get("/holidays", response_model=list[HolidayRead])
async def list_holidays(
    user: CurrentUser,
    session: DbSession,
    year: int = Query(default_factory=lambda: date.today().year),
    state_code: str | None = Query(default=None, max_length=4),
) -> list[HolidayRead]:
    """Holidays for a year.

    Filtering by `state_code` returns that state's list plus the national ones,
    which is what a location-specific calendar needs.
    """
    stmt = tenant_scoped(
        select(Holiday).where(Holiday.year == year).order_by(Holiday.holiday_date),
        Holiday,
        user,
    )
    if state_code:
        stmt = stmt.where(
            (Holiday.state_code == state_code) | (Holiday.state_code.is_(None))
        )
    rows = (await session.scalars(stmt)).all()
    return [HolidayRead.model_validate(row) for row in rows]


@router.get("/requests", response_model=Page[LeaveRequestRead])
async def list_leave_requests(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
    request_status: LeaveRequestStatus | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
) -> Page[LeaveRequestRead]:
    stmt = tenant_scoped(select(LeaveRequest), LeaveRequest, user)
    # Leave reasons are routinely medical. The list is the caller's own
    # requests, a manager's reporting line, or - for HR - everybody's.
    visible = await permissions.visible_employee_ids(session, user)
    if visible is not None:
        if employee_id is not None and employee_id not in visible:
            raise permissions.forbidden(
                "You can only see leave requests for yourself or for people "
                "who report to you."
            )
        stmt = stmt.where(LeaveRequest.employee_id.in_(visible))
    if employee_id:
        stmt = stmt.where(LeaveRequest.employee_id == employee_id)
    if request_status:
        stmt = stmt.where(LeaveRequest.status == request_status)

    rows = (
        await session.scalars(
            stmt.order_by(LeaveRequest.start_date.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return Page[LeaveRequestRead](
        items=[LeaveRequestRead.model_validate(row) for row in rows],
        total=len(rows),
        page=page,
        page_size=page_size,
    )


# TODO(V1 step 3): POST /requests, POST /requests/{id}/approve, /reject, /cancel.
# The approval chain is snapshotted onto the request at submission time so a
# later org-chart change cannot rewrite who approved what.
