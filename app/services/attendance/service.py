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
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, Request, status
from sqlalchemy import and_, or_, select
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
    PunchSource,
    RegularisationKind,
    RegularisationStatus,
    Roster,
    RosterAssignment,
    Shift,
    ShiftAssignment,
    WorkLocation,
)
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import LeaveRequest, LeaveRequestStatus, LeaveType
from app.models.notifications import Channel, DigestMode
from app.models.org import Location
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services import audit
from app.services.attendance.computation import (
    ComputedDay,
    DayInputs,
    PunchRecord,
    attendance_window,
    compute_day,
)
from app.services.leave.holidays import holidays_for_employees
from app.services.notifications.dispatch import notify, register_event

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
) -> tuple[bool, int | None, str | None]:
    """Return `(outside_geofence, metres_beyond_nearest_edge, nearest_location_name)`.

    The distance is measured to the nearest location's edge, so it is negative
    inside a geofence.

    A punch with no coordinates is not "outside" — it simply was not geofenced,
    which is the normal case for a biometric device.
    """
    if latitude is None or longitude is None:
        return False, None, None

    stmt = tenant_scoped(
        select(WorkLocation).where(WorkLocation.active.is_(True)), WorkLocation, user
    )
    locations = (await session.scalars(stmt)).all()
    if not locations:
        # No geofences configured means nothing to be outside of.
        return False, None, None

    nearest = min(
        locations,
        key=lambda loc: haversine_metres(
            latitude, longitude, float(loc.latitude), float(loc.longitude)
        )
        - loc.radius_metres,
    )
    beyond = (
        haversine_metres(latitude, longitude, float(nearest.latitude), float(nearest.longitude))
        - nearest.radius_metres
    )
    return beyond > 0, int(round(beyond)), nearest.name


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
    outside, distance, label = await _evaluate_geofence(session, user, latitude, longitude)

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
        location_label=label,
        raw_payload=raw_payload or {},
    )
    session.add(punch)
    await session.flush()

    if commit:
        await session.commit()
        await session.refresh(punch)
    return punch


# ── Inputs for a day ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ShiftPlan:
    """What an employee is scheduled to work on one date, and why."""

    shift: Shift | None
    # A roster rotation day with no shift: a week off.
    roster_week_off: bool = False
    # Nothing was assigned; the workspace's default shift applies.
    from_default: bool = False


class ShiftResolver:
    """Which shift is in force for each employee on each date, loaded in bulk.

    Resolution order: a direct shift assignment (the most specific statement
    of intent), then a roster rotation, then the workspace's default shift.
    Three queries for any number of employees and days — resolving per
    employee-day is what made a month's recompute time out.
    """

    def __init__(
        self,
        assignments: dict[uuid.UUID, list[ShiftAssignment]],
        rosters: dict[uuid.UUID, list[RosterAssignment]],
        default_shift: Shift | None,
    ) -> None:
        self._assignments = assignments
        self._rosters = rosters
        self.default_shift = default_shift

    @classmethod
    async def load(
        cls,
        session: AsyncSession,
        user: User,
        employee_ids: list[uuid.UUID],
        start: date,
        end: date,
    ) -> ShiftResolver:
        assignments: dict[uuid.UUID, list[ShiftAssignment]] = {}
        rosters: dict[uuid.UUID, list[RosterAssignment]] = {}
        if employee_ids:
            for row in (
                await session.scalars(
                    tenant_scoped(
                        select(ShiftAssignment)
                        .where(
                            ShiftAssignment.employee_id.in_(employee_ids),
                            ShiftAssignment.effective_from <= end,
                            or_(
                                ShiftAssignment.effective_to.is_(None),
                                ShiftAssignment.effective_to >= start,
                            ),
                        )
                        .order_by(ShiftAssignment.effective_from.desc()),
                        ShiftAssignment,
                        user,
                    )
                )
            ).all():
                assignments.setdefault(row.employee_id, []).append(row)
            for row in (
                await session.scalars(
                    tenant_scoped(
                        select(RosterAssignment)
                        .where(
                            RosterAssignment.employee_id.in_(employee_ids),
                            RosterAssignment.effective_from <= end,
                            or_(
                                RosterAssignment.effective_to.is_(None),
                                RosterAssignment.effective_to >= start,
                            ),
                        )
                        .order_by(RosterAssignment.effective_from.desc()),
                        RosterAssignment,
                        user,
                    )
                )
            ).all():
                rosters.setdefault(row.employee_id, []).append(row)
        default_shift = await default_shift_for(session, user)
        return cls(assignments, rosters, default_shift)

    def resolve(self, employee_id: uuid.UUID, on_date: date) -> ShiftPlan:
        for assignment in self._assignments.get(employee_id, []):
            if assignment.effective_from <= on_date and (
                assignment.effective_to is None or assignment.effective_to >= on_date
            ):
                return ShiftPlan(shift=assignment.shift)

        for roster_assignment in self._rosters.get(employee_id, []):
            if roster_assignment.effective_from <= on_date and (
                roster_assignment.effective_to is None
                or roster_assignment.effective_to >= on_date
            ):
                roster: Roster = roster_assignment.roster
                if roster.cycle_days <= 0:
                    break
                # Position in the rotation. Modulo handles dates before the anchor too.
                offset = (on_date - roster_assignment.cycle_start_date).days % roster.cycle_days
                day = next((d for d in roster.days if d.day_index == offset), None)
                if day is None:
                    break
                if day.shift is None:
                    return ShiftPlan(shift=None, roster_week_off=True)
                return ShiftPlan(shift=day.shift)

        if self.default_shift is not None:
            return ShiftPlan(shift=self.default_shift, from_default=True)
        return ShiftPlan(shift=None)


