"""Slice 07 gate: the tax computation engine.

The load-bearing test in this file is
`test_changing_configuration_changes_the_result_with_no_code_change`. If the
engine has a tax constant in it, that test fails — and everything else here is
measuring the wrong thing.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.tax.computation import (
    ProjectionInput,
    TaxRuleError,
    apply_section_ceiling,
    compare_regimes,
    compute_annual_tax,
    compute_hra_exemption,
    monthly_tds,
    months_remaining_in_year,
    project,
)


def D(value: str | int) -> Decimal:  # noqa: N802 - a Decimal alias, used everywhere below
    return Decimal(str(value))


def salary(**overrides) -> ProjectionInput:
    base = {
        "regime": "old",
        "gross_salary": D(1_200_000),
        "basic_plus_da": D(480_000),
        "hra_received": D(240_000),
    }
    base.update(overrides)
    return ProjectionInput(**base)


# ═══════════════════════════════════════════════════════════════════════════
# The headline gate: nothing is hardcoded
# ═══════════════════════════════════════════════════════════════════════════


def test_changing_configuration_changes_the_result_with_no_code_change(
    rules, synthetic_rules
) -> None:
    """The gate. Same inputs, different rule set, different answer.

    `synthetic_rules` uses values that resemble nothing in Indian tax law. An
    engine with a constant in it cannot produce the right answer against both.
    """
    inputs = salary(hra_received=D(0))

    realistic = project(inputs, rules)
    synthetic = project(inputs, synthetic_rules)

    assert realistic.annual_tax_liability != synthetic.annual_tax_liability

    # The synthetic set is checkable by hand: standard deduction 11,111, then
    # a flat 50% above 100,000, plus 10% cess.
    taxable = D(1_200_000) - D(11_111)
    expected_tax = (taxable - D(100_000)) * D(50) / D(100)
    expected_total = expected_tax + expected_tax * D(10) / D(100)

    assert synthetic.taxable_income == taxable
    assert synthetic.annual_tax_liability == expected_total.quantize(Decimal("0.01"))


def test_a_budget_change_to_a_section_ceiling_is_a_configuration_edit(rules) -> None:
    inputs = salary(declarations=(("80C", D(200_000), D(200_000)),), hra_received=D(0))

    before = project(inputs, rules)

    raised = {**rules, "section_limits": {**rules["section_limits"], "80C": {"ceiling": 200000}}}
    after = project(inputs, raised)

    assert before.taxable_income - after.taxable_income == D(50_000)


def test_a_missing_rule_fails_loudly_rather_than_defaulting(rules) -> None:
    """A silently assumed slab produces a plausible, wrong number."""
    broken = {**rules, "cess_percent": None}
    del broken["cess_percent"]

    with pytest.raises(TaxRuleError, match="cess_percent"):
        project(salary(hra_received=D(0)), broken)


def test_an_unknown_regime_names_what_is_missing(rules) -> None:
    with pytest.raises(TaxRuleError, match="regimes.composite"):
        project(salary(regime="composite"), rules)


def test_a_slab_table_with_no_open_top_is_rejected(rules) -> None:
    closed = {
        **rules,
        "regimes": {
            **rules["regimes"],
            "old": {
                **rules["regimes"]["old"],
                "slabs": [{"up_to": 250000, "rate": 0}, {"up_to": 500000, "rate": 5}],
            },
        },
    }
    with pytest.raises(TaxRuleError, match="open upper bound"):
        project(salary(hra_received=D(0)), closed)


# ═══════════════════════════════════════════════════════════════════════════
# HRA — least of three, with the binding constraint named
# ═══════════════════════════════════════════════════════════════════════════


def test_hra_computes_all_three_and_records_which_bound(rules) -> None:
    """The checklist gate.

    An employee told "your exemption is 84,000" with no explanation raises a
    ticket. The working has to say which limb bound.
    """
    working = compute_hra_exemption(
        hra_received=D(240_000),
        basic_plus_da=D(480_000),
        annual_rent=D(300_000),
        is_metro=True,
        rules=rules,
    )

    assert working.actual_hra_received == D(240_000)
    # 300,000 - 10% of 480,000 = 252,000
    assert working.rent_less_basic_percentage == D(252_000)
    # 50% of 480,000 = 240,000
    assert working.percentage_of_basic == D(240_000)

    assert working.exemption == D(240_000)
    assert working.binding_constraint == "actual_hra"
    assert "the HRA you actually received" in working.explain()


def test_hra_bound_by_rent_when_rent_is_low(rules) -> None:
    working = compute_hra_exemption(
        hra_received=D(240_000),
        basic_plus_da=D(480_000),
        annual_rent=D(120_000),
        is_metro=True,
        rules=rules,
    )
    # 120,000 - 48,000 = 72,000, the smallest of the three.
    assert working.exemption == D(72_000)
    assert working.binding_constraint == "rent_less_basic"
    assert "rent paid less" in working.explain()


def test_hra_bound_by_the_basic_percentage_in_a_non_metro(rules) -> None:
    working = compute_hra_exemption(
        hra_received=D(300_000),
        basic_plus_da=D(480_000),
        annual_rent=D(400_000),
        is_metro=False,
        rules=rules,
    )
    # 40% of 480,000 = 192,000 in a non-metro.
    assert working.exemption == D(192_000)
    assert working.binding_constraint == "percentage_of_basic"
    assert "non-metro" in working.explain()


def test_the_metro_percentage_comes_from_configuration(rules) -> None:
    doubled = {**rules, "hra": {**rules["hra"], "metro_percent": 100}}

    working = compute_hra_exemption(
        hra_received=D(500_000),
        basic_plus_da=D(480_000),
        annual_rent=D(600_000),
        is_metro=True,
        rules=doubled,
    )
    assert working.percentage_of_basic == D(480_000)


def test_paying_no_rent_gives_no_exemption(rules) -> None:
    working = compute_hra_exemption(
        hra_received=D(240_000),
        basic_plus_da=D(480_000),
        annual_rent=D(0),
        is_metro=True,
        rules=rules,
    )
    assert working.exemption == D(0)
    assert working.binding_constraint == "rent_less_basic"


def test_rent_below_ten_percent_of_basic_never_goes_negative(rules) -> None:
    working = compute_hra_exemption(
        hra_received=D(240_000),
        basic_plus_da=D(480_000),
        annual_rent=D(10_000),
        is_metro=True,
        rules=rules,
    )
    assert working.rent_less_basic_percentage == D(0)
    assert working.exemption >= D(0)


# ═══════════════════════════════════════════════════════════════════════════
# Section ceilings
# ═══════════════════════════════════════════════════════════════════════════


def test_a_section_is_capped_at_its_configured_ceiling(rules) -> None:
    allowed, ceiling = apply_section_ceiling("80C", D(200_000), rules)
    assert allowed == D(150_000)
    assert ceiling == D(150_000)


def test_an_amount_under_the_ceiling_passes_through(rules) -> None:
    allowed, _ = apply_section_ceiling("80C", D(90_000), rules)
    assert allowed == D(90_000)


def test_an_age_banded_ceiling_picks_the_right_band(rules) -> None:
    under, _ = apply_section_ceiling("80D", D(100_000), rules, age=40)
    over, _ = apply_section_ceiling("80D", D(100_000), rules, age=65)

    assert under == D(25_000)
    assert over == D(50_000)


def test_an_age_banded_ceiling_without_an_age_is_an_error(rules) -> None:
    with pytest.raises(TaxRuleError, match="no age was supplied"):
        apply_section_ceiling("80D", D(30_000), rules)


def test_a_section_with_no_configured_ceiling_is_uncapped(rules) -> None:
    """Uncapped is not the same as a ceiling of zero."""
    allowed, ceiling = apply_section_ceiling("80G", D(500_000), rules)
    assert allowed == D(500_000)
    assert ceiling is None


def test_several_lines_in_one_section_share_the_ceiling(rules) -> None:
    """Three 80C investments of 100,000 do not give a 300,000 deduction."""
    inputs = salary(
        hra_received=D(0),
        declarations=(
            ("80C", D(100_000), D(100_000)),
            ("80C", D(100_000), D(100_000)),
            ("80C", D(100_000), D(100_000)),
        ),
    )
    result = project(inputs, rules)

    line = next(line for line in result.deduction_lines if line.section == "80C")
    assert line.accepted == D(300_000)
    assert line.allowed == D(150_000)
    assert line.capped is True


# ═══════════════════════════════════════════════════════════════════════════
# Proof status flows into the computation
# ═══════════════════════════════════════════════════════════════════════════


def test_partial_acceptance_reduces_taxable_income_by_the_accepted_amount(rules) -> None:
    """The checklist gate: declared 150,000, proved 90,000.

    The deduction is 90,000 — not 150,000, and not zero.
    """
    inputs = salary(
        hra_received=D(0), declarations=(("80C", D(150_000), D(90_000)),)
    )
    result = project(inputs, rules)

    line = next(line for line in result.deduction_lines if line.section == "80C")
    assert line.declared == D(150_000)
    assert line.accepted == D(90_000)
    assert line.allowed == D(90_000)
    assert "proved and accepted" in line.note


def test_an_unproven_declaration_contributes_nothing(rules) -> None:
    """What produces the March spike, and it is correct."""
    declared_only = project(
        salary(hra_received=D(0), declarations=(("80C", D(150_000), D(0)),)), rules
    )
    proved = project(
        salary(hra_received=D(0), declarations=(("80C", D(150_000), D(150_000)),)), rules
    )

    assert declared_only.taxable_income > proved.taxable_income
    assert declared_only.taxable_income - proved.taxable_income == D(150_000)


def test_a_deduction_the_regime_disallows_is_shown_and_ignored(rules) -> None:
    """Shown rather than dropped: an employee needs to see why it did nothing."""
    result = project(
        salary(regime="new", hra_received=D(0), declarations=(("80C", D(150_000), D(150_000)),)),
        rules,
    )

    line = next(line for line in result.deduction_lines if line.section == "80C")
    assert line.allowed == D(0)
    assert "Not available under the new regime" in line.note


def test_hra_is_taxable_under_a_regime_that_disallows_it(rules) -> None:
    result = project(
        salary(regime="new", annual_rent=D(300_000), is_metro=True), rules
    )
    assert result.hra is None
    assert any("not available under the new regime" in n for n in result.notes)


# ═══════════════════════════════════════════════════════════════════════════
# Mid-year joiner
# ═══════════════════════════════════════════════════════════════════════════


def test_previous_employer_income_is_added_to_the_projection(rules) -> None:
    """Without this a mid-year joiner is under-deducted all year."""
    without = project(salary(hra_received=D(0)), rules)
    with_previous = project(
        salary(hra_received=D(0), previous_gross=D(600_000), previous_tds=D(40_000)),
        rules,
    )

    assert with_previous.gross_annual_income == D(1_800_000)
    assert with_previous.annual_tax_liability > without.annual_tax_liability
    assert any("previous employer" in n for n in with_previous.notes)


def test_previous_employer_tds_reduces_what_is_still_to_deduct() -> None:
    without = monthly_tds(
        annual_liability=D(120_000), already_deducted=D(0), months_remaining=6
    )
    with_previous = monthly_tds(
        annual_liability=D(120_000),
        already_deducted=D(0),
        months_remaining=6,
        previous_employer_tds=D(60_000),
    )

    assert without == D(20_000)
    assert with_previous == D(10_000)


def test_previous_employer_exempt_allowances_are_not_taxed_again(rules) -> None:
    result = project(
        salary(hra_received=D(0), previous_gross=D(600_000), previous_exempt=D(100_000)),
        rules,
    )
    assert result.total_exemptions == D(100_000)


# ═══════════════════════════════════════════════════════════════════════════
# TDS levelling
# ═══════════════════════════════════════════════════════════════════════════


def test_tds_spreads_the_residual_across_the_months_that_remain() -> None:
    """Not a flat twelfth. Half deducted by month six means the rest re-levels."""
    assert monthly_tds(
        annual_liability=D(120_000), already_deducted=D(60_000), months_remaining=6
    ) == D(10_000)


def test_a_late_proof_reduces_the_remaining_months() -> None:
    before = monthly_tds(
        annual_liability=D(120_000), already_deducted=D(90_000), months_remaining=3
    )
    after_proof = monthly_tds(
        annual_liability=D(90_000), already_deducted=D(90_000), months_remaining=3
    )

    assert before == D(10_000)
    assert after_proof == D(0), "an over-deduction settles at filing, not as negative TDS"


def test_tds_never_goes_negative() -> None:
    """A negative TDS line on a payslip is not a thing."""
    assert monthly_tds(
        annual_liability=D(50_000), already_deducted=D(80_000), months_remaining=2
    ) == D(0)


def test_zero_months_remaining_is_an_error() -> None:
    with pytest.raises(TaxRuleError, match="at least one"):
        monthly_tds(annual_liability=D(1000), already_deducted=D(0), months_remaining=0)


@pytest.mark.parametrize(
    "month,expected",
    [(4, 12), (5, 11), (12, 4), (1, 3), (3, 1)],
)
def test_months_remaining_counts_from_the_financial_year_start(
    month: int, expected: int
) -> None:
    assert months_remaining_in_year(month, fy_start_month=4) == expected


def test_the_financial_year_start_month_is_configurable() -> None:
    """A group company with a foreign parent may not run April to March."""
    assert months_remaining_in_year(1, fy_start_month=1) == 12
    assert months_remaining_in_year(12, fy_start_month=1) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Slabs, rebate, surcharge, cess
# ═══════════════════════════════════════════════════════════════════════════


def test_slabs_are_marginal_not_flat(rules) -> None:
    tax, _, _, _, breakdown = compute_annual_tax(
        taxable_income=D(600_000), regime="old", rules=rules
    )
    # 0 on the first 250,000; 5% on the next 250,000; 20% on the last 100,000.
    assert tax == D(32_500)
    assert len(breakdown) == 3


def test_the_slab_breakdown_shows_the_working(rules) -> None:
    _, _, _, _, breakdown = compute_annual_tax(
        taxable_income=D(600_000), regime="old", rules=rules
    )
    assert breakdown[0]["rate"] == "0"
    assert breakdown[-1]["to"] == "600000.00"


def test_the_rebate_applies_below_the_configured_income_limit(rules) -> None:
    _, rebate, _, _, _ = compute_annual_tax(
        taxable_income=D(450_000), regime="old", rules=rules
    )
    assert rebate > D(0)

    _, no_rebate, _, _, _ = compute_annual_tax(
        taxable_income=D(600_000), regime="old", rules=rules
    )
    assert no_rebate == D(0)


def test_the_rebate_never_exceeds_the_tax(rules) -> None:
    tax, rebate, _, _, _ = compute_annual_tax(
        taxable_income=D(260_000), regime="old", rules=rules
    )
    assert rebate <= tax


def test_surcharge_applies_in_the_configured_band(rules) -> None:
    _, _, low, _, _ = compute_annual_tax(
        taxable_income=D(1_000_000), regime="old", rules=rules
    )
    _, _, high, _, _ = compute_annual_tax(
        taxable_income=D(6_000_000), regime="old", rules=rules
    )

    assert low == D(0)
    assert high > D(0)


def test_a_rule_set_with_no_surcharge_bands_charges_none(rules) -> None:
    without = {k: v for k, v in rules.items() if k != "surcharge_bands"}
    _, _, surcharge, _, _ = compute_annual_tax(
        taxable_income=D(6_000_000), regime="old", rules=without
    )
    assert surcharge == D(0)


def test_cess_is_charged_on_tax_plus_surcharge(rules) -> None:
    tax, rebate, surcharge, cess, _ = compute_annual_tax(
        taxable_income=D(1_000_000), regime="old", rules=rules
    )
    expected = (tax - rebate + surcharge) * D(4) / D(100)
    assert cess == expected.quantize(Decimal("0.01"))


# ═══════════════════════════════════════════════════════════════════════════
# Regime comparison
# ═══════════════════════════════════════════════════════════════════════════


def test_the_comparison_projects_both_regimes_from_the_employees_own_numbers(rules) -> None:
    """Most employees cannot make this choice unaided.

    Showing both projections is cheaper than answering the March ticket.
    """
    comparison = compare_regimes(
        salary(
            annual_rent=D(300_000),
            is_metro=True,
            declarations=(("80C", D(150_000), D(150_000)),),
        ),
        rules,
    )

    assert set(comparison["regimes"]) == {"old", "new"}
    assert comparison["lower_liability_regime"] in {"old", "new"}
    assert Decimal(comparison["saving"]) >= D(0)


def test_the_comparison_is_worded_as_a_projection_not_advice(rules) -> None:
    """A projection is not tax advice, and should not read as it."""
    comparison = compare_regimes(salary(hra_received=D(0)), rules)
    assert "not tax advice" in comparison["note"]


def test_someone_with_large_deductions_is_shown_the_old_regime(rules) -> None:
    comparison = compare_regimes(
        salary(
            annual_rent=D(400_000),
            is_metro=True,
            declarations=(
                ("80C", D(150_000), D(150_000)),
                ("80CCD1B", D(50_000), D(50_000)),
                ("24B", D(200_000), D(200_000)),
            ),
        ),
        rules,
    )
    assert comparison["lower_liability_regime"] == "old"


def test_someone_with_no_deductions_is_shown_the_new_regime(rules) -> None:
    comparison = compare_regimes(salary(hra_received=D(0), annual_rent=D(0)), rules)
    assert comparison["lower_liability_regime"] == "new"


# ═══════════════════════════════════════════════════════════════════════════
# Determinism and edges
# ═══════════════════════════════════════════════════════════════════════════


def test_the_projection_is_deterministic(rules) -> None:
    inputs = salary(annual_rent=D(300_000), declarations=(("80C", D(150_000), D(90_000)),))
    assert project(inputs, rules).as_working() == project(inputs, rules).as_working()


def test_taxable_income_never_goes_negative(rules) -> None:
    result = project(
        ProjectionInput(
            regime="old",
            gross_salary=D(100_000),
            basic_plus_da=D(40_000),
            declarations=(("80C", D(150_000), D(150_000)),),
        ),
        rules,
    )
    assert result.taxable_income >= D(0)
    assert result.annual_tax_liability == D(0)


def test_the_standard_deduction_is_applied_automatically(rules) -> None:
    result = project(salary(hra_received=D(0)), rules)
    line = next(
        line for line in result.deduction_lines if line.section == "Standard deduction"
    )
    assert line.allowed == D(50_000)
    assert "Applied automatically" in line.note


def test_the_working_is_serialisable_and_carries_the_explanation(rules) -> None:
    """The stored snapshot has to answer "why is my TDS this"."""
    import json

    result = project(
        salary(
            annual_rent=D(300_000),
            is_metro=True,
            declarations=(("80C", D(150_000), D(90_000)),),
        ),
        rules,
    )
    working = result.as_working()

    json.dumps(working)  # must not raise
    assert working["hra"]["binding_constraint"]
    assert "limited by" in working["hra"]["explanation"]
    assert working["slabs"]
    assert any(d["section"] == "80C" for d in working["deductions"])
