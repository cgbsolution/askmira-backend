"""Attendance persistence: punches, recomputation, locks and the LOP feed.

The arithmetic lives in `computation.py`. This module is the database side —
fetching what a day needs, writing the derived result, and enforcing the two
rules payroll depends on:

- `AttendancePunch` is insert-only. There is no update or delete function here,
  by design.
- Payroll cannot run against an unlocked attendance period. Assuming zero LOP
  silently overpays, which is worse than blocking the run.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import HTTPException, Request, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.attendance import (
    AttendanceDay,
    AttendancePeriodLock,
    AttendancePunch,
    AttendanceRegularisation,
    AttendanceStatus,
    PunchDirection,
    PunchSource,
    RegularisationStatus,
    Roster,
    RosterAssignment,
    Shift,
    ShiftAssignment,
    WorkLocation,
)
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import Holiday, LeaveRequest, LeaveRequestStatus, LeaveType
from app.models.user import User
from app.services import audit
from app.services.attendance.computation import (
    ComputedDay,
    DayInputs,
    PunchRecord,
    attendance_window,
    compute_day,
)

EARTH_RADIUS_METRES = 6_371_000


class AttendanceLockError(RuntimeError):
    """Attendance for the requested period is not finalised, or is locked."""


# ── Geofencing ──────────────────────────────────────────────────────────────


def haversine_metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points.

    Accurate enough for a geofence of a few hundred metres, and cheap — this
    runs on every mobile punch.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METRES * math.asin(math.sqrt(a))


async def _evaluate_geofence(
    session: AsyncSession, user: User, latitude: float | None, longitude: float | None
) -> tuple[bool, int | None]:
    """Return `(outside_geofence, distance_to_nearest_metres)`.

    A punch with no coordinates is not "outside" — it simply was not geofenced,
    which is the normal case for a biometric device.
    """
    if latitude is None or longitude is None:
        return False, None

    stmt = tenant_scoped(
        select(WorkLocation).where(WorkLocation.active.is_(True)), WorkLocation, user
    )
    locations = (await session.scalars(stmt)).all()
    if not locations:
        # No geofences configured means nothing to be outside of.
        return False, None

    nearest = min(
        haversine_metres(latitude, longitude, float(loc.latitude), float(loc.longitude))
        - loc.radius_metres
        for loc in locations
    )
    return nearest > 0, int(round(nearest))


# ── Punches ─────────────────────────────────────────────────────────────────


async def record_punch(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    punched_at: datetime,
    direction: PunchDirection,
    source: PunchSource,
    device_id: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    raw_payload: dict | None = None,
    request: Request | None = None,
    commit: bool = True,
) -> AttendancePunch:
    """Store one punch.

    A punch outside every geofence is stored and flagged, never rejected. An
    employee who cannot punch stops trying, and then there is no data at all —
    which is a worse outcome than a flagged punch HR can judge.
    """
    outside, distance = await _evaluate_geofence(session, user, latitude, longitude)

    punch = AttendancePunch(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        punched_at=punched_at,
        direction=direction,
        source=source,
        device_id=device_id,
        latitude=latitude,
        longitude=longitude,
        outside_geofence=outside,
        nearest_location_metres=distance,
        raw_payload=raw_payload or {},
    )
    session.add(punch)
    await session.flush()

    if commit:
        await session.commit()
        await session.refresh(punch)
    return punch


# ── Inputs for a day ────────────────────────────────────────────────────────


async def _resolve_shift(
    session: AsyncSession, user: User, employee_id: uuid.UUID, on_date: date
) -> Shift | None:
    """The shift in force on a date — a direct assignment, or a roster position.

    A direct shift assignment wins over a roster: a temporary reassignment is
    the more specific statement of intent.
    """
    assignment = await session.scalar(
        tenant_scoped(
            select(ShiftAssignment)
            .where(
                ShiftAssignment.employee_id == employee_id,
                ShiftAssignment.effective_from <= on_date,
                or_(
                    ShiftAssignment.effective_to.is_(None),
                    ShiftAssignment.effective_to >= on_date,
                ),
            )
            .order_by(ShiftAssignment.effective_from.desc()),
            ShiftAssignment,
            user,
        )
    )
    if assignment is not None:
        return assignment.shift

    roster_assignment = await session.scalar(
        tenant_scoped(
            select(RosterAssignment)
            .where(
                RosterAssignment.employee_id == employee_id,
                RosterAssignment.effective_from <= on_date,
                or_(
                    RosterAssignment.effective_to.is_(None),
                    RosterAssignment.effective_to >= on_date,
                ),
            )
            .order_by(RosterAssignment.effective_from.desc()),
            RosterAssignment,
            user,
        )
    )
    if roster_assignment is None:
        return None

    roster: Roster = roster_assignment.roster
    if roster.cycle_days <= 0:
        return None

    # Position in the rotation. Modulo handles dates before the anchor too.
    offset = (on_date - roster_assignment.cycle_start_date).days % roster.cycle_days
    day = next((d for d in roster.days if d.day_index == offset), None)
    return day.shift if day is not None else None


async def _holidays_for(
    session: AsyncSession, user: User, start: date, end: date
) -> dict[date, str]:
    stmt = tenant_scoped(
        select(Holiday).where(Holiday.holiday_date >= start, Holiday.holiday_date <= end),
        Holiday,
        user,
    )
    return {h.holiday_date: h.name for h in (await session.scalars(stmt)).all()}


async def _leave_days_for(
    session: AsyncSession, user: User, employee_id: uuid.UUID, start: date, end: date
) -> dict[date, tuple[bool, bool]]:
    """Approved leave by date → `(is_half_day, is_paid)`."""
    stmt = (
        tenant_scoped(
            select(LeaveRequest, LeaveType)
            .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
            .where(
                LeaveRequest.employee_id == employee_id,
                LeaveRequest.status == LeaveRequestStatus.APPROVED,
                LeaveRequest.start_date <= end,
                LeaveRequest.end_date >= start,
            ),
            LeaveRequest,
            user,
        )
    )
    result: dict[date, tuple[bool, bool]] = {}
    for leave, leave_type in (await session.execute(stmt)).all():
        cursor = max(leave.start_date, start)
        last = min(leave.end_date, end)
        while cursor <= last:
            result[cursor] = (leave.is_half_day, leave_type.is_paid)
            cursor += timedelta(days=1)
    return result


async def _regularisations_for(
    session: AsyncSession, user: User, employee_id: uuid.UUID, start: date, end: date
) -> dict[date, AttendanceStatus]:
    stmt = tenant_scoped(
        select(AttendanceRegularisation).where(
            AttendanceRegularisation.employee_id == employee_id,
            AttendanceRegularisation.status == RegularisationStatus.APPROVED,
            AttendanceRegularisation.attendance_date >= start,
            AttendanceRegularisation.attendance_date <= end,
        ),
        AttendanceRegularisation,
        user,
    )
    return {r.attendance_date: r.requested_status for r in (await session.scalars(stmt)).all()}


# ── Recomputation ───────────────────────────────────────────────────────────


async def recompute_range(
    session: AsyncSession,
    user: User,
    *,
    employee_ids: list[uuid.UUID] | None,
    start: date,
    end: date,
    force: bool = False,
    request: Request | None = None,
) -> int:
    """Recompute `AttendanceDay` for a date range.

    Idempotent: the computation is a pure function of punches, leave, holidays
    and shift, so running it twice writes identical values.

    Refuses a locked period unless `force`, which only an explicit unlock sets.
    """
    if end < start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="End date cannot be before start date",
        )

    if not force:
        lock = await active_lock_covering(session, user, start, end)
        if lock is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Attendance for {lock.period_start} to {lock.period_end} is locked. "
                    "Unlock the period first — the unlock is audited."
                ),
            )

    if employee_ids is None:
        stmt = tenant_scoped(
            select(Employee.id).where(
                Employee.deleted_at.is_(None),
                Employee.status != EmploymentStatus.PRE_JOINING,
            ),
            Employee,
            user,
        )
        employee_ids = list((await session.scalars(stmt)).all())

    holidays = await _holidays_for(session, user, start, end)
    written = 0

    for employee_id in employee_ids:
        leave = await _leave_days_for(session, user, employee_id, start, end)
        regularisations = await _regularisations_for(session, user, employee_id, start, end)

        # Punches are fetched once for the whole range, widened by a day on
        # each side so a night shift's post-midnight OUT is available.
        punch_stmt = tenant_scoped(
            select(AttendancePunch)
            .where(
                AttendancePunch.employee_id == employee_id,
                AttendancePunch.punched_at >= datetime.combine(
                    start - timedelta(days=1), datetime.min.time(), tzinfo=UTC
                ),
                AttendancePunch.punched_at <= datetime.combine(
                    end + timedelta(days=2), datetime.min.time(), tzinfo=UTC
                ),
            )
            .order_by(AttendancePunch.punched_at),
            AttendancePunch,
            user,
        )
        all_punches = list((await session.scalars(punch_stmt)).all())

        existing_stmt = tenant_scoped(
            select(AttendanceDay).where(
                AttendanceDay.employee_id == employee_id,
                AttendanceDay.attendance_date >= start,
                AttendanceDay.attendance_date <= end,
            ),
            AttendanceDay,
            user,
        )
        existing = {d.attendance_date: d for d in (await session.scalars(existing_stmt)).all()}

        cursor = start
        while cursor <= end:
            shift = await _resolve_shift(session, user, employee_id, cursor)

            if shift is not None:
                window_start, window_end = attendance_window(shift, cursor)
                day_punches = [
                    PunchRecord(punched_at=p.punched_at, direction=p.direction)
                    for p in all_punches
                    if window_start <= p.punched_at <= window_end
                ]
            else:
                day_punches = [
                    PunchRecord(punched_at=p.punched_at, direction=p.direction)
                    for p in all_punches
                    if p.punched_at.date() == cursor
                ]

            leave_entry = leave.get(cursor)
            computed = compute_day(
                DayInputs(
                    employee_id=employee_id,
                    attendance_date=cursor,
                    shift=shift,
                    punches=day_punches,
                    is_holiday=cursor in holidays,
                    holiday_name=holidays.get(cursor),
                    on_approved_leave=leave_entry is not None,
                    leave_is_half_day=leave_entry[0] if leave_entry else False,
                    leave_is_paid=leave_entry[1] if leave_entry else True,
                    regularised_status=regularisations.get(cursor),
                )
            )

            _upsert_day(session, user, employee_id, shift, computed, existing.get(cursor))
            written += 1
            cursor += timedelta(days=1)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="attendance_day",
        actor=user,
        summary=(
            f"Recomputed attendance for {len(employee_ids)} employee(s), "
            f"{start} to {end} ({written} days)"
        ),
        request=request,
    )
    await session.commit()
    return written


def _upsert_day(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    shift: Shift | None,
    computed: ComputedDay,
    existing: AttendanceDay | None,
) -> None:
    """Write the derived day.

    Updates in place when a row already exists, so recomputation converges on
    the same values rather than accumulating duplicates.
    """
    values = {
        "shift_id": shift.id if shift is not None else None,
        "first_in": computed.first_in,
        "last_out": computed.last_out,
        "worked_minutes": computed.worked_minutes,
        "status": computed.status,
        "payable_fraction": computed.payable_fraction,
        "late_by_minutes": computed.late_by_minutes,
        "early_by_minutes": computed.early_by_minutes,
        "is_regularised": computed.is_regularised,
        "computation_note": computed.note,
        "computed_at": datetime.now(UTC),
    }

    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        return

    session.add(
        AttendanceDay(
            tenant_id=user.tenant_id,
            employee_id=employee_id,
            attendance_date=computed.attendance_date,
            **values,
        )
    )


# ── LOP feed to payroll ─────────────────────────────────────────────────────


async def get_lop_days(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    period_start: date,
    period_end: date,
    *,
    require_lock: bool = True,
) -> Decimal:
    """Loss-of-pay days for one employee over a period.

    Payroll calls this instead of accepting an LOP figure as input.

    Raises:
        AttendanceLockError: when the period is not locked and `require_lock`.
            Refusing is deliberate — falling back to zero would silently
            overpay every employee who had unpaid absence that month.
    """
    if require_lock:
        lock = await active_lock_covering(session, user, period_start, period_end)
        if lock is None:
            raise AttendanceLockError(
                f"Attendance for {period_start} to {period_end} has not been finalised. "
                "Lock the attendance period before running payroll — running without it "
                "would assume zero loss of pay and overpay anyone who had unpaid absence."
            )

    stmt = tenant_scoped(
        select(AttendanceDay).where(
            AttendanceDay.employee_id == employee_id,
            AttendanceDay.attendance_date >= period_start,
            AttendanceDay.attendance_date <= period_end,
        ),
        AttendanceDay,
        user,
    )
    days = (await session.scalars(stmt)).all()

    total = Decimal(0)
    for day in days:
        if day.status in {AttendanceStatus.HOLIDAY, AttendanceStatus.WEEK_OFF}:
            continue
        total += Decimal(1) - Decimal(str(day.payable_fraction))
    return total


async def get_payable_days(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    period_start: date,
    period_end: date,
) -> Decimal:
    stmt = tenant_scoped(
        select(AttendanceDay).where(
            AttendanceDay.employee_id == employee_id,
            AttendanceDay.attendance_date >= period_start,
            AttendanceDay.attendance_date <= period_end,
        ),
        AttendanceDay,
        user,
    )
    days = (await session.scalars(stmt)).all()
    return sum((Decimal(str(d.payable_fraction)) for d in days), start=Decimal(0))


# ── Period locks ────────────────────────────────────────────────────────────


async def active_lock_covering(
    session: AsyncSession, user: User, start: date, end: date
) -> AttendancePeriodLock | None:
    """An active lock whose period fully contains the requested range."""
    stmt = tenant_scoped(
        select(AttendancePeriodLock).where(
            AttendancePeriodLock.unlocked_at.is_(None),
            AttendancePeriodLock.period_start <= start,
            AttendancePeriodLock.period_end >= end,
        ),
        AttendancePeriodLock,
        user,
    )
    return await session.scalar(stmt)


async def lock_period(
    session: AsyncSession,
    user: User,
    period_start: date,
    period_end: date,
    request: Request | None = None,
) -> AttendancePeriodLock:
    existing = await session.scalar(
        tenant_scoped(
            select(AttendancePeriodLock).where(
                and_(
                    AttendancePeriodLock.period_start == period_start,
                    AttendancePeriodLock.period_end == period_end,
                )
            ),
            AttendancePeriodLock,
            user,
        )
    )
    if existing is not None and existing.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Attendance for {period_start} to {period_end} is already locked",
        )

    if existing is not None:
        # Re-locking after an unlock reuses the row so the history of lock,
        # unlock and re-lock stays on one record.
        existing.locked_by_user_id = user.id
        existing.locked_at = datetime.now(UTC)
        existing.unlocked_at = None
        existing.unlocked_by_user_id = None
        existing.unlock_reason = None
        lock = existing
    else:
        lock = AttendancePeriodLock(
            tenant_id=user.tenant_id,
            period_start=period_start,
            period_end=period_end,
            locked_by_user_id=user.id,
            locked_at=datetime.now(UTC),
        )
        session.add(lock)

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE,
        entity_type="attendance_period_lock",
        entity_id=lock.id,
        actor=user,
        changes={"period_start": str(period_start), "period_end": str(period_end)},
        summary=f"Locked attendance for {period_start} to {period_end}",
        request=request,
    )
    await session.commit()
    await session.refresh(lock)
    return lock


async def unlock_period(
    session: AsyncSession,
    user: User,
    lock_id: uuid.UUID,
    reason: str,
    request: Request | None = None,
) -> AttendancePeriodLock:
    """Reopen a locked period. Always audited, naming the actor and the reason."""
    lock = await session.scalar(
        tenant_scoped(
            select(AttendancePeriodLock).where(AttendancePeriodLock.id == lock_id),
            AttendancePeriodLock,
            user,
        )
    )
    if lock is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lock not found")
    if not lock.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This period is already unlocked"
        )

    lock.unlocked_by_user_id = user.id
    lock.unlocked_at = datetime.now(UTC)
    lock.unlock_reason = reason

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="attendance_period_lock",
        entity_id=lock.id,
        actor=user,
        changes={
            "period_start": str(lock.period_start),
            "period_end": str(lock.period_end),
            "reason": reason,
        },
        summary=(
            f"Unlocked attendance for {lock.period_start} to {lock.period_end} "
            f"({reason})"
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(lock)
    return lock


# ── Regularisation ──────────────────────────────────────────────────────────


async def raise_regularisation(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    attendance_date: date,
    requested_status: AttendanceStatus,
    reason: str,
    request: Request | None = None,
) -> AttendanceRegularisation:
    regularisation = AttendanceRegularisation(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        attendance_date=attendance_date,
        requested_status=requested_status,
        reason=reason,
    )
    session.add(regularisation)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="attendance_regularisation",
        entity_id=regularisation.id,
        actor=user,
        summary=f"Regularisation raised for {attendance_date}",
        request=request,
    )
    await session.commit()
    await session.refresh(regularisation)
    return regularisation


async def decide_regularisation(
    session: AsyncSession,
    user: User,
    regularisation_id: uuid.UUID,
    *,
    approve: bool,
    note: str | None = None,
    request: Request | None = None,
) -> AttendanceRegularisation:
    """Approve or reject, then recompute the affected day.

    An approval that did not trigger a recompute would leave the attendance
    record disagreeing with its own regularisation.
    """
    regularisation = await session.scalar(
        tenant_scoped(
            select(AttendanceRegularisation).where(
                AttendanceRegularisation.id == regularisation_id
            ),
            AttendanceRegularisation,
            user,
        )
    )
    if regularisation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Regularisation not found"
        )
    if regularisation.status is not RegularisationStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This request was already {regularisation.status.value}",
        )

    regularisation.status = (
        RegularisationStatus.APPROVED if approve else RegularisationStatus.REJECTED
    )
    regularisation.approver_user_id = user.id
    regularisation.decided_at = datetime.now(UTC)
    regularisation.decision_note = note

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="attendance_regularisation",
        entity_id=regularisation.id,
        actor=user,
        summary=(
            f"Regularisation for {regularisation.attendance_date} "
            f"{'approved' if approve else 'rejected'}"
        ),
        request=request,
    )
    await session.commit()

    if approve:
        await recompute_range(
            session,
            user,
            employee_ids=[regularisation.employee_id],
            start=regularisation.attendance_date,
            end=regularisation.attendance_date,
            request=request,
        )

    await session.refresh(regularisation)
    return regularisation