async def default_shift_for(session: AsyncSession, user: User) -> Shift | None:
    """The workspace's general shift, if HR has marked one."""
    return await session.scalar(
        tenant_scoped(
            select(Shift).where(Shift.is_default.is_(True), Shift.active.is_(True)),
            Shift,
            user,
        )
    )


async def resolve_shift_plan(
    session: AsyncSession, user: User, employee_id: uuid.UUID, on_date: date
) -> ShiftPlan:
    resolver = await ShiftResolver.load(session, user, [employee_id], on_date, on_date)
    return resolver.resolve(employee_id, on_date)


@dataclass(frozen=True, slots=True)
class UnpaidLeaveSpan:
    """An approved full-day unpaid request, for finding sandwiched off days."""

    start: date
    end: date
    total_days: Decimal


async def _leave_days_for(
    session: AsyncSession, user: User, employee_id: uuid.UUID, start: date, end: date
) -> tuple[dict[date, tuple[bool, bool]], list[UnpaidLeaveSpan]]:
    """Approved leave by date → `(is_half_day, is_paid)`, and the unpaid spans.

    Two half days on one date (a first half and a second half) are one full
    day: on leave if both halves are paid, loss of pay if neither is, and a
    half-payable day when only one is.
    """
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
    unpaid: list[UnpaidLeaveSpan] = []
    for leave, leave_type in (await session.execute(stmt)).all():
        if not leave_type.is_paid and not leave.is_half_day:
            unpaid.append(
                UnpaidLeaveSpan(
                    start=leave.start_date,
                    end=leave.end_date,
                    total_days=Decimal(str(leave.total_days)),
                )
            )
        cursor = max(leave.start_date, start)
        last = min(leave.end_date, end)
        while cursor <= last:
            entry = (leave.is_half_day, leave_type.is_paid)
            earlier = result.get(cursor)
            if earlier is not None and earlier[0] and entry[0]:
                both_paid = earlier[1] and entry[1]
                either_paid = earlier[1] or entry[1]
                # Full day paid, full day unpaid, or one paid half: half-payable.
                if both_paid:
                    entry = (False, True)
                elif either_paid:
                    entry = (True, False)
                else:
                    entry = (False, False)
            result[cursor] = entry
            cursor += timedelta(days=1)
    return result, unpaid


def _sandwiched_unpaid_days(
    spans: list[UnpaidLeaveSpan], is_off: Any
) -> set[date]:
    """Offs strictly between the first and last working day of unpaid leave that charged them.

    The leave engine charges them under a policy's sandwich rule; a request
    charged more days than it has working days is one where that happened.
    """
    found: set[date] = set()
    for span in spans:
        working: list[date] = []
        offs: list[date] = []
        cursor = span.start
        while cursor <= span.end:
            (offs if is_off(cursor) else working).append(cursor)
            cursor += timedelta(days=1)
        if len(working) >= 2 and span.total_days > len(working):
            found |= {d for d in offs if working[0] < d < working[-1]}
    return found


async def _regularisations_for(
    session: AsyncSession, user: User, employee_id: uuid.UUID, start: date, end: date
) -> dict[date, AttendanceRegularisation]:
    """The approved regularisation per date; the most recently decided one wins."""
    stmt = tenant_scoped(
        select(AttendanceRegularisation)
        .where(
            AttendanceRegularisation.employee_id == employee_id,
            AttendanceRegularisation.status == RegularisationStatus.APPROVED,
            AttendanceRegularisation.attendance_date >= start,
            AttendanceRegularisation.attendance_date <= end,
        )
        .order_by(AttendanceRegularisation.decided_at.asc().nulls_first()),
        AttendanceRegularisation,
        user,
    )
    return {r.attendance_date: r for r in (await session.scalars(stmt)).all()}


