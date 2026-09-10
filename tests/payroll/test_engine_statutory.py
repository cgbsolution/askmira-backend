"""Slice 09 gate: the new statutory heads as the payroll engine applies them.

`test_statutory.py` proves the arithmetic in isolation. This proves the engine
wires it in — that LWF lands on the payslip in December and not in November,
that VPF comes out of the employee and not the employer, that ESI honours the
contribution period, and that an unresolved Professional Tax state flags rather
than guesses.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models.payroll import CalculationType, ComponentType
from app.services.payroll.engine import PayrollInput, PayrollRuleError, compute_payroll
from app.services.payroll.salary_structure import resolve_ctc
from app.services.payroll.statutory import PfElection
from tests.payroll.conftest import component, employee_salary_from, rule_set, structure

WORKING_DAYS = Decimal(30)


def salary(ctc: str = "1200000"):
    shape = structure(
        component("BASIC", calculation_type=CalculationType.PERCENTAGE_OF, base="CTC",
                  value="40", counts_toward_pf=True, counts_toward_esi=True, display_order=1),
        component("HRA", calculation_type=CalculationType.PERCENTAGE_OF, base="BASIC",
                  value="50", counts_toward_esi=True, display_order=2),
        component("SPECIAL", calculation_type=CalculationType.BALANCE,
                  counts_toward_esi=True, display_order=9),
    )
    breakdown = resolve_ctc(shape, Decimal(ctc))
    return employee_salary_from(breakdown, Decimal(ctc))


def make_input(employee_salary, *, month: int = 4, rules=None, **kwargs):
    import calendar

    last = calendar.monthrange(2026, month)[1]
    return PayrollInput(
        employee_salary=employee_salary,
        period_start=date(2026, month, 1),
        period_end=date(2026, month, last),
        working_days=WORKING_DAYS,
        rule_sets=rules or {},
        **kwargs,
    )


@pytest.fixture
def lwf_rules():
    """A state collecting a flat amount in December only."""
    return rule_set(
        "labour_welfare_fund",
        {
            "applicable": True,
            "deduction_months": [12],
            "employee_amount": 20,
            "employer_amount": 40,
        },
        jurisdiction="KA",
    )


@pytest.fixture
def esi_period_rules():
    return rule_set(
        "esi",
        {
            "wage_threshold": 21000,
            "employee_rate": "0.75",
            "employer_rate": "3.25",
            "contribution_periods": [
                {"start_month": 4, "end_month": 9, "label": "APR-SEP"},
                {"start_month": 10, "end_month": 3, "label": "OCT-MAR"},
            ],
        },
    )


# ═══════════════════════════════════════════════════════════════════════════
# Labour Welfare Fund on the payslip
# ═══════════════════════════════════════════════════════════════════════════


def test_lwf_is_zero_in_a_non_deduction_month(pf_rules, lwf_rules) -> None:
    """The checklist gate, at the payslip level: zero, not a twelfth."""
    result = compute_payroll(
        make_input(salary(), month=11, rules={"pf": pf_rules, "labour_welfare_fund": lwf_rules})
    )
    assert result.lwf_employee == Decimal(0)
    assert result.lwf_employer == Decimal(0)
    assert not [line for line in result.lines if line.code.startswith("LWF")]


def test_lwf_appears_in_its_deduction_month(pf_rules, lwf_rules) -> None:
    result = compute_payroll(
        make_input(salary(), month=12, rules={"pf": pf_rules, "labour_welfare_fund": lwf_rules})
    )
    assert result.lwf_employee == Decimal(20)
    assert result.lwf_employer == Decimal(40)

    codes = {line.code for line in result.lines}
    assert {"LWF_EMPLOYEE", "LWF_EMPLOYER"} <= codes


def test_the_employee_lwf_reduces_net_pay_and_the_employer_share_does_not(
    pf_rules, lwf_rules
) -> None:
    without = compute_payroll(make_input(salary(), month=12, rules={"pf": pf_rules}))
    with_lwf = compute_payroll(
        make_input(salary(), month=12, rules={"pf": pf_rules, "labour_welfare_fund": lwf_rules})
    )

    assert with_lwf.net_pay == without.net_pay - Decimal(20)
    assert with_lwf.employer_cost == without.employer_cost + Decimal(40)


def test_the_annual_lwf_total_is_the_levy_not_twelve_times_it(pf_rules, lwf_rules) -> None:
    """Twelve months of an annual 20-rupee levy is 20 rupees."""
    total = sum(
        compute_payroll(
            make_input(
                salary(), month=month, rules={"pf": pf_rules, "labour_welfare_fund": lwf_rules}
            )
        ).lwf_employee
        for month in range(1, 13)
    )
    assert total == Decimal(20)


def test_an_unapproved_lwf_rule_set_is_refused(pf_rules) -> None:
    unapproved = rule_set(
        "labour_welfare_fund",
        {"applicable": True, "deduction_months": [12], "employee_amount": 20},
        jurisdiction="KA",
        approved=False,
    )
    with pytest.raises(PayrollRuleError, match="not been approved"):
        compute_payroll(
            make_input(
                salary(), month=12, rules={"pf": pf_rules, "labour_welfare_fund": unapproved}
            )
        )


def test_a_malformed_lwf_config_names_what_is_missing(pf_rules) -> None:
    """It surfaces as a payroll rule error, so the run records it against this
    employee rather than aborting for everyone."""
    broken = rule_set(
        "labour_welfare_fund", {"applicable": True}, jurisdiction="KA"
    )
    with pytest.raises(PayrollRuleError, match="names no deduction months"):
        compute_payroll(
            make_input(salary(), month=12, rules={"pf": pf_rules, "labour_welfare_fund": broken})
        )


# ═══════════════════════════════════════════════════════════════════════════
# VPF and the PF ceiling election
# ═══════════════════════════════════════════════════════════════════════════


def test_vpf_comes_out_of_the_employee_and_never_the_employer(pf_rules) -> None:
    """Treating VPF as a matched contribution silently inflates employer cost."""
    base = compute_payroll(make_input(salary(), rules={"pf": pf_rules}))
    with_vpf = compute_payroll(
        make_input(
            salary(), rules={"pf": pf_rules}, pf_election=PfElection(vpf_amount=Decimal(5_000))
        )
    )

    assert with_vpf.vpf == Decimal(5_000)
    assert with_vpf.pf_employer == base.pf_employer
    assert with_vpf.employer_cost == base.employer_cost
    assert with_vpf.net_pay == base.net_pay - Decimal(5_000)


def test_a_vpf_percentage_is_taken_on_the_full_wage_base_not_the_ceiling(pf_rules) -> None:
    """PF is capped at 15,000; the employee's own 10% is on their real wage."""
    result = compute_payroll(
        make_input(
            salary(), rules={"pf": pf_rules}, pf_election=PfElection(vpf_percent=Decimal(10))
        )
    )
    assert result.pf_wage_base == Decimal("40000.00")
    assert result.vpf == Decimal("4000.00"), "10% of 40,000, not of the 15,000 ceiling"


