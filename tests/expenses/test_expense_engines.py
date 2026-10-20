"""Slice 31 — policy evaluation, mileage, per-diem, currency and advances.

Organised by the way each control fails in a real expense process: a limit that
is only checked at approval, an aggregating limit that resets per claim, a
per-diem with a receipt demand, a currency convention silently substituted, and
the one finance actually asks about — a second advance issued while the first is
outstanding.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.expenses import advances, policy, rates

TODAY = date(2026, 9, 7)
D = Decimal


def rule(**kw) -> policy.ExpenseRule:
    base = {
        "category_key": "hotel",
        "city_tier": policy.CityTier.A,
        "limit_type": policy.LimitType.PER_CLAIM,
        "limit_amount": D("8000"),
        "violation_action": policy.ViolationAction.FLAG,
    }
    base.update(kw)
    return policy.ExpenseRule(**base)


def claim(**kw) -> policy.ClaimFacts:
    base = {
        "category_key": "hotel",
        "city_tier": policy.CityTier.A,
        "amount": D("5000"),
        "incurred_on": TODAY,
        "has_receipt": True,
    }
    base.update(kw)
    return policy.ClaimFacts(**base)


# ══════════════════════════════════════════════════════════════════════════════
# The verdict exists at capture and carries numbers
# ══════════════════════════════════════════════════════════════════════════════


def test_a_claim_inside_the_limit_is_within_policy():
    v = policy.evaluate(claim(), rules=[rule()], priors=[])
    assert v.kind is policy.VerdictKind.WITHIN_POLICY
    assert v.permits_submission
    assert not v.is_violation


def test_the_verdict_carries_the_limit_the_overage_and_what_to_do():
    """A message saying 'out of policy' and nothing else is not actionable.

    The employee has to be able to see the limit, what has already gone against
    it and by how much this claim is over, at capture, without asking anybody.
    """
    v = policy.evaluate(
        claim(amount=D("9500")), rules=[rule()], priors=[]
    )
    assert v.limit_amount == D("8000.00")
    assert v.this_claim == D("9500.00")
    assert v.overage == D("1500.00")
    assert "₹8,000.00" in v.message and "₹1,500.00" in v.message


def test_no_rule_permits_the_claim_and_says_so():
    """A category nobody wrote a limit for is a gap, not a breach.

    Blocking it would make every new expense category an outage until somebody
    edits policy.
    """
    v = policy.evaluate(claim(category_key="stationery"), rules=[rule()], priors=[])
    assert v.kind is policy.VerdictKind.NO_RULE
    assert v.permits_submission
    assert not v.is_violation


def test_a_rule_matches_on_tier_as_well_as_category():
    """The tier-A limit must not silently govern a tier-C city.

    A fallback would hold the employee to a limit nobody wrote for where they
    were, and they could not tell which one applied.
    """
    v = policy.evaluate(
        claim(city_tier=policy.CityTier.C, amount=D("9999")),
        rules=[rule()],
        priors=[],
    )
    assert v.kind is policy.VerdictKind.NO_RULE


# ══════════════════════════════════════════════════════════════════════════════
# All three violation actions
# ══════════════════════════════════════════════════════════════════════════════


def test_block_prevents_submission():
    v = policy.evaluate(
        claim(amount=D("12000")),
        rules=[rule(violation_action=policy.ViolationAction.BLOCK)],
        priors=[],
    )
    assert v.kind is policy.VerdictKind.BLOCKED
    assert not v.permits_submission


def test_flag_submits_with_a_flag():
    v = policy.evaluate(
        claim(amount=D("12000")),
        rules=[rule(violation_action=policy.ViolationAction.FLAG)],
        priors=[],
    )
    assert v.kind is policy.VerdictKind.FLAGGED
    assert v.permits_submission
    assert v.is_violation


def test_require_justification_blocks_until_a_reason_is_written():
    over = rule(violation_action=policy.ViolationAction.REQUIRE_JUSTIFICATION)
    without = policy.evaluate(claim(amount=D("12000")), rules=[over], priors=[])
    assert without.kind is policy.VerdictKind.NEEDS_JUSTIFICATION
    assert not without.permits_submission

    with_reason = policy.evaluate(
        claim(amount=D("12000"), justification="Conference hotel, only room left."),
        rules=[over],
        priors=[],
    )
    assert with_reason.permits_submission
    assert with_reason.is_violation


def test_whitespace_is_not_a_justification():
    """A space bar is not a written reason.

    Accepting one turns the control into a keystroke and the approver sees a
    blank where the explanation should be.
    """
    v = policy.evaluate(
        claim(amount=D("12000"), justification="   \n  "),
        rules=[rule(violation_action=policy.ViolationAction.REQUIRE_JUSTIFICATION)],
        priors=[],
    )
    assert v.kind is policy.VerdictKind.NEEDS_JUSTIFICATION


# ══════════════════════════════════════════════════════════════════════════════
# Aggregation: the limit that is not per claim
# ══════════════════════════════════════════════════════════════════════════════


def test_a_per_trip_limit_aggregates_across_the_trips_claims():
    """Four ₹3,000 hotel nights on one trip breach a ₹10,000 trip limit.

    Evaluating each claim on its own is the common bug: every one is inside the
    limit and the trip is 20% over.
    """
    trip_rule = rule(limit_type=policy.LimitType.PER_TRIP, limit_amount=D("10000"))
    priors = [
        policy.PriorClaim(
            category_key="hotel", amount=D("3000"), incurred_on=TODAY, trip_id="t1"
        )
        for _ in range(3)
    ]
    v = policy.evaluate(
        claim(amount=D("3000"), trip_id="t1"), rules=[trip_rule], priors=priors
    )
    assert v.already_claimed == D("9000.00")
    assert v.overage == D("2000.00")
    assert v.is_violation


def test_another_trips_claims_do_not_consume_this_trips_limit():
    trip_rule = rule(limit_type=policy.LimitType.PER_TRIP, limit_amount=D("10000"))
    priors = [
        policy.PriorClaim(
            category_key="hotel", amount=D("9000"), incurred_on=TODAY, trip_id="other"
        )
    ]
    v = policy.evaluate(
        claim(amount=D("3000"), trip_id="t1"), rules=[trip_rule], priors=priors
    )
    assert v.already_claimed == D("0.00")
    assert v.kind is policy.VerdictKind.WITHIN_POLICY


def test_a_per_day_limit_counts_only_that_day():
    day_rule = rule(limit_type=policy.LimitType.PER_DAY, limit_amount=D("2000"))
    priors = [
        policy.PriorClaim(category_key="hotel", amount=D("1800"), incurred_on=TODAY),
        policy.PriorClaim(
            category_key="hotel", amount=D("5000"), incurred_on=date(2026, 9, 1)
        ),
    ]
    v = policy.evaluate(
        claim(amount=D("500"), incurred_on=TODAY), rules=[day_rule], priors=priors
    )
    assert v.already_claimed == D("1800.00")
    assert v.overage == D("300.00")


def test_a_per_month_limit_counts_the_calendar_month():
    month_rule = rule(limit_type=policy.LimitType.PER_MONTH, limit_amount=D("20000"))
    priors = [
        policy.PriorClaim(
            category_key="hotel", amount=D("19000"), incurred_on=date(2026, 9, 1)
        ),
        policy.PriorClaim(
            category_key="hotel", amount=D("30000"), incurred_on=date(2026, 8, 31)
        ),
    ]
    v = policy.evaluate(
        claim(amount=D("2000"), incurred_on=TODAY),
        rules=[month_rule],
        priors=priors,
    )
    assert v.already_claimed == D("19000.00")
    assert v.is_violation


def test_aggregation_ignores_other_categories():
    month_rule = rule(limit_type=policy.LimitType.PER_MONTH, limit_amount=D("20000"))
    priors = [
        policy.PriorClaim(
            category_key="meals", amount=D("19000"), incurred_on=date(2026, 9, 2)
        )
    ]
    v = policy.evaluate(
        claim(amount=D("2000")), rules=[month_rule], priors=priors
    )
    assert v.already_claimed == D("0.00")


def test_a_per_trip_rule_on_a_claim_with_no_trip_refuses_to_guess():
    """Aggregating nothing would let every claim through under a trip limit.

    Treating it as per-claim instead would apply a trip-sized limit to a single
    expense, which is a limit nobody wrote. Both readings are wrong, so it
    raises and names the category.
    """
    priors = [
        policy.PriorClaim(
            category_key="hotel", amount=D("9000"), incurred_on=TODAY, trip_id="t1"
        )
    ]
    with pytest.raises(policy.PolicyError) as exc:
        policy.aggregate_window(
            policy.LimitType.PER_TRIP, claim(trip_id=None), priors
        )
    assert "not attached to a trip" in str(exc.value)


# ══════════════════════════════════════════════════════════════════════════════
# Receipts and pre-approval
# ══════════════════════════════════════════════════════════════════════════════


def test_a_receipt_is_not_demanded_below_the_threshold():
    """A ₹40 auto fare with a receipt demand produces fake receipts."""
    v = policy.evaluate(
        claim(
            category_key="local_travel",
            amount=D("40"),
            has_receipt=False,
        ),
        rules=[
            rule(
                category_key="local_travel",
                limit_amount=D("500"),
                requires_receipt=True,
                receipt_threshold=D("300"),
            )
        ],
        priors=[],
    )
    assert v.kind is policy.VerdictKind.WITHIN_POLICY


def test_a_missing_receipt_above_the_threshold_stops_submission():
    v = policy.evaluate(
        claim(amount=D("5000"), has_receipt=False),
        rules=[rule(requires_receipt=True, receipt_threshold=D("300"))],
        priors=[],
    )
    assert v.kind is policy.VerdictKind.RECEIPT_REQUIRED
    assert not v.permits_submission


def test_a_per_diem_rule_never_demands_a_receipt():
    """That is what a per-diem is: a fixed amount instead of itemising.

    Demanding one asks for a document that does not exist, and the employee
    produces something.
    """
    v = policy.evaluate(
        claim(category_key="per_diem", amount=D("1500"), has_receipt=False),
        rules=[
            rule(
                category_key="per_diem",
                limit_amount=D("2000"),
                requires_receipt=True,
                is_per_diem=True,
            )
        ],
        priors=[],
    )
    assert v.kind is policy.VerdictKind.WITHIN_POLICY


def test_pre_approval_is_required_where_the_rule_says_so():
    v = policy.evaluate(
        claim(amount=D("5000"), pre_approved=False),
        rules=[rule(requires_pre_approval=True)],
        priors=[],
    )
    assert v.kind is policy.VerdictKind.PRE_APPROVAL_REQUIRED
    assert not v.permits_submission

    ok = policy.evaluate(
        claim(amount=D("5000"), pre_approved=True),
        rules=[rule(requires_pre_approval=True)],
        priors=[],
    )
    assert ok.permits_submission


# ══════════════════════════════════════════════════════════════════════════════
# Violation rates
# ══════════════════════════════════════════════════════════════════════════════


def test_a_rate_below_a_usable_sample_is_none():
    """Two out of three is not a rate, and reporting 67% invites a conversation
    about somebody's character based on three claims."""
    r = policy.ViolationRate(scope="employee", key="e1", total_claims=3, violations=2)
    assert r.rate is None
    assert "too few" in r.interpretation()


