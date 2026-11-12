"""Accrual across policy versions, and leave held back for probation.

Two rules the leave year depends on:

* a policy version earns only the months it is in force — a Casual Leave
  policy that starts on 1 November earns November and December, not the ten
  months before it existed;
* when the workspace holds leave back for probation, nobody earns anything
  until that many months after joining.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from app.services.leave.engine import (
    accrual_start,
    accrual_windows,
    accrued_across,
    accrued_to_date,
    on_probation_until,
    schedule_from,
)


@dataclass
class Policy:
    name: str
    annual_quota_days: Decimal
    accrual_frequency: str
    effective_from: date
    effective_to: date | None = None
    location_id: None = None
    grade: str | None = None
    applies_to_gender: str | None = None
    is_active: bool = True


def earned(
    policies: list[Policy], *, year: int, as_of: date, joined_on: date | None = date(2020, 1, 1)
) -> Decimal:
    windows = accrual_windows(policies, year=year, location_id=None, grade=None)
    return accrued_across(windows, year=year, as_of=as_of, joined_on=joined_on)


# The live set-up asked for on 2026-11-13: nothing until October, then
# Casual Leave of 15 a year (1.25 a month).
OLD = Policy("CL (none)", Decimal(0), "annual", date(2026, 1, 1), date(2026, 10, 31))
NOV_DEC = Policy("CL Nov-Dec 2026", Decimal(15), "annual", date(2026, 11, 1), date(2026, 12, 31))
MONTHLY = Policy("CL 1.25 a month", Decimal(15), "monthly", date(2027, 1, 1))
LIVE = [OLD, NOV_DEC, MONTHLY]


def test_a_version_earns_only_the_months_it_is_in_force() -> None:
    windows = accrual_windows(LIVE, year=2026, location_id=None, grade=None)
    assert [(p.name, first, last) for p, first, last in windows] == [
        ("CL (none)", 1, 10),
        ("CL Nov-Dec 2026", 11, 12),
    ]


def test_november_and_december_credit_two_and_a_half_days_up_front() -> None:
    assert earned(LIVE, year=2026, as_of=date(2026, 11, 13)) == Decimal("2.50")
    assert earned(LIVE, year=2026, as_of=date(2026, 12, 31)) == Decimal("2.50")
    assert earned(LIVE, year=2026, as_of=date(2026, 10, 31)) == Decimal(0)


def test_from_january_it_is_one_and_a_quarter_on_the_first_of_each_month() -> None:
    assert earned(LIVE, year=2027, as_of=date(2027, 1, 1)) == Decimal("1.25")
    assert earned(LIVE, year=2027, as_of=date(2027, 3, 31)) == Decimal("3.75")
    assert earned(LIVE, year=2027, as_of=date(2027, 12, 1)) == Decimal("15.00")


def test_a_monthly_policy_starting_in_november_does_not_earn_january_to_october() -> None:
    late = Policy("CL", Decimal(15), "monthly", date(2026, 11, 1))
    assert earned([late], year=2026, as_of=date(2026, 11, 13)) == Decimal("1.25")
    assert earned([late], year=2026, as_of=date(2026, 12, 1)) == Decimal("2.50")


def test_a_quota_change_mid_year_keeps_what_the_old_version_earned() -> None:
    old = Policy("12 a year", Decimal(12), "monthly", date(2026, 1, 1), date(2026, 6, 30))
    new = Policy("18 a year", Decimal(18), "monthly", date(2026, 7, 1))
    # Six months at 1 and six at 1.5 — not 18, and not 9.
    assert earned([old, new], year=2026, as_of=date(2026, 12, 31)) == Decimal("15.00")


def test_a_version_starting_after_the_15th_earns_from_the_next_month() -> None:
    old = Policy("12 a year", Decimal(12), "monthly", date(2026, 1, 1), date(2026, 6, 19))
    new = Policy("24 a year", Decimal(24), "monthly", date(2026, 6, 20))
    # June is the old version's: it was in force on 15 June.
    assert earned([old, new], year=2026, as_of=date(2026, 6, 30)) == Decimal("6.00")
    assert earned([old, new], year=2026, as_of=date(2026, 7, 1)) == Decimal("8.00")


def test_a_single_policy_for_the_whole_year_is_unchanged() -> None:
    whole = Policy("CL", Decimal(18), "monthly", date(2020, 1, 1))
    assert earned([whole], year=2026, as_of=date(2026, 4, 1)) == Decimal("6.00")
    assert earned([whole], year=2026, as_of=date(2026, 4, 30), joined_on=date(2026, 2, 20)) == (
        Decimal("3.00")
    )


def test_an_on_joining_credit_is_given_once_across_versions() -> None:
    first = Policy("A", Decimal(10), "on_joining", date(2026, 1, 1), date(2026, 5, 31))
    second = Policy("B", Decimal(10), "on_joining", date(2026, 6, 1))
    assert earned([first, second], year=2026, as_of=date(2026, 9, 1)) == Decimal(10)


def test_quarterly_inside_a_window() -> None:
    late = Policy("Q", Decimal(12), "quarterly", date(2026, 11, 1))
    # Q4 started in October, but the policy governs only November and December.
    assert earned([late], year=2026, as_of=date(2026, 10, 20)) == Decimal(0)
    assert earned([late], year=2026, as_of=date(2026, 11, 2)) == Decimal("2.00")


# ── Probation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("joined", "months", "expected"),
    [
        (date(2026, 5, 10), 6, date(2026, 11, 10)),
        (date(2026, 8, 31), 6, date(2027, 2, 28)),  # month end clips, as Postgres does
        (date(2026, 5, 10), 0, date(2026, 5, 10)),
        (None, 6, None),
    ],
)
def test_accrual_starts_after_probation(joined: date | None, months: int, expected) -> None:
    assert accrual_start(joined, months) == expected


def test_statutory_leave_is_never_held_back_for_probation() -> None:
    assert accrual_start(date(2026, 10, 1), 6, exempt=True) == date(2026, 10, 1)


def test_on_probation_until_is_set_only_while_it_lasts() -> None:
    assert on_probation_until(date(2026, 10, 26), 6, date(2026, 11, 13)) == date(2027, 4, 26)
    assert on_probation_until(date(2026, 5, 13), 6, date(2026, 11, 13)) is None, "ends today"
    assert on_probation_until(date(2026, 10, 26), 0, date(2026, 11, 13)) is None
    assert on_probation_until(None, 6, date(2026, 11, 13)) is None


@pytest.mark.parametrize(
    ("joined", "expected_now", "expected_year_end"),
    [
        # Six months done long ago: the full 2.5.
        (date(2022, 3, 7), Decimal("2.50"), Decimal("2.50")),
        # Probation ends 10 Nov (on or before the 15th): November counts.
        (date(2026, 5, 10), Decimal("2.50"), Decimal("2.50")),
        # Ends 20 Nov: from December, and nothing until the 20th.
        (date(2026, 5, 20), Decimal(0), Decimal("1.25")),
        # Ends 10 Dec: December only, from the 10th.
        (date(2026, 6, 10), Decimal(0), Decimal("1.25")),
        # Ends 20 Dec: nothing this year.
        (date(2026, 6, 20), Decimal(0), Decimal(0)),
        # Joined in October: on probation until April, nothing this year.
        (date(2026, 10, 26), Decimal(0), Decimal(0)),
    ],
)
def test_probation_holds_back_the_november_december_credit(
    joined: date, expected_now: Decimal, expected_year_end: Decimal
) -> None:
    start = accrual_start(joined, 6)
    assert earned(LIVE, year=2026, as_of=date(2026, 11, 13), joined_on=start) == expected_now
    assert earned(LIVE, year=2026, as_of=date(2026, 12, 31), joined_on=start) == (expected_year_end)


def test_after_probation_monthly_credit_starts_with_the_first_full_month() -> None:
    start = accrual_start(date(2026, 10, 26), 6)  # 26 Apr 2027
    assert earned(LIVE, year=2027, as_of=date(2027, 4, 30), joined_on=start) == Decimal(0)
    assert earned(LIVE, year=2027, as_of=date(2027, 5, 1), joined_on=start) == Decimal("1.25")
    assert earned(LIVE, year=2027, as_of=date(2027, 12, 1), joined_on=start) == Decimal("10.00")


def test_annual_credit_never_lands_before_the_person_starts_earning() -> None:
    assert accrued_to_date(
        annual_quota=Decimal(12),
        frequency="annual",
        year=2026,
        as_of=date(2026, 3, 1),
        joined_on=date(2026, 5, 1),
    ) == Decimal(0)


def test_history_entries_add_up_to_the_balance() -> None:
    start = accrual_start(date(2026, 5, 10), 6)
    windows = accrual_windows(LIVE, year=2026, location_id=None, grade=None)

    def total_at(on: date) -> Decimal:
        return accrued_across(windows, year=2026, as_of=on, joined_on=start)

    entries = schedule_from(total_at, year=2026, as_of=date(2026, 11, 13), joined_on=start)
    assert sum(amount for _, amount in entries) == Decimal("2.50")
    assert entries[0][0] == date(2026, 11, 10), "dated the day probation ended"