async def _locked_dates(
    session: AsyncSession, user: User, start: date, end: date
) -> set[date]:
    """Dates inside any active lock that overlaps the range."""
    locks = (
        await session.scalars(
            tenant_scoped(
                select(AttendancePeriodLock).where(
                    AttendancePeriodLock.unlocked_at.is_(None),
                    AttendancePeriodLock.period_start <= end,
                    AttendancePeriodLock.period_end >= start,
                ),
                AttendancePeriodLock,
                user,
            )
        )
    ).all()
    locked: set[date] = set()
    for lock in locks:
        cursor = max(lock.period_start, start)
        while cursor <= min(lock.period_end, end):
            locked.add(cursor)
            cursor += timedelta(days=1)
    return locked


# ── Recomputation ───────────────────────────────────────────────────────────


async def recompute_range(
    session: AsyncSession,
    user: User,
    *,
    employee_ids: list[uuid.UUID] | None,
    start: date,
    end: date,
    force: bool = False,
    skip_locked: bool = False,
    missing_only: bool = False,
    request: Request | None = None,
    commit: bool = True,
) -> int:
    """Recompute `AttendanceDay` for a date range.

    Idempotent: the computation is a pure function of punches, leave, holidays,
    regularisations and shift, so running it twice writes identical values.

    Only days that are over are written. A day whose attendance window is still
    open (today, or last night's shift still running) is left alone: judging it
    now would record a missed punch-out for somebody who simply has not left
    yet. Each person's range is also clipped to their employment — nobody is
    absent before they joined or after they left.

    A locked period is refused unless `force` (which only an explicit unlock
    sets); with `skip_locked` its days are skipped instead, for the nightly job.
    `missing_only` computes only days that have no row yet, or that were
    judged without any shift (backfill) — once a shift applies they can be judged.
    `commit=False` leaves the transaction open for a caller that writes its own
    change (a regularisation decision, a leave approval) in the same commit.
    """
    if end < start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="End date cannot be before start date",
        )

    locked: set[date] = set()
    if not force:
        if skip_locked:
            locked = await _locked_dates(session, user, start, end)
        else:
            lock = await _any_lock_overlapping(session, user, start, end)
            if lock is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Attendance for {lock.period_start} to {lock.period_end} is locked. "
                        "Unlock the period first — the unlock is audited."
                    ),
                )

    people_stmt = select(
        Employee.id, Employee.date_of_joining, Employee.date_of_exit
    ).where(
        Employee.deleted_at.is_(None),
        Employee.status != EmploymentStatus.PRE_JOINING,
    )
    if employee_ids is not None:
        people_stmt = people_stmt.where(Employee.id.in_(employee_ids))
    people = (await session.execute(tenant_scoped(people_stmt, Employee, user))).all()
    ids = [row[0] for row in people]

    holidays = await holidays_for_employees(session, user, ids, start, end)
    zones = await employee_timezones(session, user, ids)
    resolver = await ShiftResolver.load(session, user, ids, start, end)
    now = datetime.now(UTC)
    written = 0

    for employee_id, joined, left in people:
        first = max(start, joined) if joined else start
        last = min(end, left) if left else end
        if last < first:
            continue
        zone = zones.get(employee_id, UTC)

        leave, unpaid_spans = await _leave_days_for(session, user, employee_id, first, last)
        regularisations = await _regularisations_for(session, user, employee_id, first, last)
        employee_holidays = holidays.get(employee_id, {})

        def is_off(day: date, employee_id: uuid.UUID = employee_id) -> bool:
            plan = resolver.resolve(employee_id, day)
            weekly = plan.shift.weekly_off_days if plan.shift is not None else []
            return (
                plan.roster_week_off
                or day.isoweekday() in (weekly or [])
                or day in holidays.get(employee_id, {})
            )

        sandwiched = _sandwiched_unpaid_days(unpaid_spans, is_off)

        # Punches are fetched once for the whole range, widened by a day on
        # each side so a night shift's post-midnight OUT is available.
        punch_stmt = tenant_scoped(
            select(AttendancePunch)
            .where(
                AttendancePunch.employee_id == employee_id,
                AttendancePunch.punched_at >= datetime.combine(
                    first - timedelta(days=1), datetime.min.time(), tzinfo=UTC
                ),
                AttendancePunch.punched_at <= datetime.combine(
                    last + timedelta(days=2), datetime.min.time(), tzinfo=UTC
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
                AttendanceDay.attendance_date >= first,
                AttendanceDay.attendance_date <= last,
            ),
            AttendanceDay,
            user,
        )
        existing = {d.attendance_date: d for d in (await session.scalars(existing_stmt)).all()}

        cursor = first
        while cursor <= last:
            day = cursor
            cursor += timedelta(days=1)
            if day in locked or (
                missing_only and day in existing and not _unjudged(existing[day])
            ):
                continue

            plan = resolver.resolve(employee_id, day)
            shift = plan.shift
            if missing_only and day in existing and shift is None and not plan.roster_week_off:
                # Still no shift for it: judging it again would change nothing.
                continue
            if shift is not None:
                window_start, window_end = attendance_window(shift, day, zone)
            else:
                window_start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
                window_end = window_start + timedelta(days=1)
            regularisation = regularisations.get(day)
            if window_end > now and regularisation is None:
                # Still in progress (or ahead); the nightly job computes it once
                # it is over. A row left from earlier inputs — leave since
                # cancelled, say — is derived data and goes.
                stale = existing.get(day)
                if stale is not None:
                    await session.delete(stale)
                continue

            day_punches = [
                PunchRecord(punched_at=p.punched_at, direction=p.direction)
                for p in all_punches
                if window_start <= p.punched_at <= window_end
            ]
            leave_entry = leave.get(day)
            computed = compute_day(
                DayInputs(
                    employee_id=employee_id,
                    attendance_date=day,
                    shift=shift,
                    punches=day_punches,
                    is_holiday=day in employee_holidays,
                    holiday_name=employee_holidays.get(day),
                    on_approved_leave=leave_entry is not None,
                    leave_is_half_day=leave_entry[0] if leave_entry else False,
                    leave_is_paid=leave_entry[1] if leave_entry else True,
                    regularised_status=(
                        regularisation.requested_status if regularisation else None
                    ),
                    regularised_kind=regularisation.kind if regularisation else None,
                    regularised_first_in=(
                        regularisation.requested_first_in if regularisation else None
                    ),
                    regularised_last_out=(
                        regularisation.requested_last_out if regularisation else None
                    ),
                    roster_week_off=plan.roster_week_off,
                    sandwiched_unpaid_leave=day in sandwiched,
                    timezone=zone,
                )
            )

            _upsert_day(session, user, employee_id, shift, computed, existing.get(day))
            written += 1

    if written or not missing_only:
        # A backfill that found nothing to fill is not an event worth a row.
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="attendance_day",
            actor=user,
            summary=(
                f"Recomputed attendance for {len(ids)} employee(s), "
                f"{start} to {end} ({written} days)"
            ),
            request=request,
        )
    if commit:
        await session.commit()
    return written