def test_a_high_rate_against_a_high_peer_rate_reads_as_a_policy_problem():
    r = policy.ViolationRate(
        scope="employee", key="e1", total_claims=40, violations=24
    )
    assert "the limit is usually the thing that is wrong" in r.interpretation(
        peer_rate=0.55
    )


def test_a_high_rate_against_a_compliant_group_does_not_blame_the_limit():
    r = policy.ViolationRate(
        scope="employee", key="e1", total_claims=40, violations=24
    )
    reading = r.interpretation(peer_rate=0.04)
    assert "the limit is usually the thing that is wrong" not in reading


# ══════════════════════════════════════════════════════════════════════════════
# Rates: effective dating
# ══════════════════════════════════════════════════════════════════════════════


def test_a_rate_revised_in_april_does_not_change_a_march_claim():
    march = rates.MileageRate(
        vehicle_type="car", rate_per_km=D("10"), effective_from=date(2026, 3, 1)
    )
    april = rates.MileageRate(
        vehicle_type="car", rate_per_km=D("12"), effective_from=date(2026, 4, 1)
    )
    resolved = rates.rate_on([march, april], date(2026, 3, 20), vehicle_type="car")
    assert resolved.rate_per_km == D("10")


def test_the_latest_rate_on_or_before_the_date_wins():
    old = rates.MileageRate(
        vehicle_type="car", rate_per_km=D("10"), effective_from=date(2026, 3, 1)
    )
    new = rates.MileageRate(
        vehicle_type="car", rate_per_km=D("12"), effective_from=date(2026, 4, 1)
    )
    assert (
        rates.rate_on([old, new], date(2026, 6, 1), vehicle_type="car").rate_per_km
        == D("12")
    )


