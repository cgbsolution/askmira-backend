"""Slice 33 — points liability, valuation, budgets, tax and reconciliation.

Organised by the way each control fails in a real rewards programme: a mutable
balance that drifts, a rate change that restates last year's accounts, an
expiry that quietly deletes what somebody earned, a budget that warns and is
never counted, a perquisite computed from a threshold nobody checked, and a
reconciliation that nets a fulfilment failure against a billing error.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.services.rewards import budgets, ledger, reconciliation, tax, valuation

D = Decimal
TODAY = date(2026, 9, 7)
T = ledger.TransactionType


def entry(
    entry_id: str,
    kind: ledger.TransactionType,
    points: int,
    rate: str,
    on: date,
    balance_after: int,
    **kw,
) -> ledger.LedgerEntry:
    return ledger.LedgerEntry(
        entry_id=entry_id,
        employee_id=kw.pop("employee_id", "e1"),
        transaction_type=kind,
        points=points,
        monetary_value=D(str(abs(points))) * D(rate),
        occurred_at=datetime(on.year, on.month, on.day, tzinfo=UTC),
        balance_after=balance_after,
        rate_per_point=D(rate),
        **kw,
    )


LEDGER = [
    entry("1", T.AWARDED, 10000, "0.50", date(2026, 3, 1), 10000),
    entry("2", T.REDEEMED, 4000, "0.50", date(2026, 4, 15), 6000),
    entry("3", T.AWARDED, 5000, "1.00", date(2026, 6, 1), 11000),
    entry("4", T.EXPIRED, 1000, "0.50", date(2026, 7, 1), 10000),
]


# ══════════════════════════════════════════════════════════════════════════════
# The ledger: derived balance, immutable checkpoints
# ══════════════════════════════════════════════════════════════════════════════


def test_the_balance_is_derived_from_the_transactions():
    assert ledger.derive_balance(LEDGER) == 10000


def test_a_complex_sequence_derives_correctly():
    rows = [
        entry("1", T.AWARDED, 500, "1.00", date(2026, 1, 1), 500),
        entry("2", T.AWARDED, 300, "1.00", date(2026, 2, 1), 800),
        entry("3", T.REDEEMED, 200, "1.00", date(2026, 3, 1), 600),
        entry("4", T.REVERSED, 100, "1.00", date(2026, 3, 15), 500),
        entry("5", T.ADJUSTED, -50, "1.00", date(2026, 4, 1), 450),
        entry("6", T.ADJUSTED, 75, "1.00", date(2026, 5, 1), 525),
        entry("7", T.EXPIRED, 25, "1.00", date(2026, 6, 1), 500),
    ]
    assert ledger.derive_balance(rows) == 500
    assert ledger.verify_chain(rows)["intact"]


def test_the_stored_checkpoint_is_compared_not_trusted():
    """A mutable balance drifts silently and nobody finds out until finance
    reconciles. A checkpoint beside a derived figure makes drift visible."""
    tampered = [*LEDGER[:2], entry("3", T.AWARDED, 5000, "1.00", date(2026, 6, 1), 99999)]
    report = ledger.verify_chain(tampered)
    assert not report["intact"]
    assert report["breaks"][0]["entry_id"] == "3"
    assert "written to outside the service" in report["note"]


def test_an_adjustment_carries_its_own_sign():
    """Forcing it positive would make every correction an award."""
    down = entry("1", T.ADJUSTED, -200, "1.00", date(2026, 1, 1), -200)
    assert down.signed_points == -200


def test_a_negative_award_is_refused():
    """A negative award is a reversal and should say so."""
    with pytest.raises(ledger.LedgerError):
        entry("1", T.AWARDED, -100, "1.00", date(2026, 1, 1), -100)


def test_a_zero_point_row_is_refused():
    with pytest.raises(ledger.LedgerError):
        entry("1", T.AWARDED, 0, "1.00", date(2026, 1, 1), 0)


# ══════════════════════════════════════════════════════════════════════════════
# Liability at the issue rate
# ══════════════════════════════════════════════════════════════════════════════


def test_liability_values_each_lot_at_its_own_rate():
    """10,000 at Rs 0.50 less 5,000 consumed, plus 5,000 at Rs 1.00.

    Balance times today's rate would say Rs 10,000 - a Rs 2,500 overstatement
    of a balance-sheet number.
    """
    result = ledger.liability(LEDGER)
    assert result.balance_points == 10000
    assert result.rupees == D("7500.00")


def test_redemptions_consume_the_oldest_award_first():
    result = ledger.liability(LEDGER)
    assert [lot["rate_per_point"] for lot in result.lots] == ["0.50", "1.00"]
    assert result.lots[0]["remaining_points"] == 5000


def test_a_rate_change_does_not_restate_earlier_awards():
    before = ledger.liability(LEDGER[:2]).rupees
    after = ledger.liability(LEDGER).rupees
    # The first lot's contribution is unchanged by the June revaluation.
    assert before == D("3000.00")
    assert after - D("5000.00") == D("2500.00")


def test_consumption_beyond_the_awards_is_reported_not_absorbed():
    """Reporting a tidy zero would hide an incomplete ledger."""
    rows = [entry("1", T.REDEEMED, 500, "0.50", date(2026, 3, 1), -500)]
    result = ledger.liability(rows)
    assert result.unmatched_consumption == 500
    assert "ledger is incomplete" in result.note


# ══════════════════════════════════════════════════════════════════════════════
# Movement
# ══════════════════════════════════════════════════════════════════════════════


def test_the_movement_report_reconciles_opening_to_closing():
    report = ledger.movement_report(
        LEDGER, period_start=date(2026, 4, 1), period_end=date(2026, 9, 30)
    )
    assert report["opening"] == 10000
    assert report["awarded"] == 5000
    assert report["redeemed"] == 4000
    assert report["expired"] == 1000
    assert report["closing"] == 10000
    assert report["reconciles"] is True


def test_the_movement_report_covers_every_transaction_type():
    rows = [
        entry("1", T.AWARDED, 500, "1.00", date(2026, 5, 1), 500),
        entry("2", T.REVERSED, 100, "1.00", date(2026, 5, 2), 400),
        entry("3", T.ADJUSTED, -50, "1.00", date(2026, 5, 3), 350),
    ]
    report = ledger.movement_report(
        rows, period_start=date(2026, 5, 1), period_end=date(2026, 5, 31)
    )
    assert report["reversed"] == 100
    assert report["adjusted"] == -50
    assert report["closing"] == 350


def test_a_period_that_ends_before_it_starts_is_refused():
    with pytest.raises(ledger.LedgerError):
        ledger.movement_report(
            LEDGER, period_start=date(2026, 9, 1), period_end=date(2026, 8, 1)
        )


# ══════════════════════════════════════════════════════════════════════════════
# Ageing and provisioning
# ══════════════════════════════════════════════════════════════════════════════


def test_ageing_buckets_by_the_age_of_the_award():
    report = ledger.ageing(LEDGER, as_of=TODAY)
    assert report["buckets"]["91-180"]["points"] == 5000
    assert report["buckets"]["181-365"]["points"] == 5000
    assert report["total_rupees"] == "7500.00"


def test_a_redemption_rate_below_a_usable_sample_is_none():
    """An optimistic rate from a small sample understates a real liability."""
    rows = [entry("1", T.AWARDED, 100, "1.00", date(2026, 1, 1), 100)]
    assert ledger.expected_redemption_rate(rows)["rate"] is None


def test_expired_points_are_not_counted_as_redemption():
    """Counting them would make an unused programme look healthy."""
    rows = [
        entry("1", T.AWARDED, 10000, "1.00", date(2026, 1, 1), 10000),
        entry("2", T.EXPIRED, 9000, "1.00", date(2026, 6, 1), 1000),
    ]
    result = ledger.expected_redemption_rate(rows)
    assert result["rate"] == 0.0
    assert result["expired"] == 9000


def test_liability_splits_by_department():
    rows = [
        entry("1", T.AWARDED, 1000, "1.00", date(2026, 1, 1), 1000, department_key="eng"),
        entry("2", T.AWARDED, 500, "1.00", date(2026, 1, 1), 1500, department_key="sales"),
    ]
    report = ledger.by_group(rows)
    assert {g["department_key"]: g["rupees"] for g in report["groups"]} == {
        "eng": "1000.00",
        "sales": "500.00",
    }


def test_a_finer_split_than_cost_centre_is_refused():
    """Any finer and the provision is being allocated to individuals."""
    with pytest.raises(ledger.LedgerError):
        ledger.by_group(LEDGER, key="employee_id")


# ══════════════════════════════════════════════════════════════════════════════
# Expiry
# ══════════════════════════════════════════════════════════════════════════════


POLICY = ledger.ExpiryPolicy(
    expire_after_days=365,
    notify_days_before=30,
    disclosure="Points expire twelve months after award; you are warned 30 days before.",
)


def test_an_expiry_policy_needs_a_disclosure():
    """Points that quietly vanish teach people the recognition was not real."""
    with pytest.raises(ledger.LedgerError) as exc:
        ledger.ExpiryPolicy(expire_after_days=365, disclosure="  ")
    assert "shown at the point of award" in str(exc.value)


def test_a_very_short_expiry_is_refused():
    with pytest.raises(ledger.LedgerError):
        ledger.ExpiryPolicy(expire_after_days=30, disclosure="Gone in a month.")


def test_a_warning_of_less_than_a_week_is_refused():
    with pytest.raises(ledger.LedgerError):
        ledger.ExpiryPolicy(
            expire_after_days=365, notify_days_before=2, disclosure="Warned late."
        )


def test_expiring_soon_names_the_lot_and_the_date():
    """"You have 300 points expiring" and "300 points, 40 of which expire on
    Friday" produce different behaviour."""
    rows = ledger.expiring_soon(LEDGER, policy=POLICY, as_of=date(2027, 2, 10))
    assert len(rows) == 1
    assert rows[0]["points"] == 5000
    assert rows[0]["expires_on"] == "2027-03-01"
    assert rows[0]["days_left"] == 19


