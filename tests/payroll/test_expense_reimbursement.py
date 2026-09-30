"""Approved expense claims, paid at cost in the next payroll run.

What these hold: the line is a non-taxable reimbursement that moves net pay and
nothing else; it is traceable to the claim it pays; the provider is registered
so an approved run consumes it; and income a run pays itself is not counted a
second time by the TDS projection.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.models.payroll import CalculationType
from app.services.payroll import inputs
from app.services.payroll.engine import InputKind, PayrollInput, compute_payroll
from app.services.payroll.salary_structure import resolve_ctc
from app.services.payroll.tds import PriorPay, TdsContext, without_income_paid_by_payroll
from app.services.tax.computation import ProjectionInput
from app.services.tax.sources import PAID_THROUGH_PAYROLL
from tests.payroll.conftest import component, employee_salary_from, structure

START, END = date(2026, 10, 1), date(2026, 10, 31)
TDS_RULES = {
    "regimes": {
        "new": {
            "slabs": [{"up_to": None, "rate": 10}],
            "standard_deduction": 0,
            "deductions_allowed": [],
        }
    },
    "cess_percent": 0,
}


def salary():
    shape = structure(
        component("BASIC", calculation_type=CalculationType.PERCENTAGE_OF, base="CTC",
                  value="40", counts_toward_pf=True, display_order=1),
        component("SPECIAL", calculation_type=CalculationType.BALANCE, display_order=9),
    )
    return employee_salary_from(resolve_ctc(shape, Decimal("1200000")), Decimal("1200000"))


def tds_context(base: ProjectionInput | None = None) -> TdsContext:
    return TdsContext(
        rules=TDS_RULES,
        rule_set_id="rs-tds",
        financial_year="2026-27",
        months_remaining=6,
        base=base
        or ProjectionInput(regime="new", gross_salary=Decimal(0), basic_plus_da=Decimal(0)),
        prior=PriorPay(),
    )


def claim_line(amount: str = "4250.50"):
    return inputs.expense_claim_line(
        claim_id=uuid.UUID(int=7),
        category_key="local_travel",
        amount=Decimal(amount),
        incurred_on=date(2026, 9, 12),
    )


def test_an_expense_claim_is_a_non_taxable_reimbursement_traced_to_its_claim() -> None:
    line = claim_line()
    assert line.kind is InputKind.REIMBURSEMENT
    assert line.taxable_amount == Decimal(0)
    assert line.amount == Decimal("4250.50")
    assert line.code == "EXPENSE_REIMB"
    assert line.source == {"type": "expense_claim", "id": str(uuid.UUID(int=7))}
    assert "Local travel" in line.name
    assert "not taxable" in line.basis


def test_a_foreign_claim_says_what_was_spent() -> None:
    line = inputs.expense_claim_line(
        claim_id=uuid.uuid4(),
        category_key="hotel",
        amount=Decimal("9420.00"),
        incurred_on=date(2026, 9, 2),
        original_amount=Decimal("100.00"),
        original_currency="USD",
    )
    assert "USD 100.00 converted" in line.basis


def test_a_float_amount_is_refused() -> None:
    with pytest.raises(TypeError):
        inputs.expense_claim_line(
            claim_id=uuid.uuid4(), category_key="taxi", amount=4250.5,  # type: ignore[arg-type]
            incurred_on=date(2026, 9, 2),
        )


def test_an_advance_balance_is_a_non_taxable_reimbursement() -> None:
    line = inputs.advance_refund_line(
        settlement_id=uuid.UUID(int=9),
        refund_amount=Decimal("1800.00"),
        advance_amount=Decimal("10000.00"),
        claims_total=Decimal("11800.00"),
        settled_on=date(2026, 9, 20),
    )
    assert line.kind is InputKind.REIMBURSEMENT
    assert line.taxable_amount == Decimal(0)
    assert line.source == {"type": "advance_refund", "id": str(uuid.UUID(int=9))}
    assert line.code == "ADVANCE_REFUND"


def test_the_claim_raises_net_pay_and_nothing_else(pf_rules) -> None:
    def run(extra):
        return compute_payroll(
            PayrollInput(
                employee_salary=salary(),
                period_start=START,
                period_end=END,
                working_days=Decimal(31),
                rule_sets={"pf": pf_rules},
                inputs=tuple(extra),
                tds=tds_context(),
            )
        )

    without = run([])
    paid = run([claim_line("4250.50")])
    assert paid.net_pay - without.net_pay == Decimal("4250.50")
    assert paid.gross_earnings == without.gross_earnings, "a reimbursement is not wages"
    assert paid.pf_employee == without.pf_employee
    assert paid.tds == without.tds, "business expenditure at cost is not income"
    assert paid.other_earnings == Decimal("4250.50")
    [computed] = paid.input_lines
    assert computed.source["type"] == "expense_claim"
    assert computed.as_dict()["amount"] == "4250.50"


def test_both_providers_are_registered_and_consumed_by_source() -> None:
    keys = {provider.key for provider in inputs.PROVIDERS}
    assert {"expense_claims", "advance_refunds"} <= keys
    assert isinstance(inputs._BY_SOURCE["expense_claim"], inputs.ExpenseClaimProvider)
    assert isinstance(inputs._BY_SOURCE["advance_refund"], inputs.AdvanceRefundProvider)


def test_income_the_run_pays_itself_is_not_projected_twice() -> None:
    base = ProjectionInput(
        regime="new",
        gross_salary=Decimal(0),
        basic_plus_da=Decimal(0),
        other_income=(
            ("ARREARS", "Salary arrears received", Decimal("30000"), "arrears", "b"),
            ("LEAVE_ENCASHMENT_IN_SERVICE", "Leave encashment in service", Decimal("9000"),
             "exit_benefits", "b"),
            ("GRATUITY_TAXABLE", "Gratuity above the exemption", Decimal("5000"),
             "exit_benefits", "b"),
        ),
    )
    kept = without_income_paid_by_payroll(base, PAID_THROUGH_PAYROLL)
    assert [line[0] for line in kept.other_income] == ["GRATUITY_TAXABLE"]
    # Nothing to strip: the same object back.
    assert without_income_paid_by_payroll(kept, PAID_THROUGH_PAYROLL) is kept


def test_arrears_paid_in_the_run_are_taxed_once(pf_rules) -> None:
    """30,000 of arrears at a flat 10%: 3,000 of tax this month, not 6,000."""
    arrears = inputs.InputLine(
        provider="arrears", code="ARREARS", name="Salary arrears", kind=InputKind.EARNING,
        amount=Decimal("30000"), taxable_amount=Decimal("30000"), basis="b",
        source={"type": "arrear_batch", "id": "x"},
    )
    base = ProjectionInput(
        regime="new",
        gross_salary=Decimal(0),
        basic_plus_da=Decimal(0),
        other_income=(("ARREARS", "Salary arrears received", Decimal("30000"), "arrears", "b"),),
    )

    def run(context):
        return compute_payroll(
            PayrollInput(
                employee_salary=salary(),
                period_start=START,
                period_end=END,
                working_days=Decimal(31),
                rule_sets={"pf": pf_rules},
                inputs=(arrears,),
                tds=context,
            )
        )

    once = run(tds_context(without_income_paid_by_payroll(base, PAID_THROUGH_PAYROLL)))
    plain = run(tds_context())
    assert once.tds == plain.tds
    assert Decimal(once.tds_working["one_off_tds"]) == Decimal("3000.00")
