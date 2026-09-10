"""Slice 02 gate: the daily attendance computation.

The night-shift cases come first because the checklist names them as the single
most likely source of a production bug in this module.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.models.attendance import AttendanceStatus, PunchDirection, Shift
from app.services.attendance.computation import (
    DayInputs,
    PunchRecord,
    attendance_window,
    compute_day,
)

DAY = date(2026, 4, 6)  # a Monday


def shift(
    *,
    start: str = "09:00",
    end: str = "18:00",
    night: bool = False,
    break_minutes: int = 60,
    grace_in: int = 0,
    grace_out: int = 0,
    half_day: int = 240,
    full_day: int = 480,
    weekly_off: list[int] | None = None,
) -> Shift:
    return Shift(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        code="GEN",
        name="General",
        start_time=time.fromisoformat(start),
        end_time=time.fromisoformat(end),
        break_minutes=break_minutes,
        grace_in_minutes=grace_in,
        grace_out_minutes=grace_out,
        half_day_threshold_minutes=half_day,
        full_day_threshold_minutes=full_day,
        is_night_shift=night,
        weekly_off_days=weekly_off if weekly_off is not None else [6, 7],
        active=True,
    )


def punch(day: date, at: str, direction: PunchDirection) -> PunchRecord:
    return PunchRecord(
        punched_at=datetime.combine(day, time.fromisoformat(at), tzinfo=UTC),
        direction=direction,
    )


def inputs(**kwargs) -> DayInputs:
    kwargs.setdefault("employee_id", uuid.uuid4())
    kwargs.setdefault("attendance_date", DAY)
    kwargs.setdefault("shift", shift())
    return DayInputs(**kwargs)


# ═══════════════════════════════════════════════════════════════════════════
# Night shifts — the checklist's headline gate
# ═══════════════════════════════════════════════════════════════════════════


def test_an_out_punch_at_0200_attaches_to_the_previous_working_day() -> None:
    """22:00-06:00 on the 6th: the OUT at 02:00 on the 7th is still the 6th."""
    night = shift(start="22:00", end="06:00", night=True, break_minutes=0)

    result = compute_day(
        inputs(
            shift=night,
            punches=[
                punch(DAY, "22:00", PunchDirection.IN),
                punch(DAY + timedelta(days=1), "02:00", PunchDirection.OUT),
            ],
        )
    )

    assert result.status is AttendanceStatus.HALF_DAY  # 4 hours worked
    assert result.worked_minutes == 240
    assert result.last_out.date() == DAY + timedelta(days=1)
    assert result.attendance_date == DAY, "the record belongs to the shift's start date"


def test_a_full_night_shift_is_a_full_day() -> None:
    night = shift(start="22:00", end="06:00", night=True, break_minutes=0)

    result = compute_day(
        inputs(
            shift=night,
            punches=[
                punch(DAY, "21:55", PunchDirection.IN),
                punch(DAY + timedelta(days=1), "06:05", PunchDirection.OUT),
            ],
        )
    )

    assert result.status is AttendanceStatus.PRESENT
    assert result.worked_minutes == 490  # 21:55 → 06:05
    assert result.payable_fraction == Decimal(1)


def test_the_attendance_window_for_a_night_shift_crosses_midnight() -> None:
    night = shift(start="22:00", end="06:00", night=True)
    start, end = attendance_window(night, DAY)

    assert start.date() == DAY
    # The window must reach into the following calendar day to catch the OUT.
    assert end.date() == DAY + timedelta(days=1)
    assert end.hour >= 6


def test_a_shift_ending_before_it_starts_is_treated_as_night_even_unflagged() -> None:
    """20:00-04:00 spans midnight whether or not someone ticked the box.

    Relying only on the flag would make a mis-configured shift silently drop
    every OUT punch.
    """
    unflagged = shift(start="20:00", end="04:00", night=False, break_minutes=0)
    _, end = attendance_window(unflagged, DAY)
    assert end.date() == DAY + timedelta(days=1)

    result = compute_day(
        inputs(
            shift=unflagged,
            punches=[
                punch(DAY, "20:00", PunchDirection.IN),
                punch(DAY + timedelta(days=1), "04:00", PunchDirection.OUT),
            ],
        )
    )
    assert result.worked_minutes == 480
    assert result.status is AttendanceStatus.PRESENT


def test_early_leaving_on_a_night_shift_measures_against_the_next_day_end() -> None:
    night = shift(start="22:00", end="06:00", night=True, break_minutes=0, grace_out=10)

    result = compute_day(
        inputs(
            shift=night,
            punches=[
                punch(DAY, "22:00", PunchDirection.IN),
                punch(DAY + timedelta(days=1), "05:00", PunchDirection.OUT),
            ],
        )
    )
    assert result.early_by_minutes == 60


# ═══════════════════════════════════════════════════════════════════════════
# Grace periods
# ═══════════════════════════════════════════════════════════════════════════


def test_eight_minutes_late_within_a_ten_minute_grace_is_not_late() -> None:
    result = compute_day(
        inputs(
            shift=shift(grace_in=10),
            punches=[
                punch(DAY, "09:08", PunchDirection.IN),
                punch(DAY, "18:00", PunchDirection.OUT),
            ],
        )
    )
    assert result.late_by_minutes == 0


def test_twelve_minutes_late_against_a_ten_minute_grace_is_late_by_twelve() -> None:
    """Grace is forgiveness, not a shifted start time — 12, not 2."""
    result = compute_day(
        inputs(
            shift=shift(grace_in=10),
            punches=[
                punch(DAY, "09:12", PunchDirection.IN),
                punch(DAY, "18:00", PunchDirection.OUT),
            ],
        )
    )
    assert result.late_by_minutes == 12


def test_exactly_at_the_grace_boundary_is_not_late() -> None:
    result = compute_day(
        inputs(
            shift=shift(grace_in=10),
            punches=[
                punch(DAY, "09:10", PunchDirection.IN),
                punch(DAY, "18:00", PunchDirection.OUT),
            ],
        )
    )
    assert result.late_by_minutes == 0


def test_arriving_early_is_never_late() -> None:
    result = compute_day(
        inputs(
            punches=[
                punch(DAY, "08:30", PunchDirection.IN),
                punch(DAY, "18:00", PunchDirection.OUT),
            ],
        )
    )
    assert result.late_by_minutes == 0


# ═══════════════════════════════════════════════════════════════════════════
# Threshold boundaries
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "out_time,expected_status,expected_payable",
    [
        # break_minutes=0, half_day=240, full_day=480
        ("12:59", AttendanceStatus.ABSENT, "0"),  # 239 minutes — just under
        ("13:00", AttendanceStatus.HALF_DAY, "0.5"),  # 240 — exactly at
        ("13:01", AttendanceStatus.HALF_DAY, "0.5"),  # 241 — just over
        ("16:59", AttendanceStatus.HALF_DAY, "0.5"),  # 479 — just under full
        ("17:00", AttendanceStatus.PRESENT, "1"),  # 480 — exactly at
        ("17:01", AttendanceStatus.PRESENT, "1"),  # 481 — just over
    ],
)
def test_threshold_boundaries_on_both_sides(
    out_time: str, expected_status: AttendanceStatus, expected_payable: str
) -> None:
    result = compute_day(
        inputs(
            shift=shift(break_minutes=0),
            punches=[
                punch(DAY, "09:00", PunchDirection.IN),
                punch(DAY, out_time, PunchDirection.OUT),
            ],
        )
    )
    assert result.status is expected_status
    assert result.payable_fraction == Decimal(expected_payable)


def test_break_minutes_are_deducted_from_worked_time() -> None:
    result = compute_day(
        inputs(
            shift=shift(break_minutes=60),
            punches=[
                punch(DAY, "09:00", PunchDirection.IN),
                punch(DAY, "18:00", PunchDirection.OUT),
            ],
        )
    )
    assert result.worked_minutes == 480  # 9 hours less a 1-hour break


# ═══════════════════════════════════════════════════════════════════════════
# Resolution order
# ═══════════════════════════════════════════════════════════════════════════


def test_approved_leave_overrides_punches() -> None:
    """Someone on approved leave who badges in is still on leave."""
    result = compute_day(
        inputs(
            on_approved_leave=True,
            punches=[
                punch(DAY, "09:00", PunchDirection.IN),
                punch(DAY, "18:00", PunchDirection.OUT),
            ],
        )
    )
    assert result.status is AttendanceStatus.ON_LEAVE
    assert result.payable_fraction == Decimal(1)


def test_a_holiday_beats_a_weekly_off_and_punches() -> None:
    result = compute_day(
        inputs(
            attendance_date=date(2026, 4, 11),  # a Saturday, also a week off
            is_holiday=True,
            holiday_name="Good Friday",
            punches=[punch(date(2026, 4, 11), "09:00", PunchDirection.IN)],
        )
    )
    assert result.status is AttendanceStatus.HOLIDAY
    assert result.note == "Good Friday"


def test_a_weekly_off_is_payable_and_needs_no_punches() -> None:
    saturday = date(2026, 4, 11)
    result = compute_day(inputs(attendance_date=saturday))
    assert result.status is AttendanceStatus.WEEK_OFF
    assert result.payable_fraction == Decimal(1)


def test_unpaid_leave_is_lop_not_on_leave() -> None:
    result = compute_day(inputs(on_approved_leave=True, leave_is_paid=False))
    assert result.status is AttendanceStatus.LOP
    assert result.payable_fraction == Decimal(0)
    assert result.lop_fraction == Decimal(1)


def test_a_half_day_on_paid_leave_is_a_fully_payable_day() -> None:
    """The employee works one half; the leave balance covers the other."""
    result = compute_day(inputs(on_approved_leave=True, leave_is_half_day=True))
    assert result.status is AttendanceStatus.ON_LEAVE
    assert result.payable_fraction == Decimal(1)
    assert result.lop_fraction == Decimal(0)


def test_a_half_day_on_unpaid_leave_costs_half_a_day_of_pay() -> None:
    result = compute_day(
        inputs(on_approved_leave=True, leave_is_half_day=True, leave_is_paid=False)
    )
    assert result.payable_fraction == Decimal("0.5")
    assert result.lop_fraction == Decimal("0.5")


# ═══════════════════════════════════════════════════════════════════════════
# Unpaired punches
# ═══════════════════════════════════════════════════════════════════════════


def test_an_in_with_no_out_needs_regularisation_rather_than_counting_absent() -> None:
    """Someone who forgot to punch out worked that day.

    Marking them absent is both wrong and the fastest way to lose employees'
    trust in the whole system.
    """
    result = compute_day(
        inputs(punches=[punch(DAY, "09:00", PunchDirection.IN)])
    )
    assert result.status is AttendanceStatus.NEEDS_REGULARISATION
    assert result.status is not AttendanceStatus.ABSENT
    assert "unpaired" in result.note.lower()


def test_an_out_with_no_in_needs_regularisation() -> None:
    result = compute_day(
        inputs(punches=[punch(DAY, "18:00", PunchDirection.OUT)])
    )
    assert result.status is AttendanceStatus.NEEDS_REGULARISATION


def test_two_ins_with_one_out_needs_regularisation() -> None:
    result = compute_day(
        inputs(
            punches=[
                punch(DAY, "09:00", PunchDirection.IN),
                punch(DAY, "13:00", PunchDirection.IN),
                punch(DAY, "18:00", PunchDirection.OUT),
            ]
        )
    )
    assert result.status is AttendanceStatus.NEEDS_REGULARISATION


def test_no_punches_at_all_is_a_plain_absence() -> None:
    """Distinct from an unpaired punch: nothing happened, so nothing to fix."""
    result = compute_day(inputs(punches=[]))
    assert result.status is AttendanceStatus.ABSENT
    assert result.payable_fraction == Decimal(0)


def test_multiple_valid_in_out_pairs_sum() -> None:
    result = compute_day(
        inputs(
            shift=shift(break_minutes=0),
            punches=[
                punch(DAY, "09:00", PunchDirection.IN),
                punch(DAY, "13:00", PunchDirection.OUT),
                punch(DAY, "14:00", PunchDirection.IN),
                punch(DAY, "18:00", PunchDirection.OUT),
            ],
        )
    )
    assert result.worked_minutes == 480
    assert result.status is AttendanceStatus.PRESENT


def test_punches_out_of_chronological_order_are_sorted_first() -> None:
    """Device imports do not guarantee order."""
    out_of_order = [
        punch(DAY, "18:00", PunchDirection.OUT),
        punch(DAY, "09:00", PunchDirection.IN),
    ]
    result = compute_day(inputs(shift=shift(break_minutes=0), punches=out_of_order))
    assert result.worked_minutes == 540
    assert result.status is AttendanceStatus.PRESENT


# ═══════════════════════════════════════════════════════════════════════════
# Regularisation and missing configuration
# ═══════════════════════════════════════════════════════════════════════════


def test_an_approved_regularisation_overrides_the_derived_status() -> None:
    result = compute_day(
        inputs(
            punches=[punch(DAY, "09:00", PunchDirection.IN)],
            regularised_status=AttendanceStatus.PRESENT,
        )
    )
    assert result.status is AttendanceStatus.PRESENT
    assert result.is_regularised
    assert result.payable_fraction == Decimal(1)


def test_no_shift_assigned_needs_regularisation_rather_than_absence() -> None:
    result = compute_day(inputs(shift=None, punches=[]))
    assert result.status is AttendanceStatus.NEEDS_REGULARISATION
    assert "No shift assigned" in result.note


# ═══════════════════════════════════════════════════════════════════════════
# Determinism — the basis of idempotent recomputation
# ═══════════════════════════════════════════════════════════════════════════


def test_computation_is_deterministic() -> None:
    payload = inputs(
        punches=[
            punch(DAY, "09:07", PunchDirection.IN),
            punch(DAY, "18:12", PunchDirection.OUT),
        ]
    )
    first = compute_day(payload)
    second = compute_day(payload)
    assert first == second, "identical inputs must produce an identical result"