async def _any_lock_overlapping(
    session: AsyncSession, user: User, start: date, end: date
) -> AttendancePeriodLock | None:
    return await session.scalar(
        tenant_scoped(
            select(AttendancePeriodLock).where(
                AttendancePeriodLock.unlocked_at.is_(None),
                AttendancePeriodLock.period_start <= end,
                AttendancePeriodLock.period_end >= start,
            ),
            AttendancePeriodLock,
            user,
        )
    )


def _unjudged(row: AttendanceDay) -> bool:
    """A day computed while no shift applied, and never corrected by hand."""
    return (
        row.shift_id is None
        and row.status is AttendanceStatus.NEEDS_REGULARISATION
        and not row.is_regularised
    )


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
        "overtime_minutes": computed.overtime_minutes,
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

    # Every day that is over must have a computed row before it is frozen: a
    # missing row counts as zero loss of pay, which silently overpays.
    await recompute_range(
        session, user, employee_ids=None, start=period_start, end=period_end,
        skip_locked=True, missing_only=True, request=request, commit=False,
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


async def _regularisation(
    session: AsyncSession, user: User, regularisation_id: uuid.UUID
) -> AttendanceRegularisation:
    row = await session.scalar(
        tenant_scoped(
            select(AttendanceRegularisation).where(
                AttendanceRegularisation.id == regularisation_id
            ),
            AttendanceRegularisation,
            user,
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Regularisation not found"
        )
    return row


#: On-duty and work-from-home can be planned; nothing else can be asked ahead.
ADVANCE_KINDS = frozenset({RegularisationKind.ON_DUTY, RegularisationKind.WORK_FROM_HOME})
#: How far ahead an on-duty or work-from-home day may be requested.
MAX_ADVANCE_DAYS = 60
#: The longest on-duty / work-from-home range raised in one go.
MAX_RANGE_DAYS = 31

KIND_LABELS = {
    RegularisationKind.MISSED_PUNCH: "missed punch",
    RegularisationKind.STATUS_CHANGE: "attendance correction",
    RegularisationKind.ON_DUTY: "on-duty request",
    RegularisationKind.WORK_FROM_HOME: "work-from-home request",
}

register_event(
    "attendance.regularisation.requested",
    "{employee_name} raised a {kind_label}",
    "{employee_name} has raised a {kind_label} for {attendance_date}: {reason}",
)
register_event(
    "attendance.regularisation.decided",
    "Your {kind_label} was {decision}",
    "Your {kind_label} for {attendance_date} was {decision}.{note_suffix}",
    preferences={Channel.IN_APP: DigestMode.IMMEDIATE, Channel.EMAIL: DigestMode.IMMEDIATE},
)


async def _approver_user_ids(
    session: AsyncSession, user: User, employee_id: uuid.UUID
) -> list[uuid.UUID]:
    """Who is told a request is waiting: the line manager, else HR."""
    manager_id = await session.scalar(
        tenant_scoped(select(Employee.manager_id).where(Employee.id == employee_id), Employee, user)
    )
    if manager_id is not None:
        found = (
            await session.scalars(
                tenant_scoped(
                    select(User.id).where(
                        User.employee_id == manager_id, User.is_active.is_(True)
                    ),
                    User,
                    user,
                )
            )
        ).all()
        if found:
            return list(found)
    return list(
        (
            await session.scalars(
                tenant_scoped(
                    select(User.id).where(
                        User.role.in_([UserRole.ADMIN, UserRole.SUB_ADMIN]),
                        User.is_active.is_(True),
                        or_(User.employee_id.is_(None), User.employee_id != employee_id),
                    ),
                    User,
                    user,
                )
            )
        ).all()
    )


async def _employee_name(session: AsyncSession, user: User, employee_id: uuid.UUID) -> str:
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        return "An employee"
    return employee.display_name or " ".join(
        part for part in (employee.first_name, employee.last_name) if part
    )


async def raise_regularisation(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    attendance_date: date,
    requested_status: AttendanceStatus,
    reason: str,
    kind: RegularisationKind = RegularisationKind.STATUS_CHANGE,
    requested_first_in: datetime | None = None,
    requested_last_out: datetime | None = None,
    request: Request | None = None,
    commit: bool = True,
) -> AttendanceRegularisation:
    """Raise a correction, missed punch, on-duty or work-from-home request for one day.

    Allowed for a locked period — people forget — but it cannot be approved
    until the period is unlocked, and the approver is told so.
    """
    today = await local_today(session, user)
    if kind in ADVANCE_KINDS:
        if attendance_date > today + timedelta(days=MAX_ADVANCE_DAYS):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "On-duty and work-from-home can be requested up to "
                    f"{MAX_ADVANCE_DAYS} days ahead."
                ),
            )
    elif attendance_date > today:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A regularisation corrects a day that has happened. This date is in the future.",
        )
    if kind is RegularisationKind.MISSED_PUNCH and (
        requested_first_in is None and requested_last_out is None
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A missed punch needs the time you came in, the time you left, or both.",
        )
    if (
        requested_first_in is not None
        and requested_last_out is not None
        and requested_last_out <= requested_first_in
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The corrected punch-out must be after the corrected punch-in.",
        )
    duplicate = await session.scalar(
        tenant_scoped(
            select(AttendanceRegularisation.id).where(
                AttendanceRegularisation.employee_id == employee_id,
                AttendanceRegularisation.attendance_date == attendance_date,
                AttendanceRegularisation.status == RegularisationStatus.PENDING,
            ),
            AttendanceRegularisation,
            user,
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"There is already a pending request for {attendance_date}. "
                "Withdraw it first to ask for something different."
            ),
        )

    regularisation = AttendanceRegularisation(
        tenant_id=user.tenant_id,
        employee_id=employee_id,
        attendance_date=attendance_date,
        kind=kind,
        requested_status=requested_status,
        requested_first_in=requested_first_in,
        requested_last_out=requested_last_out,
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
        changes={
            "employee_id": str(employee_id),
            "attendance_date": attendance_date.isoformat(),
            "kind": kind.value,
            "requested_status": requested_status.value,
        },
        summary=f"{KIND_LABELS[kind].capitalize()} raised for {attendance_date}",
        request=request,
    )
    recipients = [
        uid for uid in await _approver_user_ids(session, user, employee_id) if uid != user.id
    ]
    if recipients:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key="attendance.regularisation.requested",
            recipient_user_ids=recipients,
            payload={
                "employee_name": await _employee_name(session, user, employee_id),
                "kind_label": KIND_LABELS[kind],
                "attendance_date": attendance_date.strftime("%d %b %Y"),
                "reason": reason,
            },
            commit=False,
        )
    if commit:
        await session.commit()
        await session.refresh(regularisation)
    return regularisation