def test_electing_pf_on_actual_wages_raises_both_contributions(pf_rules) -> None:
    capped = compute_payroll(make_input(salary(), rules={"pf": pf_rules}))
    actual = compute_payroll(
        make_input(
            salary(), rules={"pf": pf_rules}, pf_election=PfElection(on_actual_wage=True)
        )
    )

    assert capped.pf_employee == Decimal("1800.00"), "12% of the 15,000 ceiling"
    assert actual.pf_employee == Decimal("4800.00"), "12% of 40,000"
    # Unlike VPF, this one moves the employer's side too.
    assert actual.pf_employer > capped.pf_employer


def test_the_vpf_line_is_recorded_separately_from_the_statutory_pf(pf_rules) -> None:
    result = compute_payroll(
        make_input(
            salary(), rules={"pf": pf_rules}, pf_election=PfElection(vpf_amount=Decimal(2_000))
        )
    )
    codes = [line.code for line in result.lines]
    assert "VPF" in codes
    assert "PF_EMPLOYEE" in codes

    vpf_line = next(line for line in result.lines if line.code == "VPF")
    assert vpf_line.component_type is ComponentType.DEDUCTION


def test_no_election_produces_no_vpf_line(pf_rules) -> None:
    result = compute_payroll(make_input(salary(), rules={"pf": pf_rules}))
    assert result.vpf == Decimal(0)
    assert "VPF" not in [line.code for line in result.lines]


# ═══════════════════════════════════════════════════════════════════════════
# ESI contribution periods
# ═══════════════════════════════════════════════════════════════════════════


def test_esi_continues_after_a_mid_period_raise(pf_rules, esi_period_rules) -> None:
    """The checklist gate, at the payslip level.

    A CTC of 30,00,000 puts the ESI wage base far above the ceiling. Because
    this employee was contributing when the period opened in April, July still
    deducts.
    """
    result = compute_payroll(
        make_input(
            salary("3000000"),
            month=7,
            rules={"pf": pf_rules, "esi": esi_period_rules},
            esi_contributing_at_period_start=True,
        )
    )
    assert result.esi_employee > Decimal(0)
    assert result.esi_employer > Decimal(0)


