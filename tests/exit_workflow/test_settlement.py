"""Slice 13 gate: the settlement computation.

Two things this file is mostly about:

- **A negative net is a demand, not a zero.** Flooring would write off a debt
  nobody decided to write off, and tell the employee they were owed nothing.
- **Every line states its basis.** The employee reads this statement on the day
  they lose access to anyone who could explain it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.exit_workflow.settlement import (
    SettlementError,
    SettlementInput,
    compute_settlement,
    notice_shortfall_recovery,
)


def D(value: str | int) -> Decimal:  # noqa: N802 - a Decimal alias, used throughout
    return Decimal(str(value))


def settlement(**overrides):
    base = {
        "final_month_salary": D(45_000),
        "final_month_basis": "18 of 30 days worked in June at 75,000 per month.",
        "leave_encashment": D(20_000),
        "leave_encashment_basis": "10 days of earned leave at 2,000 per day.",
        "gratuity": D(150_000),
        "gratuity_basis": "6 completed years at 15 days per year on 50,000 basic.",
    }
    base.update(overrides)
    return compute_settlement(SettlementInput(**base))


# ═══════════════════════════════════════════════════════════════════════════
# The positive case
# ═══════════════════════════════════════════════════════════════════════════


def test_earnings_recoveries_and_tax_reach_the_net() -> None:
    result = settlement(
        loan_outstanding=D(30_000),
        loan_basis="Outstanding principal on a personal loan.",
        final_tds=D(5_000),
        tax_basis="Full-year tax recomputed at exit on actual figures.",
    )

    assert result.total_earnings == D(215_000)
    assert result.total_recoveries == D(30_000)
    assert result.total_tax == D(5_000)
    assert result.net_amount == D(180_000)
    assert result.is_demand is False


def test_a_zero_component_produces_no_line() -> None:
    """A statement listing "Bonus: 0" invites a question with no answer."""
    result = settlement()
    codes = {line.code for line in result.lines}
    assert "BONUS" not in codes
    assert "ARREARS" not in codes


def test_earnings_come_before_recoveries_before_tax() -> None:
    """The order the reader will check them in."""
    result = settlement(
        loan_outstanding=D(30_000), loan_basis="Loan.",
        final_tds=D(5_000), tax_basis="Tax.",
    )
    types = [line.line_type for line in sorted(result.lines, key=lambda x: x.display_order)]
    assert types == sorted(types, key=lambda t: {"earning": 0, "recovery": 1, "tax": 2}[t])


# ═══════════════════════════════════════════════════════════════════════════
# The negative case — the gate
# ═══════════════════════════════════════════════════════════════════════════


def test_a_negative_net_is_a_demand_not_a_zero() -> None:
    """The checklist gate.

    Someone who resigned without notice, has an outstanding loan and did not
    return a laptop owes the company. Flooring at zero writes off a debt nobody
    decided to write off.
    """
    result = settlement(
        final_month_salary=D(15_000),
        final_month_basis="6 of 30 days worked.",
        leave_encashment=D(0),
        gratuity=D(0),
        notice_shortfall=D(60_000),
        notice_basis="30 days of notice not served at 2,000 per day on basic.",
        loan_outstanding=D(40_000),
        loan_basis="Outstanding personal loan.",
        asset_recovery=D(35_000),
        asset_basis="IT: laptop not returned (35,000).",
    )

    assert result.net_amount == D(-120_000)
    assert result.is_demand is True
    assert result.amount_due_from_employee == D(120_000)
    assert any("is a demand for" in note for note in result.notes)


def test_a_payable_settlement_reports_nothing_due_from_the_employee() -> None:
    result = settlement()
    assert result.is_demand is False
    assert result.amount_due_from_employee == D(0)


def test_a_net_of_exactly_zero_is_not_a_demand() -> None:
    """The boundary. Nothing owed either way."""
    result = settlement(
        final_month_salary=D(50_000), final_month_basis="Final month.",
        leave_encashment=D(0), gratuity=D(0),
        loan_outstanding=D(50_000), loan_basis="Loan.",
    )
    assert result.net_amount == D(0)
    assert result.is_demand is False


# ═══════════════════════════════════════════════════════════════════════════
# The basis on every line
# ═══════════════════════════════════════════════════════════════════════════


def test_every_line_carries_a_basis() -> None:
    """The checklist gate."""
    result = settlement(
        notice_shortfall=D(60_000), notice_basis="30 days not served.",
        final_tds=D(5_000), tax_basis="Recomputed at exit.",
    )
    for line in result.lines:
        assert line.basis.strip(), f"{line.code} has no basis"


def test_a_missing_basis_says_so_rather_than_rendering_blank() -> None:
    """A blank cell on a statement is an invisible defect. A line that says the
    explanation is missing is a visible one somebody will report."""
    result = settlement(gratuity=D(150_000), gratuity_basis="")
    gratuity = next(line for line in result.lines if line.code == "GRATUITY")
    assert "No computation basis was recorded" in gratuity.basis
    assert "Ask HR" in gratuity.basis


def test_lines_name_the_slice_that_produced_them() -> None:
    """So a disputed figure can be traced to the computation that made it."""
    result = settlement(loan_outstanding=D(30_000), loan_basis="Loan.")
    sources = {line.code: line.source for line in result.lines}
    assert sources["GRATUITY"] == "exit_benefits"
    assert sources["LEAVE_ENCASHMENT"] == "exit_benefits"
    assert sources["LOAN"] == "loans"


# ═══════════════════════════════════════════════════════════════════════════
# Tax at exit
# ═══════════════════════════════════════════════════════════════════════════


def test_a_tax_refund_is_shown_as_a_refund_and_explained() -> None:
    """A large refund at exit is normal for someone leaving mid-year, and an
    unexplained credit on a statement reads as an error."""
    result = settlement(final_tds=D(-25_000), tax_basis="Recomputed at exit.")
    tax_line = next(line for line in result.lines if line.line_type == "tax")

    assert tax_line.label == "Income tax refund on final computation"
    assert tax_line.amount == D(25_000), "shown as a positive amount under a refund label"
    assert result.total_tax == D(-25_000)
    assert result.net_amount == D(240_000), "the refund increases the net"
    assert any("produced a refund" in note for note in result.notes)


def test_a_tax_deduction_is_shown_as_a_deduction() -> None:
    result = settlement(final_tds=D(25_000), tax_basis="Recomputed at exit.")
    tax_line = next(line for line in result.lines if line.line_type == "tax")
    assert tax_line.label == "Income tax deducted at source"
    assert result.net_amount == D(190_000)


def test_zero_tax_produces_no_tax_line() -> None:
    assert not [line for line in settlement().lines if line.line_type == "tax"]


# ═══════════════════════════════════════════════════════════════════════════
# What is refused
# ═══════════════════════════════════════════════════════════════════════════


def test_a_float_is_refused() -> None:
    with pytest.raises(SettlementError, match="must be a Decimal"):
        settlement(gratuity=150000.0)


def test_a_negative_earning_is_refused() -> None:
    """Recoveries are positive under their own line type. A negative earning is
    ambiguous on a statement the employee has to read."""
    with pytest.raises(SettlementError, match="Recoveries are recorded as positive"):
        settlement(gratuity=D(-1))


def test_a_negative_recovery_is_refused() -> None:
    with pytest.raises(SettlementError, match="Recoveries are recorded as positive"):
        settlement(loan_outstanding=D(-1))


def test_the_result_is_json_serialisable() -> None:
    import json

    json.dumps(settlement(loan_outstanding=D(1), loan_basis="x").as_dict())


def test_the_computation_is_deterministic() -> None:
    assert settlement().as_dict() == settlement().as_dict()


# ═══════════════════════════════════════════════════════════════════════════
# Notice shortfall recovery
# ═══════════════════════════════════════════════════════════════════════════


def test_notice_recovery_is_computed_on_the_supplied_wage() -> None:
    """The checklist gate: on the configured basis.

    Which wage — basic, basic plus DA, or gross — is a contractual term. On
    gross when the contract says basic, this overcharges by a factor of two or
    more, and it is the most disputed line on a settlement.
    """
    amount, basis = notice_shortfall_recovery(
        shortfall_days=15,
        monthly_wage=D(60_000),
        days_in_month=30,
        wage_basis_label="basic + DA",
    )
    assert amount == D(30_000)
    assert "15 day(s)" in basis
    assert "2000.00 per day" in basis
    assert "basic + DA" in basis


def test_the_wage_basis_changes_the_recovery() -> None:
    on_basic, _ = notice_shortfall_recovery(
        shortfall_days=15, monthly_wage=D(30_000), days_in_month=30,
        wage_basis_label="basic",
    )
    on_gross, _ = notice_shortfall_recovery(
        shortfall_days=15, monthly_wage=D(75_000), days_in_month=30,
        wage_basis_label="gross",
    )
    assert on_gross > on_basic * 2


def test_notice_served_in_full_recovers_nothing() -> None:
    amount, basis = notice_shortfall_recovery(
        shortfall_days=0, monthly_wage=D(60_000), days_in_month=30,
        wage_basis_label="basic",
    )
    assert amount == D(0)
    assert basis == "Notice was served in full."


def test_the_day_count_of_the_month_matters() -> None:
    """A daily rate on 28 days is higher than on 31."""
    february, _ = notice_shortfall_recovery(
        shortfall_days=10, monthly_wage=D(60_000), days_in_month=28,
        wage_basis_label="basic",
    )
    march, _ = notice_shortfall_recovery(
        shortfall_days=10, monthly_wage=D(60_000), days_in_month=31,
        wage_basis_label="basic",
    )
    assert february > march


def test_negative_shortfall_days_are_refused() -> None:
    with pytest.raises(SettlementError, match="cannot be negative"):
        notice_shortfall_recovery(
            shortfall_days=-1, monthly_wage=D(60_000), days_in_month=30,
            wage_basis_label="basic",
        )


def test_a_float_wage_is_refused() -> None:
    with pytest.raises(SettlementError, match="must be a Decimal"):
        notice_shortfall_recovery(
            shortfall_days=15, monthly_wage=60000.0, days_in_month=30,
            wage_basis_label="basic",
        )
