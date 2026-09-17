"""Monthly TDS inside a payroll run.

The rule sets here are deliberately synthetic (a flat 10% slab, no cess): a
result that comes out right against them is reading configuration, not a
constant.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.payroll.tds import (
    MonthFigures,
    PriorPay,
    TdsContext,
    compute_monthly_tds,
    financial_year_for,
    months_remaining,
)
from app.services.tax.computation import ProjectionInput, TaxRuleError


def flat_rules(rate: int = 10) -> dict:
    return {
        "regimes": {
            "new": {
                "slabs": [{"up_to": None, "rate": rate}],
                "standard_deduction": 0,
                "deductions_allowed": [],
            }
        },
        "cess_percent": 0,
        "financial_year_start_month": 4,
    }


def context(
    *, rules: dict | None = None, months: int = 12, prior: PriorPay | None = None
) -> TdsContext:
    return TdsContext(
        rules=rules or flat_rules(),
        rule_set_id="rs-1",
        financial_year="2026-27",
        months_remaining=months,
        base=ProjectionInput(regime="new", gross_salary=Decimal(0), basic_plus_da=Decimal(0)),
        prior=prior or PriorPay(),
    )


def figures(monthly: str = "100000", one_off: str = "0") -> MonthFigures:
    amount = Decimal(monthly)
    return MonthFigures(
        taxable_this_month=amount,
        taxable_full_month=amount,
        basic_plus_da_this_month=Decimal(0),
        basic_plus_da_full_month=Decimal(0),
        hra_this_month=Decimal(0),
        hra_full_month=Decimal(0),
        employer_nps_this_month=Decimal(0),
        employer_nps_full_month=Decimal(0),
        professional_tax=Decimal(0),
        one_off_taxable=Decimal(one_off),
    )


def test_april_spreads_the_projected_liability_over_twelve_months() -> None:
    result = compute_monthly_tds(context(), figures())
    # 1,200,000 projected at 10% is 120,000 over 12 months.
    assert result.amount == Decimal("10000.00")
    assert result.annual_liability == Decimal("120000.00")
    assert result.working["months_remaining"] == 12
    assert result.working["computation"]["slabs"]


def test_a_one_off_payment_is_taxed_in_full_in_the_month_it_is_paid() -> None:
    result = compute_monthly_tds(context(), figures(one_off="50000"))
    assert result.recurring_amount == Decimal("10000.00")
    assert result.one_off_amount == Decimal("5000.00")
    assert result.amount == Decimal("15000.00")


def test_tds_re_levels_against_what_was_already_deducted() -> None:
    """January, nine months paid, but only 60,000 deducted: the shortfall is
    spread over the three months that remain rather than carried to March."""
    prior = PriorPay(recurring_taxable=Decimal("900000"), tds_deducted=Decimal("60000"))
    result = compute_monthly_tds(context(months=3, prior=prior), figures())
    assert result.amount == Decimal("20000.00")


def test_an_over_deduction_gives_zero_not_a_refund() -> None:
    prior = PriorPay(recurring_taxable=Decimal("900000"), tds_deducted=Decimal("130000"))
    result = compute_monthly_tds(context(months=3, prior=prior), figures())
    assert result.amount == Decimal("0.00")


def test_the_rate_is_configuration() -> None:
    assert compute_monthly_tds(context(rules=flat_rules(20)), figures()).amount == Decimal(
        "20000.00"
    )


def test_a_rule_set_missing_a_value_refuses_rather_than_assumes() -> None:
    rules = flat_rules()
    del rules["cess_percent"]
    with pytest.raises(TaxRuleError, match="cess_percent"):
        compute_monthly_tds(context(rules=rules), figures())


def test_float_pay_is_refused() -> None:
    bad = MonthFigures(
        taxable_this_month=100000.0,  # type: ignore[arg-type]
        taxable_full_month=Decimal(0),
        basic_plus_da_this_month=Decimal(0),
        basic_plus_da_full_month=Decimal(0),
        hra_this_month=Decimal(0),
        hra_full_month=Decimal(0),
        employer_nps_this_month=Decimal(0),
        employer_nps_full_month=Decimal(0),
        professional_tax=Decimal(0),
    )
    with pytest.raises(TaxRuleError, match="float"):
        compute_monthly_tds(context(), bad)


def test_financial_year_and_months_remaining_follow_the_configured_start() -> None:
    assert financial_year_for(date(2027, 1, 31), 4) == (
        "2026-27",
        date(2026, 4, 1),
        date(2027, 3, 31),
    )
    assert financial_year_for(date(2026, 4, 30), 4)[0] == "2026-27"
    assert months_remaining(4, flat_rules()) == 12
    assert months_remaining(3, flat_rules()) == 1
    january_year = {**flat_rules(), "financial_year_start_month": 1}
    assert months_remaining(3, january_year) == 10
