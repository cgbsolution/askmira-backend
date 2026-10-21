"""Time-off endpoints (FRD 5.1, V1 build order step 3).

Thin by design: parse, gate, delegate to the services under
`app.services.leave`, serialise. Who may see or decide whose leave is decided in
the services, through `app.core.permissions`, so the rule has one implementation.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status

from app.core import permissions
from app.core.config import settings
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.leave import LeaveRequestStatus
from app.schemas.common import Page
from app.schemas.leave import (
    AccrualRunRead,
    BalanceAdjustmentCreate,
    BalanceAdjustmentRead,
    BalanceHistoryRead,
    CompOffCreate,
    CompOffDecision,
    CompOffRead,
    HolidayCreate,
    HolidayRead,
    HolidayUpdate,
    LeaveAttachmentRead,
    LeaveBalanceRead,
    LeaveDecision,
    LeavePolicyCreate,
    LeavePolicyRead,
    LeavePolicyUpdate,
    LeavePreviewRead,
    LeaveRequestCreate,
    LeaveRequestRead,
    LeaveSettingsRead,
    LeaveSettingsUpdate,
    LeaveTypeCreate,
    LeaveTypeRead,
    LeaveTypeUpdate,
    OptionalHolidaysRead,
    TeamBalancesRead,
    WhoIsOutRead,
    YearEndRead,
)
from app.services.leave import attachments, balance_report, comp_off, holiday_calendar, ledger
from app.services.leave import service as leave_service

router = APIRouter(prefix="/leave", tags=["leave"])

HR_ONLY = [Depends(require_roles(*permissions.HR_ROLES))]


def _this_year() -> int:
    return leave_service.local_today().year


# ── Catalogue ───────────────────────────────────────────────────────────────


@router.get("/types", response_model=list[LeaveTypeRead])
async def list_leave_types(
    user: CurrentUser,
    session: DbSession,
    include_inactive: bool = False,
) -> list[LeaveTypeRead]:
    rows = await leave_service.list_types(
        session, user, include_inactive=include_inactive and permissions.is_hr(user)
    )
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


@router.patch("/types/{leave_type_id}", response_model=LeaveTypeRead, dependencies=HR_ONLY)
async def update_leave_type(
    leave_type_id: uuid.UUID,
    payload: LeaveTypeUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> LeaveTypeRead:
    row = await leave_service.update_type(session, user, leave_type_id, payload, request=request)
    return LeaveTypeRead.model_validate(row)


@router.post(
    "/types/{leave_type_id}/deactivate", response_model=LeaveTypeRead, dependencies=HR_ONLY
)
async def deactivate_leave_type(
    leave_type_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> LeaveTypeRead:
    row = await leave_service.set_type_active(
        session, user, leave_type_id, active=False, request=request
    )
    return LeaveTypeRead.model_validate(row)


@router.post("/types/{leave_type_id}/activate", response_model=LeaveTypeRead, dependencies=HR_ONLY)
async def activate_leave_type(
    leave_type_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> LeaveTypeRead:
    row = await leave_service.set_type_active(
        session, user, leave_type_id, active=True, request=request
    )
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


@router.patch("/policies/{policy_id}", response_model=LeavePolicyRead, dependencies=HR_ONLY)
async def update_leave_policy(
    policy_id: uuid.UUID,
    payload: LeavePolicyUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> LeavePolicyRead:
    row = await leave_service.update_policy(session, user, policy_id, payload, request=request)
    return LeavePolicyRead.model_validate(row)


@router.post(
    "/policies/{policy_id}/deactivate", response_model=LeavePolicyRead, dependencies=HR_ONLY
)
async def deactivate_leave_policy(
    policy_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> LeavePolicyRead:
    row = await leave_service.deactivate_policy(session, user, policy_id, request=request)
    return LeavePolicyRead.model_validate(row)


@router.get("/settings", response_model=LeaveSettingsRead)
async def get_leave_settings(user: CurrentUser, session: DbSession) -> LeaveSettingsRead:
    return await leave_service.get_settings(session, user)


@router.put("/settings", response_model=LeaveSettingsRead, dependencies=HR_ONLY)
async def update_leave_settings(
    payload: LeaveSettingsUpdate, user: CurrentUser, session: DbSession, request: Request
) -> LeaveSettingsRead:
    return await leave_service.update_settings(session, user, payload, request=request)


# ── Holidays ────────────────────────────────────────────────────────────────


@router.get("/holidays", response_model=list[HolidayRead])
async def list_holidays(
    user: CurrentUser,
    session: DbSession,
    year: int = Query(default_factory=_this_year, ge=2000, le=2100),
    scope: Literal["mine", "all"] = "mine",
    state_code: str | None = Query(default=None, max_length=4),
) -> list[HolidayRead]:
    """A year's holidays: yours by default, every one with `scope=all`, or a
    state's (with the national ones) with `state_code`."""
    return await holiday_calendar.list_holidays(
        session, user, year=year, scope=scope, state_code=state_code
    )


