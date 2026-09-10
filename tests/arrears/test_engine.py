"""Slice 12 gate: the arrear recomputation.

The one that matters most: **recomputation uses the rule set version that was
in force in the period**, not today's. It is the natural mistake — you have a
salary and an engine, so you call it — and it is invisible in the output. A
March PF figure computed against a July ceiling could never have been deducted
in March, and nothing on the arrear line says so.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models.payroll import CalculationType, ComponentType
from app.services.arrears.engine import (
    ArrearError,
    recompute_period,
    summarise,
)
from app.services.payroll.salary_structure import resolve_ctc
from tests.payroll.conftest import component, employee_salary_from, rule_set, structure


def D(value: str | int) -> Decimal:  # noqa: N802 - a Decimal alias, used throughout
    return Decimal(str(value))


def salary(ctc: str = "1200000"):
    shape = structure(
        component("BASIC", calculation_type=CalculationType.PERCENTAGE_OF, base="CTC",
                  value="40", counts_toward_pf=True, counts_toward_esi=True, display_order=1),
        component("HRA", calculation_type=CalculationType.PERCENTAGE_OF, base="BASIC",
                  value="50", counts_toward_esi=True, display_order=2),
        component("SPECIAL", calculation_type=CalculationType.BALANCE,
                  counts_toward_esi=True, display_order=9),
    )
    return employee_salary_from(resolve_ctc(shape, Decimal(ctc)), Decimal(ctc))


def pf_rules(ceiling: int = 15_000):
    return rule_set(
        "pf",
        {
            "wage_ceiling": ceiling,
            "employee_rate": 12,
            "employer_rate": 12,
            "apply_wage_ceiling": True,
        },
    )


# What March's payslip said, on the old salary of 12,00,000.
PAID_MARCH = [
    {"code": "BASIC", "name": "Basic", "type": "earning", "amount": "40000.00"},
    {"code": "HRA", "name": "Hra", "type": "earning", "amount": "20000.00"},
    {"code": "SPECIAL", "name": "Special", "type": "earning", "amount": "40000.00"},
    {"code": "PF_EMPLOYEE", "name": "Provident Fund (employee)", "type": "deduction",
     "amount": "1800.00"},
    {"code": "PF_EMPLOYER", "name": "Provident Fund (employer)",
     "type": "employer_contribution", "amount": "1800.00"},
]


def recompute(**overrides):
    base = {
        "period": "2026-03",
        "paid_line_items": PAID_MARCH,
        "new_salary": salary("1500000"),
        "working_days": D(31),
        "lop_days": D(0),
        "period_start": date(2026, 3, 1),
        "period_end": date(2026, 3, 31),
        "historical_rule_sets": {"pf": pf_rules()},
    }
    base.update(overrides)
    return recompute_period(**base)


# ═══════════════════════════════════════════════════════════════════════════
# The historical rule set
# ═══════════════════════════════════════════════════════════════════════════


def test_the_recomputation_uses_the_rule_set_it_is_given() -> None:
    """The checklist gate.

    Two identical recomputations, differing only in the PF ceiling, produce
    different PF arrears — which is what proves the ceiling is coming from the
    supplied rule set and not from anywhere else.
    """
    at_15k = recompute(historical_rule_sets={"pf": pf_rules(15_000)})
    at_21k = recompute(historical_rule_sets={"pf": pf_rules(21_000)})

    pf_15 = next(d for d in at_15k.diffs if d.code == "PF_EMPLOYEE")
    pf_21 = next(d for d in at_21k.diffs if d.code == "PF_EMPLOYEE")

    assert pf_15.should_have_been == D("1800.00"), "12% of the 15,000 ceiling"
    assert pf_21.should_have_been == D("2520.00"), "12% of the 21,000 ceiling"
    assert pf_15.difference != pf_21.difference


def test_the_rule_versions_used_are_recorded_on_the_result() -> None:
    """"Which rules did this use" has to be answerable without re-deriving it —
    it is the thing most likely to be wrong and the hardest to notice."""
    rules = pf_rules()
    result = recompute(historical_rule_sets={"pf": rules})
    assert result.rule_set_ids["pf"] == str(rules.id)


def test_recomputing_with_no_rule_sets_refuses_rather_than_reaching_for_current() -> None:
    """There is no code path here that can reach a current rule, and an empty
    dict is refused rather than silently falling back."""
    with pytest.raises(ArrearError, match="could never have been deducted"):
        recompute(historical_rule_sets={})


# ═══════════════════════════════════════════════════════════════════════════
# The diff
# ═══════════════════════════════════════════════════════════════════════════


def test_a_raise_produces_an_earnings_arrear_per_component() -> None:
    """Per component, never a lump sum — each is taxed and filed differently."""
    result = recompute()

    basic = next(d for d in result.diffs if d.code == "BASIC")
    assert basic.paid == D("40000.00")
    assert basic.should_have_been == D("50000.00"), "40% of the new 15,00,000"
    assert basic.difference == D("10000.00")

    hra = next(d for d in result.diffs if d.code == "HRA")
    assert hra.difference == D("5000.00")

    assert result.earnings_arrear == D("25000.00")


def test_statutory_deduction_arrears_are_computed_alongside_earnings() -> None:
    """The checklist gate.

    A higher March salary means a higher March PF, and the employee owes the
    difference. Computing only the earnings arrear pays gross and under-deducts.
    """
    result = recompute(historical_rule_sets={"pf": pf_rules(21_000)})
    pf = next(d for d in result.diffs if d.code == "PF_EMPLOYEE")
    assert pf.difference == D("720.00")
    assert result.employee_deduction_arrear == D("720.00")


def test_employer_contribution_arrears_are_computed_and_kept_separate() -> None:
    """The checklist gate.

    Owed by the company, but it never reaches the employee's net. Netting it
    against the employee's side understates what the company owes and
    overstates what the employee receives.
    """
    result = recompute(historical_rule_sets={"pf": pf_rules(21_000)})

    assert result.employer_contribution_arrear == D("720.00")
    assert result.net_payable == result.earnings_arrear - result.employee_deduction_arrear
    assert result.employer_contribution_arrear not in (result.net_payable,)


def test_the_net_payable_is_earnings_less_employee_deductions() -> None:
    result = recompute(historical_rule_sets={"pf": pf_rules(21_000)})
    assert result.net_payable == D("25000.00") - D("720.00")


def test_no_change_produces_no_movement() -> None:
    """Re-running an unchanged salary is a no-op, not a zero-value arrear."""
    result = recompute(new_salary=salary("1200000"))
    assert result.earnings_arrear == D(0)
    assert result.has_movement is False


def test_a_component_that_did_not_exist_before_is_arreared_in_full() -> None:
    result = recompute(
        paid_line_items=[
            item for item in PAID_MARCH if item["code"] != "HRA"
        ]
    )
    hra = next(d for d in result.diffs if d.code == "HRA")
    assert hra.paid == D(0)
    assert hra.difference == hra.should_have_been
    assert any("did not exist on the original payslip" in n for n in result.notes)


def test_a_component_dropped_from_the_new_structure_is_recovered_and_noted() -> None:
    """A negative arrear is a recovery. It is allowed — that is what a
    correction downwards is — but it is worth saying out loud."""
    result = recompute(
        paid_line_items=[
            *PAID_MARCH,
            {"code": "OLD_ALLOWANCE", "name": "Old allowance", "type": "earning",
             "amount": "3000.00"},
        ]
    )
    dropped = next(d for d in result.diffs if d.code == "OLD_ALLOWANCE")
    assert dropped.difference == D("-3000.00")
    assert any("has no such component" in n for n in result.notes)


def test_loss_of_pay_from_the_original_period_is_carried_through() -> None:
    """An arrear is a pay difference, not a re-adjudication of who turned up."""
    full = recompute(lop_days=D(0))
    with_lop = recompute(lop_days=D(10))
    assert with_lop.earnings_arrear < full.earnings_arrear


def test_floats_are_refused() -> None:
    with pytest.raises(ArrearError, match="must be Decimals"):
        recompute(working_days=31.0)


def test_the_diff_is_json_serialisable() -> None:
    import json

    result = recompute()
    json.dumps([d.as_dict() for d in result.diffs])


# ═══════════════════════════════════════════════════════════════════════════
# The batch summary
# ═══════════════════════════════════════════════════════════════════════════


def test_the_summary_totals_across_employees_and_periods() -> None:
    one = recompute(period="2026-03")
    two = recompute(period="2026-04", period_start=date(2026, 4, 1),
                    period_end=date(2026, 4, 30), working_days=D(30))

    summary = summarise({"emp-1": [one, two], "emp-2": [one]})
    assert summary.employee_count == 2
    assert summary.period_count == 2
    assert summary.earnings == one.earnings_arrear * 3


def test_the_summary_names_the_filings_the_movement_touches() -> None:
    """The checklist gate: an affected-filings warning surfaces to HR Ops.

    It is a warning, not an action. A revised ECR for a closed month is a
    decision with consequences, and payroll makes it.
    """
    result = recompute(historical_rule_sets={"pf": pf_rules(21_000)})
    summary = summarise({"emp-1": [result]})

    assert "PF ECR for 2026-03" in summary.affected_filings


def test_a_batch_with_no_movement_names_no_filings() -> None:
    result = recompute(new_salary=salary("1200000"))
    summary = summarise({"emp-1": [result]})
    assert summary.affected_filings == []
    assert summary.period_count == 0


def test_the_summary_is_json_serialisable() -> None:
    import json

    json.dumps(summarise({"emp-1": [recompute()]}).as_dict())


def test_the_paid_line_items_are_not_modified() -> None:
    """Nothing in this module writes to a payslip, including in memory."""
    original = [dict(item) for item in PAID_MARCH]
    recompute()
    assert original == PAID_MARCH


def test_component_types_survive_the_round_trip() -> None:
    """A deduction arrear classified as an earning would be paid out rather
    than deducted."""
    result = recompute(historical_rule_sets={"pf": pf_rules(21_000)})
    by_code = {d.code: d.component_type for d in result.diffs}

    assert by_code["BASIC"] == "earning"
    assert by_code["PF_EMPLOYEE"] == "deduction"
    assert by_code["PF_EMPLOYER"] == "employer_contribution"
    assert ComponentType.DEDUCTION.value == "deduction"
