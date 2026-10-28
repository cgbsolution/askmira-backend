"""The eight calendar tags, from what attendance already decided about a day."""

from __future__ import annotations

from datetime import date

import pytest

from app.models.attendance import AttendanceStatus as S
from app.schemas.attendance import DayTag
from app.services.attendance.calendar_tags import day_tag

TODAY = date(2026, 10, 28)
PAST = date(2026, 10, 20)
FUTURE = date(2026, 10, 30)


@pytest.mark.parametrize(
    ("status", "tag"),
    [
        (S.PRESENT, DayTag.FULL_DAY),
        (S.HALF_DAY, DayTag.PARTIAL),
        (S.ABSENT, DayTag.ABSENT),
        (S.NEEDS_REGULARISATION, DayTag.ABSENT),
        (S.ON_LEAVE, DayTag.APPROVED_LEAVE),
        (S.LOP, DayTag.APPROVED_LEAVE),
        (S.HOLIDAY, DayTag.HOLIDAY),
        (S.WEEK_OFF, DayTag.WEEK_OFF),
    ],
)
def test_a_computed_day_wears_its_status(status: S, tag: DayTag) -> None:
    assert day_tag(day=PAST, today=TODAY, status=status) is tag


def test_a_chosen_optional_holiday_has_its_own_tag() -> None:
    assert (
        day_tag(day=PAST, today=TODAY, status=S.HOLIDAY, holiday_is_optional=True)
        is DayTag.OPTIONAL_HOLIDAY
    )
    assert (
        day_tag(day=PAST, today=TODAY, status=S.HOLIDAY, holiday_is_optional=False)
        is DayTag.HOLIDAY
    )
    assert day_tag(day=FUTURE, today=TODAY, status=None, holiday_is_optional=True) is (
        DayTag.OPTIONAL_HOLIDAY
    )


def test_an_absent_day_with_leave_waiting_is_unapproved_leave() -> None:
    for status in (S.ABSENT, S.NEEDS_REGULARISATION):
        assert day_tag(day=PAST, today=TODAY, status=status, leave_pending=True) is (
            DayTag.UNAPPROVED_LEAVE
        )


def test_a_worked_day_keeps_its_status_over_a_pending_leave() -> None:
    assert day_tag(day=PAST, today=TODAY, status=S.PRESENT, leave_pending=True) is (DayTag.FULL_DAY)


def test_today_is_not_red_before_it_is_over() -> None:
    assert day_tag(day=TODAY, today=TODAY, status=S.NEEDS_REGULARISATION) is None
    assert day_tag(day=TODAY, today=TODAY, status=S.ABSENT) is None
    assert day_tag(day=TODAY, today=TODAY, status=None) is None


def test_today_marked_absent_by_hr_is_absent() -> None:
    assert day_tag(day=TODAY, today=TODAY, status=S.ABSENT, is_regularised=True) is DayTag.ABSENT


def test_today_already_worked_in_full_is_a_full_day() -> None:
    assert day_tag(day=TODAY, today=TODAY, status=S.PRESENT) is DayTag.FULL_DAY


def test_days_not_computed_show_what_is_known_in_the_order_attendance_applies_it() -> None:
    # Holiday beats a week off, which beats leave, which beats a pending request.
    assert (
        day_tag(
            day=FUTURE,
            today=TODAY,
            status=None,
            holiday_is_optional=False,
            scheduled_off=True,
            leave_approved=True,
        )
        is DayTag.HOLIDAY
    )
    assert (
        day_tag(day=FUTURE, today=TODAY, status=None, scheduled_off=True, leave_approved=True)
        is DayTag.WEEK_OFF
    )
    assert (
        day_tag(day=FUTURE, today=TODAY, status=None, leave_approved=True, leave_pending=True)
        is DayTag.APPROVED_LEAVE
    )
    assert day_tag(day=FUTURE, today=TODAY, status=None, leave_pending=True) is (
        DayTag.UNAPPROVED_LEAVE
    )


def test_an_ordinary_working_day_ahead_has_no_tag() -> None:
    assert day_tag(day=FUTURE, today=TODAY, status=None) is None


def test_nothing_before_joining() -> None:
    joined = date(2026, 10, 15)
    assert (
        day_tag(day=PAST.replace(day=10), today=TODAY, status=S.ABSENT, date_of_joining=joined)
        is None
    )
    assert (
        day_tag(
            day=date(2026, 10, 11),
            today=TODAY,
            status=None,
            scheduled_off=True,
            date_of_joining=joined,
        )
        is None
    )
    assert day_tag(day=joined, today=TODAY, status=S.PRESENT, date_of_joining=joined) is (
        DayTag.FULL_DAY
    )