async def raise_regularisation_range(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    start: date,
    end: date,
    kind: RegularisationKind,
    reason: str,
    request: Request | None = None,
) -> list[AttendanceRegularisation]:
    """On-duty or work-from-home over several days, one request per working day.

    Week offs and holidays inside the range are left out: nobody needs
    permission to work from home on a Sunday.
    """
    if kind not in ADVANCE_KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only on-duty and work-from-home can be requested for a range of days.",
        )
    if end < start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The last day cannot be before the first.",
        )
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Request at most {MAX_RANGE_DAYS} days at a time.",
        )
    resolver = await ShiftResolver.load(session, user, [employee_id], start, end)
    holidays = (await holidays_for_employees(session, user, [employee_id], start, end)).get(
        employee_id, {}
    )
    created: list[AttendanceRegularisation] = []
    cursor = start
    while cursor <= end:
        day = cursor
        cursor += timedelta(days=1)
        plan = resolver.resolve(employee_id, day)
        off = plan.roster_week_off or (
            plan.shift is not None and day.isoweekday() in (plan.shift.weekly_off_days or [])
        )
        if off or day in holidays:
            continue
        created.append(
            await raise_regularisation(
                session,
                user,
                employee_id=employee_id,
                attendance_date=day,
                requested_status=AttendanceStatus.PRESENT,
                reason=reason,
                kind=kind,
                request=request,
                commit=False,
            )
        )
    if not created:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Every day in that range is a week off or a holiday.",
        )
    await session.commit()
    for row in created:
        await session.refresh(row)
    return created