def test_expirable_returns_what_should_be_written_not_deleted():
    """An expired point that vanished from the ledger is a liability reduction
    nobody can audit."""
    rows = ledger.expirable(LEDGER, policy=POLICY, as_of=date(2027, 7, 1))
    assert {r["points"] for r in rows} == {5000}
    assert len(rows) == 2


def test_a_disabled_expiry_policy_expires_nothing():
    disabled = ledger.ExpiryPolicy(
        expire_after_days=365, disclosure="unused", enabled=False
    )
    assert ledger.expirable(LEDGER, policy=disabled, as_of=date(2030, 1, 1)) == []


# ══════════════════════════════════════════════════════════════════════════════
# Valuation
# ══════════════════════════════════════════════════════════════════════════════


VALUATIONS = [
    valuation.PointsValuation(D("2"), date(2026, 1, 1)),
    valuation.PointsValuation(D("1"), date(2026, 6, 1)),
]


def test_an_award_is_valued_at_the_rate_in_force_that_day():
    march = valuation.value_award(VALUATIONS, points=10000, awarded_on=date(2026, 3, 1))
    july = valuation.value_award(VALUATIONS, points=10000, awarded_on=date(2026, 7, 1))
    assert march["monetary_value"] == "5000.00"
    assert july["monetary_value"] == "10000.00"


