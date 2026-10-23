"""Attendance, shifts, rosters and period locks (slice 02).

There is deliberately no PATCH, PUT or DELETE for a punch. Corrections go through
regularisation, which leaves both the original observation and the correction
visible.

Who may call what:

- Everybody: punch, read their own days and punches, raise and withdraw their
  own regularisations. Taking an employee id from the request goes through
  `permissions.resolve_employee`, so only HR may name somebody else.
- Managers and HR: the regularisation queue (a manager's reporting line, all of
  it for HR, never their own) and team exceptions.
- HR: shifts, rosters, assignments, work locations, imports, recompute and the
  summary. Locking and unlocking a payroll period: admins.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.attendance import RegularisationStatus
from app.models.user import UserRole
from app.schemas.attendance import (
    AssignmentEnd,
    AssignmentRead,
    AttendanceDayRead,
    AttendanceExceptionReport,
    AttendanceSettings,
    AttendanceSettingsRead,
    AttendanceSummaryResponse,
    KioskPinSet,
    LockRead,
    LockRequest,
    MyAttendancePeriod,
    PunchCreate,
    PunchImportRequest,
    PunchImportResult,
    PunchRead,
    RecomputeRequest,
    RecomputeResult,
    RegularisationCreate,
    RegularisationDecision,
    RegularisationQueueItem,
    RegularisationRangeCreate,
    RegularisationRead,
    RosterAssignmentCreate,
    RosterCreate,
    RosterRead,
    RosterUpdate,
    ShiftAssignmentCreate,
    ShiftCreate,
    ShiftRead,
    ShiftSwapCreate,
    ShiftSwapDecision,
    ShiftSwapResponse,
    ShiftUpdate,
    TeamToday,
    UnlockRequest,
    WorkLocationCreate,
    WorkLocationRead,
)
from app.services.attendance import (
    csv_import,
    kiosk,
    punch_photos,
    review,
    setup,
    shift_swaps,
)
from app.services.attendance import service as attendance_service

router = APIRouter(prefix="/attendance", tags=["attendance"])

HR_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
ADMIN_ROLES = (UserRole.ADMIN,)


# ── Punching ────────────────────────────────────────────────────────────────


@router.post("/punch", response_model=PunchRead, status_code=status.HTTP_201_CREATED)
async def punch(
    payload: PunchCreate, user: CurrentUser, session: DbSession, request: Request
) -> PunchRead:
    """Record a punch.

    Outside every configured geofence it is stored with `outside_geofence = true`
    rather than rejected. Coordinates are kept on the punch columns only — the
    raw payload records what was asserted, not where the person was.
    """
    employee_id = permissions.resolve_employee(
        user, payload.employee_id, detail="You can only record attendance for yourself"
    )
    source = attendance_service.punch_source_for(
        user,
        employee_id=employee_id,
        punched_at=payload.punched_at,
        source=payload.source,
        device_id=payload.device_id,
        has_coordinates=payload.latitude is not None,
    )
    await attendance_service.require_location_if_configured(
        session, user, source=source, has_coordinates=payload.latitude is not None
    )
    await punch_photos.require_photo_if_configured(
        session, user, source=source, has_photo=bool(payload.photo)
    )
    photo_key = await punch_photos.store_photo(user, payload.photo) if payload.photo else None
    record = await attendance_service.record_punch(
        session,
        user,
        employee_id=employee_id,
        punched_at=payload.punched_at or datetime.now(UTC),
        direction=payload.direction,
        source=source,
        device_id=payload.device_id,
        latitude=payload.latitude,
        longitude=payload.longitude,
        raw_payload={
            "direction": payload.direction.value,
            "source": source.value,
            "recorded_by_user_id": str(user.id),
            "time_supplied_by_client": payload.punched_at is not None,
            "had_coordinates": payload.latitude is not None,
            "had_photo": photo_key is not None,
        },
        photo_key=photo_key,
        request=request,
    )
    return PunchRead.model_validate(record)


@router.get("/punches/{punch_id}/photo", response_model=None)
async def punch_photo(punch_id: uuid.UUID, user: CurrentUser, session: DbSession) -> Response:
    """The photo taken with a punch: for the person, their manager line and HR."""
    data, media = await punch_photos.photo_for(session, user, punch_id)
    return Response(
        content=data,
        media_type=media,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/punches", response_model=list[PunchRead])
async def list_punches(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
    date_from: date = Query(alias="from"),
    date_to: date = Query(alias="to"),
) -> list[PunchRead]:
    target = await review.viewable_employee(
        session, user, employee_id, detail="You can only see your own or your team's punches"
    )
    rows = await review.list_punches(
        session, user, employee_id=target, start=date_from, end=date_to
    )
    return [PunchRead.model_validate(row) for row in rows]


@router.post(
    "/import",
    response_model=PunchImportResult,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def import_punches(
    payload: PunchImportRequest, user: CurrentUser, session: DbSession, request: Request
) -> PunchImportResult:
    """Bulk import of normalised device data. Unknown codes are reported per row."""
    rows = [
        attendance_service.ImportRow(
            row=index,
            employee_code=row.employee_code,
            punched_at=row.punched_at,
            direction=row.direction,
            device_id=row.device_id,
            raw=row.model_dump(mode="json"),
        )
        for index, row in enumerate(payload.rows, start=1)
    ]
    result = await attendance_service.import_punch_rows(session, user, rows, request=request)
    return PunchImportResult.model_validate(result)


@router.post(
    "/import/csv",
    response_model=PunchImportResult,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def import_punch_csv(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    file: UploadFile = File(...),
    assumed_timezone: str = Form(default="Asia/Kolkata", max_length=64),
) -> PunchImportResult:
    """A CSV of punches: `employee_code,punched_at,direction[,device_id]`.

    Validated by its bytes (size, text, UTF-8, header), not by its name or the
    content type the browser claimed. Bad rows come back with their row number.
    """
    raw = await file.read(csv_import.MAX_BYTES + 1)
    try:
        text = csv_import.decode_csv(raw)
        parsed, errors = csv_import.parse_punch_csv(
            text, timezone_name=assumed_timezone, now=datetime.now(UTC)
        )
    except csv_import.CsvTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    except csv_import.CsvImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    rows = [
        attendance_service.ImportRow(
            row=p.row,
            employee_code=p.employee_code,
            punched_at=p.punched_at,
            direction=p.direction,
            device_id=p.device_id,
            raw={
                "row": p.row,
                "employee_code": p.employee_code,
                "punched_at_utc": p.punched_at.isoformat(),
                "direction": p.direction.value,
                "device_id": p.device_id,
                "_import": {
                    "format": "csv",
                    "assumed_timezone": assumed_timezone,
                    "timezone_was_assumed": p.timezone_assumed,
                },
            },
        )
        for p in parsed
    ]
    result = await attendance_service.import_punch_rows(
        session, user, rows, errors=[e.as_dict() for e in errors], request=request
    )
    return PunchImportResult.model_validate(result)


# ── Computed days ───────────────────────────────────────────────────────────


@router.get("/day", response_model=list[AttendanceDayRead])
async def list_days(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
    date_from: date = Query(alias="from"),
    date_to: date = Query(alias="to"),
) -> list[AttendanceDayRead]:
    target = await review.viewable_employee(
        session, user, employee_id, detail="You can only see your own or your team's attendance"
    )
    rows = await review.list_days(session, user, employee_id=target, start=date_from, end=date_to)
    return [AttendanceDayRead.model_validate(row) for row in rows]


@router.get("/period", response_model=MyAttendancePeriod)
async def my_period(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
    date_from: date = Query(alias="from"),
    date_to: date = Query(alias="to"),
) -> MyAttendancePeriod:
    """Days and totals for one person — payable days and LOP worked out here.

    Also the holidays, leave and shift a calendar needs for days not yet
    computed. Your own, your reporting line's (as their manager), or anyone's
    for HR.
    """
    target = await review.viewable_employee(
        session, user, employee_id, detail="You can only see your own or your team's attendance"
    )
    return await review.my_period(session, user, employee_id=target, start=date_from, end=date_to)


@router.get(
    "/team/today",
    response_model=TeamToday,
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def team_today(
    user: CurrentUser, session: DbSession, department_id: uuid.UUID | None = None
) -> TeamToday:
    """Who is in, out, on leave or not in yet — a manager's team, or everyone for HR."""
    return await review.team_today(session, user, department_id=department_id)


