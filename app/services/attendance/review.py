"""Attendance reads: periods, punches, the regularisation queue and exceptions.

Who sees what is decided through `app.core.permissions`, the same as everywhere:
your own records; a manager's reporting line; everybody for HR. The approval
queue additionally never contains the caller's own requests — nobody decides
their own regularisation, HR included.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.attendance import (
    AttendanceDay,
    AttendancePeriodLock,
    AttendancePunch,
    AttendanceRegularisation,
    AttendanceStatus,
    PunchDirection,
    RegularisationKind,
    RegularisationStatus,
    Shift,
)
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import LeaveRequest, LeaveRequestStatus, LeaveType
from app.models.user import User
from app.schemas.attendance import (
    AttendanceDayRead,
    AttendanceException,
    AttendanceExceptionReport,
    AttendanceSummaryResponse,
    CalendarHoliday,
    CalendarLeave,
    EmployeeAttendanceSummary,
    MyAttendancePeriod,
    RegularisationQueueItem,
    ShiftBrief,
    TeamMemberToday,
    TeamToday,
)
from app.services.attendance import service as attendance_service
from app.services.leave.holidays import holidays_for_employees

MAX_RANGE_DAYS = 93


def check_range(start: date, end: date) -> None:
    if end < start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The end date cannot be before the start date.",
        )
    if (end - start).days >= MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Ask for at most {MAX_RANGE_DAYS} days at a time.",
        )


def summarise(employee: Employee, days: list[AttendanceDay]) -> EmployeeAttendanceSummary:
    """Payable days, LOP and exception counts for one person. Pure."""
    payable = sum((Decimal(str(d.payable_fraction)) for d in days), start=Decimal(0))
    lop = sum(
        (
            Decimal(1) - Decimal(str(d.payable_fraction))
            for d in days
            if d.status not in {AttendanceStatus.HOLIDAY, AttendanceStatus.WEEK_OFF}
        ),
        start=Decimal(0),
    )
    return EmployeeAttendanceSummary(
        employee_id=employee.id,
        employee_code=employee.employee_code,
        employee_name=employee.full_name,
        payable_days=payable,
        lop_days=lop,
        present_days=sum(1 for d in days if d.status is AttendanceStatus.PRESENT),
        half_days=sum(1 for d in days if d.status is AttendanceStatus.HALF_DAY),
        absent_days=sum(
            1 for d in days if d.status in {AttendanceStatus.ABSENT, AttendanceStatus.LOP}
        ),
        on_leave_days=sum(1 for d in days if d.status is AttendanceStatus.ON_LEAVE),
        late_count=sum(1 for d in days if d.late_by_minutes > 0),
        needs_regularisation=sum(
            1 for d in days if d.status is AttendanceStatus.NEEDS_REGULARISATION
        ),
        days_computed=len(days),
    )


async def _days(
    session: AsyncSession,
    user: User,
    start: date,
    end: date,
    employee_ids: set[uuid.UUID] | None = None,
) -> list[AttendanceDay]:
    stmt = select(AttendanceDay).where(
        AttendanceDay.attendance_date >= start, AttendanceDay.attendance_date <= end
    )
    if employee_ids is not None:
        if not employee_ids:
            return []
        stmt = stmt.where(AttendanceDay.employee_id.in_(employee_ids))
    stmt = stmt.order_by(AttendanceDay.attendance_date)
    return list((await session.scalars(tenant_scoped(stmt, AttendanceDay, user))).all())


async def _employee(session: AsyncSession, user: User, employee_id: uuid.UUID) -> Employee:
    row = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    return row


# ── Days and periods ────────────────────────────────────────────────────────


async def list_days(
    session: AsyncSession, user: User, *, employee_id: uuid.UUID, start: date, end: date
) -> list[AttendanceDay]:
    check_range(start, end)
    return await _days(session, user, start, end, {employee_id})


async def viewable_employee(
    session: AsyncSession, user: User, employee_id: uuid.UUID | None, *, detail: str
) -> uuid.UUID:
    """Whose attendance the caller may read: own, reporting line's, or anyone's for HR."""
    if employee_id is None or employee_id == user.employee_id:
        return permissions.own_employee_id(user)
    if permissions.is_hr(user) or await permissions.manages(session, user, employee_id):
        return employee_id
    raise permissions.forbidden(detail)


def _coordinate(value: object) -> float | None:
    return float(value) if value is not None else None


def _brief(shift: Shift) -> ShiftBrief:
    return ShiftBrief.model_validate(shift)


async def my_period(
    session: AsyncSession, user: User, *, employee_id: uuid.UUID, start: date, end: date
) -> MyAttendancePeriod:
    check_range(start, end)
    employee = await _employee(session, user, employee_id)
    today = await attendance_service.local_today(session, user)
    # Days that are over but were never computed (the nightly job had not run
    # yet, or the person joined back-dated) are computed now rather than shown
    # as blanks. Derived data, idempotent; locked days are left alone.
    last_past = min(end, today - timedelta(days=1))
    if last_past >= start:
        await attendance_service.recompute_range(
            session, user, employee_ids=[employee_id], start=start, end=last_past,
            skip_locked=True, missing_only=True, commit=True,
        )
    days = await _days(session, user, start, end, {employee_id})
    lock = await attendance_service.active_lock_covering(session, user, start, end)

    resolver = await attendance_service.ShiftResolver.load(
        session, user, [employee_id], today, today
    )
    plan_today = resolver.resolve(employee_id, today)

    shift_ids = {d.shift_id for d in days if d.shift_id is not None}
    shifts: dict[uuid.UUID, Shift] = {}
    if shift_ids:
        shifts = {
            row.id: row
            for row in (
                await session.scalars(
                    tenant_scoped(select(Shift).where(Shift.id.in_(shift_ids)), Shift, user)
                )
            ).all()
        }
    if plan_today.shift is not None:
        shifts[plan_today.shift.id] = plan_today.shift

    holidays = (await holidays_for_employees(session, user, [employee_id], start, end)).get(
        employee_id, {}
    )

    leave_rows = (
        await session.execute(
            tenant_scoped(
                select(LeaveRequest, LeaveType.name)
                .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
                .where(
                    LeaveRequest.employee_id == employee_id,
                    LeaveRequest.status.in_(
                        [LeaveRequestStatus.APPROVED, LeaveRequestStatus.PENDING]
                    ),
                    LeaveRequest.start_date <= end,
                    LeaveRequest.end_date >= start,
                ),
                LeaveRequest,
                user,
            )
        )
    ).all()
    leaves: list[CalendarLeave] = []
    for leave, type_name in leave_rows:
        cursor = max(leave.start_date, start)
        while cursor <= min(leave.end_date, end):
            leaves.append(
                CalendarLeave(
                    leave_date=cursor,
                    leave_type=type_name,
                    is_half_day=leave.is_half_day,
                    status=leave.status.value,
                )
            )
            cursor += timedelta(days=1)

    return MyAttendancePeriod(
        period_start=start,
        period_end=end,
        is_locked=lock is not None,
        summary=summarise(employee, days),
        days=[AttendanceDayRead.model_validate(d) for d in days],
        today=today,
        date_of_joining=employee.date_of_joining,
        shift_today=_brief(plan_today.shift) if plan_today.shift is not None else None,
        shift_today_is_default=plan_today.from_default,
        has_no_shift=plan_today.shift is None and not plan_today.roster_week_off,
        shifts=[_brief(row) for row in shifts.values()],
        holidays=[
            CalendarHoliday(holiday_date=day, name=name) for day, name in sorted(holidays.items())
        ],
        leaves=leaves,
    )


async def team_today(
    session: AsyncSession, user: User, *, department_id: uuid.UUID | None = None
) -> TeamToday:
    """Who is in, out, on leave or not in yet today — a manager's team, or everyone for HR."""
    today = await attendance_service.local_today(session, user)
    scope = await _others_in_scope(session, user)
    stmt = select(Employee).where(
        Employee.deleted_at.is_(None),
        Employee.status.not_in([EmploymentStatus.PRE_JOINING, EmploymentStatus.EXITED]),
    )
    if department_id is not None:
        stmt = stmt.where(Employee.department_id == department_id)
    if scope is not None:
        if not scope:
            return TeamToday(today=today, members=[], counts={})
        stmt = stmt.where(Employee.id.in_(scope))
    stmt = stmt.order_by(Employee.first_name)
    employees = list((await session.scalars(tenant_scoped(stmt, Employee, user))).all())
    ids = [e.id for e in employees]
    if not ids:
        return TeamToday(today=today, members=[], counts={})

    zones = await attendance_service.employee_timezones(session, user, ids)
    resolver = await attendance_service.ShiftResolver.load(session, user, ids, today, today)
    holidays = await holidays_for_employees(session, user, ids, today, today)

    punches = (
        await session.scalars(
            tenant_scoped(
                select(AttendancePunch)
                .where(
                    AttendancePunch.employee_id.in_(ids),
                    AttendancePunch.punched_at
                    >= datetime.combine(
                        today - timedelta(days=1), datetime.min.time(), tzinfo=UTC
                    ),
                )
                .order_by(AttendancePunch.punched_at),
                AttendancePunch,
                user,
            )
        )
    ).all()
    by_person: dict[uuid.UUID, list[AttendancePunch]] = {}
    for punch in punches:
        if punch.punched_at.astimezone(zones.get(punch.employee_id, UTC)).date() == today:
            by_person.setdefault(punch.employee_id, []).append(punch)

    leave_today = {
        row[0]: row[1]
        for row in (
            await session.execute(
                tenant_scoped(
                    select(LeaveRequest.employee_id, LeaveType.name)
                    .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
                    .where(
                        LeaveRequest.employee_id.in_(ids),
                        LeaveRequest.status == LeaveRequestStatus.APPROVED,
                        LeaveRequest.start_date <= today,
                        LeaveRequest.end_date >= today,
                    ),
                    LeaveRequest,
                    user,
                )
            )
        ).all()
    }
    remote_today = {
        row.employee_id: row.kind
        for row in (
            await session.scalars(
                tenant_scoped(
                    select(AttendanceRegularisation).where(
                        AttendanceRegularisation.employee_id.in_(ids),
                        AttendanceRegularisation.attendance_date == today,
                        AttendanceRegularisation.status == RegularisationStatus.APPROVED,
                        AttendanceRegularisation.kind.in_(
                            [RegularisationKind.ON_DUTY, RegularisationKind.WORK_FROM_HOME]
                        ),
                    ),
                    AttendanceRegularisation,
                    user,
                )
            )
        ).all()
    }

    members: list[TeamMemberToday] = []
    for employee in employees:
        plan = resolver.resolve(employee.id, today)
        shift = plan.shift
        own = by_person.get(employee.id, [])
        first_in = next((p.punched_at for p in own if p.direction is PunchDirection.IN), None)
        last = own[-1] if own else None
        late = 0
        if shift is not None and first_in is not None:
            zone = zones.get(employee.id, UTC)
            scheduled = datetime.combine(today, shift.start_time, tzinfo=zone)
            minutes = int((first_in - scheduled).total_seconds() // 60)
            late = minutes if minutes > shift.grace_in_minutes else 0

        detail: str | None = None
        if last is not None:
            state = "in" if last.direction is PunchDirection.IN else "out"
        elif employee.id in leave_today:
            state, detail = "on_leave", leave_today[employee.id]
        elif employee.id in remote_today:
            state = (
                "on_duty" if remote_today[employee.id] is RegularisationKind.ON_DUTY
                else "work_from_home"
            )
        elif today in holidays.get(employee.id, {}):
            state, detail = "holiday", holidays[employee.id][today]
        elif plan.roster_week_off or (
            shift is not None and today.isoweekday() in (shift.weekly_off_days or [])
        ):
            state = "week_off"
        else:
            state = "not_in"

        members.append(
            TeamMemberToday(
                employee_id=employee.id,
                employee_code=employee.employee_code,
                employee_name=employee.full_name,
                designation=employee.designation,
                state=state,
                first_in=first_in,
                last_punch_at=last.punched_at if last else None,
                last_direction=last.direction if last else None,
                last_punch_location=last.location_label if last else None,
                last_punch_outside=bool(last and last.outside_geofence),
                last_punch_latitude=_coordinate(last.latitude if last else None),
                last_punch_longitude=_coordinate(last.longitude if last else None),
                shift_name=shift.name if shift else None,
                shift_start=shift.start_time if shift else None,
                late_by_minutes=late,
                detail=detail,
            )
        )

    counts: dict[str, int] = {}
    for member in members:
        counts[member.state] = counts.get(member.state, 0) + 1
    counts["late"] = sum(1 for m in members if m.late_by_minutes > 0)
    return TeamToday(today=today, members=members, counts=counts)


async def period_summaries(
    session: AsyncSession, user: User, *, start: date, end: date
) -> AttendanceSummaryResponse:
    """What HR Ops reviews before payroll, with the lock state that gates it."""
    check_range(start, end)
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
    by_employee: dict[uuid.UUID, list[AttendanceDay]] = {}
    for day in await _days(session, user, start, end):
        by_employee.setdefault(day.employee_id, []).append(day)

    lock = await attendance_service.active_lock_covering(session, user, start, end)
    return AttendanceSummaryResponse(
        period_start=start,
        period_end=end,
        is_locked=lock is not None,
        employees=[summarise(e, by_employee.get(e.id, [])) for e in employees],
    )


async def list_punches(
    session: AsyncSession, user: User, *, employee_id: uuid.UUID, start: date, end: date
) -> list[AttendancePunch]:
    """Punches whose local date falls in the range, for the employee's timezone."""
    check_range(start, end)
    zone = (await attendance_service.employee_timezones(session, user, [employee_id])).get(
        employee_id, UTC
    )
    stmt = tenant_scoped(
        select(AttendancePunch)
        .where(
            AttendancePunch.employee_id == employee_id,
            AttendancePunch.punched_at
            >= datetime.combine(start - timedelta(days=1), datetime.min.time(), tzinfo=UTC),
            AttendancePunch.punched_at
            < datetime.combine(end + timedelta(days=2), datetime.min.time(), tzinfo=UTC),
        )
        .order_by(AttendancePunch.punched_at),
        AttendancePunch,
        user,
    )
    rows = (await session.scalars(stmt)).all()
    return [p for p in rows if start <= p.punched_at.astimezone(zone).date() <= end]


async def list_locks(session: AsyncSession, user: User) -> list[AttendancePeriodLock]:
    stmt = tenant_scoped(
        select(AttendancePeriodLock).order_by(AttendancePeriodLock.period_start.desc()),
        AttendancePeriodLock,
        user,
    )
    return list((await session.scalars(stmt)).all())


# ── Regularisations ─────────────────────────────────────────────────────────


async def list_regularisations(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    status_filter: RegularisationStatus | None = None,
) -> list[AttendanceRegularisation]:
    stmt = select(AttendanceRegularisation).where(
        AttendanceRegularisation.employee_id == employee_id
    )
    if status_filter is not None:
        stmt = stmt.where(AttendanceRegularisation.status == status_filter)
    stmt = stmt.order_by(
        AttendanceRegularisation.attendance_date.desc(),
        AttendanceRegularisation.created_at.desc(),
    )
    return list(
        (await session.scalars(tenant_scoped(stmt, AttendanceRegularisation, user))).all()
    )


async def _others_in_scope(session: AsyncSession, user: User) -> set[uuid.UUID] | None:
    """Everybody this caller may review, never including themselves.

    `None` means the whole tenant except the caller (HR).
    """
    visible = await permissions.visible_employee_ids(session, user)
    if visible is None:
        return None
    return {eid for eid in visible if eid != user.employee_id}


def _covered(locks: list[AttendancePeriodLock], on: date) -> bool:
    return any(lock.period_start <= on <= lock.period_end for lock in locks)


async def _active_locks(session: AsyncSession, user: User) -> list[AttendancePeriodLock]:
    stmt = tenant_scoped(
        select(AttendancePeriodLock).where(AttendancePeriodLock.unlocked_at.is_(None)),
        AttendancePeriodLock,
        user,
    )
    return list((await session.scalars(stmt)).all())


async def pending_regularisations(
    session: AsyncSession, user: User
) -> list[RegularisationQueueItem]:
    """Requests waiting on this person: a reporting line, or all for HR. Never their own."""
    from app.services.approvals import rules

    scope = await _others_in_scope(session, user)
    stmt = (
        select(AttendanceRegularisation, Employee)
        .join(Employee, Employee.id == AttendanceRegularisation.employee_id)
        .where(AttendanceRegularisation.status == RegularisationStatus.PENDING)
        .order_by(AttendanceRegularisation.attendance_date)
    )
    if user.employee_id is not None:
        stmt = stmt.where(AttendanceRegularisation.employee_id != user.employee_id)
    pairs = (await session.execute(tenant_scoped(stmt, AttendanceRegularisation, user))).all()
    if not permissions.is_hr(user):
        scope = scope or set()
        # Not HR: what a chain has put in front of the caller (or someone they
        # stand in for), and, for requests from before chains, the reporting line.
        acting = {str(a) for a in await rules.acting_for(session, user)}
        pairs = [
            (reg, emp)
            for reg, emp in pairs
            if (reg.approval_chain and rules.waiting_on(reg.approval_chain, acting))
            or (not reg.approval_chain and reg.employee_id in scope)
        ]
    if not pairs:
        return []

    dates = [reg.attendance_date for reg, _ in pairs]
    ids = {reg.employee_id for reg, _ in pairs}
    days = await _days(session, user, min(dates), max(dates), ids)
    current = {(d.employee_id, d.attendance_date): d for d in days}
    locks = await _active_locks(session, user)

    items: list[RegularisationQueueItem] = []
    for reg, employee in pairs:
        day = current.get((reg.employee_id, reg.attendance_date))
        items.append(
            RegularisationQueueItem(
                id=reg.id,
                employee_id=reg.employee_id,
                attendance_date=reg.attendance_date,
                kind=reg.kind,
                requested_status=reg.requested_status,
                requested_first_in=reg.requested_first_in,
                requested_last_out=reg.requested_last_out,
                reason=reg.reason,
                status=reg.status,
                decided_at=reg.decided_at,
                decision_note=reg.decision_note,
                created_at=reg.created_at,
                employee_code=employee.employee_code,
                employee_name=employee.full_name,
                current_status=day.status if day else None,
                current_note=day.computation_note if day else None,
                period_locked=_covered(locks, reg.attendance_date),
                approval_chain=list(reg.approval_chain or []),
            )
        )
    return items


# ── Exceptions ──────────────────────────────────────────────────────────────


async def exceptions_report(
    session: AsyncSession, user: User, *, start: date, end: date
) -> AttendanceExceptionReport:
    """Days somebody should look at, for the caller's team (or everybody, for HR)."""
    check_range(start, end)
    scope = await _others_in_scope(session, user)

    emp_stmt = select(Employee).where(
        Employee.deleted_at.is_(None), Employee.status != EmploymentStatus.PRE_JOINING
    )
    if scope is not None:
        if not scope:
            return AttendanceExceptionReport(
                period_start=start, period_end=end, employees_in_scope=0, exceptions=[]
            )
        emp_stmt = emp_stmt.where(Employee.id.in_(scope))
    employees = {
        e.id: e for e in (await session.scalars(tenant_scoped(emp_stmt, Employee, user))).all()
    }
    ids = set(employees)
    if not ids:
        return AttendanceExceptionReport(
            period_start=start, period_end=end, employees_in_scope=0, exceptions=[]
        )

    day_stmt = select(AttendanceDay).where(
        AttendanceDay.employee_id.in_(ids),
        AttendanceDay.attendance_date >= start,
        AttendanceDay.attendance_date <= end,
        or_(
            AttendanceDay.status.in_(
                [AttendanceStatus.NEEDS_REGULARISATION, AttendanceStatus.ABSENT]
            ),
            AttendanceDay.late_by_minutes > 0,
            AttendanceDay.early_by_minutes > 0,
        ),
    )
    days = (await session.scalars(tenant_scoped(day_stmt, AttendanceDay, user))).all()

    pending_stmt = select(
        AttendanceRegularisation.employee_id, AttendanceRegularisation.attendance_date
    ).where(
        AttendanceRegularisation.employee_id.in_(ids),
        AttendanceRegularisation.status == RegularisationStatus.PENDING,
        AttendanceRegularisation.attendance_date >= start,
        AttendanceRegularisation.attendance_date <= end,
    )
    pending = set(
        (await session.execute(tenant_scoped(pending_stmt, AttendanceRegularisation, user))).all()
    )

    out: list[AttendanceException] = []

    def add(employee_id: uuid.UUID, on: date, kind: str, detail: str, day=None) -> None:
        employee = employees[employee_id]
        out.append(
            AttendanceException(
                employee_id=employee_id,
                employee_code=employee.employee_code,
                employee_name=employee.full_name,
                attendance_date=on,
                kind=kind,
                status=day.status if day is not None else None,
                late_by_minutes=day.late_by_minutes if day is not None else 0,
                early_by_minutes=day.early_by_minutes if day is not None else 0,
                detail=detail,
                has_pending_regularisation=(employee_id, on) in pending,
            )
        )

    for day in days:
        if day.status is AttendanceStatus.NEEDS_REGULARISATION:
            add(day.employee_id, day.attendance_date, "needs_regularisation",
                day.computation_note or "Needs regularisation", day)
        elif day.status is AttendanceStatus.ABSENT:
            add(day.employee_id, day.attendance_date, "absent",
                day.computation_note or "Absent", day)
        if day.late_by_minutes > 0:
            add(day.employee_id, day.attendance_date, "late",
                f"Late by {day.late_by_minutes} minutes", day)
        if day.early_by_minutes > 0:
            add(day.employee_id, day.attendance_date, "early_out",
                f"Left {day.early_by_minutes} minutes early", day)

    zones = await attendance_service.employee_timezones(session, user, list(ids))
    punch_stmt = select(AttendancePunch).where(
        AttendancePunch.employee_id.in_(ids),
        AttendancePunch.outside_geofence.is_(True),
        AttendancePunch.punched_at
        >= datetime.combine(start - timedelta(days=1), datetime.min.time(), tzinfo=UTC),
        AttendancePunch.punched_at
        < datetime.combine(end + timedelta(days=2), datetime.min.time(), tzinfo=UTC),
    )
    for punch in (await session.scalars(tenant_scoped(punch_stmt, AttendancePunch, user))).all():
        local = punch.punched_at.astimezone(zones.get(punch.employee_id, UTC))
        if not start <= local.date() <= end:
            continue
        distance = (
            f" ({punch.nearest_location_metres} m outside the nearest work location)"
            if punch.nearest_location_metres is not None
            else ""
        )
        add(
            punch.employee_id,
            local.date(),
            "outside_geofence",
            f"Punch {punch.direction.value} at {local:%H:%M} was outside every geofence{distance}. "
            "It was recorded, not rejected.",
        )

    out.sort(key=lambda e: (e.attendance_date, e.employee_code, e.kind))
    return AttendanceExceptionReport(
        period_start=start, period_end=end, employees_in_scope=len(ids), exceptions=out
    )
