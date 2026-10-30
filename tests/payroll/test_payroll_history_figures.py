"""factoHR payslip figures from register rows. Pure: no database.

Gates:
  * Lines come in register order, zero lines left out, TDS never a line.
  * The register's net, adjustment and rounding reach the paid amount through
    lines of their own; what is left over may only be paisa rounding.
  * A row that cannot be made to add up, and a payslip the PDF would refuse,
    are errors (the import does not run while any remains).
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import date
from decimal import Decimal

from app.models.employee import EmploymentStatus
from app.services.payroll import history_import
from app.services.payroll.history_parse import parse_register
from tests.payroll.payroll_history_books import FEB25, FNF, Sheet, book, person

D = Decimal


def _person(code: str = "CHD24-600") -> history_import.Person:
    return history_import.Person(
        id=uuid.uuid4(),
        code=code,
        name="Test Person",
        first_last="Test Person",
        status=EmploymentStatus.ACTIVE,
        joined=date(2024, 1, 1),
        exited=None,
        legal_entity_id=None,
        project_id=None,
    )


def _rows(**cells):
    row = person("CHD24-600", "Test Person", date(2024, 1, 1), ctc=30000, basic=12000, hra=4800,
                 special=10000, bonus=1000, workable=28, whole=True, tds=500, esi=0, **cells)
    sheet = Sheet("External", FEB25, [row], above=[["Salary Register  FEB- 2025"], None])
    return parse_register("Final Consolidated Salary Register Feb-2025.xlsx", book([sheet])).rows


def _slip(rows, issues):
    return history_import._build_payslip(
        _person(), rows, 2025, 2, projects={}, unmatched_accounts=Counter(), issues=issues
    )


def test_lines_follow_the_register_and_tds_is_only_the_payslip_tds() -> None:
    issues: list = []
    slip = _slip(_rows(), issues)
    assert [line["code"] for line in slip.line_items] == [
        "BASIC", "HRA", "SPECIAL", "BONUS", "PF_EMPLOYEE", "INSURANCE", "PT", "PF_EMPLOYER",
        "GRATUITY_PROVISION",
    ]
    assert slip.tds == D("500.00")
    assert slip.gross_earnings == D("27800.00")
    assert slip.total_deductions == D("1440") + D("239") + D("200") + D("500")
    assert slip.net_pay == slip.gross_earnings - slip.total_deductions
    assert slip.employer_cost == slip.gross_earnings + D("1560") + _q(D(12000) / 26 * 15 / 12)
    assert slip.input_lines == [] and not issues
    line = slip.line_items[0]
    assert line["source"] == {"column": "T", "header": "BASIC SALARY", "raw": "12000"}
    assert line["basis"] == (
        "As paid in factoHR — Feb 2025 register, Final Consolidated Salary Register "
        "Feb-2025.xlsx, row 4"
    )
    assert slip.pf_wage_base == D("12000.00") and slip.esi_wage_base == D("0.00")


def _q(value: Decimal) -> Decimal:
    return value.quantize(D("0.01"))


def test_a_hand_set_net_is_a_net_diff_line_and_the_bank_follows() -> None:
    issues: list = []
    rows = _rows(net=20000, bank=20000, rounded=20000)
    slip = _slip(rows, issues)
    lines = {line["code"]: D(line["amount"]) for line in slip.input_lines}
    assert lines == {"NET_DIFF": D("20000") - (slip.gross_earnings - slip.total_deductions)}
    assert slip.net_pay == D("20000.00") and not issues
    assert slip.input_lines[0]["taxable_amount"] == "0.00"


def test_a_row_that_cannot_reach_the_paid_amount_is_an_error(monkeypatch) -> None:
    # With the net check switched off, a net set 5 rupees off by hand has
    # nothing to explain it: more than paisa rounding is left over.
    monkeypatch.setattr(history_import, "_CELL_TOLERANCE", D("10"))
    issues: list = []
    base = _rows()[0]
    rows = _rows(net=base.net_salary + 5, bank=base.net_salary + 5,
                 rounded=base.net_salary + 5)
    slip = _slip(rows, issues)
    assert [i.category for i in issues] == ["row_does_not_add_up"]
    assert {line["code"] for line in slip.input_lines} == {"PAISA_ROUNDING"}


def test_a_payslip_the_pdf_would_refuse_is_an_error() -> None:
    issues: list = []
    slip = _slip(_rows(), issues)
    slip.net_pay += D("1.00")
    history_import._pdf_check(slip, "Acme", issues)
    assert [i.category for i in issues] == ["pdf_check"]


def test_an_fnf_row_without_a_divisor_works_on_paid_days() -> None:
    ff = person("CHD24-600", "Test Person", date(2024, 1, 1), ctc=18000, basic=7200, hra=2880,
                special=5510, bonus=600, worked=10)
    rows = parse_register(
        "Final Consolidated Salary Register Feb-2025.xlsx",
        book([
            Sheet("External", FEB25, [], above=[["Salary Register  FEB- 2025"], None]),
            Sheet("F&F", FNF, [ff], above=[None]),
        ]),
    ).rows
    issues: list = []
    slip = _slip(rows, issues)
    assert slip.working_days == D("10.00") and slip.paid_days == D("10.00")
    assert slip.not_employed_days == D("0.00")
    assert slip.kind == "fnf"
