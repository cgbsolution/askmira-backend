"""Slice 08 gate: gratuity computation.

The headline test is `test_the_four_year_240_day_boundary_behaves_per_configuration`,
which runs the *same* service period under both readings and asserts they give
different, correct answers. If the engine took a position, one of the two would
fail.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.models.exit_benefits import ExitReason, GratuityConfig, ServiceRounding
from app.services.exit_benefits.gratuity import (
    GratuityError,
    compute_gratuity,
    compute_service,
)


def D(value: str | int) -> Decimal:  # noqa: N802 - a Decimal alias, used throughout
    return Decimal(str(value))


def config(**overrides) -> GratuityConfig:
    """A tenant configuration.

    These values resemble the common Indian setup. They are fixtures — the
    engine reads them and knows none of them.
    """
    base = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "eligibility_years": 5,
        "eligibility_days_in_final_year": None,
        "formula_basis": ["BASIC", "DA"],
        "days_per_year_divisor": 26,
        "days_per_year": Decimal(15),
        "max_exempt_amount": Decimal(2_000_000),
        "max_payable_amount": None,
        "service_rounding": ServiceRounding.NEAREST_YEAR_SIX_MONTH_RULE,
        "unpaid_leave_break_threshold_days": None,
        "include_notice_period_in_service": True,
        "eligibility_waived_reasons": ["death", "disablement"],
    }
    base.update(overrides)
    return GratuityConfig(**base)


JOINED = date(2020, 4, 1)


# ═══════════════════════════════════════════════════════════════════════════
# The contested boundary — the headline gate
# ═══════════════════════════════════════════════════════════════════════════


def test_the_four_year_240_day_boundary_behaves_per_configuration() -> None:
    """The checklist gate: the interpretation is configurable, not baked in.

    Same employee, same service, two tenant configurations, two different and
    correct answers. An engine that took a position would fail one of these.
    """
    # Four years and roughly 250 days.
    exit_date = date(2024, 12, 7)

    strict = compute_gratuity(
        joined_on=JOINED,
        exit_date=exit_date,
        exit_reason=ExitReason.RESIGNATION,
        monthly_wages=D(50_000),
        config=config(eligibility_days_in_final_year=None),
    )
    permissive = compute_gratuity(
        joined_on=JOINED,
        exit_date=exit_date,
        exit_reason=ExitReason.RESIGNATION,
        monthly_wages=D(50_000),
        config=config(eligibility_days_in_final_year=240),
    )

    assert strict.is_eligible is False
    assert permissive.is_eligible is True
    assert permissive.service.final_year_rule_applied is True
    assert permissive.gross_amount > D(0)


def test_the_strict_reading_explains_that_the_day_rule_is_off() -> None:
    """An HR user seeing "not eligible" needs to know the setting exists."""
    result = compute_gratuity(
        joined_on=JOINED,
        exit_date=date(2024, 12, 7),
        exit_reason=ExitReason.RESIGNATION,
        monthly_wages=D(50_000),
        config=config(eligibility_days_in_final_year=None),
    )
    assert "final-year day rule is switched off" in result.ineligibility_reason


def test_the_permissive_reading_records_that_it_is_a_tenant_setting() -> None:
    """The working must not read as though the Act says this."""
    result = compute_gratuity(
        joined_on=JOINED,
        exit_date=date(2024, 12, 7),
        exit_reason=ExitReason.RESIGNATION,
        monthly_wages=D(50_000),
        config=config(eligibility_days_in_final_year=240),
    )
    note = " ".join(result.service.notes)
    assert "tenant setting" in note
    assert "not a fixed reading of the Act" in note


def test_just_below_the_day_rule_is_still_ineligible() -> None:
    """Four years and 200 days does not meet a 240-day rule."""
    result = compute_gratuity(
        joined_on=JOINED,
        exit_date=date(2024, 10, 18),
        exit_reason=ExitReason.RESIGNATION,
        monthly_wages=D(50_000),
        config=config(eligibility_days_in_final_year=240),
    )
    assert result.is_eligible is False


def test_the_day_count_itself_is_configurable() -> None:
    """A tenant whose counsel reads it as 190 days configures 190."""
    exit_date = date(2024, 10, 18)  # ~200 days into the fifth year

    at_240 = compute_gratuity(
        joined_on=JOINED, exit_date=exit_date, exit_reason=ExitReason.RESIGNATION,
        monthly_wages=D(50_000), config=config(eligibility_days_in_final_year=240),
    )
    at_190 = compute_gratuity(
        joined_on=JOINED, exit_date=exit_date, exit_reason=ExitReason.RESIGNATION,
        monthly_wages=D(50_000), config=config(eligibility_days_in_final_year=190),
    )

    assert at_240.is_eligible is False
    assert at_190.is_eligible is True


# ═══════════════════════════════════════════════════════════════════════════
# Service rounding
# ═══════════════════════════════════════════════════════════════════════════


def test_exactly_six_months_rounds_down() -> None:
    """The conventional rule is *more than* six months, not six or more."""
    service = compute_service(
        joined_on=date(2018, 1, 1), as_of=date(2024, 7, 1), config=config()
    )
    assert service.rounded_years == D(6)
    assert "rounded down" in service.rounding_applied


def test_six_months_and_a_day_rounds_up() -> None:
    service = compute_service(
        joined_on=date(2018, 1, 1), as_of=date(2024, 7, 15), config=config()
    )
    assert service.rounded_years == D(7)
    assert "rounded up" in service.rounding_applied


@pytest.mark.parametrize(
    "mode,expected",
    [
        (ServiceRounding.ALWAYS_ROUND_DOWN, D(6)),
        (ServiceRounding.ALWAYS_ROUND_UP, D(7)),
        (ServiceRounding.NEAREST_YEAR_SIX_MONTH_RULE, D(7)),
    ],
)
def test_each_rounding_mode_applies(mode: ServiceRounding, expected: Decimal) -> None:
    service = compute_service(
        joined_on=date(2018, 1, 1),
        as_of=date(2024, 10, 1),  # 6 years 9 months
        config=config(service_rounding=mode),
    )
    assert service.rounded_years == expected


def test_exact_fraction_mode_keeps_the_part_year() -> None:
    """Some contractual schemes pay pro rata rather than in whole years."""
    service = compute_service(
        joined_on=date(2018, 1, 1),
        as_of=date(2024, 7, 1),
        config=config(service_rounding=ServiceRounding.EXACT_FRACTION),
    )
    assert service.rounded_years != service.rounded_years.to_integral_value()


def test_an_exit_before_joining_is_an_error() -> None:
    with pytest.raises(GratuityError, match="before the joining date"):
        compute_service(joined_on=date(2024, 1, 1), as_of=date(2023, 1, 1), config=config())


# ═══════════════════════════════════════════════════════════════════════════
# Death and disablement
# ═══════════════════════════════════════════════════════════════════════════


def test_death_waives_the_eligibility_period() -> None:
    """The checklist gate, and the case where getting it wrong is worst."""
    result = compute_gratuity(
        joined_on=date(2023, 4, 1),
        exit_date=date(2024, 6, 1),  # barely over a year
        exit_reason=ExitReason.DEATH,
        monthly_wages=D(50_000),
        config=config(),
    )

    assert result.is_eligible is True
    assert result.eligibility_waived is True
    assert result.gross_amount > D(0)


def test_disablement_waives_the_eligibility_period() -> None:
    result = compute_gratuity(
        joined_on=date(2023, 4, 1),
        exit_date=date(2024, 6, 1),
        exit_reason=ExitReason.DISABLEMENT,
        monthly_wages=D(50_000),
        config=config(),
    )
    assert result.eligibility_waived is True


def test_which_reasons_waive_is_configurable() -> None:
    """A tenant may extend the waiver; it should not require a release."""
    result = compute_gratuity(
        joined_on=date(2023, 4, 1),
        exit_date=date(2024, 6, 1),
        exit_reason=ExitReason.RETIREMENT,
        monthly_wages=D(50_000),
        config=config(eligibility_waived_reasons=["death", "disablement", "retirement"]),
    )
    assert result.eligibility_waived is True


def test_a_resignation_short_of_the_period_is_not_waived() -> None:
    result = compute_gratuity(
        joined_on=date(2023, 4, 1),
        exit_date=date(2024, 6, 1),
        exit_reason=ExitReason.RESIGNATION,
        monthly_wages=D(50_000),
        config=config(),
    )
    assert result.is_eligible is False
    assert result.gross_amount == D(0)


# ═══════════════════════════════════════════════════════════════════════════
# The formula and its configuration
# ═══════════════════════════════════════════════════════════════════════════


def test_the_formula_multiplies_wages_days_and_years() -> None:
    """(50,000 / 26) x 15 x 10 = 288,461.54"""
    result = compute_gratuity(
        joined_on=date(2014, 4, 1),
        exit_date=date(2024, 4, 1),
        exit_reason=ExitReason.RESIGNATION,
        monthly_wages=D(50_000),
        config=config(),
    )
    expected = (D(50_000) / D(26) * D(15) * D(10)).quantize(Decimal("0.01"))
    assert result.gross_amount == expected


def test_changing_the_divisor_changes_the_result(  ) -> None:
    """Not every establishment uses 26."""
    at_26 = compute_gratuity(
        joined_on=date(2014, 4, 1), exit_date=date(2024, 4, 1),
        exit_reason=ExitReason.RESIGNATION, monthly_wages=D(50_000),
        config=config(days_per_year_divisor=26),
    )
    at_30 = compute_gratuity(
        joined_on=date(2014, 4, 1), exit_date=date(2024, 4, 1),
        exit_reason=ExitReason.RESIGNATION, monthly_wages=D(50_000),
        config=config(days_per_year_divisor=30),
    )
    assert at_26.gross_amount > at_30.gross_amount


def test_changing_the_days_per_year_changes_the_result() -> None:
    fifteen = compute_gratuity(
        joined_on=date(2014, 4, 1), exit_date=date(2024, 4, 1),
        exit_reason=ExitReason.RESIGNATION, monthly_wages=D(50_000),
        config=config(days_per_year=Decimal(15)),
    )
    seven = compute_gratuity(
        joined_on=date(2014, 4, 1), exit_date=date(2024, 4, 1),
        exit_reason=ExitReason.RESIGNATION, monthly_wages=D(50_000),
        config=config(days_per_year=Decimal(7)),
    )
    assert fifteen.gross_amount > seven.gross_amount


def test_an_unmapped_wage_basis_is_refused() -> None:
    """The SME signs off which components are wages before anything computes."""
    with pytest.raises(GratuityError, match="Compliance SME maps"):
        compute_gratuity(
            joined_on=date(2014, 4, 1), exit_date=date(2024, 4, 1),
            exit_reason=ExitReason.RESIGNATION, monthly_wages=D(50_000),
            config=config(formula_basis=[]),
        )


def test_a_zero_divisor_is_refused() -> None:
    with pytest.raises(GratuityError, match="greater than zero"):
        compute_gratuity(
            joined_on=date(2014, 4, 1), exit_date=date(2024, 4, 1),
            exit_reason=ExitReason.RESIGNATION, monthly_wages=D(50_000),
            config=config(days_per_year_divisor=0),
        )


def test_a_float_wage_is_refused() -> None:
    with pytest.raises(GratuityError, match="must be a Decimal"):
        compute_gratuity(
            joined_on=date(2014, 4, 1), exit_date=date(2024, 4, 1),
            exit_reason=ExitReason.RESIGNATION,
            monthly_wages=50000.0,  # type: ignore[arg-type]
            config=config(),
        )


def test_a_contractual_ceiling_caps_the_payout() -> None:
    result = compute_gratuity(
        joined_on=date(2000, 4, 1), exit_date=date(2024, 4, 1),
        exit_reason=ExitReason.RETIREMENT, monthly_wages=D(500_000),
        config=config(max_payable_amount=Decimal(1_000_000)),
    )
    assert result.gross_amount == D(1_000_000)
    assert result.capped_by_contract is True


# ═══════════════════════════════════════════════════════════════════════════
# Tax — the excess must reach TDS
# ═══════════════════════════════════════════════════════════════════════════


def test_an_amount_below_the_ceiling_is_wholly_exempt() -> None:
    result = compute_gratuity(
        joined_on=date(2014, 4, 1), exit_date=date(2024, 4, 1),
        exit_reason=ExitReason.RESIGNATION, monthly_wages=D(50_000),
        config=config(max_exempt_amount=Decimal(2_000_000)),
    )
    assert result.exempt_amount == result.gross_amount
    assert result.taxable_amount == D(0)


def test_the_excess_above_the_ceiling_is_taxable() -> None:
    """The checklist gate: the excess must provably reach TDS."""
    result = compute_gratuity(
        joined_on=date(2000, 4, 1), exit_date=date(2024, 4, 1),
        exit_reason=ExitReason.RETIREMENT, monthly_wages=D(500_000),
        config=config(max_exempt_amount=Decimal(2_000_000)),
    )

    assert result.gross_amount > D(2_000_000)
    assert result.exempt_amount == D(2_000_000)
    assert result.taxable_amount == result.gross_amount - D(2_000_000)
    assert result.taxable_amount > D(0)


def test_the_exemption_ceiling_is_configuration_not_a_constant() -> None:
    """It has been raised repeatedly and will be again."""
    low = compute_gratuity(
        joined_on=date(2000, 4, 1), exit_date=date(2024, 4, 1),
        exit_reason=ExitReason.RETIREMENT, monthly_wages=D(500_000),
        config=config(max_exempt_amount=Decimal(1_000_000)),
    )
    high = compute_gratuity(
        joined_on=date(2000, 4, 1), exit_date=date(2024, 4, 1),
        exit_reason=ExitReason.RETIREMENT, monthly_wages=D(500_000),
        config=config(max_exempt_amount=Decimal(3_000_000)),
    )
    assert low.taxable_amount > high.taxable_amount


# ═══════════════════════════════════════════════════════════════════════════
# Unpaid leave and notice period
# ═══════════════════════════════════════════════════════════════════════════


def test_unpaid_leave_beyond_the_threshold_does_not_count() -> None:
    with_break = compute_service(
        joined_on=date(2018, 1, 1), as_of=date(2024, 1, 1),
        config=config(unpaid_leave_break_threshold_days=90),
        unpaid_leave_days=200,
    )
    assert with_break.excluded_days == 200
    assert with_break.countable_days < with_break.total_days
    assert any("exceed the 90-day threshold" in n for n in with_break.notes)


def test_unpaid_leave_within_the_threshold_still_counts() -> None:
    service = compute_service(
        joined_on=date(2018, 1, 1), as_of=date(2024, 1, 1),
        config=config(unpaid_leave_break_threshold_days=90),
        unpaid_leave_days=30,
    )
    assert service.excluded_days == 0
    assert any("within the threshold" in n for n in service.notes)


def test_notice_period_exclusion_is_configurable() -> None:
    included = compute_service(
        joined_on=date(2018, 1, 1), as_of=date(2024, 1, 1),
        config=config(include_notice_period_in_service=True), notice_period_days=90,
    )
    excluded = compute_service(
        joined_on=date(2018, 1, 1), as_of=date(2024, 1, 1),
        config=config(include_notice_period_in_service=False), notice_period_days=90,
    )
    assert included.countable_days == excluded.countable_days + 90


# ═══════════════════════════════════════════════════════════════════════════
# Working
# ═══════════════════════════════════════════════════════════════════════════


def test_the_working_is_serialisable_and_explains_the_outcome() -> None:
    import json

    result = compute_gratuity(
        joined_on=JOINED, exit_date=date(2024, 12, 7),
        exit_reason=ExitReason.RESIGNATION, monthly_wages=D(50_000),
        config=config(eligibility_days_in_final_year=240),
    )
    working = result.as_working()

    json.dumps(working)
    assert working["service"]["final_year_rule_applied"] is True
    assert working["service"]["rounding_applied"]
    assert working["service"]["notes"]


def test_computation_is_deterministic() -> None:
    """Required for the accrual job to be idempotent."""
    args = {
        "joined_on": JOINED,
        "exit_date": date(2024, 12, 7),
        "exit_reason": ExitReason.RESIGNATION,
        "monthly_wages": D(50_000),
        "config": config(eligibility_days_in_final_year=240),
    }
    assert compute_gratuity(**args).as_working() == compute_gratuity(**args).as_working()
