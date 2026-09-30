"""Approved corrections, roster offs and overtime in the day computation.

Before these, an approved missed-punch correction kept the status but threw
the corrected times away — the day read "Present, 0 minutes worked" — and a
roster's off day with no shift became loss of pay.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal

from app.models.attendance import AttendanceStatus, PunchDirection, RegularisationKind
from app.services.attendance.computation import DayInputs, compute_day, scheduled_minutes

from .test_computation import DAY, punch, shift


def at(day: date, clock: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(clock), tzinfo=UTC)


def test_missed_punch_out_takes_the_corrected_time_and_keeps_the_real_punch_in():
    result = compute_day(
        DayInputs(
            employee_id=None,
            attendance_date=DAY,
            shift=shift(),
            punches=[punch(DAY, "09:04", PunchDirection.IN)],
            regularised_status=AttendanceStatus.PRESENT,
            regularised_kind=RegularisationKind.MISSED_PUNCH,
            regularised_last_out=at(DAY, "18:30"),
        )
    )
    assert result.status is AttendanceStatus.PRESENT
    assert result.payable_fraction == Decimal(1)
    assert result.first_in == at(DAY, "09:04")
    assert result.last_out == at(DAY, "18:30")
    # 09:04 to 18:30 is 566 minutes, less the 60-minute break.
    assert result.worked_minutes == 506
    assert result.is_regularised
    assert "Missed punch" in result.note


def test_a_late_arrival_is_still_late_after_a_missed_punch_correction():
    result = compute_day(
        DayInputs(
            employee_id=None,
            attendance_date=DAY,
            shift=shift(grace_in=10),
            punches=[punch(DAY, "09:45", PunchDirection.IN)],
            regularised_status=AttendanceStatus.PRESENT,
            regularised_kind=RegularisationKind.MISSED_PUNCH,
            regularised_last_out=at(DAY, "18:00"),
        )
    )
    assert result.late_by_minutes == 45


def test_work_from_home_counts_as_present_with_no_times():
    result = compute_day(
        DayInputs(
            employee_id=None,
            attendance_date=DAY,
            shift=shift(),
            regularised_status=AttendanceStatus.PRESENT,
            regularised_kind=RegularisationKind.WORK_FROM_HOME,
        )
    )
    assert result.status is AttendanceStatus.PRESENT
    assert result.payable_fraction == Decimal(1)
    assert result.worked_minutes == 0
    assert result.note == "Work from home (approved)"


def test_a_status_correction_without_times_keeps_the_punched_times():
    result = compute_day(
        DayInputs(
            employee_id=None,
            attendance_date=DAY,
            shift=shift(),
            punches=[
                punch(DAY, "09:00", PunchDirection.IN),
                punch(DAY, "13:30", PunchDirection.OUT),
            ],
            regularised_status=AttendanceStatus.HALF_DAY,
            regularised_kind=RegularisationKind.STATUS_CHANGE,
        )
    )
    assert result.status is AttendanceStatus.HALF_DAY
    assert result.payable_fraction == Decimal("0.5")
    assert result.first_in == at(DAY, "09:00")
    assert result.last_out == at(DAY, "13:30")


def test_a_roster_off_day_is_a_week_off_not_loss_of_pay():
    result = compute_day(
        DayInputs(employee_id=None, attendance_date=DAY, shift=None, roster_week_off=True)
    )
    assert result.status is AttendanceStatus.WEEK_OFF
    assert result.payable_fraction == Decimal(1)
    assert result.lop_fraction == Decimal(0)


def test_overtime_is_work_beyond_the_scheduled_hours():
    general = shift()  # 09:00-18:00 with an hour's break: 480 scheduled minutes
    assert scheduled_minutes(general) == 480
    result = compute_day(
        DayInputs(
            employee_id=None,
            attendance_date=DAY,
            shift=general,
            punches=[
                punch(DAY, "09:00", PunchDirection.IN),
                punch(DAY, "20:00", PunchDirection.OUT),
            ],
        )
    )
    assert result.status is AttendanceStatus.PRESENT
    assert result.worked_minutes == 600
    assert result.overtime_minutes == 120


def test_a_night_shift_schedules_across_midnight():
    assert scheduled_minutes(shift(start="22:00", end="06:00", night=True)) == 420


def test_no_overtime_on_a_half_day():
    result = compute_day(
        DayInputs(
            employee_id=None,
            attendance_date=DAY,
            shift=shift(),
            punches=[
                punch(DAY, "09:00", PunchDirection.IN),
                punch(DAY, "15:00", PunchDirection.OUT),
            ],
        )
    )
    assert result.status is AttendanceStatus.HALF_DAY
    assert result.overtime_minutes == 0


def test_an_off_day_inside_unpaid_leave_is_loss_of_pay_under_the_sandwich_rule():
    result = compute_day(
        DayInputs(
            employee_id=None,
            attendance_date=date(2026, 4, 11),  # Saturday
            shift=shift(),
            on_approved_leave=True,
            leave_is_paid=False,
            sandwiched_unpaid_leave=True,
        )
    )
    assert result.status is AttendanceStatus.LOP
    assert result.payable_fraction == Decimal(0)


def test_sandwiched_days_are_found_only_when_the_leave_charged_them():
    from app.services.attendance.service import UnpaidLeaveSpan, _sandwiched_unpaid_days

    def weekend(day: date) -> bool:
        return day.isoweekday() in (6, 7)

    friday, monday = date(2026, 4, 10), date(2026, 4, 13)
    charged = UnpaidLeaveSpan(start=friday, end=monday, total_days=Decimal(4))
    not_charged = UnpaidLeaveSpan(start=friday, end=monday, total_days=Decimal(2))
    assert _sandwiched_unpaid_days([charged], weekend) == {date(2026, 4, 11), date(2026, 4, 12)}
    assert _sandwiched_unpaid_days([not_charged], weekend) == set()