def test_no_rate_in_force_raises_rather_than_guessing():
    r = rates.MileageRate(
        vehicle_type="car", rate_per_km=D("12"), effective_from=date(2026, 4, 1)
    )
    with pytest.raises(rates.RateError) as exc:
        rates.rate_on([r], date(2026, 1, 1), vehicle_type="car")
    assert "nobody authorised" in str(exc.value)


def test_a_rate_for_another_vehicle_is_not_borrowed():
    car = rates.MileageRate(
        vehicle_type="car", rate_per_km=D("12"), effective_from=date(2026, 4, 1)
    )
    with pytest.raises(rates.RateError):
        rates.rate_on([car], TODAY, vehicle_type="two_wheeler")


# ══════════════════════════════════════════════════════════════════════════════
# Mileage: flags, never refuses
# ══════════════════════════════════════════════════════════════════════════════


def test_a_distance_beyond_tolerance_is_flagged_not_refused():
    """A route is a straight line and a road is not.

    Rejecting a diversion round roadworks teaches the employee to declare
    exactly the route distance next time, which destroys the only signal the
    check had.
    """
    r = rates.MileageRate(
        vehicle_type="car", rate_per_km=D("12"), effective_from=date(2026, 4, 1)
    )
    out = rates.mileage_claim(distance_km=D("140"), rate=r, route_km=D("110"))
    assert out.exceeds_tolerance
    assert out.amount == D("1680.00")
    assert "Flagged for the approver, not" in out.note