async def cancel_regularisation(
    session: AsyncSession,
    user: User,
    regularisation_id: uuid.UUID,
    *,
    request: Request | None = None,
) -> AttendanceRegularisation:
    """Withdraw a pending request. Only the person who raised it can."""
    regularisation = await _regularisation(session, user, regularisation_id)
    if user.employee_id is None or regularisation.employee_id != user.employee_id:
        raise permissions.forbidden("Only the person a regularisation is about can withdraw it.")
    if regularisation.status is not RegularisationStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This request was already {regularisation.status.value}",
        )
    regularisation.status = RegularisationStatus.CANCELLED
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="attendance_regularisation",
        entity_id=regularisation.id,
        actor=user,
        summary=f"Regularisation for {regularisation.attendance_date} withdrawn",
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
    """Approve or reject; an approval recomputes the day in the same transaction.

    Only the person's manager (their reporting line) or HR may decide, and never
    for themselves. An approval inside a locked payroll period is refused: it
    would change a paid day, and that needs the audited unlock first.
    """
    regularisation = await _regularisation(session, user, regularisation_id)
    await permissions.require_manager_or_hr(
        session,
        user,
        regularisation.employee_id,
        detail="Only this person's manager or HR can decide their regularisation.",
    )
    if regularisation.status is not RegularisationStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This request was already {regularisation.status.value}",
        )
    if not approve and not (note or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Say why the regularisation is rejected.",
        )
    if approve:
        lock = await active_lock_covering(
            session, user, regularisation.attendance_date, regularisation.attendance_date
        )
        if lock is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Attendance for {lock.period_start} to {lock.period_end} is locked for "
                    "payroll. HR must unlock the period, with a recorded reason, before this "
                    "correction can change the day."
                ),
            )

    regularisation.status = (
        RegularisationStatus.APPROVED if approve else RegularisationStatus.REJECTED
    )
    regularisation.approver_user_id = user.id
    regularisation.decided_at = datetime.now(UTC)
    regularisation.decision_note = (note or "").strip() or None

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="attendance_regularisation",
        entity_id=regularisation.id,
        actor=user,
        changes={"note": regularisation.decision_note},
        summary=(
            f"Regularisation for {regularisation.attendance_date} "
            f"{'approved' if approve else 'rejected'}"
        ),
        request=request,
    )

    if approve:
        # Same transaction: an approval whose recompute failed would leave the
        # record disagreeing with its own regularisation.
        await recompute_range(
            session,
            user,
            employee_ids=[regularisation.employee_id],
            start=regularisation.attendance_date,
            end=regularisation.attendance_date,
            request=request,
            commit=False,
        )

    subject = await session.scalar(
        tenant_scoped(
            select(User.id).where(
                User.employee_id == regularisation.employee_id, User.is_active.is_(True)
            ),
            User,
            user,
        )
    )
    if subject is not None:
        await notify(
            session,
            tenant_id=user.tenant_id,
            event_key="attendance.regularisation.decided",
            recipient_user_ids=[subject],
            payload={
                "kind_label": KIND_LABELS[regularisation.kind],
                "attendance_date": regularisation.attendance_date.strftime("%d %b %Y"),
                "decision": "approved" if approve else "rejected",
                "note_suffix": (
                    f" Note: {regularisation.decision_note}" if regularisation.decision_note else ""
                ),
            },
            commit=False,
        )

    await session.commit()
    await session.refresh(regularisation)
    return regularisation


