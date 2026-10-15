"""The legal ceiling on a wage period's deductions, from an approved rule set.

Gross is 100,000 and PF takes 1,800, as in `test_engine_inputs`. With a 50%
ceiling, deductions may total 50,000: recoveries that can wait are held back to
fit, and deductions that cannot wait are never cut but flag the payslip.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.payroll.engine import (
    InputKind,
    PayrollInput,
    PayrollRuleError,
    compute_payroll,
)
from tests.payroll.conftest import rule_set
from tests.payroll.test_engine_inputs import END, START, line, loan, salary


def ceiling(percent: str = "50", *, approved: bool = True):
    return rule_set("wage_deductions", {"max_deduction_percent": percent}, approved=approved)


def run(pf_rules, *, inputs=(), loans=(), wage_deductions=None):
    rules = {"pf": pf_rules}
    if wage_deductions is not None:
        rules["wage_deductions"] = wage_deductions
    return compute_payroll(
        PayrollInput(
            employee_salary=salary(),
            period_start=START,
            period_end=END,
            working_days=Decimal(30),
            rule_sets=rules,
            inputs=tuple(inputs),
            loan_recoveries=tuple(loans),
        )
    )


def test_without_a_ceiling_rule_set_nothing_changes(pf_rules) -> None:
    result = run(pf_rules, inputs=[line(InputKind.RECOVERY, "60000", code="ADV")])
    assert result.input_lines[0].skipped is False
    assert result.over_deduction_ceiling is None


def test_a_recovery_that_would_pass_the_ceiling_waits(pf_rules) -> None:
    """1,800 PF + 60,000 would be 61.8% of wages: it fits net pay, not the law."""
    result = run(
        pf_rules,
        inputs=[line(InputKind.RECOVERY, "60000", code="ADV")],
        wage_deductions=ceiling(),
    )
    recovery = result.input_lines[0]
    assert recovery.skipped is True
    assert recovery.amount == Decimal(0)
    assert "50% of wages" in recovery.basis
    assert result.net_pay == Decimal("98200.00")
    assert result.over_deduction_ceiling is None


def test_a_recovery_inside_the_ceiling_is_taken(pf_rules) -> None:
    result = run(
        pf_rules,
        inputs=[line(InputKind.RECOVERY, "48200", code="ADV")],
        wage_deductions=ceiling(),
    )
    assert result.input_lines[0].skipped is False
    assert result.total_deductions == Decimal("50000.00")


def test_a_loan_floor_is_raised_by_the_ceiling(pf_rules) -> None:
    """The scheme's floor is 10,000, but the ceiling leaves only 48,200 of room."""
    result = run(pf_rules, loans=[loan("50000", "10000")], wage_deductions=ceiling())
    decision = result.input_lines[0]
    assert decision.skipped is True
    assert "legal ceiling" in decision.basis
    partial = run(pf_rules, loans=[loan("50000", "10000", partial=True)], wage_deductions=ceiling())
    assert partial.input_lines[0].amount == Decimal("48200.00")


def test_deductions_that_cannot_wait_are_kept_and_flagged(pf_rules) -> None:
    result = run(
        pf_rules,
        inputs=[line(InputKind.DEDUCTION, "60000", code="ATTACHMENT")],
        wage_deductions=ceiling(),
    )
    assert result.total_deductions == Decimal("61800.00")
    flag = result.over_deduction_ceiling
    assert flag is not None
    assert flag["share_percent"] == "61.80"
    assert flag["ceiling_percent"] == "50"
    assert flag["ceiling"] == "50000.00"


def test_an_unapproved_ceiling_is_refused(pf_rules) -> None:
    with pytest.raises(PayrollRuleError, match="not been approved"):
        run(pf_rules, wage_deductions=ceiling(approved=False))


def test_a_ceiling_outside_0_to_100_is_refused(pf_rules) -> None:
    with pytest.raises(PayrollRuleError, match="percentage"):
        run(pf_rules, wage_deductions=ceiling("150"))
