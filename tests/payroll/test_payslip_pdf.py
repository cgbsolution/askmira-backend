"""The payslip PDF: what is on the page, and that it adds up."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.payroll.payslip_pdf import (
    PayslipMismatchError,
    build_payslip_document,
    mask_identifier,
    payslip_html,
    render_payslip_pdf,
)

LINES = [
    {"code": "BASIC", "name": "Basic", "type": "earning", "amount": "40000.00"},
    {"code": "SPECIAL", "name": "Special allowance", "type": "earning", "amount": "60000.00"},
    {"code": "PF_EMPLOYEE", "name": "Provident Fund (employee)", "type": "deduction",
     "amount": "1800.00"},
    {"code": "PF_EMPLOYER", "name": "Provident Fund (employer)",
     "type": "employer_contribution", "amount": "1800.00"},
    {"code": "PT", "name": "Professional Tax", "type": "deduction", "amount": "200.00"},
]
INPUTS = [
    {"code": "EXPENSE_REIMB", "name": "Expense reimbursement: Taxi", "kind": "reimbursement",
     "amount": "1250.00", "skipped": False},
    {"code": "LOAN_EMI", "name": "Salary advance instalment", "kind": "recovery",
     "amount": "1000.00", "skipped": False},
    {"code": "ADVANCE_RECOVERY", "name": "Travel advance recovery", "kind": "recovery",
     "amount": "0.00", "skipped": True},
    {"code": "REWARD_PERQUISITE", "name": "Reward perquisite (non-cash)", "kind": "perquisite",
     "amount": "500.00", "skipped": False},
]


def document(**overrides):
    values = dict(
        company_name="Acme <Technologies> Pvt Ltd",
        company_address="12 MG Road, Bengaluru",
        period_year=2026,
        period_month=9,
        employee_name="Meera Iyer",
        employee_code="DEMO005",
        designation="Engineer",
        date_of_joining=date(2026, 9, 15),
        pan="ABCDE1234F",
        uan="100200300400",
        payment_date=date(2026, 9, 30),
        working_days="30",
        paid_days="16",
        lop_days="0",
        not_employed_days="14",
        line_items=LINES,
        input_lines=INPUTS,
        tds="10000.00",
        gross_earnings="100000.00",
        other_earnings="1250.00",
        total_deductions="13000.00",
        net_pay="88250.00",
    )
    values.update(overrides)
    return build_payslip_document(**values)


def test_the_page_adds_up_to_the_payslip() -> None:
    doc = document()
    assert doc.total_earnings == Decimal("101250.00")
    assert doc.total_deductions == Decimal("13000.00")
    assert doc.net_pay == Decimal("88250.00")
    assert ("Income tax (TDS)", Decimal("10000.00")) in doc.deductions
    assert ("Expense reimbursement: Taxi", Decimal("1250.00")) in doc.earnings
    # A skipped recovery took nothing and is not a deduction line.
    assert all("Travel advance" not in name for name, _ in doc.deductions)
    assert doc.employer_contributions == [("Provident Fund (employer)", Decimal("1800.00"))]
    assert doc.perquisites == [("Reward perquisite (non-cash)", Decimal("500.00"))]


def test_a_payslip_that_does_not_add_up_is_not_rendered() -> None:
    with pytest.raises(PayslipMismatchError):
        document(net_pay="90000.00")


def test_mask_identifier_still_keeps_the_last_four() -> None:
    assert mask_identifier("ABCDE1234F") == "XXXXXX234F"
    assert mask_identifier("100200300400") == "XXXXXXXX0400"
    assert mask_identifier(None) is None
    assert mask_identifier("  ") is None


def test_the_payslip_prints_pan_and_uan_in_full() -> None:
    doc = document()
    assert (doc.pan, doc.uan) == ("ABCDE1234F", "100200300400")
    page = payslip_html(doc)
    assert "ABCDE1234F" in page and "100200300400" in page
    assert "XXXXXX" not in page


def test_the_page_says_what_an_employee_needs_and_escapes_it() -> None:
    page = payslip_html(document())
    assert "Payslip for September 2026" in page
    assert "Rupees Eighty Eight Thousand Two Hundred Fifty Only" in page
    assert "88,250.00" in page
    assert "Not employed" in page and "14 day(s)" in page
    assert "computer-generated" in page
    assert "ABCDE1234F" in page  # in full since 2026-10-27
    assert "<Technologies>" not in page and "&lt;Technologies&gt;" in page


def test_it_renders_to_a_pdf() -> None:
    data = render_payslip_pdf(document())
    assert data[:5] == b"%PDF-"