@router.post(
    "/holidays",
    response_model=HolidayRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def create_holiday(
    payload: HolidayCreate, user: CurrentUser, session: DbSession, request: Request
) -> HolidayRead:
    return await holiday_calendar.create_holiday(session, user, payload, request=request)


@router.patch("/holidays/{holiday_id}", response_model=HolidayRead, dependencies=HR_ONLY)
async def update_holiday(
    holiday_id: uuid.UUID,
    payload: HolidayUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> HolidayRead:
    return await holiday_calendar.update_holiday(
        session, user, holiday_id, payload, request=request
    )


@router.delete(
    "/holidays/{holiday_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=HR_ONLY,
)
async def delete_holiday(
    holiday_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await holiday_calendar.delete_holiday(session, user, holiday_id, request=request)


@router.get("/optional-holidays", response_model=OptionalHolidaysRead)
async def my_optional_holidays(
    user: CurrentUser,
    session: DbSession,
    year: int = Query(default_factory=_this_year, ge=2000, le=2100),
) -> OptionalHolidaysRead:
    return await holiday_calendar.optional_holidays(session, user, year=year)


@router.post("/optional-holidays/{holiday_id}", response_model=OptionalHolidaysRead)
async def choose_optional_holiday(
    holiday_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> OptionalHolidaysRead:
    return await holiday_calendar.select_optional(session, user, holiday_id, request=request)


@router.delete("/optional-holidays/{holiday_id}", response_model=OptionalHolidaysRead)
async def withdraw_optional_holiday(
    holiday_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> OptionalHolidaysRead:
    return await holiday_calendar.withdraw_optional(session, user, holiday_id, request=request)


# ── Balances ────────────────────────────────────────────────────────────────


@router.get("/balances", response_model=list[LeaveBalanceRead])
async def list_balances(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
    year: int = Query(default_factory=_this_year, ge=2000, le=2100),
) -> list[LeaveBalanceRead]:
    return await leave_service.balances(session, user, employee_id=employee_id, year=year)


@router.get("/balances/team", response_model=TeamBalancesRead)
async def team_balances(
    user: CurrentUser,
    session: DbSession,
    year: int = Query(default_factory=_this_year, ge=2000, le=2100),
    q: str | None = Query(default=None, max_length=128),
    department_id: uuid.UUID | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
) -> TeamBalancesRead:
    """Everybody's balances for HR; a manager's reporting line otherwise."""
    return await balance_report.team_balances(
        session,
        user,
        year=year,
        q=q,
        department_id=department_id,
        page=page,
        page_size=page_size,
    )


@router.get("/balances/team.csv", response_model=None)
async def export_team_balances(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    year: int = Query(default_factory=_this_year, ge=2000, le=2100),
    q: str | None = Query(default=None, max_length=128),
    department_id: uuid.UUID | None = None,
) -> Response:
    """The team table as CSV, one row per person and leave type. Cells cannot run as formulas."""
    content = await balance_report.export_csv(
        session, user, year=year, q=q, department_id=department_id, request=request
    )
    await session.commit()
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="leave-balances-{year}.csv"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/balances/history", response_model=BalanceHistoryRead)
async def balance_history(
    user: CurrentUser,
    session: DbSession,
    leave_type_id: uuid.UUID,
    employee_id: uuid.UUID | None = None,
    year: int = Query(default_factory=_this_year, ge=2000, le=2100),
) -> BalanceHistoryRead:
    return await ledger.history(
        session, user, employee_id=employee_id, leave_type_id=leave_type_id, year=year
    )


@router.post(
    "/balances/adjustments",
    response_model=BalanceAdjustmentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=HR_ONLY,
)
async def adjust_balance(
    payload: BalanceAdjustmentCreate, user: CurrentUser, session: DbSession, request: Request
) -> BalanceAdjustmentRead:
    return await ledger.adjust(session, user, payload, request=request)


@router.post("/accruals/run", response_model=AccrualRunRead, dependencies=HR_ONLY)
async def run_accruals(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    year: int = Query(default_factory=_this_year, ge=2000, le=2100),
) -> AccrualRunRead:
    return await leave_service.run_accruals(session, user, year=year, request=request)


@router.post("/year-end/{year}", response_model=YearEndRead, dependencies=HR_ONLY)
async def close_leave_year(
    year: int, user: CurrentUser, session: DbSession, request: Request
) -> YearEndRead:
    """Carry forward and lapse for a finished year. The daily job does this on
    1 January; running it by hand again changes nothing already closed."""
    return await ledger.close_year(session, user, year=year, request=request)


# ── Requests ────────────────────────────────────────────────────────────────


@router.post(
    "/attachments", response_model=LeaveAttachmentRead, status_code=status.HTTP_201_CREATED
)
async def upload_attachment(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    file: Annotated[UploadFile, File()],
) -> LeaveAttachmentRead:
    """A supporting document (PDF, PNG or JPEG) to attach to your request."""
    if not permissions.is_hr(user):
        # A login with no employee record has no request to attach it to.
        permissions.own_employee_id(user)
    data = await file.read(settings.MAX_UPLOAD_BYTES + 1)
    return await attachments.upload(session, user, data, request=request)


@router.post("/requests/preview", response_model=LeavePreviewRead)
async def preview_request(
    payload: LeaveRequestCreate, user: CurrentUser, session: DbSession
) -> LeavePreviewRead:
    return await leave_service.preview(session, user, payload)


@router.post("/requests", response_model=LeaveRequestRead, status_code=status.HTTP_201_CREATED)
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


@router.get("/requests/{request_id}/document")
async def download_request_document(
    request_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> Response:
    filename, mime, data = await leave_service.document(session, user, request_id, request=request)
    return Response(
        content=data,
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
    department_id: uuid.UUID | None = None,
    team: bool = False,
) -> list[WhoIsOutRead]:
    return await leave_service.who_is_out(
        session, user, start=start, end=end, department_id=department_id, team_only=team
    )


# ── Comp-off ────────────────────────────────────────────────────────────────


@router.get("/comp-off", response_model=list[CompOffRead])
async def list_comp_off(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
    mine: bool = True,
) -> list[CompOffRead]:
    return await comp_off.list_claims(session, user, employee_id=employee_id, mine_only=mine)


@router.post("/comp-off", response_model=CompOffRead, status_code=status.HTTP_201_CREATED)
async def claim_comp_off(
    payload: CompOffCreate, user: CurrentUser, session: DbSession, request: Request
) -> CompOffRead:
    return await comp_off.create(session, user, payload, request=request)


@router.get("/comp-off/approvals", response_model=list[CompOffRead])
async def comp_off_approvals(user: CurrentUser, session: DbSession) -> list[CompOffRead]:
    return await comp_off.approvals(session, user)


@router.post("/comp-off/{claim_id}/approve", response_model=CompOffRead)
async def approve_comp_off(
    claim_id: uuid.UUID,
    payload: CompOffDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> CompOffRead:
    return await comp_off.decide(
        session, user, claim_id, approve=True, note=payload.note, request=request
    )


@router.post("/comp-off/{claim_id}/reject", response_model=CompOffRead)
async def reject_comp_off(
    claim_id: uuid.UUID,
    payload: CompOffDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> CompOffRead:
    return await comp_off.decide(
        session, user, claim_id, approve=False, note=payload.note, request=request
    )


@router.post("/comp-off/{claim_id}/cancel", response_model=CompOffRead)
async def cancel_comp_off(
    claim_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> CompOffRead:
    return await comp_off.cancel(session, user, claim_id, request=request)
