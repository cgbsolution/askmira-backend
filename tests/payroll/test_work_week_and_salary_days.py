"""Work weeks (5-day, 6-day, alternate Saturdays) and how salary is divided into days.

Pure rules: which dates are weekly offs, what a leave request costs on an
alternate-Saturday week, how an unpaid holiday is paid, and the payroll divisor
under each salary-days basis.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest

from app.models.attendance import AttendanceStatus, PunchDirection, Shift, week_of_month
from app.schemas.attendance import ShiftCreate
from app.services.attendance.computation import DayInputs, PunchRecord, compute_day
from app.services.leave.engine import count_leave_days
from app.services.payroll.engine import PayrollRuleError
from app.services.payroll.salary_days import salary_days


def work_week(weekly: list[int], alternate: dict[str, list[int]] | None = None) -> Shift:
    return Shift(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        code="GEN",
        name="General",
        start_time=time(9, 0),
        end_time=time(18, 0),
        break_minutes=60,
        grace_in_minutes=0,
        grace_out_minutes=0,
        half_day_threshold_minutes=240,
        full_day_threshold_minutes=480,
        is_night_shift=False,
        weekly_off_days=weekly,
        alternate_off_weeks=alternate or {},
        active=True,
    )


FIVE_DAY = work_week([6, 7])
SIX_DAY = work_week([7])
ALT_SAT = work_week([7], {"6": [2, 4]})  # Sundays, and the 2nd and 4th Saturday

# November 2026: starts on a Sunday. Saturdays: 7, 14, 21, 28. Sundays: 1, 8, 15, 22, 29.
NOV_START, NOV_END = date(2026, 11, 1), date(2026, 11, 30)


# ── Which week of the month ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("day", "week"),
    [(1, 1), (7, 1), (8, 2), (14, 2), (15, 3), (21, 3), (22, 4), (28, 4), (29, 5), (31, 5)],
)
def test_week_of_month_counts_by_date(day: int, week: int) -> None:
    assert week_of_month(date(2026, 10, day)) == week


def test_alternate_saturdays_are_the_2nd_and_4th_only() -> None:
    saturdays = [date(2026, 11, d) for d in (7, 14, 21, 28)]
    assert [ALT_SAT.is_off_on(d) for d in saturdays] == [False, True, False, True]
    assert ALT_SAT.is_off_on(date(2026, 11, 8))  # a Sunday, every week
    assert not ALT_SAT.is_off_on(date(2026, 11, 9))  # a Monday


def test_a_month_with_five_saturdays_keeps_the_5th_as_a_working_day() -> None:
    # August 2026 has Saturdays on 1, 8, 15, 22, 29.
    assert not ALT_SAT.is_off_on(date(2026, 8, 29))
    first_third_fifth = work_week([7], {"6": [1, 3, 5]})
    assert first_third_fifth.is_off_on(date(2026, 8, 29))
    assert not first_third_fifth.is_off_on(date(2026, 8, 8))


def test_working_days_in_a_month_follow_the_week() -> None:
    def working(shift: Shift) -> int:
        return sum(1 for d in range(1, 31) if not shift.is_off_on(date(2026, 11, d)))

    assert working(FIVE_DAY) == 21
    assert working(SIX_DAY) == 25
    assert working(ALT_SAT) == 23


# ── Shift schema ────────────────────────────────────────────────────────────


def _shift_payload(**extra: object) -> dict:
    return {
        "code": "ALT",
        "name": "Alternate Saturdays",
        "start_time": "09:30",
        "end_time": "18:30",
        "half_day_threshold_minutes": 240,
        "full_day_threshold_minutes": 480,
        **extra,
    }


def test_shift_schema_cleans_alternate_weeks() -> None:
    shift = ShiftCreate.model_validate(
        _shift_payload(weekly_off_days=[7], alternate_off_weeks={"6": [4, 2, 2], "1": []})
    )
    assert shift.alternate_off_weeks == {"6": [2, 4]}


def test_shift_schema_drops_alternate_weeks_for_an_every_week_off() -> None:
    shift = ShiftCreate.model_validate(
        _shift_payload(weekly_off_days=[6, 7], alternate_off_weeks={"6": [2, 4]})
    )
    assert shift.alternate_off_weeks == {}


@pytest.mark.parametrize(
    "weeks",
    [{"8": [1]}, {"6": [0]}, {"6": [6]}, {"6": [1, 2, 3, 4, 5]}, {"sat": [2]}],
)
def test_shift_schema_refuses_bad_alternate_weeks(weeks: dict) -> None:
    with pytest.raises(ValueError):
        ShiftCreate.model_validate(_shift_payload(weekly_off_days=[7], alternate_off_weeks=weeks))


# ── Leave on an alternate-Saturday week ─────────────────────────────────────


def test_leave_skips_only_the_off_saturdays() -> None:
    # Fri 13 to Mon 16 Nov: Sat 14 is the 2nd Saturday (off), Sun 15 off.
    count = count_leave_days(
        date(2026, 11, 13),
        date(2026, 11, 16),
        half_day=False,
        weekly_off_days=ALT_SAT.is_off_on,
        holidays={},
    )
    assert count.total == Decimal(2)
    # Fri 20 to Mon 23: Sat 21 is the 3rd Saturday, a working day, so charged.
    count = count_leave_days(
        date(2026, 11, 20),
        date(2026, 11, 23),
        half_day=False,
        weekly_off_days=ALT_SAT.is_off_on,
        holidays={},
    )
    assert count.total == Decimal(3)


def test_leave_still_accepts_a_weekday_list() -> None:
    count = count_leave_days(
        date(2026, 11, 13),
        date(2026, 11, 16),
        half_day=False,
        weekly_off_days=[6, 7],
        holidays={},
    )
    assert count.total == Decimal(2)


# ── Attendance on alternate Saturdays and unpaid holidays ───────────────────


def _day(day: date, **kwargs: object) -> DayInputs:
    return DayInputs(employee_id=uuid.uuid4(), attendance_date=day, shift=ALT_SAT, **kwargs)


def test_attendance_marks_the_2nd_saturday_off_and_the_3rd_absent() -> None:
    assert compute_day(_day(date(2026, 11, 14))).status is AttendanceStatus.WEEK_OFF
    third = compute_day(_day(date(2026, 11, 21)))
    assert third.status is AttendanceStatus.ABSENT
    assert third.payable_fraction == Decimal(0)


def test_a_paid_holiday_is_payable() -> None:
    day = compute_day(_day(date(2026, 11, 10), is_holiday=True, holiday_name="Diwali"))
    assert day.status is AttendanceStatus.HOLIDAY
    assert day.payable_fraction == Decimal(1)


def test_an_unpaid_holiday_is_a_day_off_without_pay() -> None:
    day = compute_day(
        _day(date(2026, 11, 10), is_holiday=True, holiday_name="Shutdown", holiday_is_paid=False)
    )
    assert day.status is AttendanceStatus.HOLIDAY
    assert day.payable_fraction == Decimal(0)
    assert "unpaid" in (day.note or "")


def test_working_an_unpaid_holiday_pays_the_day_not_overtime() -> None:
    on = date(2026, 11, 10)
    punches = [
        PunchRecord(datetime.combine(on, time(9, 0), tzinfo=UTC), PunchDirection.IN),
        PunchRecord(datetime.combine(on, time(18, 0), tzinfo=UTC), PunchDirection.OUT),
    ]
    day = compute_day(
        _day(on, is_holiday=True, holiday_name="Shutdown", holiday_is_paid=False, punches=punches)
    )
    assert day.payable_fraction == Decimal(1)
    assert day.overtime_minutes == 0


# ── Salary days ─────────────────────────────────────────────────────────────

FULL_MONTH = (NOV_START, NOV_END)


def test_calendar_basis_is_the_days_in_the_month() -> None:
    days = salary_days(
        "calendar",
        period_start=NOV_START,
        period_end=NOV_END,
        window=FULL_MONTH,
        lop_days=Decimal(2),
    )
    assert (days.working_days, days.lop_days, days.not_employed_days) == (30, 2, 0)


def test_fixed_basis_divides_by_the_fixed_days() -> None:
    days = salary_days(
        "fixed",
        period_start=NOV_START,
        period_end=NOV_END,
        window=FULL_MONTH,
        lop_days=Decimal(2),
        fixed_days=26,
    )
    assert (days.working_days, days.lop_days, days.not_employed_days) == (26, 2, 0)
    assert days.basis == "fixed"


def test_fixed_basis_scales_a_joining_month() -> None:
    # Joined 16 Nov: 15 of 30 calendar days not employed is 13 of 26.
    days = salary_days(
        "fixed",
        period_start=NOV_START,
        period_end=NOV_END,
        window=(date(2026, 11, 16), NOV_END),
        lop_days=Decimal(0),
        fixed_days=26,
    )
    assert days.not_employed_days == Decimal("13.00")
    assert days.working_days - days.not_employed_days == 13


def test_fixed_basis_never_deducts_more_than_the_month() -> None:
    # Absent all 25 working days plus 2 sandwiched Sundays on a 26-day basis.
    days = salary_days(
        "fixed",
        period_start=NOV_START,
        period_end=NOV_END,
        window=FULL_MONTH,
        lop_days=Decimal(27),
        fixed_days=26,
    )
    assert days.lop_days == 26
    assert days.note


def test_fixed_basis_without_days_falls_back_to_calendar() -> None:
    days = salary_days(
        "fixed",
        period_start=NOV_START,
        period_end=NOV_END,
        window=FULL_MONTH,
        lop_days=Decimal(0),
        fixed_days=None,
    )
    assert days.basis == "calendar"
    assert days.working_days == 30


@pytest.mark.parametrize(("shift", "expected"), [(FIVE_DAY, 21), (SIX_DAY, 25), (ALT_SAT, 23)])
def test_working_days_basis_follows_each_persons_week(shift: Shift, expected: int) -> None:
    days = salary_days(
        "working_days",
        period_start=NOV_START,
        period_end=NOV_END,
        window=FULL_MONTH,
        lop_days=Decimal(1),
        is_off=shift.is_off_on,
    )
    assert days.working_days == expected
    assert days.lop_days == 1


def test_working_days_basis_counts_only_working_days_outside_employment() -> None:
    # Joined Mon 16 Nov on a five-day week: 1-15 Nov hold 10 working days.
    days = salary_days(
        "working_days",
        period_start=NOV_START,
        period_end=NOV_END,
        window=(date(2026, 11, 16), NOV_END),
        lop_days=Decimal(0),
        is_off=FIVE_DAY.is_off_on,
    )
    assert days.working_days == 21
    assert days.not_employed_days == 10


def test_working_days_basis_with_no_working_day_employed_falls_back() -> None:
    # Employed only Sat 28 and Sun 29 Nov on a five-day week: no working day.
    days = salary_days(
        "working_days",
        period_start=NOV_START,
        period_end=NOV_END,
        window=(date(2026, 11, 28), date(2026, 11, 29)),
        lop_days=Decimal(0),
        is_off=FIVE_DAY.is_off_on,
    )
    assert days.basis == "calendar"
    assert days.not_employed_days == 28


def test_one_days_pay_differs_by_basis() -> None:
    """A 26,000 salary: what one day of loss of pay costs under each basis."""
    monthly = Decimal(26000)
    per_day = {
        "calendar": monthly
        / salary_days(
            "calendar",
            period_start=NOV_START,
            period_end=NOV_END,
            window=FULL_MONTH,
            lop_days=Decimal(0),
        ).working_days,
        "fixed 26": monthly / 26,
        "5-day": monthly / 21,
        "alt Sat": monthly / 23,
    }
    assert per_day["fixed 26"] == Decimal(1000)
    assert per_day["calendar"] < per_day["fixed 26"] < per_day["alt Sat"] < per_day["5-day"]


def test_engine_still_refuses_unpaid_days_beyond_the_divisor() -> None:
    # The clamp lives in salary_days; the engine keeps its own guard.
    from app.services.payroll.engine import PayrollInput, compute_payroll

    with pytest.raises(PayrollRuleError):
        compute_payroll(
            PayrollInput(
                employee_salary=None,  # type: ignore[arg-type]
                period_start=NOV_START,
                period_end=NOV_END,
                working_days=Decimal(26),
                lop_days=Decimal(27),
            )
        )
