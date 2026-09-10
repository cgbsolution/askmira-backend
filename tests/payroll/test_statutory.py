"""Slice 09 gates: LWF, statutory bonus, ESI contribution periods, PT, VPF.

Every configuration in this file is a *shape*, not a claim about what any
state's real values are. The numbers are chosen to make the arithmetic easy to
check by eye (a 200-rupee levy, a 10% rate) precisely so that nobody mistakes a
test fixture for an SME-approved rate.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.payroll.statutory import (
    PfElection,
    StatutoryError,
    compute_bonus,
    compute_lwf,
    compute_vpf,
    esi_contribution_period,
    resolve_pf_base,
    resolve_professional_tax_state,
    should_continue_esi,
)


def D(value: str | int) -> Decimal:  # noqa: N802 - a Decimal alias, used throughout
    return Decimal(str(value))


# ═══════════════════════════════════════════════════════════════════════════
# Labour Welfare Fund
# ═══════════════════════════════════════════════════════════════════════════

# A state that collects a flat amount once a year, in December.
ANNUAL_FLAT = {
    "applicable": True,
    "deduction_months": [12],
    "employee_amount": 20,
    "employer_amount": 40,
}

# A state that collects twice a year, in June and December.
HALF_YEARLY_FLAT = {
    "applicable": True,
    "deduction_months": [6, 12],
    "employee_amount": 12,
    "employer_amount": 36,
}

# A state that collects monthly as a percentage of wages.
MONTHLY_PERCENT = {
    "applicable": True,
    "deduction_months": list(range(1, 13)),
    "employee_percent": "0.2",
    "employer_percent": "0.4",
}


def test_a_state_that_does_not_levy_lwf_deducts_nothing() -> None:
    result = compute_lwf(
        config={"applicable": False}, month=12, monthly_wages=D(50_000)
    )
    assert result.employee_contribution == D(0)
    assert result.employer_contribution == D(0)
    assert "does not levy" in result.reason


def test_a_non_deduction_month_is_exactly_zero_not_a_proration() -> None:
    """The checklist gate.

    An annual 20-rupee levy prorated across twelve months would be 1.67 a
    month. It is not. In eleven months of the year the answer is zero.
    """
    for month in range(1, 12):
        result = compute_lwf(config=ANNUAL_FLAT, month=month, monthly_wages=D(50_000))
        assert result.employee_contribution == D(0), f"month {month}"
        assert result.employer_contribution == D(0), f"month {month}"
        assert result.is_deduction_month is False

    december = compute_lwf(config=ANNUAL_FLAT, month=12, monthly_wages=D(50_000))
    assert december.employee_contribution == D(20)
    assert december.employer_contribution == D(40)


def test_a_half_yearly_state_deducts_in_exactly_its_two_months() -> None:
    deducted = [
        month
        for month in range(1, 13)
        if compute_lwf(
            config=HALF_YEARLY_FLAT, month=month, monthly_wages=D(50_000)
        ).employee_contribution
        > 0
    ]
    assert deducted == [6, 12]


def test_flat_and_percentage_states_run_through_the_same_code_path() -> None:
    """The checklist gate: no branching on frequency or amount style.

    Both shapes are one call with different data. If this ever needed an
    `if state ==` it would be the start of a per-state ladder.
    """
    flat = compute_lwf(config=ANNUAL_FLAT, month=12, monthly_wages=D(10_000))
    percent = compute_lwf(config=MONTHLY_PERCENT, month=12, monthly_wages=D(10_000))

    assert flat.employee_contribution == D(20)
    assert percent.employee_contribution == D(20), "0.2% of 10,000"
    assert percent.employer_contribution == D(40)


def test_monthly_and_annual_states_differ_only_in_their_deduction_months() -> None:
    monthly_hits = sum(
        1
        for month in range(1, 13)
        if compute_lwf(
            config=MONTHLY_PERCENT, month=month, monthly_wages=D(10_000)
        ).is_deduction_month
    )
    annual_hits = sum(
        1
        for month in range(1, 13)
        if compute_lwf(
            config=ANNUAL_FLAT, month=month, monthly_wages=D(10_000)
        ).is_deduction_month
    )
    assert monthly_hits == 12
    assert annual_hits == 1


def test_wages_above_the_ceiling_are_exempt() -> None:
    config = {**ANNUAL_FLAT, "wage_ceiling": 25_000}
    below = compute_lwf(config=config, month=12, monthly_wages=D(20_000))
    above = compute_lwf(config=config, month=12, monthly_wages=D(30_000))

    assert below.employee_contribution == D(20)
    assert above.employee_contribution == D(0)
    assert "above the LWF ceiling" in above.reason


def test_an_exempt_designation_is_exempt() -> None:
    config = {**ANNUAL_FLAT, "exempt_designations": ["Managing Director"]}
    result = compute_lwf(
        config=config, month=12, monthly_wages=D(10_000), designation="managing director"
    )
    assert result.employee_contribution == D(0)
    assert "exempt" in result.reason


def test_wage_banded_states_pick_the_matching_band() -> None:
    config = {
        "applicable": True,
        "deduction_months": [6, 12],
        "wage_bands": [
            {"up_to": 3_000, "label": "up to 3000", "employee_amount": 6, "employer_amount": 18},
            {"up_to": None, "label": "above 3000", "employee_amount": 12, "employer_amount": 36},
        ],
    }
    low = compute_lwf(config=config, month=6, monthly_wages=D(2_500))
    high = compute_lwf(config=config, month=6, monthly_wages=D(9_000))

    assert low.employee_contribution == D(6)
    assert low.wage_band == "up to 3000"
    assert high.employee_contribution == D(12)
    assert high.wage_band == "above 3000"


def test_an_applicable_config_with_no_deduction_months_is_refused() -> None:
    with pytest.raises(StatutoryError, match="names no deduction months"):
        compute_lwf(
            config={"applicable": True, "employee_amount": 20},
            month=12,
            monthly_wages=D(10_000),
        )


def test_a_config_with_neither_amounts_nor_percentages_is_refused() -> None:
    with pytest.raises(StatutoryError, match="neither flat amounts"):
        compute_lwf(
            config={"applicable": True, "deduction_months": [12]},
            month=12,
            monthly_wages=D(10_000),
        )


def test_a_closed_last_band_is_refused_rather_than_silently_exempting() -> None:
    config = {
        "applicable": True,
        "deduction_months": [12],
        "wage_bands": [{"up_to": 3_000, "employee_amount": 6}],
    }
    with pytest.raises(StatutoryError, match="open upper bound"):
        compute_lwf(config=config, month=12, monthly_wages=D(50_000))


def test_lwf_refuses_a_float_wage() -> None:
    with pytest.raises(StatutoryError, match="must be a Decimal"):
        compute_lwf(config=ANNUAL_FLAT, month=12, monthly_wages=50_000.0)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════
# Statutory bonus
# ═══════════════════════════════════════════════════════════════════════════

BONUS_CONFIG = {
    "eligibility_wage_ceiling": 21_000,
    "calculation_wage_ceiling": 7_000,
    "minimum_percentage": "8.33",
    "maximum_percentage": "20",
    "minimum_service_days": 30,
}


def test_wages_above_the_eligibility_ceiling_are_ineligible() -> None:
    result = compute_bonus(
        config=BONUS_CONFIG,
        monthly_wages=D(25_000),
        days_worked=D(240),
        total_working_days=D(240),
    )
    assert result.is_eligible is False
    assert result.amount == D(0)
    assert "eligibility ceiling" in (result.ineligibility_reason or "")


def test_too_few_days_worked_is_ineligible() -> None:
    result = compute_bonus(
        config=BONUS_CONFIG,
        monthly_wages=D(15_000),
        days_worked=D(20),
        total_working_days=D(240),
    )
    assert result.is_eligible is False
    assert "below the 30 days required" in (result.ineligibility_reason or "")


def test_bonus_is_computed_on_the_lower_of_wage_and_calculation_ceiling() -> None:
    """The two ceilings are separate, and conflating them is the common error.

    Someone on 15,000 is *eligible* (below 21,000) but their bonus is computed
    on 7,000 (the calculation ceiling), not on 15,000.
    """
    result = compute_bonus(
        config=BONUS_CONFIG,
        monthly_wages=D(15_000),
        days_worked=D(240),
        total_working_days=D(240),
    )
    assert result.is_eligible is True
    assert result.eligible_wage == D(7_000)
    # 7,000 x 12 x 8.33% = 6,997.20
    assert result.amount == Decimal("6997.20")
    assert any("calculation ceiling" in note for note in result.notes)


def test_a_wage_below_both_ceilings_is_computed_on_the_actual_wage() -> None:
    result = compute_bonus(
        config=BONUS_CONFIG,
        monthly_wages=D(5_000),
        days_worked=D(240),
        total_working_days=D(240),
    )
    assert result.eligible_wage == D(5_000)
    assert result.notes == []


def test_loss_of_pay_reduces_the_bonus_proportionally() -> None:
    """The checklist gate: days worked comes from attendance and matters."""
    full = compute_bonus(
        config=BONUS_CONFIG,
        monthly_wages=D(7_000),
        days_worked=D(240),
        total_working_days=D(240),
    )
    half = compute_bonus(
        config=BONUS_CONFIG,
        monthly_wages=D(7_000),
        days_worked=D(120),
        total_working_days=D(240),
    )
    assert half.amount == (full.amount / 2).quantize(Decimal("0.01"))
    assert any("Prorated" in note for note in half.notes)


def test_a_declared_percentage_above_the_minimum_is_honoured() -> None:
    minimum = compute_bonus(
        config=BONUS_CONFIG,
        monthly_wages=D(7_000),
        days_worked=D(240),
        total_working_days=D(240),
    )
    declared = compute_bonus(
        config=BONUS_CONFIG,
        monthly_wages=D(7_000),
        days_worked=D(240),
        total_working_days=D(240),
        percentage=D(20),
    )
    assert declared.amount > minimum.amount
    assert declared.percentage == D(20)


def test_a_percentage_below_the_statutory_minimum_is_refused() -> None:
    with pytest.raises(StatutoryError, match="below the statutory minimum"):
        compute_bonus(
            config=BONUS_CONFIG,
            monthly_wages=D(7_000),
            days_worked=D(240),
            total_working_days=D(240),
            percentage=D(5),
        )


def test_a_percentage_above_the_statutory_maximum_is_refused() -> None:
    with pytest.raises(StatutoryError, match="above the statutory maximum"):
        compute_bonus(
            config=BONUS_CONFIG,
            monthly_wages=D(7_000),
            days_worked=D(240),
            total_working_days=D(240),
            percentage=D(30),
        )


@pytest.mark.parametrize(
    "missing",
    [
        "eligibility_wage_ceiling",
        "calculation_wage_ceiling",
        "minimum_percentage",
        "maximum_percentage",
        "minimum_service_days",
    ],
)
def test_every_bonus_value_must_be_supplied_by_the_sme(missing: str) -> None:
    """No statutory value has a default in code, and the error names it."""
    config = {key: value for key, value in BONUS_CONFIG.items() if key != missing}
    with pytest.raises(StatutoryError, match=missing):
        compute_bonus(
            config=config,
            monthly_wages=D(7_000),
            days_worked=D(240),
            total_working_days=D(240),
        )


def test_bonus_refuses_a_float() -> None:
    with pytest.raises(StatutoryError, match="must be Decimals"):
        compute_bonus(
            config=BONUS_CONFIG,
            monthly_wages=7000.0,  # type: ignore[arg-type]
            days_worked=D(240),
            total_working_days=D(240),
        )


def test_bonus_working_is_json_serialisable() -> None:
    import json

    result = compute_bonus(
        config=BONUS_CONFIG,
        monthly_wages=D(15_000),
        days_worked=D(200),
        total_working_days=D(240),
    )
    json.dumps(result.as_working())


# ═══════════════════════════════════════════════════════════════════════════
# ESI contribution periods
# ═══════════════════════════════════════════════════════════════════════════

ESI_PERIODS = {
    "contribution_periods": [
        {"start_month": 4, "end_month": 9, "label": "APR-SEP"},
        {"start_month": 10, "end_month": 3, "label": "OCT-MAR"},
    ]
}


@pytest.mark.parametrize(
    ("month", "label"),
    [(4, "APR-SEP"), (7, "APR-SEP"), (9, "APR-SEP"),
     (10, "OCT-MAR"), (12, "OCT-MAR"), (1, "OCT-MAR"), (3, "OCT-MAR")],
)
def test_every_month_maps_to_a_contribution_period(month: int, label: str) -> None:
    """Including the period that wraps the calendar year."""
    assert esi_contribution_period(month, ESI_PERIODS).label == label


def test_contributions_continue_after_a_mid_period_ceiling_crossing() -> None:
    """The checklist gate, and the reason this module exists.

    An employee below the ceiling in April who gets a raise in July keeps
    contributing through September. Dropping them in July is the intuitive
    implementation and it is wrong.
    """
    threshold = D(21_000)
    applies, reason = should_continue_esi(
        current_wages=D(28_000),
        threshold=threshold,
        month=7,
        config=ESI_PERIODS,
        was_contributing_at_period_start=True,
    )
    assert applies is True
    assert "continue to the end of the APR-SEP contribution period" in reason


def test_the_crossing_employee_drops_out_at_the_next_period() -> None:
    """October opens a new period, and they start it above the ceiling."""
    applies, _ = should_continue_esi(
        current_wages=D(28_000),
        threshold=D(21_000),
        month=10,
        config=ESI_PERIODS,
        was_contributing_at_period_start=False,
    )
    assert applies is False


def test_a_wage_cut_does_not_bring_someone_into_esi_mid_period() -> None:
    """The rule runs both ways: coverage begins at a period boundary."""
    applies, reason = should_continue_esi(
        current_wages=D(18_000),
        threshold=D(21_000),
        month=7,
        config=ESI_PERIODS,
        was_contributing_at_period_start=False,
    )
    assert applies is False
    assert "next contribution period" in reason


def test_someone_within_the_ceiling_all_period_simply_contributes() -> None:
    applies, _ = should_continue_esi(
        current_wages=D(18_000),
        threshold=D(21_000),
        month=7,
        config=ESI_PERIODS,
        was_contributing_at_period_start=True,
    )
    assert applies is True


def test_a_config_without_periods_is_refused_rather_than_assumed() -> None:
    with pytest.raises(StatutoryError, match="names no contribution periods"):
        esi_contribution_period(7, {})


def test_a_month_outside_every_period_is_refused() -> None:
    partial = {"contribution_periods": [{"start_month": 4, "end_month": 9}]}
    with pytest.raises(StatutoryError, match="No ESI contribution period covers"):
        esi_contribution_period(11, partial)


# ═══════════════════════════════════════════════════════════════════════════
# Multi-state Professional Tax
# ═══════════════════════════════════════════════════════════════════════════

PERIOD_START = date(2025, 6, 1)
PERIOD_END = date(2025, 6, 30)


def test_an_employee_in_one_state_all_period_resolves_cleanly() -> None:
    resolution = resolve_professional_tax_state(
        location_history=[(date(2024, 1, 1), "KA")],
        period_start=PERIOD_START,
        period_end=PERIOD_END,
    )
    assert resolution.state_code == "KA"
    assert resolution.needs_review is False


def test_a_mid_period_state_change_is_flagged_not_guessed() -> None:
    """The checklist gate.

    Neither state is picked. PT is filed per state; guessing files in the wrong
    one, and nobody notices until an inspection.
    """
    resolution = resolve_professional_tax_state(
        location_history=[(date(2024, 1, 1), "KA"), (date(2025, 6, 15), "MH")],
        period_start=PERIOD_START,
        period_end=PERIOD_END,
    )
    assert resolution.needs_review is True
    assert resolution.state_code is None
    assert "KA" in resolution.reason
    assert "MH" in resolution.reason
    assert "will not guess" in resolution.reason


def test_a_transfer_before_the_period_is_not_a_mid_period_change() -> None:
    resolution = resolve_professional_tax_state(
        location_history=[(date(2024, 1, 1), "KA"), (date(2025, 5, 1), "MH")],
        period_start=PERIOD_START,
        period_end=PERIOD_END,
    )
    assert resolution.state_code == "MH"
    assert resolution.needs_review is False


def test_a_transfer_after_the_period_does_not_affect_it() -> None:
    resolution = resolve_professional_tax_state(
        location_history=[(date(2024, 1, 1), "KA"), (date(2025, 8, 1), "MH")],
        period_start=PERIOD_START,
        period_end=PERIOD_END,
    )
    assert resolution.state_code == "KA"
    assert resolution.needs_review is False


def test_a_move_within_the_same_state_is_not_a_change() -> None:
    """Two offices in Karnataka is one PT state."""
    resolution = resolve_professional_tax_state(
        location_history=[(date(2024, 1, 1), "KA"), (date(2025, 6, 15), "KA")],
        period_start=PERIOD_START,
        period_end=PERIOD_END,
    )
    assert resolution.state_code == "KA"
    assert resolution.needs_review is False


def test_no_work_location_is_flagged() -> None:
    resolution = resolve_professional_tax_state(
        location_history=[], period_start=PERIOD_START, period_end=PERIOD_END
    )
    assert resolution.needs_review is True
    assert "no work location" in resolution.reason


# ═══════════════════════════════════════════════════════════════════════════
# The PF ceiling election and VPF
# ═══════════════════════════════════════════════════════════════════════════


def test_the_ceiling_caps_the_pf_base_by_default() -> None:
    resolved = resolve_pf_base(
        wage_base=D(50_000), ceiling=D(15_000), ceiling_applies_by_default=True
    )
    assert resolved.base == D(15_000)
    assert resolved.ceiling_applied is True


def test_an_employee_election_lifts_the_ceiling() -> None:
    resolved = resolve_pf_base(
        wage_base=D(50_000),
        ceiling=D(15_000),
        ceiling_applies_by_default=True,
        election=PfElection(on_actual_wage=True),
    )
    assert resolved.base == D(50_000)
    assert resolved.ceiling_applied is False
    assert "elected PF on actual wages" in resolved.reason


def test_wages_within_the_ceiling_are_not_marked_as_capped() -> None:
    resolved = resolve_pf_base(
        wage_base=D(12_000), ceiling=D(15_000), ceiling_applies_by_default=True
    )
    assert resolved.base == D(12_000)
    assert resolved.ceiling_applied is False


def test_vpf_is_computed_on_the_full_wage_base_not_the_capped_one() -> None:
    """An employee electing VPF is contributing out of their actual wages."""
    amount = compute_vpf(
        pf_wage_base=D(50_000), election=PfElection(vpf_percent=D(10))
    )
    assert amount == D(5_000)


def test_a_fixed_vpf_amount_is_taken_as_given() -> None:
    assert compute_vpf(pf_wage_base=D(50_000), election=PfElection(vpf_amount=D(3_000))) == D(3_000)


def test_no_election_means_no_vpf() -> None:
    assert compute_vpf(pf_wage_base=D(50_000), election=None) == D(0)
    assert compute_vpf(pf_wage_base=D(50_000), election=PfElection()) == D(0)


def test_a_vpf_election_carrying_both_forms_is_refused() -> None:
    with pytest.raises(StatutoryError, match="not both"):
        PfElection(vpf_percent=D(10), vpf_amount=D(3_000))


def test_a_negative_vpf_election_is_refused() -> None:
    with pytest.raises(StatutoryError, match="cannot be negative"):
        PfElection(vpf_percent=D(-5))


def test_a_float_vpf_election_is_refused() -> None:
    with pytest.raises(StatutoryError, match="must be Decimals"):
        PfElection(vpf_percent=10.0)  # type: ignore[arg-type]
