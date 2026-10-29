"""Leave arithmetic: the numbers an employee will dispute.

Day counts, accrual for joiners, balance maths, which policy applies, what a
request is refused for, and how the approval chain moves.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.services.leave import engine
from app.services.leave.engine import (
    Approver,
    Balance,
    LeaveRuleError,
    PolicyRules,
    accrued_to_date,
    apply_decision,
    assess_request,
    build_chain,
    count_leave_days,
    current_step,
    select_policy,
)

WEEKEND = (6, 7)
# 2026-04-03 is a Friday; 2026-04-06 a Monday.
FRIDAY = date(2026, 4, 3)
MONDAY = date(2026, 4, 6)


# ── Day counting ────────────────────────────────────────────────────────────


def test_a_weekend_inside_the_range_is_not_charged() -> None:
    count = count_leave_days(FRIDAY, MONDAY, half_day=False, weekly_off_days=WEEKEND, holidays={})
    assert count.total == Decimal(2)
    assert count.working_dates == (FRIDAY, MONDAY)
    assert len(count.weekly_offs) == 2


def test_a_holiday_inside_the_range_is_not_charged_and_is_named() -> None:
    holidays = {MONDAY: "Ugadi"}
    count = count_leave_days(
        FRIDAY, MONDAY, half_day=False, weekly_off_days=WEEKEND, holidays=holidays
    )
    assert count.total == Decimal(1)
    assert count.holidays == ((MONDAY, "Ugadi"),)


def test_a_holiday_on_a_weekly_off_is_counted_once_as_the_weekly_off() -> None:
    saturday = date(2026, 4, 4)
    count = count_leave_days(
        FRIDAY, MONDAY, half_day=False, weekly_off_days=WEEKEND, holidays={saturday: "X"}
    )
    assert count.total == Decimal(2)
    assert count.holidays == ()


def test_a_half_day_costs_half() -> None:
    count = count_leave_days(MONDAY, MONDAY, half_day=True, weekly_off_days=WEEKEND, holidays={})
    assert count.total == Decimal("0.5")


def test_a_half_day_on_a_weekly_off_costs_nothing() -> None:
    saturday = date(2026, 4, 4)
    count = count_leave_days(
        saturday, saturday, half_day=True, weekly_off_days=WEEKEND, holidays={}
    )
    assert count.total == Decimal(0)


def test_a_half_day_across_several_dates_is_refused() -> None:
    with pytest.raises(LeaveRuleError, match="single date"):
        count_leave_days(FRIDAY, MONDAY, half_day=True, weekly_off_days=WEEKEND, holidays={})


def test_an_end_before_the_start_is_refused() -> None:
    with pytest.raises(LeaveRuleError, match="before it starts"):
        count_leave_days(MONDAY, FRIDAY, half_day=False, weekly_off_days=WEEKEND, holidays={})


def test_a_six_day_week_charges_saturday() -> None:
    count = count_leave_days(FRIDAY, MONDAY, half_day=False, weekly_off_days=(7,), holidays={})
    assert count.total == Decimal(3)


# ── Accrual ─────────────────────────────────────────────────────────────────


def test_monthly_accrual_credits_each_started_month() -> None:
    accrued = accrued_to_date(
        annual_quota=Decimal(18), frequency="monthly", year=2026, as_of=date(2026, 4, 1),
        joined_on=date(2020, 1, 1),
    )
    assert accrued == Decimal("6.00")


def test_monthly_accrual_is_the_full_quota_after_the_year() -> None:
    accrued = accrued_to_date(
        annual_quota=Decimal(18), frequency="monthly", year=2025, as_of=date(2026, 4, 1),
        joined_on=None,
    )
    assert accrued == Decimal("18.00")


def test_monthly_accrual_is_nothing_for_a_future_year() -> None:
    accrued = accrued_to_date(
        annual_quota=Decimal(18), frequency="monthly", year=2027, as_of=date(2026, 4, 1),
        joined_on=None,
    )
    assert accrued == Decimal(0)


@pytest.mark.parametrize(
    ("joined_on", "expected"),
    [
        (date(2026, 3, 15), Decimal("3.00")),  # on the cutoff: March counts
        (date(2026, 3, 16), Decimal("1.50")),  # after it: from April
        (date(2026, 5, 1), Decimal(0)),  # not joined yet as of April
    ],
)
def test_a_mid_year_joiner_earns_only_months_served(joined_on: date, expected: Decimal) -> None:
    accrued = accrued_to_date(
        annual_quota=Decimal(18), frequency="monthly", year=2026, as_of=date(2026, 4, 30),
        joined_on=joined_on,
    )
    assert accrued == expected


def test_annual_accrual_is_credited_up_front_and_pro_rated_for_joiners() -> None:
    veteran = accrued_to_date(
        annual_quota=Decimal(12), frequency="annual", year=2026, as_of=date(2026, 1, 2),
        joined_on=date(2019, 6, 1),
    )
    joiner = accrued_to_date(
        annual_quota=Decimal(12), frequency="annual", year=2026, as_of=date(2026, 7, 2),
        joined_on=date(2026, 7, 1),
    )
    assert veteran == Decimal("12.00")
    assert joiner == Decimal("6.00")


def test_quarterly_accrual_credits_started_quarters() -> None:
    accrued = accrued_to_date(
        annual_quota=Decimal(12), frequency="quarterly", year=2026, as_of=date(2026, 4, 1),
        joined_on=None,
    )
    assert accrued == Decimal("6.00")


def test_on_joining_credits_the_whole_quota() -> None:
    accrued = accrued_to_date(
        annual_quota=Decimal(182), frequency="on_joining", year=2026, as_of=date(2026, 2, 1),
        joined_on=date(2025, 1, 1),
    )
    assert accrued == Decimal(182)


def test_accrual_never_returns_a_float() -> None:
    accrued = accrued_to_date(
        annual_quota=Decimal(15), frequency="monthly", year=2026, as_of=date(2026, 7, 1),
        joined_on=None,
    )
    assert isinstance(accrued, Decimal)
    assert accrued == Decimal("8.75")


def test_an_unknown_frequency_is_refused() -> None:
    with pytest.raises(LeaveRuleError, match="frequency"):
        accrued_to_date(
            annual_quota=Decimal(12), frequency="fortnightly", year=2026,
            as_of=date(2026, 4, 1), joined_on=None,
        )


# ── Balances ────────────────────────────────────────────────────────────────


def test_available_matches_the_encashment_ledger_formula() -> None:
    balance = Balance(
        opening=Decimal(2), accrued=Decimal(6), carried_forward=Decimal(3),
        used=Decimal("1.5"), encashed=Decimal(1), pending=Decimal(2),
    )
    assert balance.available == Decimal("8.5")
    assert balance.available_after_pending == Decimal("6.5")


# ── Policy selection ────────────────────────────────────────────────────────


@dataclass
class _Policy:
    name: str
    location_id: uuid.UUID | None = None
    grade: str | None = None
    effective_from: date = date(2025, 1, 1)
    effective_to: date | None = None
    is_active: bool = True


BLR = uuid.uuid4()


def test_the_most_specific_policy_wins() -> None:
    policies = [
        _Policy("everyone"),
        _Policy("bengaluru", location_id=BLR),
        _Policy("bengaluru-managers", location_id=BLR, grade="M3"),
    ]
    chosen = select_policy(policies, location_id=BLR, grade="M3", on=date(2026, 4, 1))
    assert chosen is not None and chosen.name == "bengaluru-managers"


def test_a_policy_for_another_population_never_applies() -> None:
    policies = [_Policy("mumbai", location_id=uuid.uuid4()), _Policy("m3", grade="M3")]
    assert select_policy(policies, location_id=BLR, grade="P3", on=date(2026, 4, 1)) is None


def test_the_version_in_force_on_the_date_is_chosen() -> None:
    policies = [
        _Policy("old", effective_from=date(2024, 1, 1), effective_to=date(2025, 12, 31)),
        _Policy("new", effective_from=date(2026, 1, 1)),
    ]
    assert select_policy(policies, location_id=None, grade=None, on=date(2025, 6, 1)).name == "old"
    assert select_policy(policies, location_id=None, grade=None, on=date(2026, 6, 1)).name == "new"


def test_an_inactive_policy_is_ignored() -> None:
    policies = [_Policy("off", is_active=False)]
    assert select_policy(policies, location_id=None, grade=None, on=date(2026, 1, 1)) is None


# ── Assessment ──────────────────────────────────────────────────────────────


def _count(total: str) -> engine.DayCount:
    return engine.DayCount(total=Decimal(total), working_dates=(), weekly_offs=(), holidays=())


def _assess(**overrides):
    arguments = dict(
        count=_count("2"),
        start=date(2026, 5, 4),
        today=date(2026, 4, 1),
        half_day=False,
        allows_half_day=True,
        requires_document=False,
        has_document=False,
        rules=PolicyRules(),
        balance=Balance(accrued=Decimal(10)),
        overlapping=0,
    )
    arguments.update(overrides)
    return assess_request(**arguments)


def test_an_ordinary_request_is_allowed() -> None:
    assert _assess().allowed


def test_insufficient_balance_is_refused_counting_pending_requests() -> None:
    result = _assess(balance=Balance(accrued=Decimal(3), pending=Decimal(2)))
    assert not result.allowed
    assert "1 days available after pending" in result.problems[0]


def test_a_balance_never_goes_below_zero() -> None:
    result = _assess(balance=Balance(accrued=Decimal(1)))
    assert not result.allowed
    assert result.problems == [
        "You have 1 days available after pending requests; this request needs 2."
    ]


def test_with_nothing_left_leave_cannot_be_applied() -> None:
    for balance in (Balance(), Balance(accrued=Decimal(2), pending=Decimal(2)), None):
        result = _assess(balance=balance)
        assert not result.allowed
        assert any("No balance is available" in p for p in result.problems)


def test_exactly_the_balance_is_allowed_including_a_half_day() -> None:
    assert _assess(balance=Balance(accrued=Decimal(2))).allowed
    half = _assess(count=_count("0.5"), half_day=True, balance=Balance(accrued=Decimal("0.5")))
    assert half.allowed


def test_short_notice_is_refused() -> None:
    result = _assess(start=date(2026, 4, 3), rules=PolicyRules(min_notice_days=7))
    assert any("7 days' notice" in p for p in result.problems)


def test_notice_does_not_apply_to_a_backdated_request_but_it_is_flagged() -> None:
    result = _assess(start=date(2026, 3, 20), rules=PolicyRules(min_notice_days=7))
    assert result.allowed
    assert any("in the past" in w for w in result.warnings)


def test_too_many_consecutive_days_is_refused() -> None:
    result = _assess(count=_count("6"), rules=PolicyRules(max_consecutive_days=5))
    assert any("5 consecutive" in p for p in result.problems)


def test_an_overlap_is_refused() -> None:
    assert not _assess(overlapping=1).allowed


def test_a_request_with_no_policy_is_refused_with_a_way_forward() -> None:
    result = _assess(rules=None, balance=None)
    assert any("Ask HR" in p for p in result.problems)


def test_a_zero_day_request_is_refused() -> None:
    assert not _assess(count=_count("0")).allowed


def test_a_half_day_on_a_type_that_disallows_it_is_refused() -> None:
    assert not _assess(half_day=True, allows_half_day=False, count=_count("0.5")).allowed


def test_a_missing_required_document_is_refused() -> None:
    assert not _assess(requires_document=True).allowed
    assert _assess(requires_document=True, has_document=True).allowed


# ── Approval chain ──────────────────────────────────────────────────────────

NOW = datetime(2026, 4, 1, 9, 30, tzinfo=UTC)
MANAGER = Approver("manager", uuid.uuid4(), uuid.uuid4(), "Vikram Shah")
HR = Approver("hr", None, None, "HR team")


def test_a_chain_needs_an_approver() -> None:
    with pytest.raises(LeaveRuleError):
        build_chain([])


def test_a_single_approval_finishes_the_chain() -> None:
    chain = build_chain([MANAGER])
    updated, outcome = apply_decision(
        chain, approve=True, actor_user_id=MANAGER.user_id, actor_name="Vikram Shah",
        note=None, at=NOW,
    )
    assert outcome == "approved"
    assert updated[0]["status"] == "approved"
    assert updated[0]["decided_at"] == NOW.isoformat()
    assert current_step(updated) is None


def test_a_two_step_chain_advances_then_finishes() -> None:
    chain = build_chain([MANAGER, HR])
    step_one, outcome = apply_decision(
        chain, approve=True, actor_user_id=uuid.uuid4(), actor_name="A", note=None, at=NOW
    )
    assert outcome == "advanced"
    assert current_step(step_one) == 1
    step_two, outcome = apply_decision(
        step_one, approve=True, actor_user_id=uuid.uuid4(), actor_name="B", note=None, at=NOW
    )
    assert outcome == "approved"


def test_a_rejection_closes_the_chain_and_skips_later_steps() -> None:
    chain = build_chain([MANAGER, HR])
    updated, outcome = apply_decision(
        chain, approve=False, actor_user_id=uuid.uuid4(), actor_name="A", note="Release week",
        at=NOW,
    )
    assert outcome == "rejected"
    assert [s["status"] for s in updated] == ["rejected", "skipped"]
    assert updated[0]["note"] == "Release week"


def test_deciding_a_finished_chain_is_refused() -> None:
    chain, _ = apply_decision(
        build_chain([MANAGER]), approve=True, actor_user_id=uuid.uuid4(), actor_name="A",
        note=None, at=NOW,
    )
    with pytest.raises(LeaveRuleError, match="already been decided"):
        apply_decision(
            chain, approve=True, actor_user_id=uuid.uuid4(), actor_name="A", note=None, at=NOW
        )


def test_the_original_chain_is_not_mutated() -> None:
    chain = build_chain([MANAGER])
    apply_decision(
        chain, approve=True, actor_user_id=uuid.uuid4(), actor_name="A", note=None, at=NOW
    )
    assert chain[0]["status"] == "pending"