def test_esi_stops_at_the_next_period_boundary(pf_rules, esi_period_rules) -> None:
    result = compute_payroll(
        make_input(
            salary("3000000"),
            month=10,
            rules={"pf": pf_rules, "esi": esi_period_rules},
            esi_contributing_at_period_start=False,
        )
    )
    assert result.esi_employee == Decimal(0)
    assert result.esi_employer == Decimal(0)


def test_a_wage_cut_does_not_start_esi_mid_period(pf_rules, esi_period_rules) -> None:
    result = compute_payroll(
        make_input(
            salary("240000"),
            month=7,
            rules={"pf": pf_rules, "esi": esi_period_rules},
            esi_contributing_at_period_start=False,
        )
    )
    assert result.esi_wage_base < Decimal(21_000), "within the ceiling"
    assert result.esi_employee == Decimal(0), "but coverage begins at the next period"


def test_without_a_coverage_record_esi_falls_back_to_this_month(
    pf_rules, esi_period_rules
) -> None:
    """A first month, or a tenant that has not started tracking coverage.

    Deciding from this month's wages is correct at a period boundary and is the
    only thing that can honestly be inferred without a record.
    """
    within = compute_payroll(
        make_input(salary("240000"), month=7, rules={"pf": pf_rules, "esi": esi_period_rules})
    )
    above = compute_payroll(
        make_input(salary("3000000"), month=7, rules={"pf": pf_rules, "esi": esi_period_rules})
    )
    assert within.esi_employee > Decimal(0)
    assert above.esi_employee == Decimal(0)


def test_a_rule_set_without_periods_keeps_the_simple_threshold_test(
    pf_rules, esi_rules
) -> None:
    """Backwards compatible: a tenant whose SME has not supplied contribution
    periods still gets the plain ceiling test rather than an error."""
    result = compute_payroll(
        make_input(
            salary("3000000"),
            month=7,
            rules={"pf": pf_rules, "esi": esi_rules},
            esi_contributing_at_period_start=True,
        )
    )
    assert result.esi_employee == Decimal(0), "above the ceiling, and no period rule to apply"


# ═══════════════════════════════════════════════════════════════════════════
# Multi-state Professional Tax
# ═══════════════════════════════════════════════════════════════════════════


def test_an_unresolved_pt_state_flags_and_deducts_nothing(pf_rules, pt_rules) -> None:
    """The checklist gate: flagged, not guessed.

    The rule set is present and would happily produce a number. It is not used,
    because the state this employee belongs to for this period is not settled.
    """
    result = compute_payroll(
        make_input(
            salary(),
            rules={"pf": pf_rules, "professional_tax": pt_rules},
            state_review="EMP001: work location changed during the period",
        )
    )
    assert result.professional_tax == Decimal(0)
    assert result.review_flags == ["EMP001: work location changed during the period"]
    assert "PT" not in [line.code for line in result.lines]


def test_a_resolved_pt_state_deducts_normally(pf_rules, pt_rules) -> None:
    result = compute_payroll(
        make_input(salary(), rules={"pf": pf_rules, "professional_tax": pt_rules})
    )
    assert result.professional_tax == Decimal(200)
    assert result.review_flags == []


def test_a_clean_payslip_carries_no_review_flags(pf_rules) -> None:
    result = compute_payroll(make_input(salary(), rules={"pf": pf_rules}))
    assert result.review_flags == []


# ═══════════════════════════════════════════════════════════════════════════
# The heads together
# ═══════════════════════════════════════════════════════════════════════════


def test_total_deductions_account_for_every_head(pf_rules, pt_rules, lwf_rules) -> None:
    """A head that is computed but left out of the total silently overpays."""
    result = compute_payroll(
        make_input(
            salary(),
            month=12,
            rules={"pf": pf_rules, "professional_tax": pt_rules, "labour_welfare_fund": lwf_rules},
            pf_election=PfElection(vpf_amount=Decimal(3_000)),
        )
    )
    expected = (
        result.pf_employee
        + result.vpf
        + result.esi_employee
        + result.professional_tax
        + result.lwf_employee
        + result.tds
    )
    assert result.total_deductions == expected
    assert result.net_pay == result.gross_earnings - result.total_deductions


def test_the_computation_stays_deterministic(pf_rules, lwf_rules) -> None:
    def run():
        return compute_payroll(
            make_input(
                salary(),
                month=12,
                rules={"pf": pf_rules, "labour_welfare_fund": lwf_rules},
                pf_election=PfElection(vpf_percent=Decimal("2.5")),
            )
        )

    first, second = run(), run()
    assert first.net_pay == second.net_pay
    assert first.lwf_employee == second.lwf_employee
    assert first.vpf == second.vpf
