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
    ("kind", "tds", "ok"),
    [
        (C, Decimal("1000"), True),
        (C, None, True),
        (EmploymentType.FULL_TIME, Decimal("1000"), False),  # only a consultant has a fixed TDS
        (EmploymentType.FULL_TIME, None, True),
    ],
)
def test_consultant_billing_rules(kind, tds, ok) -> None:
    problem = consultant_billing_problem(
        kind,
        gst_applicable=False,
        gst_rate=None,
        gstin=None,
        tds_section=None,
        route=None,
        contact_name=None,
        contact_email=None,
        fixed_tds_amount=tds,
    )
    assert (problem is None) is ok


def test_create_refuses_gst_without_a_gstin() -> None:
    with pytest.raises(ValidationError, match="GSTIN"):
        EmployeeCreate(
            employee_code="C1",
            first_name="A",
            work_email="a@example.com",
            employment_type=C,
            gst_applicable=True,
        )