@router.post(
    "/recompute",
    response_model=RecomputeResult,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def recompute(
    payload: RecomputeRequest, user: CurrentUser, session: DbSession, request: Request
) -> RecomputeResult:
    """Recompute a date range. Idempotent; refused inside a locked period."""
    written = await attendance_service.recompute_range(
        session,
        user,
        employee_ids=payload.employee_ids,
        start=payload.start,
        end=payload.end,
        request=request,
    )
    return RecomputeResult(days_computed=written)


@router.get(
    "/summary",
    response_model=AttendanceSummaryResponse,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def summary(
    user: CurrentUser, session: DbSession, period_start: date, period_end: date
) -> AttendanceSummaryResponse:
    """Payable days, LOP and exceptions per employee, with the lock state payroll reads."""
    return await review.period_summaries(session, user, start=period_start, end=period_end)


@router.get(
    "/exceptions",
    response_model=AttendanceExceptionReport,
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def exceptions(
    user: CurrentUser,
    session: DbSession,
    date_from: date = Query(alias="from"),
    date_to: date = Query(alias="to"),
) -> AttendanceExceptionReport:
    """Days to look at: a manager's reporting line, or everybody for HR."""
    return await review.exceptions_report(session, user, start=date_from, end=date_to)


# ── Regularisation ──────────────────────────────────────────────────────────


@router.post(
    "/regularisation",
    response_model=RegularisationRead,
    status_code=status.HTTP_201_CREATED,
)
async def raise_regularisation(
    payload: RegularisationCreate, user: CurrentUser, session: DbSession, request: Request
) -> RegularisationRead:
    employee_id = permissions.resolve_employee(
        user, payload.employee_id, detail="You can only regularise your own attendance"
    )
    record = await attendance_service.raise_regularisation(
        session,
        user,
        employee_id=employee_id,
        attendance_date=payload.attendance_date,
        requested_status=payload.requested_status,
        reason=payload.reason,
        kind=payload.kind,
        requested_first_in=payload.requested_first_in,
        requested_last_out=payload.requested_last_out,
        request=request,
    )
    return RegularisationRead.model_validate(record)


@router.post(
    "/regularisations/range",
    response_model=list[RegularisationRead],
    status_code=status.HTTP_201_CREATED,
)
async def raise_regularisation_range(
    payload: RegularisationRangeCreate, user: CurrentUser, session: DbSession, request: Request
) -> list[RegularisationRead]:
    """Several days at once; week offs and holidays are skipped.

    On-duty or work-from-home for anyone, or HR marking someone's days present,
    half day or absent, applied at once.
    """
    employee_id = permissions.resolve_employee(
        user, payload.employee_id, detail="You can only request this for yourself"
    )
    records = await attendance_service.raise_regularisation_range(
        session,
        user,
        employee_id=employee_id,
        start=payload.start_date,
        end=payload.end_date,
        kind=payload.kind,
        reason=payload.reason,
        requested_status=payload.requested_status,
        request=request,
    )
    return [RegularisationRead.model_validate(row) for row in records]


@router.get("/regularisations", response_model=list[RegularisationRead])
async def list_regularisations(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
    status_filter: RegularisationStatus | None = Query(default=None, alias="status"),
) -> list[RegularisationRead]:
    target = permissions.resolve_employee(
        user, employee_id, detail="You can only see your own regularisations"
    )
    rows = await review.list_regularisations(
        session, user, employee_id=target, status_filter=status_filter
    )
    return [RegularisationRead.model_validate(row) for row in rows]


@router.get(
    "/regularisations/pending",
    response_model=list[RegularisationQueueItem],
)
async def pending_regularisations(
    user: CurrentUser, session: DbSession
) -> list[RegularisationQueueItem]:
    """Waiting on the caller: the step a chain gave them (or someone they stand in
    for), older requests from their reporting line, or all for HR. Never their own."""
    return await review.pending_regularisations(session, user)


@router.post("/regularisations/{regularisation_id}/cancel", response_model=RegularisationRead)
async def cancel_regularisation(
    regularisation_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> RegularisationRead:
    record = await attendance_service.cancel_regularisation(
        session, user, regularisation_id, request=request
    )
    return RegularisationRead.model_validate(record)


@router.patch(
    "/regularisation/{regularisation_id}",
    response_model=RegularisationRead,
)
async def decide_regularisation(
    regularisation_id: uuid.UUID,
    payload: RegularisationDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> RegularisationRead:
    """Approve or reject the current step: its approver, their delegate or HR; never your own."""
    record = await attendance_service.decide_regularisation(
        session,
        user,
        regularisation_id,
        approve=payload.approve,
        note=payload.note,
        request=request,
    )
    return RegularisationRead.model_validate(record)


# ── Period locks ────────────────────────────────────────────────────────────


@router.post(
    "/lock",
    response_model=LockRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def lock_period(
    payload: LockRequest, user: CurrentUser, session: DbSession, request: Request
) -> LockRead:
    """Finalise attendance for a period. Payroll requires this."""
    lock = await attendance_service.lock_period(
        session, user, payload.period_start, payload.period_end, request
    )
    return LockRead.model_validate(lock)


@router.post(
    "/lock/{lock_id}/unlock",
    response_model=LockRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def unlock_period(
    lock_id: uuid.UUID,
    payload: UnlockRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> LockRead:
    """Reopen a period. Audited, naming the actor and the reason."""
    lock = await attendance_service.unlock_period(session, user, lock_id, payload.reason, request)
    return LockRead.model_validate(lock)


@router.get("/locks", response_model=list[LockRead])
async def list_locks(user: CurrentUser, session: DbSession) -> list[LockRead]:
    return [LockRead.model_validate(row) for row in await review.list_locks(session, user)]


# ── Settings ────────────────────────────────────────────────────────────────


@router.get("/settings", response_model=AttendanceSettingsRead)
async def get_settings(user: CurrentUser, session: DbSession) -> AttendanceSettingsRead:
    """How punches record location here, and the work locations the punch map draws."""
    return await setup.attendance_settings_with_locations(session, user)


@router.put(
    "/settings",
    response_model=AttendanceSettings,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def update_settings(
    payload: AttendanceSettings, user: CurrentUser, session: DbSession, request: Request
) -> AttendanceSettings:
    return await setup.update_attendance_settings(session, user, payload, request)


# ── Shifts ──────────────────────────────────────────────────────────────────


@router.get(
    "/shifts",
    response_model=list[ShiftRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_shifts(
    user: CurrentUser, session: DbSession, include_inactive: bool = False
) -> list[ShiftRead]:
    rows = await setup.list_shifts(session, user, include_inactive=include_inactive)
    return [ShiftRead.model_validate(row) for row in rows]


@router.post(
    "/shifts",
    response_model=ShiftRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def create_shift(
    payload: ShiftCreate, user: CurrentUser, session: DbSession, request: Request
) -> ShiftRead:
    return ShiftRead.model_validate(await setup.create_shift(session, user, payload, request))


@router.put(
    "/shifts/{shift_id}",
    response_model=ShiftRead,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def update_shift(
    shift_id: uuid.UUID,
    payload: ShiftUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> ShiftRead:
    return ShiftRead.model_validate(
        await setup.update_shift(session, user, shift_id, payload, request)
    )


@router.post(
    "/shifts/{shift_id}/default",
    response_model=ShiftRead,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def make_default_shift(
    shift_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> ShiftRead:
    """The shift that applies to anyone with no shift or roster assignment."""
    return ShiftRead.model_validate(await setup.set_default_shift(session, user, shift_id, request))


# ── Rosters ─────────────────────────────────────────────────────────────────


@router.get(
    "/rosters",
    response_model=list[RosterRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_rosters(user: CurrentUser, session: DbSession) -> list[RosterRead]:
    return [RosterRead.model_validate(row) for row in await setup.list_rosters(session, user)]


@router.post(
    "/rosters",
    response_model=RosterRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def create_roster(
    payload: RosterCreate, user: CurrentUser, session: DbSession, request: Request
) -> RosterRead:
    return RosterRead.model_validate(await setup.create_roster(session, user, payload, request))


@router.put(
    "/rosters/{roster_id}",
    response_model=RosterRead,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def update_roster(
    roster_id: uuid.UUID,
    payload: RosterUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> RosterRead:
    return RosterRead.model_validate(
        await setup.update_roster(session, user, roster_id, payload, request)
    )


# ── Assignments ─────────────────────────────────────────────────────────────


@router.get(
    "/assignments",
    response_model=list[AssignmentRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_assignments(
    user: CurrentUser, session: DbSession, employee_id: uuid.UUID | None = None
) -> list[AssignmentRead]:
    return await setup.list_assignments(session, user, employee_id=employee_id)


@router.post(
    "/shift-assignments",
    response_model=AssignmentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def assign_shift(
    payload: ShiftAssignmentCreate, user: CurrentUser, session: DbSession, request: Request
) -> AssignmentRead:
    """Effective-dated. The previous open assignment closes the day before."""
    return await setup.assign_shift(session, user, payload, request)


@router.post(
    "/roster-assignments",
    response_model=AssignmentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def assign_roster(
    payload: RosterAssignmentCreate, user: CurrentUser, session: DbSession, request: Request
) -> AssignmentRead:
    return await setup.assign_roster(session, user, payload, request)


@router.post(
    "/assignments/{kind}/{assignment_id}/end",
    response_model=AssignmentRead,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def end_assignment(
    kind: Literal["shift", "roster"],
    assignment_id: uuid.UUID,
    payload: AssignmentEnd,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AssignmentRead:
    return await setup.end_assignment(
        session,
        user,
        kind=kind,
        assignment_id=assignment_id,
        effective_to=payload.effective_to,
        request=request,
    )


# ── Work locations ──────────────────────────────────────────────────────────


@router.get(
    "/work-locations",
    response_model=list[WorkLocationRead],
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def list_work_locations(user: CurrentUser, session: DbSession) -> list[WorkLocationRead]:
    rows = await setup.list_work_locations(session, user)
    return [WorkLocationRead.model_validate(row) for row in rows]


@router.post(
    "/work-locations",
    response_model=WorkLocationRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def create_work_location(
    payload: WorkLocationCreate, user: CurrentUser, session: DbSession, request: Request
) -> WorkLocationRead:
    return WorkLocationRead.model_validate(
        await setup.create_work_location(session, user, payload, request)
    )


# ── Shift swaps and coverage ────────────────────────────────────────────────


@router.get("/shift-swaps")
async def my_shift_swaps(user: CurrentUser, session: DbSession) -> list[dict]:
    """Swaps you asked for or were asked into."""
    return await shift_swaps.list_swaps(session, user)


@router.post("/shift-swaps", status_code=status.HTTP_201_CREATED)
async def request_shift_swap(
    payload: ShiftSwapCreate, user: CurrentUser, session: DbSession, request: Request
) -> dict:
    """Ask a colleague to trade shifts with you on one day."""
    return await shift_swaps.request_swap(
        session,
        user,
        partner_employee_id=payload.partner_employee_id,
        swap_date=payload.swap_date,
        reason=payload.reason,
        request=request,
    )


@router.post("/shift-swaps/{swap_id}/respond")
async def respond_to_shift_swap(
    swap_id: uuid.UUID,
    payload: ShiftSwapResponse,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict:
    """The colleague asked agrees or declines."""
    return await shift_swaps.respond_to_swap(
        session, user, swap_id, accept=payload.accept, request=request
    )


@router.post("/shift-swaps/{swap_id}/cancel")
async def cancel_shift_swap(
    swap_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict:
    return await shift_swaps.cancel_swap(session, user, swap_id, request=request)


@router.get(
    "/shift-swaps/pending",
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def shift_swaps_to_decide(user: CurrentUser, session: DbSession) -> list[dict]:
    """Agreed swaps waiting for you: your reporting line's, or everyone's for HR."""
    return await shift_swaps.pending_for_approver(session, user)


@router.post(
    "/shift-swaps/{swap_id}/decide",
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def decide_shift_swap(
    swap_id: uuid.UUID,
    payload: ShiftSwapDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict:
    return await shift_swaps.decide_swap(
        session, user, swap_id, approve=payload.approve, note=payload.note, request=request
    )


@router.get(
    "/coverage",
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def shift_coverage(
    user: CurrentUser,
    session: DbSession,
    start: date = Query(alias="from"),
    end: date = Query(alias="to"),
    location_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    work_area: str | None = Query(default=None, max_length=80),
) -> dict:
    """Per day and shift: scheduled, on leave, available, and any shortfall, by site and line."""
    return await shift_swaps.coverage(
        session,
        user,
        start=start,
        end=end,
        location_id=location_id,
        department_id=department_id,
        work_area=work_area,
    )


# ── Kiosk PIN ───────────────────────────────────────────────────────────────


class BadgeIssue(BaseModel):
    employee_id: uuid.UUID
    badge_number: str = Field(min_length=4, max_length=64)


@router.get("/badges", dependencies=[Depends(require_roles(*HR_ROLES))])
async def list_badges(employee_id: uuid.UUID, user: CurrentUser, session: DbSession) -> list[dict]:
    """The kiosk badges issued to one person, revoked ones included."""
    return await kiosk.list_badges(session, user, employee_id)


@router.post(
    "/badges", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_roles(*HR_ROLES))]
)
async def issue_badge(
    payload: BadgeIssue, user: CurrentUser, session: DbSession, request: Request
) -> dict:
    """Issue a card that punches at a kiosk on its own. Only a hash of its number is kept."""
    return await kiosk.issue_badge(
        session, user, payload.employee_id, payload.badge_number, request=request
    )


@router.post("/badges/{badge_id}/revoke", dependencies=[Depends(require_roles(*HR_ROLES))])
async def revoke_badge(
    badge_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict:
    return await kiosk.revoke_badge(session, user, badge_id, request=request)


@router.get("/kiosk-pin")
async def my_kiosk_pin(user: CurrentUser, session: DbSession) -> dict:
    """Whether you have a kiosk PIN, and whether it is paused. Never the PIN."""
    return await kiosk.my_pin_status(session, user)


@router.put("/kiosk-pin")
async def set_my_kiosk_pin(
    payload: KioskPinSet, user: CurrentUser, session: DbSession, request: Request
) -> dict:
    """Set or change your PIN for punching at a shared kiosk."""
    return await kiosk.set_my_pin(session, user, payload.pin, request=request)
