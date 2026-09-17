"""Inputs from other modules and TDS inside `compute_payroll`.

What these hold: a one-off payment moves net pay but not the structure's gross
(which the ECR, the bonus wage and the registers read as wages); a perquisite
moves tax but not cash; a recovery never takes someone below the floor; and the
salary trace in `lines` carries nothing but the structure and statutory lines,
because the arrears engine diffs it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models.payroll import CalculationType
from app.services.payroll.engine import (
    InputKind,
    InputLine,
    LoanRecoveryInput,
    PayrollInput,
    PayrollRuleError,
    compute_payroll,
)
from app.services.payroll.salary_structure import resolve_ctc
from app.services.payroll.tds import PriorPay, TdsContext
from app.services.tax.computation import ProjectionInput
from tests.payroll.conftest import component, employee_salary_from, structure

START, END = date(2026, 4, 1), date(2026, 4, 30)


def salary(ctc: str = "1200000"):
    """Monthly BASIC 40,000 (PF base), SPECIAL 60,000: gross 100,000."""
    shape = structure(
        component("BASIC", calculation_type=CalculationType.PERCENTAGE_OF, base="CTC",
                  value="40", counts_toward_pf=True, display_order=1),
        component("SPECIAL", calculation_type=CalculationType.BALANCE, display_order=9),
    )
    return employee_salary_from(resolve_ctc(shape, Decimal(ctc)), Decimal(ctc))


def tds_context(rate: int = 10) -> TdsContext:
    return TdsContext(
        rules={
            "regimes": {
                "new": {
                    "slabs": [{"up_to": None, "rate": rate}],
                    "standard_deduction": 0,
                    "deductions_allowed": [],
                }
            },
            "cess_percent": 0,
        },
        rule_set_id="rs-tds",
        financial_year="2026-27",
        months_remaining=12,
        base=ProjectionInput(regime="new", gross_salary=Decimal(0), basic_plus_da=Decimal(0)),
        prior=PriorPay(),
    )


def run(pf_rules, *, inputs=(), loans=(), tds=None):
    return compute_payroll(
        PayrollInput(
            employee_salary=salary(),
            period_start=START,
            period_end=END,
            working_days=Decimal(30),
            rule_sets={"pf": pf_rules},
            inputs=tuple(inputs),
            loan_recoveries=tuple(loans),
            tds=tds,
        )
    )


def line(kind: InputKind, amount: str, taxable: str = "0", code: str = "X") -> InputLine:
    return InputLine(
        provider="test",
        code=code,
        name=code.title(),
        kind=kind,
        amount=Decimal(amount),
        taxable_amount=Decimal(taxable),
        basis="test",
        source={"type": "test", "id": code},
    )


def test_without_a_tds_context_tds_stays_zero_as_before(pf_rules) -> None:
    """The arrears engine recomputes structures without one; that must not move."""
    result = run(pf_rules)
    assert result.tds == Decimal(0)
    assert result.tds_working is None
    assert result.net_pay == Decimal("98200.00")


def test_tds_is_computed_from_the_projection(pf_rules) -> None:
    result = run(pf_rules, tds=tds_context())
    assert result.tds == Decimal("10000.00")
    assert result.net_pay == Decimal("88200.00")
    assert result.tds_working["recurring_tds"] == "10000.00"


def test_a_one_off_earning_raises_net_but_not_gross(pf_rules) -> None:
    result = run(pf_rules, inputs=[line(InputKind.EARNING, "20000", "20000", "BONUS")])
    assert result.gross_earnings == Decimal("100000.00")
    assert result.other_earnings == Decimal("20000.00")
    assert result.net_pay == Decimal("118200.00")
    assert result.pf_employee == Decimal("1800.00"), "a one-off is not PF wages"


def test_the_salary_trace_carries_no_input_line(pf_rules) -> None:
    """The arrears engine diffs `lines`; a one-off there reads as a component."""
    result = run(
        pf_rules,
        inputs=[line(InputKind.EARNING, "20000", "20000", "BONUS")],
        tds=tds_context(),
    )
    codes = {item.code for item in result.lines}
    assert "BONUS" not in codes and "TDS" not in codes
    assert [item.code for item in result.input_lines] == ["BONUS"]


def test_one_off_taxable_income_is_taxed_this_month(pf_rules) -> None:
    result = run(
        pf_rules,
        inputs=[line(InputKind.EARNING, "20000", "20000", "BONUS")],
        tds=tds_context(),
    )
    assert result.tds == Decimal("12000.00")


def test_a_reimbursement_pays_cash_and_adds_no_tax(pf_rules) -> None:
    with_claim = run(
        pf_rules, inputs=[line(InputKind.REIMBURSEMENT, "3000", code="FUEL")], tds=tds_context()
    )
    assert with_claim.tds == Decimal("10000.00")
    assert with_claim.net_pay == Decimal("91200.00")


def test_a_perquisite_adds_tax_and_no_cash(pf_rules) -> None:
    result = run(
        pf_rules,
        inputs=[line(InputKind.PERQUISITE, "10000", "10000", "PERK")],
        tds=tds_context(),
    )
    assert result.tds == Decimal("11000.00")
    assert result.net_pay == Decimal("87200.00")
    assert result.other_earnings == Decimal("0.00")


def test_an_employer_contribution_is_cost_not_pay(pf_rules) -> None:
    base = run(pf_rules)
    result = run(pf_rules, inputs=[line(InputKind.EMPLOYER_CONTRIBUTION, "500", code="ER")])
    assert result.net_pay == base.net_pay
    assert result.employer_cost == base.employer_cost + Decimal("500")


def test_a_recovery_that_does_not_fit_is_skipped_not_part_taken(pf_rules) -> None:
    result = run(pf_rules, inputs=[line(InputKind.RECOVERY, "500000", code="ADV")])
    assert result.net_pay == Decimal("98200.00")
    recovery = result.input_lines[0]
    assert recovery.skipped is True
    assert recovery.amount == Decimal(0)
    assert recovery.requested == Decimal("500000.00")


def test_a_recovery_that_fits_is_taken(pf_rules) -> None:
    result = run(pf_rules, inputs=[line(InputKind.RECOVERY, "8200", code="ADV")])
    assert result.net_pay == Decimal("90000.00")
    assert result.input_lines[0].skipped is False


def loan(emi: str, floor: str, *, partial: bool = False) -> LoanRecoveryInput:
    return LoanRecoveryInput(
        provider="loan_emi",
        code="LOAN_EMI",
        name="Loan instalment",
        emi=Decimal(emi),
        outstanding_principal=Decimal("100000"),
        carried_forward=Decimal(0),
        minimum_net_pay=Decimal(floor),
        allow_partial=partial,
        source={"type": "loan_account", "id": "L1", "period": "2026-04"},
    )


def test_a_loan_emi_that_breaches_the_floor_is_skipped_and_carried(pf_rules) -> None:
    result = run(pf_rules, loans=[loan("10000", "95000")])
    assert result.net_pay == Decimal("98200.00")
    decision = result.input_lines[0]
    assert decision.skipped is True
    assert decision.recovery["carried_forward"] == "10000.00"


def test_a_loan_emi_above_the_floor_is_recovered(pf_rules) -> None:
    result = run(pf_rules, loans=[loan("10000", "50000")])
    assert result.net_pay == Decimal("88200.00")
    assert result.input_lines[0].amount == Decimal("10000.00")


def test_deductions_beyond_earnings_refuse_rather_than_pay_negative(pf_rules) -> None:
    with pytest.raises(PayrollRuleError, match="Net pay would be"):
        run(pf_rules, inputs=[line(InputKind.DEDUCTION, "150000", code="DED")])


def test_a_float_input_is_refused(pf_rules) -> None:
    bad = InputLine("test", "X", "X", InputKind.EARNING, 100.0)  # type: ignore[arg-type]
    with pytest.raises(PayrollRuleError, match="float"):
        run(pf_rules, inputs=[bad])


def test_more_taxable_than_paid_is_refused(pf_rules) -> None:
    with pytest.raises(PayrollRuleError, match="taxable"):
        run(pf_rules, inputs=[line(InputKind.EARNING, "100", "200")])


def test_a_deduction_cannot_carry_taxable_income(pf_rules) -> None:
    with pytest.raises(PayrollRuleError, match="no taxable income"):
        run(pf_rules, inputs=[line(InputKind.DEDUCTION, "100", "100")])
