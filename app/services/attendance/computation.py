"""The daily attendance computation.

Pure functions over already-fetched data: no session, no I/O. The database work
lives in `service.py`. Keeping the arithmetic separable is what makes the night
shift case testable, and the night shift case is the one that breaks in
production.

═══════════════════════════════════════════════════════════════════════════════
NIGHT SHIFTS
═══════════════════════════════════════════════════════════════════════════════

When a shift spans midnight — 22:00 to 06:00 — the punches for one *working*
day fall on two *calendar* days. An OUT punch at 02:00 on the 5th belongs to
the AttendanceDay of the 4th.

The rule implemented here: for a night shift on date D, the attendance window
runs from `D + start_time` to `D+1 + end_time`, plus a tolerance on each side
for early arrival and late departure. A punch is attributed to the working day
whose window contains it.

For a day shift the window is simply D's start to D's end, with the same
tolerance. This means a day-shift employee who stays until 23:30 still has that
OUT attributed to that day, which is what an employee expects.
═══════════════════════════════════════════════════════════════════════════════

Resolution order, from the slice spec, and it matters — a holiday that is also
a week off is a holiday, and approved leave beats a stray punch:

    1. Holiday calendar for the employee's location  → HOLIDAY
    2. Weekly off from shift or roster               → WEEK_OFF
    3. Approved leave for that date                  → ON_LEAVE
    4. Pair punches, compute worked minutes
    5. Apply thresholds                              → PRESENT / HALF_DAY / ABSENT
    6. Apply grace periods before marking late/early
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from app.models.attendance import AttendanceStatus, PunchDirection, Shift

__all__ = [
    "ComputedDay",
    "DayInputs",
    "PunchRecord",
    "attendance_window",
    "compute_day",
]

# How far before a shift starts, and after it ends, a punch still counts as
# belonging to that working day. Generous on purpose: an employee who arrives
# an hour early has not started a different day.
_EARLY_TOLERANCE = timedelta(hours=4)
_LATE_TOLERANCE = timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class PunchRecord:
    """A punch, reduced to what the computation needs."""

    punched_at: datetime
    direction: PunchDirection


@dataclass(frozen=True, slots=True)
class DayInputs:
    """Everything needed to compute one employee-day."""

    employee_id: object
    attendance_date: date
    shift: Shift | None
    punches: list[PunchRecord] = field(default_factory=list)
    is_holiday: bool = False
    holiday_name: str | None = None
    # An approved leave covering this date, and whether it is a half day.
    on_approved_leave: bool = False
    leave_is_half_day: bool = False
    leave_is_paid: bool = True
    # An approved regularisation overriding the derived status.
    regularised_status: AttendanceStatus | None = None


@dataclass(frozen=True, slots=True)
class ComputedDay:
    attendance_date: date
    status: AttendanceStatus
    payable_fraction: Decimal
    worked_minutes: int = 0
    first_in: datetime | None = None
    last_out: datetime | None = None
    late_by_minutes: int = 0
    early_by_minutes: int = 0
    is_regularised: bool = False
    note: str = ""

    @property
    def is_lop(self) -> bool:
        """Whether this day costs the employee pay.

        A half day on leave-without-pay contributes 0.5 LOP days, so LOP is
        derived from `payable_fraction` rather than from the status alone.
        """
        return self.status in {AttendanceStatus.LOP, AttendanceStatus.ABSENT}

    @property
    def lop_fraction(self) -> Decimal:
        if self.status in {
            AttendanceStatus.HOLIDAY,
            AttendanceStatus.WEEK_OFF,
        }:
            return Decimal(0)
        return Decimal(1) - self.payable_fraction


def _combine(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=UTC)


def attendance_window(shift: Shift, working_day: date) -> tuple[datetime, datetime]:
    """The datetime range whose punches belong to `working_day`.

    For a night shift the window crosses midnight into the next calendar day.
    This is the single rule that makes an 02:00 OUT attach to the previous
    day's record.
    """
    start = _combine(working_day, shift.start_time)

    if shift.is_night_shift or shift.end_time <= shift.start_time:
        # Ends on the following calendar day.
        end = _combine(working_day + timedelta(days=1), shift.end_time)
    else:
        end = _combine(working_day, shift.end_time)

    return start - _EARLY_TOLERANCE, end + _LATE_TOLERANCE


def _is_weekly_off(shift: Shift | None, day: date) -> bool:
    if shift is None:
        return False
    # ISO weekday: Monday = 1 … Sunday = 7.
    return day.isoweekday() in (shift.weekly_off_days or [])


def _pair_punches(
    punches: list[PunchRecord],
) -> tuple[int, datetime | None, datetime | None, bool]:
    """Pair INs with OUTs and total the worked minutes.

    Returns `(worked_minutes, first_in, last_out, has_unpaired)`.

    An unpaired IN does not silently become a zero-hours day: the caller turns
    `has_unpaired` into NEEDS_REGULARISATION so a human looks at it. Someone who
    forgot to punch out worked that day; recording them absent is both wrong and
    the fastest way to lose employees' trust in the system.
    """
    ordered = sorted(punches, key=lambda p: p.punched_at)

    worked = timedelta()
    open_in: datetime | None = None
    first_in: datetime | None = None
    last_out: datetime | None = None
    has_unpaired = False

    for punch in ordered:
        if punch.direction is PunchDirection.IN:
            if open_in is not None:
                # Two INs with no OUT between them — the first is unpaired.
                has_unpaired = True
            open_in = punch.punched_at
            if first_in is None:
                first_in = punch.punched_at
        else:
            if open_in is None:
                # An OUT with no matching IN.
                has_unpaired = True
            else:
                worked += punch.punched_at - open_in
                open_in = None
            last_out = punch.punched_at

    if open_in is not None:
        has_unpaired = True

    return int(worked.total_seconds() // 60), first_in, last_out, has_unpaired


def compute_day(inputs: DayInputs) -> ComputedDay:
    """Compute one employee-day.

    Deterministic: the same inputs always produce the same result, which is
    what makes recomputation idempotent.
    """
    day = inputs.attendance_date
    shift = inputs.shift

    # ── An approved regularisation wins outright ────────────────────────────
    if inputs.regularised_status is not None:
        return ComputedDay(
            attendance_date=day,
            status=inputs.regularised_status,
            payable_fraction=_payable_for(inputs.regularised_status),
            is_regularised=True,
            note="Set by an approved regularisation",
        )

    # ── 1. Holiday ──────────────────────────────────────────────────────────
    if inputs.is_holiday:
        return ComputedDay(
            attendance_date=day,
            status=AttendanceStatus.HOLIDAY,
            payable_fraction=Decimal(1),
            note=inputs.holiday_name or "Public holiday",
        )

    # ── 2. Weekly off ───────────────────────────────────────────────────────
    if _is_weekly_off(shift, day):
        return ComputedDay(
            attendance_date=day,
            status=AttendanceStatus.WEEK_OFF,
            payable_fraction=Decimal(1),
            note="Weekly off",
        )

    # ── 3. Approved leave — beats any punch ─────────────────────────────────
    # Someone on approved leave who walks in and badges is still on leave. The
    # approval is the authority; the punch is noise.
    if inputs.on_approved_leave:
        if inputs.leave_is_half_day:
            # A half day on *paid* leave is a fully payable day: the employee
            # works one half and the leave balance covers the other. On
            # *unpaid* leave only the worked half is payable, so the other half
            # is loss of pay.
            return ComputedDay(
                attendance_date=day,
                status=(
                    AttendanceStatus.ON_LEAVE
                    if inputs.leave_is_paid
                    else AttendanceStatus.HALF_DAY
                ),
                payable_fraction=Decimal(1) if inputs.leave_is_paid else Decimal("0.5"),
                note=(
                    "Half day on approved paid leave"
                    if inputs.leave_is_paid
                    else "Half day on approved leave without pay"
                ),
            )
        return ComputedDay(
            attendance_date=day,
            status=AttendanceStatus.ON_LEAVE if inputs.leave_is_paid else AttendanceStatus.LOP,
            payable_fraction=Decimal(1) if inputs.leave_is_paid else Decimal(0),
            note="Approved leave" if inputs.leave_is_paid else "Approved leave without pay",
        )

    # ── No shift assigned: cannot judge the day ─────────────────────────────
    if shift is None:
        return ComputedDay(
            attendance_date=day,
            status=AttendanceStatus.NEEDS_REGULARISATION,
            payable_fraction=Decimal(0),
            note="No shift assigned for this date — attendance cannot be evaluated",
        )

    # ── 4. Pair punches ─────────────────────────────────────────────────────
    worked_minutes, first_in, last_out, has_unpaired = _pair_punches(inputs.punches)

    if not inputs.punches:
        return ComputedDay(
            attendance_date=day,
            status=AttendanceStatus.ABSENT,
            payable_fraction=Decimal(0),
            note="No punches recorded",
        )

    if has_unpaired:
        return ComputedDay(
            attendance_date=day,
            status=AttendanceStatus.NEEDS_REGULARISATION,
            payable_fraction=Decimal(0),
            worked_minutes=worked_minutes,
            first_in=first_in,
            last_out=last_out,
            note=(
                "A punch is unpaired — most likely a missed punch out. "
                "Raise a regularisation rather than treating this as an absence."
            ),
        )

    # Break time is not worked time.
    worked_minutes = max(0, worked_minutes - shift.break_minutes)

    # ── 5. Thresholds ───────────────────────────────────────────────────────
    if worked_minutes >= shift.full_day_threshold_minutes:
        status = AttendanceStatus.PRESENT
        payable = Decimal(1)
        note = "Full day"
    elif worked_minutes >= shift.half_day_threshold_minutes:
        status = AttendanceStatus.HALF_DAY
        payable = Decimal("0.5")
        note = (
            f"Worked {worked_minutes} minutes, below the full-day threshold of "
            f"{shift.full_day_threshold_minutes}"
        )
    else:
        status = AttendanceStatus.ABSENT
        payable = Decimal(0)
        note = (
            f"Worked {worked_minutes} minutes, below the half-day threshold of "
            f"{shift.half_day_threshold_minutes}"
        )

    # ── 6. Grace ────────────────────────────────────────────────────────────
    late_by = 0
    early_by = 0

    if first_in is not None:
        scheduled_in = _combine(day, shift.start_time)
        lateness = int((first_in - scheduled_in).total_seconds() // 60)
        # Grace is forgiveness, not a shifted start: 8 minutes late against a
        # 10-minute grace is 0 late, 12 minutes late is 12 — not 2.
        late_by = lateness if lateness > shift.grace_in_minutes else 0

    if last_out is not None:
        if shift.is_night_shift or shift.end_time <= shift.start_time:
            scheduled_out = _combine(day + timedelta(days=1), shift.end_time)
        else:
            scheduled_out = _combine(day, shift.end_time)
        earliness = int((scheduled_out - last_out).total_seconds() // 60)
        early_by = earliness if earliness > shift.grace_out_minutes else 0

    return ComputedDay(
        attendance_date=day,
        status=status,
        payable_fraction=payable,
        worked_minutes=worked_minutes,
        first_in=first_in,
        last_out=last_out,
        late_by_minutes=late_by,
        early_by_minutes=early_by,
        note=note,
    )


def _payable_for(status: AttendanceStatus) -> Decimal:
    return {
        AttendanceStatus.PRESENT: Decimal(1),
        AttendanceStatus.HOLIDAY: Decimal(1),
        AttendanceStatus.WEEK_OFF: Decimal(1),
        AttendanceStatus.ON_LEAVE: Decimal(1),
        AttendanceStatus.HALF_DAY: Decimal("0.5"),
        AttendanceStatus.ABSENT: Decimal(0),
        AttendanceStatus.LOP: Decimal(0),
        AttendanceStatus.NEEDS_REGULARISATION: Decimal(0),
    }[status]