# ── Self-service punch rules ────────────────────────────────────────────────


def punch_source_for(
    user: User,
    *,
    employee_id: uuid.UUID,
    punched_at: datetime | None,
    source: PunchSource | None,
    device_id: str | None,
    has_coordinates: bool,
) -> PunchSource:
    """Decide the source of a punch recorded through the API, and refuse forgery.

    A punch somebody records for themselves — HR included — is timestamped by
    the server and comes from the web or a phone. A back-dated "biometric" punch
    typed in by the person it is about is not an observation of anything.
    HR recording a punch for somebody else (a manual correction) may set the
    time and the source.
    """
    default = PunchSource.MOBILE_GEO if has_coordinates else PunchSource.WEB
    acting_for_self = user.employee_id is not None and employee_id == user.employee_id
    if permissions.is_hr(user) and not acting_for_self:
        return source or PunchSource.MANUAL
    if punched_at is not None or device_id is not None:
        raise permissions.forbidden(
            "A punch you record for yourself is timestamped by the server. "
            "To correct a past day, raise a regularisation."
        )
    if source not in (None, PunchSource.WEB, PunchSource.MOBILE_GEO):
        raise permissions.forbidden(
            "A punch you record for yourself comes from the web or your phone."
        )
    return source or default


async def require_location_if_configured(
    session: AsyncSession, user: User, *, source: PunchSource, has_coordinates: bool
) -> None:
    """Refuse a self-service punch without a location when the workspace requires one.

    Only punches people make for themselves from the web or the app: devices,
    imports and HR's manual corrections carry no browser to ask.
    """
    if has_coordinates or source not in (PunchSource.WEB, PunchSource.MOBILE_GEO):
        return
    tenant = await session.get(Tenant, user.tenant_id)
    mode = (((tenant.settings or {}).get("attendance") or {}) if tenant else {}).get(
        "location_mode", "optional"
    )
    if mode == "required":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Your company records a location with every punch. Allow location access "
                "for this site in your browser, then punch again."
            ),
        )


# ── Bulk import ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ImportRow:
    row: int
    employee_code: str
    punched_at: datetime
    direction: PunchDirection
    device_id: str | None
    raw: dict[str, Any]