def test_points_per_rupee_is_inverted_once_here():
    """An inverted rate in one report is a liability wrong by the square of the
    factor, and it looks entirely plausible."""
    assert VALUATIONS[0].rupees_per_point == D("0.500000")
    assert VALUATIONS[1].rupees_per_point == D("1.000000")


def test_no_valuation_in_force_refuses_rather_than_guessing():
    with pytest.raises(valuation.ValuationError) as exc:
        valuation.rate_on(VALUATIONS, as_of=date(2025, 12, 1))
    assert "unquantified liability" in str(exc.value)


def test_a_zero_valuation_is_refused():
    """It would value every outstanding point at nothing and remove a real
    liability from the balance sheet."""
    with pytest.raises(valuation.ValuationError):
        valuation.PointsValuation(D("0"), date(2026, 1, 1))


def test_the_revaluation_preview_says_the_liability_does_not_move():
    impact = valuation.revaluation_impact(
        outstanding_points=10000,
        current_liability=D("5000"),
        new_points_per_rupee=D("1"),
    )
    assert impact["liability_after_change"] == "5000.00"
    assert impact["hypothetical_if_revalued"] == "10000.00"
    assert "each award keeps the rate it was made at" in impact["note"]


# ══════════════════════════════════════════════════════════════════════════════
# Budgets
# ══════════════════════════════════════════════════════════════════════════════


S = budgets.BudgetScope
E = budgets.EnforcementMode
P0, P1 = date(2026, 9, 1), date(2026, 9, 30)
KEYS = {S.MANAGER: "m1", S.DEPARTMENT: "eng", S.COMPANY: "acme"}


