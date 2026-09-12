"""Attendance endpoints (slice 02).

There is deliberately no PATCH or DELETE for a punch. Corrections go through
regularisation, which leaves both the original observation and the correction
visible.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.attendance import (
    AttendanceDay,
    AttendancePeriodLock,
    AttendanceStatus,
    PunchDirection,
    PunchSource,
    RegularisationStatus,
)
from app.models.employee import Employee, EmploymentStatus
from app.models.user import UserRole
from app.services.attendance import service as attendance_service

router = APIRouter(prefix="/attendance", tags=["attendance"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)


# ── Contracts ───────────────────────────────────────────────────────────────


class PunchCreate(BaseModel):
    employee_id: uuid.UUID | None = Field(
        default=None,
        description="Omit to punch for yourself. Only HR may punch for someone else.",
    )
    punched_at: datetime | None = Field(
        default=None, description="Defaults to now. Supplied by device imports."
    )
    direction: PunchDirection
    source: PunchSource = PunchSource.WEB
    device_id: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class PunchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    punched_at: datetime
    direction: PunchDirection
    source: PunchSource
    outside_geofence: bool
    nearest_location_metres: int | None


class PunchImportRow(BaseModel):
    """One normalised row from a device export.

    Slice 04 covers speaking to devices directly; this accepts already
    normalised data so an import can happen today.
    """

    employee_code: str
    punched_at: datetime
    direction: PunchDirection
    device_id: str | None = None


class PunchImportRequest(BaseModel):
    rows: list[PunchImportRow] = Field(min_length=1, max_length=5000)


class PunchImportResult(BaseModel):
    imported: int
    skipped_unknown_employee: list[str]


class AttendanceDayRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    attendance_date: date
    status: AttendanceStatus
    payable_fraction: Decimal
    worked_minutes: int
    first_in: datetime | None
    last_out: datetime | None
    late_by_minutes: int
    early_by_minutes: int
    is_regularised: bool
    computation_note: str | None


class RecomputeRequest(BaseModel):
    employee_ids: list[uuid.UUID] | None = None
    start: date
    end: date


class RegularisationCreate(BaseModel):
    employee_id: uuid.UUID | None = None
    attendance_date: date
    requested_status: AttendanceStatus
    reason: str = Field(min_length=3, max_length=1000)


class RegularisationDecision(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=1000)


class RegularisationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    attendance_date: date
    requested_status: AttendanceStatus
    reason: str
    status: RegularisationStatus
    decided_at: datetime | None


class LockRequest(BaseModel):
    period_start: date
    period_end: date


class UnlockRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class LockRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    period_start: date
    period_end: date
    locked_at: datetime
    unlocked_at: datetime | None
    unlock_reason: str | None


class EmployeeAttendanceSummary(BaseModel):
    employee_id: uuid.UUID
    employee_code: str
    employee_name: str
    payable_days: Decimal
    lop_days: Decimal
    present_days: int
    half_days: int
    absent_days: int
    on_leave_days: int
    late_count: int
    needs_regularisation: int


class AttendanceSummaryResponse(BaseModel):
    """The screen HR Ops looks at before running payroll.

    `is_locked` is on the response because it is the gate: payroll will refuse
    until it is true.
    """

    period_start: date
    period_end: date
    is_locked: bool
    employees: list[EmployeeAttendanceSummary]


# ── Punching ────────────────────────────────────────────────────────────────


def _resolve_employee(user, supplied: uuid.UUID | None) -> uuid.UUID:
    """Whose attendance record this touches: the caller's own, unless HR."""
    return permissions.resolve_employee(
        user, supplied, detail="You can only record attendance for yourself"
    )