async def import_punch_rows(
    session: AsyncSession,
    user: User,
    rows: list[ImportRow],
    *,
    errors: list[dict[str, Any]] | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Store normalised punch rows; report per-row problems instead of failing.

    Deduplicates on (employee, timestamp, direction) against what is already
    stored and within the file, so importing the same export twice adds nothing.
    """
    all_errors: list[dict[str, Any]] = list(errors or [])
    codes = {r.employee_code for r in rows}
    by_code: dict[str, uuid.UUID] = {}
    if codes:
        stmt = tenant_scoped(
            select(Employee.id, Employee.employee_code).where(
                Employee.employee_code.in_(codes), Employee.deleted_at.is_(None)
            ),
            Employee,
            user,
        )
        by_code = {code: eid for eid, code in (await session.execute(stmt)).all()}

    existing: set[tuple[uuid.UUID, datetime, PunchDirection]] = set()
    known = [r for r in rows if r.employee_code in by_code]
    if known:
        stmt = tenant_scoped(
            select(
                AttendancePunch.employee_id,
                AttendancePunch.punched_at,
                AttendancePunch.direction,
            ).where(
                AttendancePunch.employee_id.in_({by_code[r.employee_code] for r in known}),
                AttendancePunch.punched_at >= min(r.punched_at for r in known),
                AttendancePunch.punched_at <= max(r.punched_at for r in known),
            ),
            AttendancePunch,
            user,
        )
        existing = {
            (eid, at, direction) for eid, at, direction in (await session.execute(stmt)).all()
        }

    imported = 0
    duplicates = 0
    unknown: set[str] = set()
    seen: set[tuple[uuid.UUID, datetime, PunchDirection]] = set()

    for row in rows:
        employee_id = by_code.get(row.employee_code)
        if employee_id is None:
            unknown.add(row.employee_code)
            all_errors.append(
                {
                    "row": row.row,
                    "employee_code": row.employee_code,
                    "error": f"No employee with code {row.employee_code}. The row was skipped.",
                }
            )
            continue
        key = (employee_id, row.punched_at, row.direction)
        if key in existing or key in seen:
            duplicates += 1
            continue
        seen.add(key)
        await record_punch(
            session,
            user,
            employee_id=employee_id,
            punched_at=row.punched_at,
            direction=row.direction,
            source=PunchSource.IMPORT,
            device_id=row.device_id,
            raw_payload=row.raw,
            commit=False,
        )
        imported += 1

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="attendance_punch",
        actor=user,
        changes={
            "imported": imported,
            "duplicates": duplicates,
            "unknown_codes": sorted(unknown),
            "row_errors": len(all_errors),
        },
        summary=(
            f"Imported {imported} punch(es); {duplicates} duplicate(s) skipped; "
            f"{len(all_errors)} row(s) with problems"
        ),
        request=request,
    )
    await session.commit()
    return {
        "imported": imported,
        "duplicates": duplicates,
        "skipped_unknown_employee": sorted(unknown),
        "errors": sorted(all_errors, key=lambda e: e["row"]),
    }


# ── Scheduler entry point ───────────────────────────────────────────────────


async def nightly_recompute(
    session: AsyncSession, user: User, *, as_of: date
) -> dict[str, Any]:
    """The nightly job's body for one tenant.

    Recomputes the last three days — a night shift that started the day before
    yesterday only closes its window yesterday morning — and backfills any day
    since the start of last month that has no row yet (the server was down, the
    job was added mid-month, someone joined with a back-dated joining date).
    Locked days are skipped, never unlocked. Idempotent.
    """
    recent_start = as_of - timedelta(days=3)
    recent_end = as_of - timedelta(days=1)
    written = await recompute_range(
        session, user, employee_ids=None, start=recent_start, end=recent_end,
        skip_locked=True, commit=False,
    )
    backfill_start = (as_of.replace(day=1) - timedelta(days=1)).replace(day=1)
    backfilled = 0
    if backfill_start < recent_start:
        backfilled = await recompute_range(
            session, user, employee_ids=None, start=backfill_start,
            end=recent_start - timedelta(days=1), skip_locked=True, missing_only=True,
            commit=False,
        )
    await session.commit()
    return {
        "from": recent_start.isoformat(),
        "to": recent_end.isoformat(),
        "days_computed": written,
        "days_backfilled": backfilled,
        "backfill_from": backfill_start.isoformat(),
    }


# ── Timezones ───────────────────────────────────────────────────────────────

#: Where an employee with no work location is judged. The product is
#: India-first; a tenant elsewhere sets `settings["timezone"]`.
DEFAULT_TIMEZONE = "Asia/Kolkata"


def _zone(name: str | None, fallback: tzinfo = UTC) -> tzinfo:
    if not name:
        return fallback
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return fallback


async def tenant_timezone(session: AsyncSession, user: User) -> tzinfo:
    tenant = await session.get(Tenant, user.tenant_id)
    name = ((tenant.settings or {}).get("timezone") if tenant else None) or DEFAULT_TIMEZONE
    return _zone(name, ZoneInfo(DEFAULT_TIMEZONE))


async def local_today(session: AsyncSession, user: User) -> date:
    """Today where the workspace is — not the server's UTC date.

    Between midnight and 05:30 IST the UTC date is still yesterday; using it
    let people "regularise" tomorrow and hid today's punches.
    """
    return datetime.now(await tenant_timezone(session, user)).date()


async def employee_timezones(
    session: AsyncSession, user: User, employee_ids: list[uuid.UUID] | set[uuid.UUID]
) -> dict[uuid.UUID, tzinfo]:
    """Where each person works, as a timezone.

    Shift times are wall-clock times at the place of work. Comparing a 09:00
    shift start with a punch stored in UTC as if both were UTC marks everybody
    in India five and a half hours early — so the location's timezone is
    applied, and someone with no location is judged in the workspace's zone
    (India by default), never in UTC.
    """
    ids = list(employee_ids)
    if not ids:
        return {}
    fallback = await tenant_timezone(session, user)
    stmt = tenant_scoped(
        select(Employee.id, Location.timezone)
        .outerjoin(Location, Location.id == Employee.location_id)
        .where(Employee.id.in_(ids)),
        Employee,
        user,
    )
    zones = {eid: _zone(name, fallback) for eid, name in (await session.execute(stmt)).all()}
    return {eid: zones.get(eid, fallback) for eid in ids}