def test_every_applicable_scope_is_checked():
    """A manager inside their own budget but under an exhausted departmental
    cap is stopped by the department, and told which."""
    manager = budgets.Budget("b1", S.MANAGER, "m1", P0, P1, 1000, 0, E.HARD_STOP)
    department = budgets.Budget("b2", S.DEPARTMENT, "eng", P0, P1, 5000, 4950, E.HARD_STOP)
    decision = budgets.check(
        [manager, department], points=200, scope_keys=KEYS, as_of=TODAY
    )
    assert not decision.permitted
    assert decision.blocking_scope is S.DEPARTMENT
    assert "department budget for eng" in decision.message


def test_a_soft_warning_permits_and_records_the_overspend():
    """A warning nobody counts is a warning nobody acts on."""
    manager = budgets.Budget("b1", S.MANAGER, "m1", P0, P1, 1000, 900, E.SOFT_WARNING)
    decision = budgets.check([manager], points=200, scope_keys=KEYS, as_of=TODAY)
    assert decision.permitted
    assert decision.overspend_points == 100
    assert decision.warnings[0]["overspend"] == 100


def test_an_unconfigured_scope_does_not_constrain():
    """Unconfigured and zero have to be different states, or a tenant with
    manager budgets and no departmental ones finds everything blocked."""
    decision = budgets.check([], points=5000, scope_keys=KEYS, as_of=TODAY)
    assert decision.permitted
    assert "Unconfigured is not the same as zero" in decision.message


def test_another_managers_budget_is_irrelevant():
    other = budgets.Budget("b3", S.MANAGER, "m2", P0, P1, 10, 10, E.HARD_STOP)
    assert budgets.check([other], points=500, scope_keys=KEYS, as_of=TODAY).permitted


def test_a_budget_from_another_period_is_irrelevant():
    past = budgets.Budget(
        "b4", S.MANAGER, "m1", date(2026, 8, 1), date(2026, 8, 31), 10, 10, E.HARD_STOP
    )
    assert budgets.check([past], points=500, scope_keys=KEYS, as_of=TODAY).permitted


def test_a_zero_allocation_blocks_everything_and_is_a_real_setting():
    zero = budgets.Budget("b5", S.MANAGER, "m1", P0, P1, 0, 0, E.HARD_STOP)
    assert not budgets.check([zero], points=1, scope_keys=KEYS, as_of=TODAY).permitted


def test_a_negative_allocation_is_a_data_error():
    with pytest.raises(budgets.BudgetError):
        budgets.Budget("b6", S.MANAGER, "m1", P0, P1, -5)


def test_utilisation_reports_rupees_at_the_supplied_rate():
    manager = budgets.Budget("b1", S.MANAGER, "m1", P0, P1, 1000, 900, E.SOFT_WARNING)
    report = budgets.utilisation([manager], as_of=TODAY, rate_per_point=D("0.50"))
    assert report["budgets"][0]["consumed_rupees"] == "450.00"
    assert report["budgets"][0]["utilisation"] == 0.9


# ══════════════════════════════════════════════════════════════════════════════
# Tax treatment
# ══════════════════════════════════════════════════════════════════════════════


RULES = list(tax.DEFAULT_RULES)
REVIEWED = [
    tax.TaxTreatmentRule(
        reward_type="merchandise",
        treatment=tax.TaxTreatment.GIFT_RULE,
        exemption_threshold_per_year=D("5000"),
        effective_from=date(2020, 4, 1),
        reviewed=True,
        reviewed_by="Advisor LLP",
        reviewed_on=date(2026, 4, 1),
    )
]


def test_the_indian_financial_year_boundary_is_april():
    assert tax.financial_year(date(2026, 3, 31)) == "2025-26"
    assert tax.financial_year(date(2026, 4, 1)) == "2026-27"


def test_every_shipped_rule_is_unreviewed():
    """Shipping a confident number here creates a client liability, and they
    discover it in an assessment years later."""
    assert all(not r.reviewed for r in tax.DEFAULT_RULES)


def test_a_rule_marked_reviewed_has_to_name_the_reviewer():
    with pytest.raises(tax.TaxError):
        tax.TaxTreatmentRule(
            reward_type="x",
            treatment=tax.TaxTreatment.EXEMPT,
            exemption_threshold_per_year=D("0"),
            effective_from=date(2020, 4, 1),
            reviewed=True,
        )


