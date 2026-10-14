"""The payroll journal: it balances, it refuses unmapped lines, and Tally gets its signs.

What these hold:

- every payslip amount posts to the side its nature says, and the journal
  balances because net pay is earnings less deductions;
- an unmapped line stops the journal, naming the code, rather than inventing a
  ledger;
- a negative employer contribution (an arrear refund) posts to the other side;
- Tally debits are negative and "deemed positive", credits positive;
- departments become cost centres without unbalancing anything.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from xml.etree import ElementTree

import pytest

from app.models.accounting import LedgerMapping, LedgerNature
from app.models.payroll import Payslip
from app.services.payroll.journal import (
    VoucherMeta,
    build_journal,
    postings_for,
    serialise,
)

META = VoucherMeta(
    voucher_number="PAY-2026-04-abc",
    voucher_date=date(2026, 4, 30),
    narration="Payroll for 04/2026",
    company_name="Acme & Sons",
)


def payslip(**overrides) -> Payslip:
    values = {
        "line_items": [
            {"code": "BASIC", "name": "Basic", "type": "earning", "amount": "40000.00"},
            {"code": "SPECIAL", "name": "Special", "type": "earning", "amount": "60000.00"},
            {"code": "PF_EMPLOYEE", "name": "PF", "type": "deduction", "amount": "1800.00"},
            {"code": "PF_EMPLOYER", "name": "PF (er)", "type": "employer_contribution",
             "amount": "1800.00"},
        ],
        "input_lines": [
            {"code": "OVERTIME", "name": "OT", "kind": "earning", "amount": "500.00"},
            {"code": "LOAN_EMI", "name": "Loan", "kind": "recovery", "amount": "2000.00"},
            {"code": "SKIPPED", "name": "Held", "kind": "recovery", "amount": "900.00",
             "skipped": True},
            {"code": "PERQ", "name": "Perquisite", "kind": "perquisite", "amount": "300.00"},
        ],
        "tds": Decimal("10000.00"),
        "net_pay": Decimal("86700.00"),
    }
    values.update(overrides)
    return Payslip(**values)


def mapping(code: str, nature: LedgerNature, debit: str | None, credit: str | None):
    return LedgerMapping(line_code=code, nature=nature, debit_ledger=debit, credit_ledger=credit)


MAPPINGS = {
    "BASIC": mapping("BASIC", LedgerNature.EARNING, "Salaries", None),
    "SPECIAL": mapping("SPECIAL", LedgerNature.EARNING, "Salaries", None),
    "OVERTIME": mapping("OVERTIME", LedgerNature.EARNING, "Overtime wages", None),
    "PF_EMPLOYEE": mapping("PF_EMPLOYEE", LedgerNature.DEDUCTION, None, "PF payable"),
    "PF_EMPLOYER": mapping(
        "PF_EMPLOYER", LedgerNature.EMPLOYER_CONTRIBUTION, "Employer PF", "PF payable"
    ),
    "LOAN_EMI": mapping("LOAN_EMI", LedgerNature.DEDUCTION, None, "Staff loans"),
    "TDS": mapping("TDS", LedgerNature.DEDUCTION, None, "TDS payable"),
    "NET_PAY": mapping("NET_PAY", LedgerNature.NET_PAY, None, "Salary payable"),
}


def by_ledger(lines):
    return {(line.ledger, line.cost_centre): (line.debit, line.credit) for line in lines}


def test_the_journal_balances_and_posts_each_side() -> None:
    lines, missing = build_journal(postings_for(payslip(), None), MAPPINGS)
    assert missing == []
    ledgers = by_ledger(lines)
    assert ledgers[("Salaries", None)] == (Decimal("100000.00"), Decimal(0))
    assert ledgers[("Overtime wages", None)] == (Decimal("500.00"), Decimal(0))
    assert ledgers[("Employer PF", None)] == (Decimal("1800.00"), Decimal(0))
    assert ledgers[("PF payable", None)] == (Decimal(0), Decimal("3600.00"))
    assert ledgers[("TDS payable", None)] == (Decimal(0), Decimal("10000.00"))
    assert ledgers[("Staff loans", None)] == (Decimal(0), Decimal("2000.00"))
    assert ledgers[("Salary payable", None)] == (Decimal(0), Decimal("86700.00"))
    assert sum(line.debit for line in lines) == sum(line.credit for line in lines)


def test_a_skipped_recovery_and_a_perquisite_post_nothing() -> None:
    codes = {p.line_code for p in postings_for(payslip(), None)}
    assert "SKIPPED" not in codes and "PERQ" not in codes


def test_an_unmapped_line_stops_the_journal() -> None:
    partial = {k: v for k, v in MAPPINGS.items() if k != "OVERTIME"}
    lines, missing = build_journal(postings_for(payslip(), None), partial)
    assert lines == [] and missing == ["OVERTIME"]


def test_an_employer_contribution_needs_both_ledgers() -> None:
    broken = {
        **MAPPINGS,
        "PF_EMPLOYER": mapping("PF_EMPLOYER", LedgerNature.EMPLOYER_CONTRIBUTION, "Employer PF",
                               None),
    }
    _, missing = build_journal(postings_for(payslip(), None), broken)
    assert missing == ["PF_EMPLOYER"]


def test_a_journal_that_does_not_add_up_is_refused() -> None:
    with pytest.raises(ValueError, match="does not balance"):
        build_journal(postings_for(payslip(net_pay=Decimal("90000.00")), None), MAPPINGS)


def test_a_negative_employer_contribution_posts_to_the_other_side() -> None:
    refund = payslip(
        line_items=[
            {"code": "BASIC", "name": "Basic", "type": "earning", "amount": "1000.00"},
        ],
        input_lines=[
            {"code": "PF_ER_ARREAR", "name": "Refund", "kind": "employer_contribution",
             "amount": "-100.00"},
        ],
        tds=Decimal(0),
        net_pay=Decimal("1000.00"),
    )
    mappings = {
        **MAPPINGS,
        "PF_ER_ARREAR": mapping("PF_ER_ARREAR", LedgerNature.EMPLOYER_CONTRIBUTION,
                                "Employer PF", "PF payable"),
    }
    lines, _ = build_journal(postings_for(refund, None), mappings)
    ledgers = by_ledger(lines)
    assert ledgers[("Employer PF", None)] == (Decimal(0), Decimal("100.00"))
    assert ledgers[("PF payable", None)] == (Decimal("100.00"), Decimal(0))


def test_cost_centres_split_lines_without_unbalancing() -> None:
    postings = postings_for(payslip(), "Engineering") + postings_for(payslip(), "Sales")
    lines, _ = build_journal(postings, MAPPINGS)
    assert ("Salaries", "Engineering") in by_ledger(lines)
    assert ("Salaries", "Sales") in by_ledger(lines)
    assert sum(line.debit for line in lines) == sum(line.credit for line in lines)


def test_tally_debits_are_negative_and_deemed_positive() -> None:
    lines, _ = build_journal(postings_for(payslip(), None), MAPPINGS)
    content, media_type, extension = serialise("tally", lines, META)
    assert media_type == "application/xml" and extension == "xml"
    root = ElementTree.fromstring(content)
    entries = {
        entry.findtext("LEDGERNAME"): (
            entry.findtext("ISDEEMEDPOSITIVE"),
            Decimal(entry.findtext("AMOUNT")),
        )
        for entry in root.iter("ALLLEDGERENTRIES.LIST")
    }
    assert entries["Salaries"] == ("Yes", Decimal("-100000.00"))
    assert entries["Salary payable"] == ("No", Decimal("86700.00"))
    assert sum(amount for _, amount in entries.values()) == 0
    assert root.findtext(".//SVCURRENTCOMPANY") == "Acme & Sons"


def test_the_csv_formats_carry_every_line() -> None:
    lines, _ = build_journal(postings_for(payslip(), None), MAPPINGS)
    generic, _, _ = serialise("csv", lines, META)
    zoho, _, _ = serialise("zoho", lines, META)
    assert generic.splitlines()[0].startswith("Date,Voucher number,Ledger")
    assert len(generic.strip().splitlines()) == len(lines) + 1
    assert zoho.splitlines()[0].startswith("Journal Date,Journal Number")
    assert len(zoho.strip().splitlines()) == len(lines) + 1
