"""Slice 01 gate: LOP proration and the PF wage-base correction.

The PF test here is a regression guard. The previous implementation computed PF
on gross, which over-deducts for every employee whose structure has allowances
outside the PF wage base. If that regresses, this fails.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models.payroll import CalculationType, ComponentType
from app.services.payroll.engine import PayrollInput, PayrollRuleError, compute_payroll
from app.services.payroll.salary_structure import resolve_ctc
from tests.payroll.conftest import component, employee_salary_from, rule_set, structure

PERIOD_START = date(2026, 4, 1)
PERIOD_END = date(2026, 4, 30)
WORKING_DAYS = Decimal(30)


def salary_with_da(ctc: str = "1200000"):
    """BASIC + DA in the PF base, HRA and SPECIAL outside it.

    Chosen so gross and the PF wage base are clearly different numbers — the
    whole point of the regression.
    """
    shape = structure(
        component("BASIC", calculation_type=CalculationType.PERCENTAGE_OF, base="CTC",
                  value="40", counts_toward_pf=True, counts_toward_esi=True, display_order=1),
        component("DA", calculation_type=CalculationType.PERCENTAGE_OF, base="BASIC",
                  value="10", counts_toward_pf=True, counts_toward_esi=True, display_order=2),
        component("HRA", calculation_type=CalculationType.PERCENTAGE_OF, base="BASIC",
                  value="50", counts_toward_esi=True, display_order=3),
        component("SPECIAL", calculation_type=CalculationType.BALANCE,
                  counts_toward_esi=True, display_order=9),
    )
    breakdown = resolve_ctc(shape, Decimal(ctc))
    return employee_salary_from(breakdown, Decimal(ctc))


def make_input(salary, *, lop=Decimal(0), rules=None, working_days=WORKING_DAYS):
    return PayrollInput(
        employee_salary=salary,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        working_days=working_days,
        lop_days=lop,
        rule_sets=rules or {},
    )


# ── PF wage base ────────────────────────────────────────────────────────────


def test_pf_is_computed_on_basic_plus_da_not_on_gross(pf_rules) -> None:
    """The regression this slice exists to fix.

    CTC 1,200,000 → monthly BASIC 40,000, DA 4,000, HRA 20,000, SPECIAL 36,000.
    Gross is 100,000; the PF wage base is 44,000. With the statutory ceiling
    applied both would land on the ceiling, so the ceiling is switched off here
    to make the two numbers visibly different.
    """
    pf_rules.rules = {**pf_rules.rules, "apply_wage_ceiling": False}
    salary = salary_with_da()
    result = compute_payroll(make_input(salary, rules={"pf": pf_rules}))

    assert result.gross_earnings == Decimal("100000.00")
    assert result.pf_wage_base == Decimal("44000.00"), "PF base must be BASIC + DA only"

    # 12% of 44,000 = 5,280. On gross it would have been 12,000 — the old bug.
    assert result.pf_employee == Decimal("5280.00")
    assert result.pf_employee != Decimal("12000.00")


def test_pf_respects_the_statutory_wage_ceiling(pf_rules) -> None:
    """The ceiling comes from the rule set, never from a constant in the code."""
    salary = salary_with_da()
    result = compute_payroll(make_input(salary, rules={"pf": pf_rules}))

    # Base 44,000 is above the 15,000 ceiling, so PF is 12% of 15,000.
    assert result.pf_employee == Decimal("1800.00")
    assert result.pf_employer == Decimal("1800.00")


def test_pf_uses_the_full_base_when_the_ceiling_is_waived(pf_rules) -> None:
    pf_rules.rules = {**pf_rules.rules, "apply_wage_ceiling": False}
    salary = salary_with_da("600000")  # monthly BASIC 20,000 + DA 2,000 = 22,000
    result = compute_payroll(make_input(salary, rules={"pf": pf_rules}))

    assert result.pf_wage_base == Decimal("22000.00")
    assert result.pf_employee == Decimal("2640.00")


# ── LOP proration ───────────────────────────────────────────────────────────


def test_lop_prorates_only_components_flagged_for_it(pf_rules) -> None:
    """An employer contribution outside CTC proration must not be reduced."""
    shape = structure(
        component("BASIC", calculation_type=CalculationType.PERCENTAGE_OF, base="CTC",
                  value="50", counts_toward_pf=True, pro_rate_on_lop=True, display_order=1),
        component("GRATUITY_ER", name="Gratuity provision",
                  component_type=ComponentType.EMPLOYER_CONTRIBUTION,
                  calculation_type=CalculationType.FIXED, value="24000",
                  pro_rate_on_lop=False, display_order=2),
        component("SPECIAL", calculation_type=CalculationType.BALANCE, display_order=9),
    )
    salary = employee_salary_from(resolve_ctc(shape, Decimal("1200000")), Decimal("1200000"))

    full = compute_payroll(make_input(salary, rules={"pf": pf_rules}))
    with_lop = compute_payroll(
        make_input(salary, lop=Decimal(3), rules={"pf": pf_rules})
    )

    basic_full = next(line for line in full.lines if line.code == "BASIC").amount
    basic_lop = next(line for line in with_lop.lines if line.code == "BASIC").amount
    gratuity_full = next(line for line in full.lines if line.code == "GRATUITY_ER").amount
    gratuity_lop = next(line for line in with_lop.lines if line.code == "GRATUITY_ER").amount

    # 27/30 of 50,000 = 45,000
    assert basic_full == Decimal("50000.00")
    assert basic_lop == Decimal("45000.00")
    # Not prorated.
    assert gratuity_full == gratuity_lop == Decimal("2000.00")

    assert with_lop.paid_days == Decimal(27)
    assert with_lop.lop_days == Decimal(3)


def test_statutory_is_computed_on_prorated_amounts_not_full_ones(pf_rules) -> None:
    """Prorating the net after computing PF on the full amount is wrong."""
    pf_rules.rules = {**pf_rules.rules, "apply_wage_ceiling": False}
    salary = salary_with_da()

    full = compute_payroll(make_input(salary, rules={"pf": pf_rules}))
    half = compute_payroll(
        make_input(salary, lop=Decimal(15), rules={"pf": pf_rules})
    )

    assert half.pf_wage_base == full.pf_wage_base / 2
    assert half.pf_employee == full.pf_employee / 2


def test_lop_exceeding_working_days_is_rejected(pf_rules) -> None:
    salary = salary_with_da()
    with pytest.raises(PayrollRuleError, match="exceed working days"):
        compute_payroll(make_input(salary, lop=Decimal(31), rules={"pf": pf_rules}))


def test_zero_working_days_is_rejected(pf_rules) -> None:
    salary = salary_with_da()
    with pytest.raises(PayrollRuleError, match="greater than zero"):
        compute_payroll(make_input(salary, working_days=Decimal(0), rules={"pf": pf_rules}))


# ── Rule set gating ─────────────────────────────────────────────────────────


def test_payroll_refuses_to_run_without_a_pf_rule_set() -> None:
    salary = salary_with_da()
    with pytest.raises(PayrollRuleError, match="No PF rule set"):
        compute_payroll(make_input(salary))


def test_payroll_refuses_an_unapproved_rule_set() -> None:
    """The Compliance SME sign-off gate, enforced in code rather than process."""
    unapproved = rule_set(
        "pf",
        {"wage_ceiling": 15000, "employee_rate": 12, "employer_rate": 12},
        approved=False,
    )
    salary = salary_with_da()
    with pytest.raises(PayrollRuleError, match="not been approved by a Compliance SME"):
        compute_payroll(make_input(salary, rules={"pf": unapproved}))


def test_a_rule_set_missing_a_required_key_names_the_key(pf_rules) -> None:
    pf_rules.rules = {"employee_rate": 12}
    salary = salary_with_da()
    with pytest.raises(PayrollRuleError, match="wage_ceiling"):
        compute_payroll(make_input(salary, rules={"pf": pf_rules}))


# ── ESI and Professional Tax ────────────────────────────────────────────────


def test_esi_applies_below_the_threshold_only(pf_rules, esi_rules) -> None:
    low = salary_with_da("240000")  # monthly gross 20,000 — under 21,000
    high = salary_with_da("1200000")  # monthly gross 100,000 — over

    low_result = compute_payroll(
        make_input(low, rules={"pf": pf_rules, "esi": esi_rules})
    )
    high_result = compute_payroll(
        make_input(high, rules={"pf": pf_rules, "esi": esi_rules})
    )

    assert low_result.esi_employee > 0
    assert high_result.esi_employee == 0


def test_professional_tax_reads_the_state_slab(pf_rules, pt_rules) -> None:
    salary = salary_with_da()
    result = compute_payroll(
        make_input(salary, rules={"pf": pf_rules, "professional_tax": pt_rules})
    )
    # Monthly gross 100,000 falls in the open-ended top slab.
    assert result.professional_tax == Decimal("200.00")


def test_a_professional_tax_slab_table_with_no_open_bound_is_rejected(pf_rules) -> None:
    closed = rule_set(
        "professional_tax",
        {"slabs": [{"up_to": 15000, "amount": 150}]},
        jurisdiction="KA",
    )
    salary = salary_with_da()
    with pytest.raises(PayrollRuleError, match="open upper bound"):
        compute_payroll(
            make_input(salary, rules={"pf": pf_rules, "professional_tax": closed})
        )


# ── Totals ──────────────────────────────────────────────────────────────────


def test_net_pay_is_gross_less_deductions(pf_rules, pt_rules) -> None:
    salary = salary_with_da()
    result = compute_payroll(
        make_input(salary, rules={"pf": pf_rules, "professional_tax": pt_rules})
    )
    assert result.net_pay == result.gross_earnings - result.total_deductions


def test_tds_is_zero_rather_than_a_guess(pf_rules) -> None:
    """Income tax projection is slice 07.

    A wrong deduction from someone's salary is worse than no deduction, so this
    stays zero until the declarations module exists.
    """
    salary = salary_with_da()
    result = compute_payroll(make_input(salary, rules={"pf": pf_rules}))
    assert result.tds == Decimal(0)


def test_every_computed_line_carries_its_basis(pf_rules, pt_rules) -> None:
    """A payslip has to be explainable to the employee who receives it."""
    salary = salary_with_da()
    result = compute_payroll(
        make_input(salary, lop=Decimal(2), rules={"pf": pf_rules, "professional_tax": pt_rules})
    )
    assert all(line.basis for line in result.lines)

    pf_line = next(line for line in result.lines if line.code == "PF_EMPLOYEE")
    assert pf_line.rule_set_id == str(pf_rules.id), "a statutory line names the rule set used"
