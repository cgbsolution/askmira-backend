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
    {
        "code": "PF_EMPLOYEE",
        "name": "Provident Fund (employee)",
        "type": "deduction",
        "amount": "1800.00",
    },
    {
        "code": "PF_EMPLOYER",
        "name": "Provident Fund (employer)",
        "type": "employer_contribution",
        "amount": "1800.00",
    },
    {"code": "PT", "name": "Professional Tax", "type": "deduction", "amount": "200.00"},
]
INPUTS = [
    {
        "code": "EXPENSE_REIMB",
        "name": "Expense reimbursement: Taxi",
        "kind": "reimbursement",
        "amount": "1250.00",
        "skipped": False,
    },
    {
        "code": "LOAN_EMI",
        "name": "Salary advance instalment",
        "kind": "recovery",
        "amount": "1000.00",
        "skipped": False,
    },
    {
        "code": "ADVANCE_RECOVERY",
        "name": "Travel advance recovery",
        "kind": "recovery",
        "amount": "0.00",
        "skipped": True,
    },
    {
        "code": "REWARD_PERQUISITE",
        "name": "Reward perquisite (non-cash)",
        "kind": "perquisite",
        "amount": "500.00",
        "skipped": False,
    },
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


def test_the_page_is_factohrs_salary_slip_and_escapes_what_it_prints() -> None:
    """The layout the user asked for on 2026-11-03: their old factoHR payslip."""
    page = payslip_html(
        document(
            pf_number="PYKRP00536030000010667",
            bank_name="HSBC BANK",
            bank_account="074112855006",
            department="IT",
            payment_mode="NEFT",
        )
    )
    assert "ACME &lt;TECHNOLOGIES&gt; PVT LTD" in page  # the name in capitals, escaped
    assert "<Technologies>" not in page
    assert "SALARY SLIP FOR THE MONTH OF SEP-26" in page
    for label, value in (
        ("Employee Name", "Meera Iyer"),
        ("Employee Code", "DEMO005"),
        ("Designation", "Engineer"),
        ("Department", "IT"),
        ("DOJ", "15-09-2026"),
        ("PAN", "ABCDE1234F"),
        ("PF A/c No", "PYKRP00536030000010667"),
        ("UAN", "100200300400"),
        ("Bank Name", "HSBC BANK"),
        ("Bank A/c No", "074112855006"),
        ("Payable Days", "30.0"),
        ("Net Paid Days", "16.00"),
    ):
        assert f">{label}</td>" in page and f": {value}</td>" in page, label
    for heading in ("EARNINGS", "Actual", "Arrears", "Amount", "DEDUCTIONS"):
        assert heading in page
    assert "Gross Earnings" in page and "Total Deductions" in page
    assert "INR Eighty Eight Thousand Two Hundred Fifty Only" in page
    assert "Mode of Payment : NEFT" in page
    assert "*This is a system generated payslip and does not require" in page
    # Names print in capitals, as factoHR's did.
    assert "EXPENSE REIMBURSEMENT: TAXI" in page and "INCOME TAX (TDS)" in page


def test_actual_is_the_full_month_rate_and_arrears_have_their_column() -> None:
    doc = document(
        monthly_rates={"BASIC": "75000.00", "SPECIAL": "112500.00"},
        input_lines=[
            *INPUTS,
            {
                "provider": "arrears",
                "code": "ARREARS",
                "name": "Salary arrears",
                "kind": "earning",
                "amount": "500.00",
                "skipped": False,
            },
        ],
        other_earnings="1750.00",
        net_pay="88750.00",
    )
    rows = {row.name: row for row in doc.earning_rows}
    assert (rows["Basic"].actual, rows["Basic"].amount) == (
        Decimal("75000.00"),
        Decimal("40000.00"),
    )
    assert rows["Salary arrears"].arrears == Decimal("500.00")
    assert rows["Salary arrears"].actual is None
    assert doc.total_actual == Decimal("187500.00")
    assert doc.total_arrears == Decimal("500.00")
    assert doc.total_earnings == Decimal("101750.00")


def test_an_imported_month_reads_its_rate_from_the_register_row() -> None:
    """factoHR's register holds each component twice: the rate, then what was earned."""
    from app.services.payroll.payslip_pdf import register_value

    details = {
        "cells": {
            "Basic Salary (Q)": "25833",
            "Basic Salary (V)": "23250",
            "Designation (E)": "Senior Software Engineer",
            "Department (F)": "IT",
        }
    }
    line = {
        "code": "BASIC",
        "name": "BASIC SALARY",
        "type": "earning",
        "amount": "23250",
        "source": {"column": "V", "header": "Basic Salary", "raw": "23250"},
    }
    doc = document(
        line_items=[line],
        input_lines=[],
        tds="0",
        gross_earnings="23250",
        other_earnings="0",
        total_deductions="0",
        net_pay="23250",
        import_details=details,
    )
    assert doc.earning_rows[0].actual == Decimal("25833")
    assert register_value(details, "Designation") == "Senior Software Engineer"
    assert register_value(details, "department") == "IT"
    assert register_value(details, "Grade") is None


def test_it_renders_to_a_pdf_with_the_rupee_sign_and_a_logo() -> None:
    import io

    from PIL import Image
    from pypdf import PdfReader

    logo = io.BytesIO()
    Image.new("RGBA", (600, 200), (0, 120, 200, 0)).save(logo, format="PNG")
    data = render_payslip_pdf(document(logo=logo.getvalue()))
    assert data[:5] == b"%PDF-"
    page = PdfReader(io.BytesIO(data)).pages[0]
    text = page.extract_text()
    assert "₹" in text  # ₹, from the bundled glyph
    assert "SALARY SLIP FOR THE MONTH OF SEP-26" in text
    assert len(page.images) == 1
    # Small enough for a year of payslips in one ZIP.
    assert len(data) < 60_000


def test_a_logo_that_is_not_an_image_is_left_out() -> None:
    data = render_payslip_pdf(document(logo=b"not an image"))
    assert data[:5] == b"%PDF-"


def test_a_person_on_several_register_rows_gets_the_main_rows_rate() -> None:
    """A main row and an arrears row: the line's source is a list, and the
    cells live per row. The rate is the main row's; nothing crashes."""
    details = {
        "cells": {},
        "row_details": [
            {
                "file": "Sep.xlsx",
                "sheet": "Arrears",
                "row": 9,
                "kind": "arrears",
                "cells": {
                    "Basic Salary (Q)": "0",
                    "Basic Salary (V)": "1000",
                    "Designation (E)": "",
                },
            },
            {
                "file": "Sep.xlsx",
                "sheet": "Salary Register",
                "row": 4,
                "kind": "main",
                "cells": {
                    "Basic Salary (Q)": "25833",
                    "Basic Salary (V)": "23250",
                    "Designation (E)": "Senior Software Engineer",
                },
            },
        ],
    }
    line = {
        "code": "BASIC",
        "name": "BASIC SALARY",
        "type": "earning",
        "amount": "24250",
        "source": [
            {
                "file": "Sep.xlsx",
                "sheet": "Salary Register",
                "row": 4,
                "column": "V",
                "header": "Basic Salary",
                "raw": "23250",
            },
            {
                "file": "Sep.xlsx",
                "sheet": "Arrears",
                "row": 9,
                "column": "V",
                "header": "Basic Salary",
                "raw": "1000",
            },
        ],
    }
    from app.services.payroll.payslip_pdf import register_value

    doc = document(
        line_items=[line],
        input_lines=[],
        tds="0",
        gross_earnings="24250",
        other_earnings="0",
        total_deductions="0",
        net_pay="24250",
        import_details=details,
    )
    assert doc.earning_rows[0].actual == Decimal("25833")
    assert register_value(details, "Designation") == "Senior Software Engineer"
    # Neither an odd source nor odd details stops the payslip.
    odd = dict(line, source="not a dict")
    assert (
        document(
            line_items=[odd],
            input_lines=[],
            tds="0",
            gross_earnings="24250",
            other_earnings="0",
            total_deductions="0",
            net_pay="24250",
            import_details=["x"],
        )
        .earning_rows[0]
        .actual
        is None
    )


def test_without_a_rate_on_record_a_full_month_shows_what_was_paid() -> None:
    """factoHR's 2023 registers had only the paid column. Full month: the rate is
    what was paid. Part month: left blank, never guessed."""
    line = {
        "code": "BASIC",
        "name": "BASIC SALARY",
        "type": "earning",
        "amount": "28192",
        "source": {"column": "M", "header": "BASIC SALARY", "raw": "28192"},
    }
    details = {"cells": {"BASIC SALARY (M)": "28192"}}
    kwargs = dict(
        line_items=[line],
        input_lines=[],
        tds="0",
        gross_earnings="28192",
        other_earnings="0",
        total_deductions="0",
        net_pay="28192",
        import_details=details,
    )
    full = document(working_days="30", paid_days="30", not_employed_days="0", **kwargs)
    assert full.earning_rows[0].actual == Decimal("28192")
    part = document(working_days="30", paid_days="27", not_employed_days="0", **kwargs)
    assert part.earning_rows[0].actual is None
