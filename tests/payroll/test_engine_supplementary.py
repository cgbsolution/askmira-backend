"""Wage-type input lines and supplementary (off-cycle) payslips.

What these hold:

- overtime and shift pay (`counts_as_wages`) raise the ESI contribution and the
  PT slab, but never PF wages, and never decide ESI coverage;
- a supplementary payslip pays its input lines only: no structure, no PF, no
  LWF, PT topped up to the month's slab, ESI only if the month was covered,
  and TDS only on the one-off pay — the regular run took the month's share.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models.payroll import CalculationType
from app.services.payroll.engine import (
    InputKind,
    InputLine,
    PayrollInput,
    SupplementaryContext,
    compute_payroll,
)
from app.services.payroll.salary_structure import resolve_ctc
from app.services.payroll.tds import PriorPay, TdsContext
from app.services.tax.computation import ProjectionInput
from tests.payroll.conftest import component, employee_salary_from, rule_set, structure

START, END = date(2026, 4, 1), date(2026, 4, 30)


def small_salary(ctc: str = "216000"):
    """Monthly BASIC 9,000 (PF and ESI), SPECIAL 9,000 (ESI): gross 18,000."""
    shape = structure(
        component("BASIC", calculation_type=CalculationType.PERCENTAGE_OF, base="CTC",
                  value="50", counts_toward_pf=True, counts_toward_esi=True,
                  display_order=1),
        component("SPECIAL", calculation_type=CalculationType.BALANCE,
                  counts_toward_esi=True, display_order=9),
    )
    return employee_salary_from(resolve_ctc(shape, Decimal(ctc)), Decimal(ctc))


def wage_line(amount: str, code: str = "OVERTIME") -> InputLine:
    return InputLine(
        provider="overtime",
        code=code,
        name=code.title(),
        kind=InputKind.EARNING,
        amount=Decimal(amount),
        taxable_amount=Decimal(amount),
        basis="test",
        source={"type": "overtime_entry", "id": code},
        counts_as_wages=True,
    )


def bonus_line(amount: str) -> InputLine:
    return InputLine(
        provider="payroll_adjustments",
        code="ADJ_BONUS",
        name="Bonus",
        kind=InputKind.EARNING,
        amount=Decimal(amount),
        taxable_amount=Decimal(amount),
        basis="test",
        source={"type": "payroll_adjustment", "id": "b"},
    )


def tds_context() -> TdsContext:
    return TdsContext(
        rules={
            "regimes": {
                "new": {
                    "slabs": [{"up_to": None, "rate": 10}],
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


def compute(rules, *, inputs=(), supplementary=None, tds=None):
    return compute_payroll(
        PayrollInput(
            employee_salary=small_salary(),
            period_start=START,
            period_end=END,
            working_days=Decimal(30),
            rule_sets=rules,
            inputs=tuple(inputs),
            tds=tds,
            supplementary=supplementary,
        )
    )


def test_overtime_raises_esi_contribution_but_not_pf(pf_rules, esi_rules) -> None:
    rules = {"pf": pf_rules, "esi": esi_rules}
    without = compute(rules)
    with_ot = compute(rules, inputs=[wage_line("2000")])

    assert with_ot.pf_employee == without.pf_employee, "overtime is not PF wages"
    assert with_ot.esi_wage_base == Decimal("20000.00")
    assert with_ot.esi_employee == Decimal("150.00")  # 0.75% of 20,000
    assert with_ot.esi_eligibility_base == Decimal("18000.00")


def test_overtime_never_decides_esi_coverage(pf_rules, esi_rules) -> None:
    """Structure wages of 18,000 are covered even when overtime takes them past 21,000."""
    result = compute({"pf": pf_rules, "esi": esi_rules}, inputs=[wage_line("5000")])
    assert result.esi_employee == Decimal("172.50")  # 0.75% of 23,000
    assert result.esi_eligibility_base == Decimal("18000.00")


def test_a_bonus_is_not_wages_for_esi(pf_rules, esi_rules) -> None:
    result = compute({"pf": pf_rules, "esi": esi_rules}, inputs=[bonus_line("5000")])
    assert result.esi_wage_base == Decimal("18000.00")


def test_overtime_can_move_the_professional_tax_slab(pf_rules, pt_rules) -> None:
    rules = {"pf": pf_rules, "professional_tax": pt_rules}
    assert compute(rules).professional_tax == Decimal("150.00")  # 18,000 → 150
    assert compute(rules, inputs=[wage_line("3000")]).professional_tax == Decimal("200.00")


def test_a_supplementary_payslip_pays_only_its_input_lines(pf_rules, esi_rules) -> None:
    result = compute(
        {"pf": pf_rules, "esi": esi_rules},
        inputs=[bonus_line("10000")],
        supplementary=SupplementaryContext(
            regular_gross=Decimal("18000"), pt_already_deducted=Decimal(0), esi_covered=True
        ),
    )
    assert result.supplementary is True
    assert result.gross_earnings == Decimal("0.00")
    assert result.pf_employee == Decimal(0)
    assert result.pf_wage_base == Decimal(0)
    assert result.esi_employee == Decimal(0), "a bonus is not ESI wages"
    assert result.net_pay == Decimal("10000.00")
    assert not [line for line in result.lines if line.code in ("BASIC", "SPECIAL", "PF_EMPLOYEE")]


def test_supplementary_overtime_is_charged_esi_only_when_the_month_was_covered(
    pf_rules, esi_rules
) -> None:
    rules = {"pf": pf_rules, "esi": esi_rules}
    covered = compute(
        rules,
        inputs=[wage_line("2000")],
        supplementary=SupplementaryContext(Decimal("18000"), Decimal(0), esi_covered=True),
    )
    uncovered = compute(
        rules,
        inputs=[wage_line("2000")],
        supplementary=SupplementaryContext(Decimal("18000"), Decimal(0), esi_covered=False),
    )
    assert covered.esi_employee == Decimal("15.00")  # 0.75% of 2,000
    assert uncovered.esi_employee == Decimal(0)


def test_supplementary_professional_tax_is_only_the_slab_difference(pf_rules, pt_rules) -> None:
    rules = {"pf": pf_rules, "professional_tax": pt_rules}
    same_slab = compute(
        rules,
        inputs=[wage_line("1000")],
        supplementary=SupplementaryContext(Decimal("18000"), Decimal("150"), esi_covered=False),
    )
    next_slab = compute(
        rules,
        inputs=[wage_line("3000")],
        supplementary=SupplementaryContext(Decimal("18000"), Decimal("150"), esi_covered=False),
    )
    assert same_slab.professional_tax == Decimal(0)
    assert next_slab.professional_tax == Decimal("50.00")


def test_supplementary_lwf_is_never_taken_twice(pf_rules) -> None:
    lwf = rule_set(
        "labour_welfare_fund",
        {
            "applicable": True,
            "deduction_months": [4],
            "employee_amount": 20,
            "employer_amount": 40,
        },
        jurisdiction="KA",
    )
    regular = compute({"pf": pf_rules, "labour_welfare_fund": lwf})
    supplementary = compute(
        {"pf": pf_rules, "labour_welfare_fund": lwf},
        inputs=[bonus_line("1000")],
        supplementary=SupplementaryContext(Decimal("18000"), Decimal(0), esi_covered=False),
    )
    assert regular.lwf_employee > 0
    assert supplementary.lwf_employee == Decimal(0)


def test_supplementary_tds_is_only_the_one_off_share(pf_rules) -> None:
    regular = compute({"pf": pf_rules}, tds=tds_context())
    supplementary = compute(
        {"pf": pf_rules},
        inputs=[bonus_line("10000")],
        tds=tds_context(),
        supplementary=SupplementaryContext(Decimal("18000"), Decimal(0), esi_covered=False),
    )
    # 10% flat: the month's recurring share is the regular run's; the bonus adds 1,000.
    assert regular.tds == Decimal("1800.00")
    assert supplementary.tds == Decimal("1000.00")
    assert supplementary.tds_working["supplementary"] is True
    assert supplementary.net_pay == Decimal("9000.00")