@router.post("/punch", response_model=PunchRead, status_code=status.HTTP_201_CREATED)
async def punch(
    payload: PunchCreate, user: CurrentUser, session: DbSession, request: Request
) -> PunchRead:
    """Record a punch.

    A punch outside every configured geofence is stored with
    `outside_geofence = true` rather than rejected.
    """
    from datetime import UTC

    employee_id = _resolve_employee(user, payload.employee_id)
    record = await attendance_service.record_punch(
        session,
        user,
        employee_id=employee_id,
        punched_at=payload.punched_at or datetime.now(UTC),
        direction=payload.direction,
        source=payload.source,
        device_id=payload.device_id,
        latitude=payload.latitude,
        longitude=payload.longitude,
        raw_payload=payload.model_dump(mode="json"),
        request=request,
    )
    return PunchRead.model_validate(record)


@router.post(
    "/import",
    response_model=PunchImportResult,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def import_punches(
    payload: PunchImportRequest, user: CurrentUser, session: DbSession, request: Request
) -> PunchImportResult:
    """Bulk import of normalised device data.

    An unknown employee code is reported rather than failing the whole batch —
    a device export routinely contains a few stale ids, and rejecting the
    batch means nobody's attendance loads.
    """
    codes = {row.employee_code for row in payload.rows}
    stmt = tenant_scoped(
        select(Employee.id, Employee.employee_code).where(Employee.employee_code.in_(codes)),
        Employee,
        user,
    )
    by_code = {code: eid for eid, code in (await session.execute(stmt)).all()}

    imported = 0
    for row in payload.rows:
        employee_id = by_code.get(row.employee_code)
        if employee_id is None:
            continue
        await attendance_service.record_punch(
            session,
            user,
            employee_id=employee_id,
            punched_at=row.punched_at,
            direction=row.direction,
            source=PunchSource.IMPORT,
            device_id=row.device_id,
            raw_payload=row.model_dump(mode="json"),
            commit=False,
        )
        imported += 1

    await session.commit()
    return PunchImportResult(
        imported=imported,
        skipped_unknown_employee=sorted(codes - set(by_code)),
    )


# ── Computed days ───────────────────────────────────────────────────────────


@router.get("/day", response_model=list[AttendanceDayRead])
async def list_days(
    user: CurrentUser,
    session: DbSession,
    employee_id: uuid.UUID | None = None,
    date_from: date = Query(alias="from"),
    date_to: date = Query(alias="to"),
) -> list[AttendanceDayRead]:
    target = _resolve_employee(user, employee_id)
    stmt = tenant_scoped(
        select(AttendanceDay)
        .where(
            AttendanceDay.employee_id == target,
            AttendanceDay.attendance_date >= date_from,
            AttendanceDay.attendance_date <= date_to,
        )
        .order_by(AttendanceDay.attendance_date),
        AttendanceDay,
        user,
    )
    return [AttendanceDayRead.model_validate(row) for row in (await session.scalars(stmt)).all()]


@router.post("/recompute", dependencies=[Depends(require_roles(*HR_ROLES))])
async def recompute(
    payload: RecomputeRequest, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, int]:
    """Recompute a date range. Idempotent — running twice changes nothing."""
    written = await attendance_service.recompute_range(
        session,
        user,
        employee_ids=payload.employee_ids,
        start=payload.start,
        end=payload.end,
        request=request,
    )
    return {"days_computed": written}


# ── Regularisation ──────────────────────────────────────────────────────────


@router.post(
    "/regularisation",
    response_model=RegularisationRead,
    status_code=status.HTTP_201_CREATED,
)
async def raise_regularisation(
    payload: RegularisationCreate, user: CurrentUser, session: DbSession, request: Request
) -> RegularisationRead:
    employee_id = _resolve_employee(user, payload.employee_id)
    record = await attendance_service.raise_regularisation(
        session,
        user,
        employee_id=employee_id,
        attendance_date=payload.attendance_date,
        requested_status=payload.requested_status,
        reason=payload.reason,
        request=request,
    )
    return RegularisationRead.model_validate(record)


@router.patch(
    "/regularisation/{regularisation_id}",
    response_model=RegularisationRead,
    dependencies=[Depends(require_roles(*HR_ROLES, UserRole.MANAGER))],
)
async def decide_regularisation(
    regularisation_id: uuid.UUID,
    payload: RegularisationDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> RegularisationRead:
    """Approve or reject. An approval recomputes the affected day."""
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
    dependencies=[Depends(require_roles(UserRole.TENANT_ADMIN, UserRole.HR_ADMIN))],
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
    dependencies=[Depends(require_roles(UserRole.TENANT_ADMIN, UserRole.HR_ADMIN))],
)
async def unlock_period(
    lock_id: uuid.UUID,
    payload: UnlockRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> LockRead:
    """Reopen a period. Audited, naming the actor and the reason."""
    lock = await attendance_service.unlock_period(
        session, user, lock_id, payload.reason, request
    )
    return LockRead.model_validate(lock)


@router.get("/locks", response_model=list[LockRead])
async def list_locks(user: CurrentUser, session: DbSession) -> list[LockRead]:
    stmt = tenant_scoped(
        select(AttendancePeriodLock).order_by(AttendancePeriodLock.period_start.desc()),
        AttendancePeriodLock,
        user,
    )
    return [LockRead.model_validate(row) for row in (await session.scalars(stmt)).all()]


# ── Summary ─────────────────────────────────────────────────────────────────


@router.get(
    "/summary",
    response_model=AttendanceSummaryResponse,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def summary(
    user: CurrentUser,
    session: DbSession,
    period_start: date,
    period_end: date,
) -> AttendanceSummaryResponse:
    """Payable days, LOP and exceptions per employee.

    This is what HR Ops reviews before running payroll, so it reports the lock
    state alongside the numbers — the numbers are not actionable until the
    period is locked.
    """
    employees = (
        await session.scalars(
            tenant_scoped(
                select(Employee)
                .where(
                    Employee.deleted_at.is_(None),
                    Employee.status != EmploymentStatus.PRE_JOINING,
                )
                .order_by(Employee.employee_code),
                Employee,
                user,
            )
        )
    ).all()

    days = (
        await session.scalars(
            tenant_scoped(
                select(AttendanceDay).where(
                    AttendanceDay.attendance_date >= period_start,
                    AttendanceDay.attendance_date <= period_end,
                ),
                AttendanceDay,
                user,
            )
        )
    ).all()

    by_employee: dict[uuid.UUID, list[AttendanceDay]] = {}
    for day in days:
        by_employee.setdefault(day.employee_id, []).append(day)

    rows: list[EmployeeAttendanceSummary] = []
    for employee in employees:
        employee_days = by_employee.get(employee.id, [])
        payable = sum(
            (Decimal(str(d.payable_fraction)) for d in employee_days), start=Decimal(0)
        )
        lop = sum(
            (
                Decimal(1) - Decimal(str(d.payable_fraction))
                for d in employee_days
                if d.status not in {AttendanceStatus.HOLIDAY, AttendanceStatus.WEEK_OFF}
            ),
            start=Decimal(0),
        )
        rows.append(
            EmployeeAttendanceSummary(
                employee_id=employee.id,
                employee_code=employee.employee_code,
                employee_name=employee.full_name,
                payable_days=payable,
                lop_days=lop,
                present_days=sum(
                    1 for d in employee_days if d.status is AttendanceStatus.PRESENT
                ),
                half_days=sum(
                    1 for d in employee_days if d.status is AttendanceStatus.HALF_DAY
                ),
                absent_days=sum(
                    1
                    for d in employee_days
                    if d.status in {AttendanceStatus.ABSENT, AttendanceStatus.LOP}
                ),
                on_leave_days=sum(
                    1 for d in employee_days if d.status is AttendanceStatus.ON_LEAVE
                ),
                late_count=sum(1 for d in employee_days if d.late_by_minutes > 0),
                needs_regularisation=sum(
                    1
                    for d in employee_days
                    if d.status is AttendanceStatus.NEEDS_REGULARISATION
                ),
            )
        )

    lock = await attendance_service.active_lock_covering(session, user, period_start, period_end)
    return AttendanceSummaryResponse(
        period_start=period_start,
        period_end=period_end,
        is_locked=lock is not None,
        employees=rows,
    )
