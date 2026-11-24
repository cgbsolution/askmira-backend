"""The ₹25,000 PF ceiling (S.O. 5109(E), from 17 Sep 2026) and ESI on PF wages.

Figures follow a real October 2026 register: Basic 16,335, HRA 6,534, Special
4,999 and Bonus 1,361 paid PF 1,960 and ESI 123 although gross is 29,229,
because ESI is charged on the same wages as PF (Basic); Basic 45,256 paid PF
3,000 at the new ceiling.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models.payroll import CalculationType
from app.services.payroll.engine import PayrollInput, compute_payroll
from app.services.payroll.rule_sets import rule_problems
from app.services.payroll.salary_structure import resolve_ctc
from tests.payroll.conftest import component, employee_salary_from, rule_set, structure

PF25 = {
    "wage_ceiling": 25000,
    "employee_rate": 12,
    "employer_rate": 12,
    "eps_wage_ceiling": 25000,
    "edli_wage_ceiling": 25000,
    "eps_rate": 8.33,
    "apply_wage_ceiling": True,
    "contribution_rounding": "nearest_rupee",
}
ESI = {
    "wage_threshold": 21000,
    "employee_rate": "0.75",
    "employer_rate": "3.25",
    "contribution_rounding": "next_rupee",
}


def cgb_salary(basic: str, hra: str, special: str, bonus: str):
    def yearly(v: str) -> str:
        return str(Decimal(v) * 12)

    shape = structure(
        component(
            "BASIC",
            value=yearly(basic),
            counts_toward_pf=True,
            counts_toward_esi=True,
            display_order=1,
        ),
        component("HRA", value=yearly(hra), counts_toward_esi=True, display_order=2),
        component("BONUS", value=yearly(bonus), display_order=4),
        component(
            "SPECIAL",
            calculation_type=CalculationType.BALANCE,
            counts_toward_esi=True,
            display_order=9,
        ),
    )
    monthly = sum(Decimal(v) for v in (basic, hra, special, bonus))
    return employee_salary_from(resolve_ctc(shape, monthly * 12), monthly * 12)


def month(salary, esi_rules: dict, *, not_employed: int = 0, lop: int = 0):
    return compute_payroll(
        PayrollInput(
            employee_salary=salary,
            period_start=date(2026, 11, 1),
            period_end=date(2026, 11, 30),
            working_days=Decimal(30),
            not_employed_days=Decimal(not_employed),
            lop_days=Decimal(lop),
            rule_sets={"pf": rule_set("pf", PF25), "esi": rule_set("esi", esi_rules)},
        )
    )


def test_pf_is_capped_at_25000_not_15000() -> None:
    result = month(cgb_salary("45256", "18103", "21727", "0"), ESI)
    assert result.pf_employee == Decimal("3000.00")
    assert result.pf_employer == Decimal("3000.00")


def test_pf_below_the_new_ceiling_is_on_the_whole_basic() -> None:
    result = month(cgb_salary("16335", "6534", "4999", "1361"), ESI)
    assert result.pf_employee == Decimal("1960.00")


def test_esi_on_pf_wages_covers_a_gross_above_21000_when_basic_is_below() -> None:
    salary = cgb_salary("16335", "6534", "4999", "1361")
    result = month(salary, {**ESI, "wage_base": "pf_wages"})
    assert result.esi_employee == Decimal("123.00")  # 0.75% of 16,335, rounded up
    assert result.esi_employer == Decimal("531.00")  # 3.25% of 16,335, rounded up
    assert result.esi_wage_base == Decimal("16335.00")


def test_without_the_key_esi_stays_on_the_flagged_wage_types() -> None:
    # Basic + HRA + Special = 27,868, over 21,000: not covered.
    result = month(cgb_salary("16335", "6534", "4999", "1361"), ESI)
    assert result.esi_employee == Decimal("0")


def test_esi_on_pf_wages_still_stops_above_the_threshold() -> None:
    result = month(cgb_salary("26624", "0", "10649", "1749"), {**ESI, "wage_base": "pf_wages"})
    assert result.esi_employee == Decimal("0")


def test_esi_on_pf_wages_charges_the_whole_basic_not_the_pf_ceiling() -> None:
    # PF is capped; ESI wages are the full PF wage base, never the PF ceiling.
    result = month(cgb_salary("20375", "8150", "6236", "1697"), {**ESI, "wage_base": "pf_wages"})
    assert result.pf_employee == Decimal("2445.00")
    assert result.esi_employee == Decimal("153.00")


@pytest.mark.parametrize("value", ["pf_wages", "esi_wage_types"])
def test_the_wage_base_choices_are_accepted(value: str) -> None:
    assert rule_problems("esi", {**ESI, "wage_base": value}) == []


def test_a_misspelt_wage_base_is_refused() -> None:
    problems = rule_problems("esi", {**ESI, "wage_base": "basic"})
    assert problems and "wage_base" in problems[0]


def test_a_late_joiner_above_the_ceiling_is_not_covered_by_a_part_month() -> None:
    # Joined on the 24th at Basic 50,000: 7 days' Basic (11,667) is under 21,000,
    # but ESI coverage follows the rate of wages for a full month.
    result = month(
        cgb_salary("50000", "20000", "24346", "0"),
        {**ESI, "wage_base": "pf_wages"},
        not_employed=23,
    )
    assert result.esi_employee == Decimal("0")
    assert result.esi_employer == Decimal("0")
    assert result.esi_eligibility_base == Decimal("50000.00")


def test_a_late_joiner_below_the_ceiling_pays_on_what_is_paid() -> None:
    # Basic 16,335 is covered; 15 of 30 days paid, so ESI is on 8,167.50.
    result = month(
        cgb_salary("16335", "6534", "4999", "1361"),
        {**ESI, "wage_base": "pf_wages"},
        not_employed=15,
    )
    assert result.esi_employee == Decimal("62.00")  # 0.75% of 8,167.50, rounded up
    assert result.esi_wage_base == Decimal("8167.50")


def test_loss_of_pay_does_not_bring_a_high_earner_under_esi() -> None:
    result = month(cgb_salary("26624", "0", "10649", "1749"), ESI, lop=20)
    assert result.esi_employee == Decimal("0")
