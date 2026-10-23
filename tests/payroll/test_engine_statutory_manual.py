"""Statutory MANUAL rows of the Salary master, as the payroll engine applies them.

A TDS / PT / PF / ESIC / LWF MANUAL amount on a person's salary is deducted
instead of what the engine would work out. It is never paid as a line of its
own, and leaving it off changes nothing.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from app.models.payroll import ComponentType, EmployeeSalaryComponent
from app.services.payroll.engine import PayrollInput, compute_payroll
from tests.payroll.test_engine_statutory import WORKING_DAYS, salary


def _with_manual(employee_salary, code: str, monthly: str):
    employee_salary.components.append(
        EmployeeSalaryComponent(
            id=uuid.uuid4(),
            tenant_id=employee_salary.tenant_id,
            component_code=code,
            component_name=code.replace("_", " "),
            component_type=ComponentType.DEDUCTION,
            annual_amount=Decimal(monthly) * 12,
            monthly_amount=Decimal(monthly),
            taxable=False,
            part_of_ctc=False,
            pro_rate_on_lop=False,
            counts_toward_pf=False,
            counts_toward_esi=False,
            display_order=500,
            is_manual=True,
        )
    )
    return employee_salary


def _run(employee_salary, rules, **kwargs):
    return compute_payroll(
        PayrollInput(
            employee_salary=employee_salary,
            period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30),
            working_days=WORKING_DAYS,
            rule_sets=rules,
            **kwargs,
        )
    )


def test_without_manual_rows_nothing_changes(pf_rules, pt_rules) -> None:
    result = _run(salary(), {"pf": pf_rules, "professional_tax": pt_rules})
    assert result.professional_tax == Decimal("200")
    assert result.pf_employee == Decimal("1800")


def test_a_manual_pt_replaces_the_slab_and_is_not_a_line_of_its_own(pf_rules, pt_rules) -> None:
    result = _run(
        _with_manual(salary(), "PT_MANUAL", "0.01"), {"pf": pf_rules, "professional_tax": pt_rules}
    )
    assert result.professional_tax == Decimal("0.01")
    codes = [line.code for line in result.lines]
    assert "PT_MANUAL" not in codes
    pt_line = next(line for line in result.lines if line.code == "PT")
    assert pt_line.amount == Decimal("0.01")
    assert "salary master" in pt_line.basis
    baseline = _run(salary(), {"pf": pf_rules, "professional_tax": pt_rules})
    assert result.net_pay - baseline.net_pay == Decimal("199.99")


def test_a_manual_zero_pt_deducts_nothing(pf_rules, pt_rules) -> None:
    result = _run(
        _with_manual(salary(), "PT_MANUAL", "0"), {"pf": pf_rules, "professional_tax": pt_rules}
    )
    assert result.professional_tax == Decimal("0")
    assert not any(line.code == "PT" for line in result.lines)


def test_a_manual_pf_replaces_the_employee_share_only(pf_rules) -> None:
    result = _run(_with_manual(salary(), "PF_MANUAL", "2500"), {"pf": pf_rules})
    assert result.pf_employee == Decimal("2500")
    assert result.pf_employer == Decimal("1800")  # still 12% of capped wages
    line = next(line for line in result.lines if line.code == "PF_EMPLOYEE")
    assert line.amount == Decimal("2500") and "salary master" in line.basis


def test_manual_esi_and_lwf_apply_even_without_their_rules(pf_rules) -> None:
    employee_salary = _with_manual(_with_manual(salary(), "ESIC_MANUAL", "150"), "LWF_MANUAL", "20")
    result = _run(employee_salary, {"pf": pf_rules})
    assert result.esi_employee == Decimal("150")
    assert result.lwf_employee == Decimal("20")
    codes = {line.code: line.amount for line in result.lines}
    assert codes["ESI_EMPLOYEE"] == Decimal("150")
    assert codes["LWF_EMPLOYEE"] == Decimal("20")
    assert result.total_deductions == Decimal("1800") + Decimal("150") + Decimal("20")


def test_a_manual_tds_is_deducted_and_says_so(pf_rules) -> None:
    result = _run(_with_manual(salary(), "TDS_MANUAL", "4000"), {"pf": pf_rules})
    assert result.tds == Decimal("4000")
    assert result.tds_working is not None
    assert result.tds_working["manual_override"] == "4000.00"