def test_the_gift_rule_taxes_the_whole_value_once_crossed():
    """Not only the excess. Collapsing the two treatments into one would
    silently pick a behaviour for both, and the difference is money."""
    events = [
        tax.RewardEvent("e1", "merchandise", D("3000"), date(2026, 5, 1)),
        tax.RewardEvent("e1", "merchandise", D("2500"), date(2026, 8, 1)),
    ]
    result = tax.assess(
        events,
        employee_id="e1",
        reward_type="merchandise",
        fy="2026-27",
        rules=REVIEWED,
        as_of=TODAY,
    )
    assert result.cumulative_value == D("5500.00")
    assert result.taxable_value == D("5500.00")


def test_a_perquisite_threshold_taxes_only_the_excess():
    rules = [
        tax.TaxTreatmentRule(
            reward_type="merchandise",
            treatment=tax.TaxTreatment.PERQUISITE,
            exemption_threshold_per_year=D("5000"),
            effective_from=date(2020, 4, 1),
            reviewed=True,
            reviewed_by="Advisor LLP",
        )
    ]
    events = [tax.RewardEvent("e1", "merchandise", D("5500"), date(2026, 5, 1))]
    result = tax.assess(
        events,
        employee_id="e1",
        reward_type="merchandise",
        fy="2026-27",
        rules=rules,
        as_of=TODAY,
    )
    assert result.taxable_value == D("500.00")


def test_below_the_threshold_nothing_is_taxable_and_the_headroom_is_shown():
    events = [tax.RewardEvent("e1", "merchandise", D("3000"), date(2026, 5, 1))]
    result = tax.assess(
        events,
        employee_id="e1",
        reward_type="merchandise",
        fy="2026-27",
        rules=REVIEWED,
        as_of=TODAY,
    )
    assert result.taxable_value == D("0.00")
    assert "2000.00 would cross it" in result.note


def test_rewards_in_another_financial_year_do_not_count():
    events = [
        tax.RewardEvent("e1", "merchandise", D("4000"), date(2026, 3, 1)),
        tax.RewardEvent("e1", "merchandise", D("4000"), date(2026, 5, 1)),
    ]
    result = tax.assess(
        events,
        employee_id="e1",
        reward_type="merchandise",
        fy="2026-27",
        rules=REVIEWED,
        as_of=TODAY,
    )
    assert result.cumulative_value == D("4000.00")
    assert result.taxable_value == D("0.00")


def test_payroll_refuses_a_provisional_perquisite():
    """A perquisite added on an unreviewed reading is a deduction the employee
    queries and the client cannot defend."""
    events = [tax.RewardEvent("e1", "merchandise", D("9000"), date(2026, 5, 1))]
    with pytest.raises(tax.TaxError) as exc:
        tax.payroll_perquisites(
            events, employee_id="e1", fy="2026-27", rules=RULES, as_of=TODAY
        )
    assert "reviewed by a tax advisor" in str(exc.value)


def test_payroll_accepts_a_reviewed_rule():
    events = [tax.RewardEvent("e1", "merchandise", D("9000"), date(2026, 5, 1))]
    out = tax.payroll_perquisites(
        events, employee_id="e1", fy="2026-27", rules=REVIEWED, as_of=TODAY
    )
    assert out[0].taxable_value == D("9000.00")
    assert out[0].reviewed is True


def test_an_unmapped_reward_type_is_refused():
    """Treating an unmapped reward as exempt is the assumption that creates the
    liability."""
    with pytest.raises(tax.TaxError) as exc:
        tax.treatment_for([], reward_type="mystery", as_of=TODAY)
    assert "creates the liability" in str(exc.value)


def test_the_employee_statement_shows_taxable_and_non_taxable():
    """A recognition programme that produces an unexplained deduction has spent
    money to make somebody feel worse."""
    events = [
        tax.RewardEvent("e1", "merchandise", D("3000"), date(2026, 5, 1), description="Headphones"),
        tax.RewardEvent("e1", "cash_equivalent", D("1000"), date(2026, 6, 1)),
    ]
    statement = tax.reward_statement(
        events, employee_id="e1", fy="2026-27", rules=RULES, as_of=TODAY
    )
    assert statement["total_value"] == "4000.00"
    assert statement["any_provisional"] is True
    assert len(statement["rewards"]) == 2


