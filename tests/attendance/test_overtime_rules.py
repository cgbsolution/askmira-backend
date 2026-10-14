"""Overtime: rest-day work is recorded, and the policy counts and prices it.

What these hold:

- work on a weekly off or a holiday is kept as overtime minutes, and the day
  stays a payable rest day — nobody is asked to regularise a day off;
- the policy's minimum, rounding and daily cap decide what counts;
- the price comes from the policy's components, divisors and multiplier, so no
  statutory number lives in the code.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from app.models.attendance import AttendanceStatus, PunchDirection
from app.models.payroll_inputs import OvertimePolicy
from app.services.attendance.computation import compute_day
from app.services.attendance.overtime import counted_minutes, price
from tests.attendance.test_computation import inputs, punch

SATURDAY = date(2026, 4, 11)


def policy(**overrides) -> OvertimePolicy:
    values = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "name": "Plant",
        "rate_multiplier": Decimal("2.00"),
        "wage_component_codes": ["BASIC", "DA"],
        "days_divisor": 26,
        "hours_per_day": Decimal("8.00"),
        "minimum_minutes": 30,
        "rounding_minutes": 15,
        "daily_cap_minutes": 240,
        "quarterly_cap_minutes": None,
        "count_rest_days": True,
        "requires_approval": True,
        "effective_from": date(2026, 4, 1),
        "is_active": True,
    }
    values.update(overrides)
    return OvertimePolicy(**values)


def test_work_on_a_weekly_off_is_recorded_as_overtime() -> None:
    result = compute_day(
        inputs(
            attendance_date=SATURDAY,
            punches=[
                punch(SATURDAY, "09:00", PunchDirection.IN),
                punch(SATURDAY, "14:00", PunchDirection.OUT),
            ],
        )
    )
    assert result.status is AttendanceStatus.WEEK_OFF
    assert result.payable_fraction == Decimal(1)
    # Five hours less the shift's one-hour break.
    assert result.worked_minutes == 240
    assert result.overtime_minutes == 240


def test_work_on_a_holiday_is_recorded_as_overtime() -> None:
    result = compute_day(
        inputs(
            is_holiday=True,
            holiday_name="Diwali",
            punches=[
                punch(date(2026, 4, 6), "10:00", PunchDirection.IN),
                punch(date(2026, 4, 6), "13:00", PunchDirection.OUT),
            ],
        )
    )
    assert result.status is AttendanceStatus.HOLIDAY
    assert result.overtime_minutes == 120
    assert result.note.startswith("Diwali")


def test_an_unpaired_punch_on_a_rest_day_leaves_the_day_alone() -> None:
    result = compute_day(
        inputs(attendance_date=SATURDAY, punches=[punch(SATURDAY, "09:00", PunchDirection.IN)])
    )
    assert result.status is AttendanceStatus.WEEK_OFF
    assert result.overtime_minutes == 0
    assert result.note == "Weekly off"


def test_below_the_minimum_counts_nothing() -> None:
    assert counted_minutes(policy(), 29) == 0


def test_minutes_are_rounded_down_to_the_block() -> None:
    assert counted_minutes(policy(), 52) == 45


def test_the_daily_cap_applies_after_rounding() -> None:
    assert counted_minutes(policy(), 300) == 240


def test_a_policy_with_no_minimum_or_rounding_counts_exactly() -> None:
    exact = policy(minimum_minutes=0, rounding_minutes=0, daily_cap_minutes=None)
    assert counted_minutes(exact, 7) == 7


def test_the_price_comes_entirely_from_the_policy() -> None:
    # 26,000 a month / 26 days / 8 hours = 125 an hour; double rate; 90 minutes.
    hourly, amount, basis = price(policy(), Decimal("26000"), 90)
    assert hourly == Decimal("125.0000")
    assert amount == Decimal("375.00")
    assert "2.00x" in basis and "BASIC + DA" in basis


def test_a_different_multiplier_changes_the_price() -> None:
    _, amount, _ = price(policy(rate_multiplier=Decimal("1.50")), Decimal("26000"), 60)
    assert amount == Decimal("187.50")