def test_a_distance_inside_tolerance_is_not_flagged():
    r = rates.MileageRate(
        vehicle_type="car", rate_per_km=D("12"), effective_from=date(2026, 4, 1)
    )
    out = rates.mileage_claim(distance_km=D("118"), rate=r, route_km=D("110"))
    assert not out.exceeds_tolerance


def test_no_route_means_nothing_to_compare_against():
    r = rates.MileageRate(
        vehicle_type="car", rate_per_km=D("12"), effective_from=date(2026, 4, 1)
    )
    out = rates.mileage_claim(distance_km=D("140"), rate=r)
    assert out.variance_percent is None
    assert not out.exceeds_tolerance


# ══════════════════════════════════════════════════════════════════════════════
# Per-diem
# ══════════════════════════════════════════════════════════════════════════════


def test_a_per_diem_claim_requires_no_receipt():
    r = rates.PerDiemRate(
        city_tier=policy.CityTier.B,
        grade="M3",
        amount=D("1500"),
        effective_from=date(2026, 4, 1),
    )
    out = rates.per_diem_claim(days=3, rate=r)
    assert out.requires_receipt is False
    assert out.amount == D("4500.00")
    assert "No receipt is needed" in out.note


def test_a_per_diem_says_what_it_covers():
    """So a policy can say what may *also* be claimed, rather than asking the
    employee to prove what the per-diem was for."""
    r = rates.PerDiemRate(
        city_tier=policy.CityTier.A,
        grade="M3",
        amount=D("2500"),
        effective_from=date(2026, 4, 1),
        includes_accommodation=True,
    )
    assert rates.per_diem_claim(days=1, rate=r).covers == ("meals", "accommodation")


