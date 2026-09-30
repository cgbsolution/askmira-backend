"""Leave arithmetic added with the depth work: the rules an employee will argue about.

Two halves of one day, the sandwich rule, unpaid leave with no balance to run
out of, what carries into a new year and what lapses, comp-off expiry, the
policy correction tie-break, and a balance history that adds up to the card.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from app.services.leave import engine
from app.services.leave.engine import (
    Balance,
    PolicyRules,
    accrual_schedule,
    accrued_to_date,
    assess_request,
    comp_off_lapse,
    count_leave_days,
    overlapping_requests,
    select_policy,
    year_end_split,
)

WEEKEND = (6, 7)
# 2026-04-03 is a Friday; 2026-04-06 a Monday.
FRIDAY = date(2026, 4, 3)
SATURDAY = date(2026, 4, 4)
SUNDAY = date(2026, 4, 5)
MONDAY = date(2026, 4, 6)
TUESDAY = date(2026, 4, 7)
WEDNESDAY = date(2026, 4, 8)


# ── Half-day pairs ──────────────────────────────────────────────────────────


def test_a_first_half_and_a_second_half_on_the_same_day_do_not_clash() -> None:
    clashes = overlapping_requests(
        start=MONDAY,
        end=MONDAY,
        half_day=True,
        session="second_half",
        existing=[(MONDAY, MONDAY, True, "first_half")],
    )
    assert clashes == 0


def test_two_first_halves_on_the_same_day_clash() -> None:
    clashes = overlapping_requests(
        start=MONDAY,
        end=MONDAY,
        half_day=True,
        session="first_half",
        existing=[(MONDAY, MONDAY, True, "first_half")],
    )
    assert clashes == 1


def test_a_half_day_without_a_session_clashes_with_any_half_day() -> None:
    clashes = overlapping_requests(
        start=MONDAY,
        end=MONDAY,
        half_day=True,
        session="second_half",
        existing=[(MONDAY, MONDAY, True, None)],
    )
    assert clashes == 1


def test_a_half_day_inside_a_full_day_request_clashes() -> None:
    clashes = overlapping_requests(
        start=TUESDAY,
        end=TUESDAY,
        half_day=True,
        session="first_half",
        existing=[(MONDAY, WEDNESDAY, False, None)],
    )
    assert clashes == 1


def test_requests_on_other_dates_do_not_clash() -> None:
    clashes = overlapping_requests(
        start=MONDAY,
        end=TUESDAY,
        half_day=False,
        session=None,
        existing=[(FRIDAY, FRIDAY, False, None), (WEDNESDAY, WEDNESDAY, True, "first_half")],
    )
    assert clashes == 0


# ── Sandwich rule ───────────────────────────────────────────────────────────


def test_without_the_sandwich_rule_the_weekend_is_free() -> None:
    count = count_leave_days(FRIDAY, MONDAY, half_day=False, weekly_off_days=WEEKEND, holidays={})
    assert count.total == Decimal(2)
    assert count.sandwiched == ()


def test_the_sandwich_rule_charges_a_weekend_between_two_leave_days() -> None:
    count = count_leave_days(
        FRIDAY, MONDAY, half_day=False, weekly_off_days=WEEKEND, holidays={}, sandwich=True
    )
    assert count.total == Decimal(4)
    assert count.sandwiched == (SATURDAY, SUNDAY)
    assert count.weekly_offs == ()


def test_the_sandwich_rule_charges_a_holiday_between_two_leave_days() -> None:
    count = count_leave_days(
        MONDAY,
        WEDNESDAY,
        half_day=False,
        weekly_off_days=WEEKEND,
        holidays={TUESDAY: "Ugadi"},
        sandwich=True,
    )
    assert count.total == Decimal(3)
    assert count.sandwiched == (TUESDAY,)
    assert count.holidays == ()


def test_an_off_at_the_edge_of_the_range_is_never_sandwiched() -> None:
    # Saturday to Tuesday: the weekend comes before the first leave day.
    count = count_leave_days(
        SATURDAY, TUESDAY, half_day=False, weekly_off_days=WEEKEND, holidays={}, sandwich=True
    )
    assert count.total == Decimal(2)
    assert count.sandwiched == ()
    assert count.weekly_offs == (SATURDAY, SUNDAY)


def test_a_single_leave_day_sandwiches_nothing() -> None:
    count = count_leave_days(
        FRIDAY, SUNDAY, half_day=False, weekly_off_days=WEEKEND, holidays={}, sandwich=True
    )
    assert count.total == Decimal(1)


# ── Unpaid leave ────────────────────────────────────────────────────────────


def _count(total: str) -> engine.DayCount:
    return engine.DayCount(total=Decimal(total), working_dates=(), weekly_offs=(), holidays=())


def _assess(**overrides):
    arguments = dict(
        count=_count("5"),
        start=date(2026, 5, 4),
        today=date(2026, 4, 1),
        half_day=False,
        allows_half_day=True,
        requires_document=False,
        has_document=False,
        rules=PolicyRules(),
        balance=Balance(accrued=Decimal(1)),
        overlapping=0,
    )
    arguments.update(overrides)
    return assess_request(**arguments)


def test_paid_leave_beyond_the_balance_is_refused() -> None:
    assert not _assess().allowed


def test_unpaid_leave_skips_the_balance_check_entirely() -> None:
    result = _assess(is_paid=False)
    assert result.allowed
    assert not any("available" in p for p in result.problems)


def test_unpaid_leave_needs_no_policy() -> None:
    result = _assess(is_paid=False, rules=None, balance=None)
    assert result.allowed


def test_a_paid_type_without_a_policy_is_still_refused() -> None:
    result = _assess(rules=None, balance=None)
    assert any("No leave policy" in p for p in result.problems)


def test_unpaid_leave_still_honours_the_policy_notice_rule() -> None:
    result = _assess(is_paid=False, start=date(2026, 4, 3), rules=PolicyRules(min_notice_days=7))
    assert any("notice" in p for p in result.problems)


# ── Year end ────────────────────────────────────────────────────────────────


def test_up_to_the_limit_carries_and_the_rest_lapses() -> None:
    assert year_end_split(Decimal("12.5"), Decimal(10)) == (Decimal(10), Decimal("2.5"))


def test_a_balance_under_the_limit_carries_whole() -> None:
    assert year_end_split(Decimal(4), Decimal(10)) == (Decimal(4), Decimal(0))


def test_a_zero_limit_lapses_everything() -> None:
    assert year_end_split(Decimal(6), Decimal(0)) == (Decimal(0), Decimal(6))


def test_a_negative_balance_neither_carries_nor_lapses() -> None:
    assert year_end_split(Decimal(-2), Decimal(10)) == (Decimal(0), Decimal(0))


def test_an_uncapped_balance_carries_whole() -> None:
    assert year_end_split(Decimal(3), None) == (Decimal(3), Decimal(0))


def test_carry_in_and_adjustments_count_in_the_balance() -> None:
    balance = Balance(
        opening=Decimal(2),
        accrued=Decimal(12),
        carried_forward=Decimal(5),
        adjusted=Decimal("-1.5"),
        used=Decimal(4),
        encashed=Decimal(3),
    )
    assert balance.available == Decimal("10.5")


# ── Comp-off expiry ─────────────────────────────────────────────────────────


def test_an_unused_expired_credit_lapses_whole() -> None:
    assert comp_off_lapse(
        available=Decimal(2), expiring=Decimal(1), still_valid=Decimal(1)
    ) == Decimal(1)


def test_a_used_credit_lapses_nothing_oldest_used_first() -> None:
    # Two credits of 1, one day taken: the older one was used.
    assert comp_off_lapse(
        available=Decimal(1), expiring=Decimal(1), still_valid=Decimal(1)
    ) == Decimal(0)


def test_a_half_used_credit_lapses_the_remaining_half() -> None:
    assert comp_off_lapse(
        available=Decimal("0.5"), expiring=Decimal(1), still_valid=Decimal(0)
    ) == Decimal("0.5")


def test_nothing_expiring_lapses_nothing() -> None:
    assert comp_off_lapse(available=Decimal(3), expiring=Decimal(0), still_valid=Decimal(0)) == 0


# ── Policy corrections ──────────────────────────────────────────────────────


@dataclass
class _Policy:
    name: str
    location_id: uuid.UUID | None = None
    grade: str | None = None
    effective_from: date = date(2026, 1, 1)
    effective_to: date | None = None
    is_active: bool = True


def test_a_correction_with_the_same_start_date_wins_over_the_original() -> None:
    policies = [_Policy("original"), _Policy("correction")]
    chosen = select_policy(policies, location_id=None, grade=None, on=date(2026, 6, 1))
    assert chosen is not None and chosen.name == "correction"


# ── Accrual schedule ────────────────────────────────────────────────────────


@pytest.mark.parametrize("frequency", ["monthly", "quarterly", "annual", "on_joining"])
@pytest.mark.parametrize("joined_on", [date(2019, 4, 1), date(2026, 3, 10), date(2026, 3, 20)])
def test_the_schedule_adds_up_to_the_accrual(frequency: str, joined_on: date) -> None:
    as_of = date(2026, 9, 30)
    schedule = accrual_schedule(
        annual_quota=Decimal(18), frequency=frequency, year=2026, as_of=as_of, joined_on=joined_on
    )
    total = sum((amount for _, amount in schedule), Decimal(0))
    assert total == accrued_to_date(
        annual_quota=Decimal(18), frequency=frequency, year=2026, as_of=as_of, joined_on=joined_on
    )
    assert all(on <= as_of for on, _ in schedule)


def test_a_monthly_schedule_credits_on_the_first_of_each_month() -> None:
    schedule = accrual_schedule(
        annual_quota=Decimal(12),
        frequency="monthly",
        year=2026,
        as_of=date(2026, 3, 31),
        joined_on=date(2020, 1, 1),
    )
    assert schedule == [
        (date(2026, 1, 1), Decimal("1.00")),
        (date(2026, 2, 1), Decimal("1.00")),
        (date(2026, 3, 1), Decimal("1.00")),
    ]


def test_accrual_for_a_future_year_is_read_at_the_leave_start() -> None:
    # A December request for 20 January leave on 18 days a year, credited monthly.
    assert accrued_to_date(
        annual_quota=Decimal(18),
        frequency="monthly",
        year=2027,
        as_of=date(2027, 1, 20),
        joined_on=date(2020, 1, 1),
    ) == Decimal("1.50")


# ── Gender-limited policies ─────────────────────────────────────────────────


@dataclass
class _GenderPolicy:
    name: str
    applies_to_gender: str | None = None
    location_id: uuid.UUID | None = None
    grade: str | None = None
    effective_from: date = date(2026, 1, 1)
    effective_to: date | None = None
    is_active: bool = True


MATERNITY = [_GenderPolicy("maternity", applies_to_gender="female")]


def test_a_womens_policy_does_not_cover_a_man() -> None:
    assert (
        select_policy(MATERNITY, location_id=None, grade=None, on=date(2026, 6, 1), gender="male")
        is None
    )


def test_a_womens_policy_covers_a_woman() -> None:
    chosen = select_policy(
        MATERNITY, location_id=None, grade=None, on=date(2026, 6, 1), gender="female"
    )
    assert chosen is not None and chosen.name == "maternity"


@pytest.mark.parametrize("gender", [None, "undisclosed", "non_binary"])
def test_an_unrecorded_gender_is_never_shut_out(gender: str | None) -> None:
    chosen = select_policy(
        MATERNITY, location_id=None, grade=None, on=date(2026, 6, 1), gender=gender
    )
    assert chosen is not None


def test_a_gender_limited_policy_is_more_specific_than_the_catch_all() -> None:
    policies = [_GenderPolicy("everyone"), _GenderPolicy("women", applies_to_gender="female")]
    chosen = select_policy(
        policies, location_id=None, grade=None, on=date(2026, 6, 1), gender="female"
    )
    assert chosen is not None and chosen.name == "women"
