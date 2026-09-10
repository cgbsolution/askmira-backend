"""Slice 14 gate: unclaimed reimbursement entitlement becomes taxable.

The surprise this prevents: an employee with a 24,000 fuel entitlement who
submitted no bills discovers in March that the whole 24,000 is taxable salary
and the tax comes out of one month's pay. It is avoidable, and only avoidable in
advance.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.loans.reimbursements import (
    ReimbursementError,
    accepted_for_payment,
    position,
    should_warn,
)


def D(value: str | int) -> Decimal:  # noqa: N802 - a Decimal alias, used throughout
    return Decimal(str(value))


def claim(**overrides) -> dict:
    base = {"amount": D(6_000), "accepted_amount": D(6_000), "status": "verified"}
    base.update(overrides)
    return base


def at(**overrides):
    base = {
        "component_code": "FUEL",
        "financial_year": "2026-2027",
        "entitlement": D(24_000),
        "claims": [],
    }
    base.update(overrides)
    return position(**base)


# ═══════════════════════════════════════════════════════════════════════════
# The year-end taxable balance
# ═══════════════════════════════════════════════════════════════════════════


def test_nothing_claimed_makes_the_whole_entitlement_taxable() -> None:
    """The checklist gate."""
    result = at()
    assert result.unclaimed == D(24_000)
    assert result.taxable_if_year_ended_now == D(24_000)
    assert any("becomes taxable salary" in note for note in result.notes)


def test_accepted_claims_reduce_the_taxable_balance() -> None:
    result = at(claims=[claim(), claim()])
    assert result.accepted == D(12_000)
    assert result.unclaimed == D(12_000)


def test_a_fully_used_entitlement_leaves_nothing_taxable() -> None:
    result = at(claims=[claim(amount=D(24_000), accepted_amount=D(24_000))])
    assert result.unclaimed == D(0)
    assert not any("becomes taxable" in note for note in result.notes)


def test_an_unverified_claim_does_not_reduce_the_taxable_balance() -> None:
    """Being told in February that you are covered and in March that you are
    not is exactly the surprise this module exists to avoid."""
    result = at(claims=[claim(status="submitted")])

    assert result.accepted == D(0)
    assert result.unclaimed == D(24_000)
    assert any("submitted but not yet verified" in note for note in result.notes)


def test_a_rejected_claim_does_not_reduce_the_balance() -> None:
    result = at(claims=[claim(status="rejected", accepted_amount=D(0))])
    assert result.unclaimed == D(24_000)


def test_a_partial_acceptance_reduces_by_the_accepted_amount_only() -> None:
    result = at(claims=[claim(amount=D(10_000), accepted_amount=D(4_000), status="partial")])
    assert result.claimed == D(10_000)
    assert result.accepted == D(4_000)
    assert result.unclaimed == D(20_000)


def test_acceptance_beyond_the_entitlement_is_noted_and_not_exempt() -> None:
    """People do submit more bills than they are entitled to claim. It is not
    an error, and it is not exempt either."""
    result = at(claims=[claim(amount=D(30_000), accepted_amount=D(30_000))])

    assert result.excess_accepted == D(6_000)
    assert result.unclaimed == D(0)
    assert any("beyond the annual entitlement" in note for note in result.notes)


def test_the_position_is_json_serialisable() -> None:
    import json

    json.dumps(at(claims=[claim()]).as_dict())


def test_a_float_is_refused() -> None:
    with pytest.raises(ReimbursementError, match="must be a Decimal"):
        at(entitlement=24000.0)


# ═══════════════════════════════════════════════════════════════════════════
# What pays out
# ═══════════════════════════════════════════════════════════════════════════


def test_a_verified_claim_pays_in_full() -> None:
    amount, reason = accepted_for_payment(
        claim_amount=D(6_000), accepted_amount=D(6_000),
        remaining_entitlement=D(24_000), status="verified",
    )
    assert amount == D(6_000)
    assert "in full" in reason


def test_a_partial_acceptance_pays_the_accepted_amount_only() -> None:
    """The checklist gate."""
    amount, reason = accepted_for_payment(
        claim_amount=D(10_000), accepted_amount=D(4_000),
        remaining_entitlement=D(24_000), status="partial",
    )
    assert amount == D(4_000)
    assert "the rest is not paid" in reason


def test_a_rejected_claim_pays_nothing() -> None:
    amount, reason = accepted_for_payment(
        claim_amount=D(6_000), accepted_amount=D(0),
        remaining_entitlement=D(24_000), status="rejected",
    )
    assert amount == D(0)
    assert "rejected" in reason


def test_an_unverified_claim_pays_nothing_yet() -> None:
    amount, reason = accepted_for_payment(
        claim_amount=D(6_000), accepted_amount=D(6_000),
        remaining_entitlement=D(24_000), status="submitted",
    )
    assert amount == D(0)
    assert "awaiting verification" in reason


def test_payment_is_capped_at_the_remaining_entitlement() -> None:
    amount, reason = accepted_for_payment(
        claim_amount=D(10_000), accepted_amount=D(10_000),
        remaining_entitlement=D(3_000), status="verified",
    )
    assert amount == D(3_000)
    assert "only 3000 of the annual entitlement is left" in reason


def test_accepting_more_than_was_submitted_is_refused() -> None:
    with pytest.raises(ReimbursementError, match="More cannot be accepted"):
        accepted_for_payment(
            claim_amount=D(5_000), accepted_amount=D(6_000),
            remaining_entitlement=D(24_000), status="verified",
        )


# ═══════════════════════════════════════════════════════════════════════════
# Warning in advance
# ═══════════════════════════════════════════════════════════════════════════


YEAR_END = date(2027, 3, 31)


def test_a_warning_fires_inside_the_reminder_window() -> None:
    """The checklist gate: advance notification."""
    warn, message = should_warn(
        today=date(2027, 2, 1), financial_year_end=YEAR_END,
        unclaimed=D(24_000), reminder_months_before_year_end=2,
    )
    assert warn is True
    assert "Submit bills before the year ends" in message
    assert "24000.00" in message


def test_no_warning_fires_early_in_the_year() -> None:
    """A reminder every month from April is a reminder nobody reads."""
    warn, message = should_warn(
        today=date(2026, 6, 1), financial_year_end=YEAR_END,
        unclaimed=D(24_000), reminder_months_before_year_end=2,
    )
    assert warn is False
    assert "month(s) remain" in message


def test_no_warning_fires_when_the_entitlement_is_used() -> None:
    warn, message = should_warn(
        today=date(2027, 2, 1), financial_year_end=YEAR_END,
        unclaimed=D(0), reminder_months_before_year_end=2,
    )
    assert warn is False
    assert "fully used" in message


def test_after_the_year_ends_it_is_no_longer_a_warning() -> None:
    """Telling someone in April about a March consequence is an announcement,
    not a warning."""
    warn, message = should_warn(
        today=date(2027, 4, 15), financial_year_end=YEAR_END,
        unclaimed=D(24_000), reminder_months_before_year_end=2,
    )
    assert warn is False
    assert "now taxable" in message


def test_the_reminder_window_is_configuration() -> None:
    early = should_warn(
        today=date(2026, 12, 1), financial_year_end=YEAR_END,
        unclaimed=D(24_000), reminder_months_before_year_end=4,
    )
    late = should_warn(
        today=date(2026, 12, 1), financial_year_end=YEAR_END,
        unclaimed=D(24_000), reminder_months_before_year_end=1,
    )
    assert early[0] is True
    assert late[0] is False


def test_the_warning_names_the_amount_and_the_months_left() -> None:
    """A warning that does not say how much or how long is not actionable."""
    _, message = should_warn(
        today=date(2027, 3, 1), financial_year_end=YEAR_END,
        unclaimed=D(18_000), reminder_months_before_year_end=2,
    )
    assert "18000.00" in message
    assert "0 month(s)" in message or "1 month(s)" in message