def test_a_zero_day_per_diem_is_refused():
    r = rates.PerDiemRate(
        city_tier=policy.CityTier.A,
        grade="M3",
        amount=D("2500"),
        effective_from=date(2026, 4, 1),
    )
    with pytest.raises(rates.RateError):
        rates.per_diem_claim(days=0, rate=r)


# ══════════════════════════════════════════════════════════════════════════════
# Currency
# ══════════════════════════════════════════════════════════════════════════════


def test_both_amounts_are_stored():
    out = rates.convert(
        amount=D("80"),
        currency="EUR",
        transaction_date=date(2026, 9, 3),
        settlement_date=date(2026, 9, 20),
        convention=rates.ConversionConvention.TRANSACTION_DATE,
        rate=D("94.20"),
        rate_source="RBI reference",
    )
    assert out.original_amount == D("80.00")
    assert out.original_currency == "EUR"
    assert out.converted_amount == D("7536.00")
    assert out.converted_currency == "INR"


def test_the_convention_chooses_the_rate_date_and_is_recorded():
    common = {
        "amount": D("80"),
        "currency": "EUR",
        "transaction_date": date(2026, 9, 3),
        "settlement_date": date(2026, 9, 20),
        "rate": D("94.20"),
        "rate_source": "RBI reference",
    }
    txn = rates.convert(
        convention=rates.ConversionConvention.TRANSACTION_DATE, **common
    )
    stl = rates.convert(
        convention=rates.ConversionConvention.SETTLEMENT_DATE, **common
    )
    assert txn.rate_date == date(2026, 9, 3)
    assert stl.rate_date == date(2026, 9, 20)
    assert txn.as_dict()["convention"] == "transaction_date"
    assert stl.as_dict()["convention"] == "settlement_date"


def test_settlement_convention_with_no_settlement_date_refuses():
    """Rather than quietly falling back to the transaction date.

    The two produce different numbers, and substituting one convention for the
    other silently is the discrepancy a finance team finds at year end and
    cannot explain.
    """
    with pytest.raises(rates.RateError) as exc:
        rates.convert(
            amount=D("80"),
            currency="EUR",
            transaction_date=date(2026, 9, 3),
            settlement_date=None,
            convention=rates.ConversionConvention.SETTLEMENT_DATE,
            rate=D("94.20"),
            rate_source="RBI reference",
        )
    assert "convention nobody chose" in str(exc.value)


def test_converting_the_reporting_currency_into_itself_is_refused():
    with pytest.raises(rates.RateError):
        rates.convert(
            amount=D("80"),
            currency="INR",
            transaction_date=TODAY,
            settlement_date=None,
            convention=rates.ConversionConvention.TRANSACTION_DATE,
            rate=D("1"),
            rate_source="x",
        )


def test_a_zero_rate_is_refused():
    with pytest.raises(rates.RateError):
        rates.convert(
            amount=D("80"),
            currency="EUR",
            transaction_date=TODAY,
            settlement_date=None,
            convention=rates.ConversionConvention.TRANSACTION_DATE,
            rate=D("0"),
            rate_source="x",
        )


# ══════════════════════════════════════════════════════════════════════════════
# Advances: settlement against the trip
# ══════════════════════════════════════════════════════════════════════════════


def advance(**kw) -> advances.AdvanceFacts:
    base = {
        "advance_id": "a1",
        "trip_id": "t1",
        "employee_id": "e1",
        "amount": D("40000"),
        "issued_on": date(2026, 3, 1),
    }
    base.update(kw)
    return advances.AdvanceFacts(**base)


def test_claims_matching_the_advance_settle_square():
    out = advances.settle(advance(), claims_total=D("40000"), trip_ids_of_claims=("t1",))
    assert out.outcome is advances.SettlementOutcome.SQUARE
    assert out.recovery_amount == D("0.00")
    assert out.refund_amount == D("0.00")


