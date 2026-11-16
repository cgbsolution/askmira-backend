"""A consultant's GST and fixed TDS, typed on their record.

GST is paid on top of the fee as a cash line that is not income: it raises net
pay and nothing else. The fixed TDS replaces the projected salary tax on a
regular payslip; TDS MANUAL on the salary master still wins over it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.employee import EmploymentType
from app.schemas.employee import EmployeeCreate, consultant_billing_problem
from app.services.payroll.engine import InputKind, InputLine, SupplementaryContext
from tests.payroll.test_engine_statutory import salary
from tests.payroll.test_engine_statutory_manual import _run, _with_manual


def _gst(amount: str) -> InputLine:
    return InputLine(
        provider="consultant_gst",
        code="GST",
        name="GST",
        kind=InputKind.EARNING,
        amount=Decimal(amount),
        taxable_amount=Decimal(0),
        source={"type": "consultant_gst", "id": "x:2026-04", "period": "2026-04"},
    )


def test_gst_raises_net_pay_and_nothing_else(pf_rules, pt_rules) -> None:
    rules = {"pf": pf_rules, "professional_tax": pt_rules}
    baseline = _run(salary(), rules)
    result = _run(salary(), rules, inputs=(_gst("9000"),))
    assert result.net_pay - baseline.net_pay == Decimal("9000")
    assert result.gross_earnings == baseline.gross_earnings
    assert result.pf_employee == baseline.pf_employee
    assert result.professional_tax == baseline.professional_tax
    assert result.input_lines[0].taxable_amount == Decimal("0")


def test_fixed_tds_is_deducted_and_says_so(pf_rules) -> None:
    baseline = _run(salary(), {"pf": pf_rules})
    result = _run(salary(), {"pf": pf_rules}, fixed_tds=Decimal("5000"))
    assert result.tds == Decimal("5000.00")
    assert result.tds_working is not None
    assert result.tds_working["fixed_tds"] == "5000.00"
    assert baseline.net_pay - result.net_pay == Decimal("5000")


def test_tds_manual_on_the_salary_master_wins_over_fixed_tds(pf_rules) -> None:
    result = _run(
        _with_manual(salary(), "TDS_MANUAL", "4000"), {"pf": pf_rules}, fixed_tds=Decimal("5000")
    )
    assert result.tds == Decimal("4000")


def test_a_supplementary_payslip_does_not_take_fixed_tds_again(pf_rules) -> None:
    result = _run(
        salary(),
        {"pf": pf_rules},
        fixed_tds=Decimal("5000"),
        inputs=(_gst("100"),),
        supplementary=SupplementaryContext(
            regular_gross=Decimal("50000"), pt_already_deducted=Decimal(0), esi_covered=False
        ),
    )
    assert result.tds == Decimal("0")


# ── What the record accepts ─────────────────────────────────────────────────

C = EmploymentType.CONSULTANT


@pytest.mark.parametrize(
    ("kind", "applicable", "gst", "tds", "ok"),
    [
        (C, True, Decimal("1800"), Decimal("1000"), True),
        (C, False, None, Decimal("1000"), True),
        (C, False, None, None, True),
        (C, True, None, None, False),  # GST yes needs an amount
        (C, False, Decimal("1800"), None, False),  # an amount needs GST yes
        (EmploymentType.FULL_TIME, False, None, Decimal("1000"), False),
        (EmploymentType.FULL_TIME, False, None, None, True),
    ],
)
def test_consultant_billing_rules(kind, applicable, gst, tds, ok) -> None:
    assert (consultant_billing_problem(kind, applicable, gst, tds) is None) is ok


def test_create_refuses_gst_without_an_amount() -> None:
    with pytest.raises(ValidationError, match="GST amount"):
        EmployeeCreate(
            employee_code="C1",
            first_name="A",
            work_email="a@example.com",
            employment_type=C,
            gst_applicable=True,
        )