def test_an_unreviewed_assessment_says_so_in_the_note():
    events = [tax.RewardEvent("e1", "merchandise", D("9000"), date(2026, 5, 1))]
    result = tax.assess(
        events,
        employee_id="e1",
        reward_type="merchandise",
        fy="2026-27",
        rules=RULES,
        as_of=TODAY,
    )
    assert result.note.startswith("PROVISIONAL")
    assert "Do not file on this figure" in result.note


# ══════════════════════════════════════════════════════════════════════════════
# Reconciliation
# ══════════════════════════════════════════════════════════════════════════════


def test_a_clean_month_reports_clean():
    result = reconciliation.reconcile(
        redemptions=[reconciliation.RedemptionRecord("r1", "e1", 1000, D("500"))],
        fulfilments=[reconciliation.FulfilmentRecord("r1", "o1")],
        invoices=[reconciliation.InvoiceLine("o1", D("500"))],
        period_label="2026-09",
    )
    assert result["clean"] is True
    assert result["gap_count"] == 0


def test_a_seeded_fulfilment_gap_is_detected():
    """The employee has paid in points and has nothing to show for it."""
    result = reconciliation.reconcile(
        redemptions=[reconciliation.RedemptionRecord("r1", "e1", 1000, D("500"))],
        fulfilments=[],
        invoices=[],
        period_label="2026-09",
    )
    gap = result["gaps"][0]
    assert gap["kind"] == "unfulfilled_redemption"
    assert gap["employee_id"] == "e1"


def test_a_pending_order_still_counts_as_unfulfilled():
    result = reconciliation.reconcile(
        redemptions=[reconciliation.RedemptionRecord("r1", "e1", 1000, D("500"))],
        fulfilments=[reconciliation.FulfilmentRecord("r1", "o1", status="pending")],
        invoices=[],
        period_label="2026-09",
    )
    assert result["gaps"][0]["kind"] == "unfulfilled_redemption"
    assert "'pending'" in result["gaps"][0]["detail"]


def test_an_orphan_fulfilment_is_a_billing_problem():
    result = reconciliation.reconcile(
        redemptions=[],
        fulfilments=[reconciliation.FulfilmentRecord("r9", "o9")],
        invoices=[],
        period_label="2026-09",
    )
    assert result["gaps"][0]["kind"] == "orphan_fulfilment"


def test_a_value_mismatch_is_reported_separately():
    result = reconciliation.reconcile(
        redemptions=[reconciliation.RedemptionRecord("r1", "e1", 1000, D("500"))],
        fulfilments=[reconciliation.FulfilmentRecord("r1", "o1")],
        invoices=[reconciliation.InvoiceLine("o1", D("560"))],
        period_label="2026-09",
    )
    assert result["gaps"][0]["kind"] == "value_mismatch"


def test_gaps_are_not_netted_against_each_other():
    """A fulfilment failure and a billing error of the same size would cancel
    to zero in a variance figure, and both are real."""
    result = reconciliation.reconcile(
        redemptions=[reconciliation.RedemptionRecord("r1", "e1", 1000, D("500"))],
        fulfilments=[reconciliation.FulfilmentRecord("r9", "o9")],
        invoices=[reconciliation.InvoiceLine("o9", D("500"))],
        period_label="2026-09",
    )
    kinds = set(result["gaps_by_kind"])
    assert "unfulfilled_redemption" in kinds
    assert "orphan_fulfilment" in kinds
    assert result["clean"] is False


def test_a_small_rounding_difference_is_within_tolerance():
    result = reconciliation.reconcile(
        redemptions=[reconciliation.RedemptionRecord("r1", "e1", 1000, D("500.00"))],
        fulfilments=[reconciliation.FulfilmentRecord("r1", "o1")],
        invoices=[reconciliation.InvoiceLine("o1", D("500.50"))],
        period_label="2026-09",
    )
    assert result["clean"] is True


def test_an_uninvoiced_fulfilment_lands_in_the_wrong_period():
    result = reconciliation.reconcile(
        redemptions=[reconciliation.RedemptionRecord("r1", "e1", 1000, D("500"))],
        fulfilments=[reconciliation.FulfilmentRecord("r1", "o1")],
        invoices=[],
        period_label="2026-09",
    )
    assert result["gaps"][0]["kind"] == "uninvoiced_fulfilment"