def test_an_under_spent_advance_produces_a_payroll_recovery():
    out = advances.settle(advance(), claims_total=D("31500"), trip_ids_of_claims=("t1",))
    assert out.outcome is advances.SettlementOutcome.RECOVERY_DUE_FROM_EMPLOYEE
    assert out.recovery_amount == D("8500.00")
    assert out.outcome.needs_payroll_recovery


def test_an_over_spent_advance_produces_a_refund():
    out = advances.settle(advance(), claims_total=D("44000"), trip_ids_of_claims=("t1",))
    assert out.outcome is advances.SettlementOutcome.REFUND_DUE_TO_EMPLOYEE
    assert out.refund_amount == D("4000.00")
    assert not out.outcome.needs_payroll_recovery


def test_claims_from_another_trip_cannot_settle_this_advance():
    """Netting across trips lets an under-spent March trip quietly settle an
    over-spent June one, and the balance nobody would look at again is the one
    that mattered."""
    with pytest.raises(advances.AdvanceError) as exc:
        advances.settle(
            advance(), claims_total=D("40000"), trip_ids_of_claims=("t1", "t9")
        )
    assert "netting across trips" in str(exc.value)


def test_an_already_settled_advance_cannot_be_settled_again():
    with pytest.raises(advances.AdvanceError) as exc:
        advances.settle(
            advance(settled_on=date(2026, 5, 1)),
            claims_total=D("40000"),
            trip_ids_of_claims=("t1",),
        )
    assert "twice" in str(exc.value)


# ══════════════════════════════════════════════════════════════════════════════
# The block finance actually cares about
# ══════════════════════════════════════════════════════════════════════════════


def test_an_unsettled_advance_past_the_threshold_blocks_a_new_one():
    decision = advances.advance_block([advance()], today=TODAY)
    assert not decision.permitted
    assert decision.total_outstanding == D("40000.00")


def test_the_refusal_names_the_trip_and_says_what_clears_it():
    """A refusal with no detail sends the employee to finance, and finance says
    yes — because the person in front of them has a trip on Monday and nobody
    can see which old advance is the problem."""
    message = advances.advance_block([advance()], today=TODAY).message
    assert "t1" in message
    assert "2026-03-01" in message
    assert "Submit the claims" in message


def test_a_recent_advance_does_not_block():
    decision = advances.advance_block(
        [advance(issued_on=date(2026, 8, 25))], today=TODAY
    )
    assert decision.permitted
    assert decision.total_outstanding == D("40000.00")


def test_a_settled_advance_does_not_block_however_old():
    decision = advances.advance_block(
        [advance(settled_on=date(2026, 4, 1))], today=TODAY
    )
    assert decision.permitted
    assert decision.total_outstanding == D("0.00")


def test_a_fully_claimed_advance_does_not_block():
    decision = advances.advance_block(
        [advance(claimed_against=D("40000"))], today=TODAY
    )
    assert decision.permitted


def test_a_zero_day_threshold_is_refused():
    """It would block every employee who has ever taken an advance, including
    one issued this morning."""
    with pytest.raises(advances.AdvanceError):
        advances.advance_block([advance()], today=TODAY, block_after_days=0)


def test_the_ageing_report_buckets_by_age():
    rows = [
        advance(advance_id="a1", issued_on=date(2026, 9, 1)),
        advance(advance_id="a2", issued_on=date(2026, 8, 1), amount=D("10000")),
        advance(advance_id="a3", issued_on=date(2026, 3, 1), amount=D("20000")),
    ]
    report = advances.ageing_report(rows, today=TODAY)
    assert report["buckets"]["0-30"]["count"] == 1
    assert report["buckets"]["31-45"]["count"] == 1
    assert report["buckets"]["over_90"]["count"] == 1
    assert report["blocking_count"] == 1
    assert report["total_outstanding"] == "70000.00"


def test_a_settled_advance_reports_no_outstanding_balance():
    """Once it is settled the balance moved to a recovery or a refund, and
    continuing to report it as outstanding would double-count it."""
    settled = advance(settled_on=date(2026, 4, 1), claimed_against=D("0"))
    assert settled.outstanding == D("0")
