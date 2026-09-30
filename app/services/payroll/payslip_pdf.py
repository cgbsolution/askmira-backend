"""A payslip as a PDF, for the employee to keep and HR to hand over.

Rendered with the letter engine's renderer (`app.services.documents.pdf`), from
the stored payslip — never recomputed. Every figure on the page is a figure on
the payslip row, and the page's totals are checked against the row's before it
is rendered: a PDF whose lines do not add up to the net pay it states is worse
than no PDF.

Statutory identifiers are masked to their last four characters. A payslip is
forwarded to landlords and banks; the full PAN and UAN do not need to travel
with it.
"""

from __future__ import annotations

import calendar
import html
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.services.documents.indian_numbers import amount_in_words, group_indian
from app.services.documents.pdf import Letterhead, render_pdf

__all__ = [
    "PayslipDocument",
    "PayslipMismatchError",
    "build_payslip_document",
    "mask_identifier",
    "payslip_html",
    "render_payslip_pdf",
]

ZERO = Decimal(0)
RUPEE = Decimal("0.01")

_CASH_IN = ("earning", "reimbursement")
_CASH_OUT = ("deduction", "recovery")


class PayslipMismatchError(ValueError):
    """The lines on the page do not add up to the payslip's own totals."""


def _d(value: Any) -> Decimal:
    return Decimal(str(value)) if value not in (None, "") else ZERO


def _money(value: Decimal) -> str:
    return group_indian(value.quantize(RUPEE, rounding=ROUND_HALF_UP))


def _days(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text


def mask_identifier(value: str | None, keep: int = 4) -> str | None:
    """`ABCDE1234F` → `XXXXXX234F`. `None` when there is nothing on record."""
    if not value or not value.strip():
        return None
    text = value.strip()
    if len(text) <= keep:
        return "X" * len(text)
    return "X" * (len(text) - keep) + text[-keep:]


@dataclass(frozen=True, slots=True)
class PayslipDocument:
    """Everything printed on the page, already checked to add up."""

    company_name: str
    company_address: str | None
    period_label: str
    employee_name: str
    employee_code: str
    designation: str | None
    date_of_joining: date | None
    pan_masked: str | None
    uan_masked: str | None
    payment_date: date | None
    working_days: Decimal
    paid_days: Decimal
    lop_days: Decimal
    not_employed_days: Decimal
    earnings: list[tuple[str, Decimal]]
    deductions: list[tuple[str, Decimal]]
    employer_contributions: list[tuple[str, Decimal]] = field(default_factory=list)
    perquisites: list[tuple[str, Decimal]] = field(default_factory=list)
    total_earnings: Decimal = ZERO
    total_deductions: Decimal = ZERO
    net_pay: Decimal = ZERO
    reference: str = ""


def build_payslip_document(
    *,
    company_name: str,
    company_address: str | None,
    period_year: int,
    period_month: int,
    employee_name: str,
    employee_code: str,
    designation: str | None,
    date_of_joining: date | None,
    pan: str | None,
    uan: str | None,
    payment_date: date | None,
    working_days: Any,
    paid_days: Any,
    lop_days: Any,
    not_employed_days: Any,
    line_items: list[dict[str, Any]],
    input_lines: list[dict[str, Any]],
    tds: Any,
    gross_earnings: Any,
    other_earnings: Any,
    total_deductions: Any,
    net_pay: Any,
) -> PayslipDocument:
    """Arrange a stored payslip into earnings and deductions. Pure.

    Raises `PayslipMismatchError` when the lines do not reproduce the stored
    totals — which would mean the trace and the totals disagree, and nobody
    should be handed that.
    """
    earnings: list[tuple[str, Decimal]] = []
    deductions: list[tuple[str, Decimal]] = []
    employer: list[tuple[str, Decimal]] = []
    perquisites: list[tuple[str, Decimal]] = []

    for line in line_items:
        amount = _d(line.get("amount"))
        name = str(line.get("name") or line.get("code") or "")
        kind = str(line.get("type", "earning"))
        if kind == "earning":
            earnings.append((name, amount))
        elif kind == "deduction":
            if amount != ZERO:
                deductions.append((name, amount))
        elif kind == "employer_contribution" and amount != ZERO:
            employer.append((name, amount))

    tds_amount = _d(tds)
    if tds_amount != ZERO:
        deductions.append(("Income tax (TDS)", tds_amount))

    for line in input_lines:
        amount = _d(line.get("amount"))
        if line.get("skipped") or amount == ZERO:
            continue
        name = str(line.get("name") or line.get("code") or "")
        kind = str(line.get("kind", ""))
        if kind in _CASH_IN:
            earnings.append((name, amount))
        elif kind in _CASH_OUT:
            deductions.append((name, amount))
        elif kind == "perquisite":
            perquisites.append((name, amount))
        elif kind == "employer_contribution":
            employer.append((name, amount))

    total_earnings = sum((amount for _, amount in earnings), start=ZERO)
    total_out = sum((amount for _, amount in deductions), start=ZERO)
    stated_in = _d(gross_earnings) + _d(other_earnings)
    stated_out = _d(total_deductions)
    stated_net = _d(net_pay)
    if (
        total_earnings.quantize(RUPEE) != stated_in.quantize(RUPEE)
        or total_out.quantize(RUPEE) != stated_out.quantize(RUPEE)
        or (total_earnings - total_out).quantize(RUPEE) != stated_net.quantize(RUPEE)
    ):
        raise PayslipMismatchError(
            f"The payslip's lines add up to earnings {total_earnings} and deductions "
            f"{total_out}, but it states {stated_in}, {stated_out} and net {stated_net}. "
            "It is not rendered; ask payroll to look at this payslip."
        )

    return PayslipDocument(
        company_name=company_name,
        company_address=company_address,
        period_label=f"{calendar.month_name[period_month]} {period_year}",
        employee_name=employee_name,
        employee_code=employee_code,
        designation=designation,
        date_of_joining=date_of_joining,
        pan_masked=mask_identifier(pan),
        uan_masked=mask_identifier(uan),
        payment_date=payment_date,
        working_days=_d(working_days),
        paid_days=_d(paid_days),
        lop_days=_d(lop_days),
        not_employed_days=_d(not_employed_days),
        earnings=earnings,
        deductions=deductions,
        employer_contributions=employer,
        perquisites=perquisites,
        total_earnings=total_earnings,
        total_deductions=total_out,
        net_pay=stated_net,
        reference=f"PAYSLIP/{employee_code}/{period_year}-{period_month:02d}",
    )


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _rows(lines: list[tuple[str, Decimal]]) -> str:
    return "".join(
        f'<tr><td>{_e(name)}</td><td class="amount">{_money(amount)}</td></tr>'
        for name, amount in lines
    )


_CSS = """
h2 { font-size: 13pt; margin: 0 0 6pt 0; }
h3 { font-size: 10.5pt; margin: 8pt 0 2pt 0; }
p { margin: 3pt 0; line-height: 1.3; }
table.slip { width: 100%; margin: 0 0 6pt 0; }
table.slip td, table.slip th { padding: 2pt 6pt; font-size: 9.5pt; border: 1px solid #e8e0cf; }
table.slip th { background-color: #dcefe6; text-align: left; }
td.amount, th.amount { text-align: right; }
tr.total td { background-color: #f2ece0; font-weight: bold; }
table.facts { margin: 0 0 6pt 0; }
table.facts td { padding: 1pt 6pt; font-size: 9.5pt; border: none; }
td.label { color: #6b788d; width: 18%; }
.net { font-size: 13pt; font-weight: bold; color: #14595c; }
.small { font-size: 8.5pt; color: #6b788d; }
"""


def payslip_html(document: PayslipDocument) -> str:
    """The page body. Pure; every value escaped."""
    facts: list[tuple[str, str]] = [
        ("Employee", document.employee_name),
        ("Employee code", document.employee_code),
    ]
    if document.designation:
        facts.append(("Designation", document.designation))
    if document.date_of_joining:
        facts.append(("Date of joining", f"{document.date_of_joining:%d %b %Y}"))
    facts.append(("PAN", document.pan_masked or "Not on record"))
    facts.append(("UAN", document.uan_masked or "Not on record"))
    if document.payment_date:
        facts.append(("Paid on", f"{document.payment_date:%d %b %Y}"))
    days = f"{_days(document.paid_days)} of {_days(document.working_days)}"
    facts.append(("Days paid", days))
    if document.lop_days:
        facts.append(("Loss of pay", f"{_days(document.lop_days)} day(s)"))
    if document.not_employed_days:
        facts.append(("Not employed", f"{_days(document.not_employed_days)} day(s)"))

    # Two facts to a row, so the whole payslip fits on one page.
    pairs = [facts[i : i + 2] for i in range(0, len(facts), 2)]
    fact_rows = "".join(
        "<tr>"
        + "".join(
            f'<td class="label">{_e(label)}</td><td>{_e(value)}</td>' for label, value in pair
        )
        + ("<td></td><td></td>" if len(pair) == 1 else "")
        + "</tr>"
        for pair in pairs
    )
    employer = (
        '<h3>Employer contributions</h3><p class="small">Paid by the company on your behalf; '
        "not part of your net pay.</p>"
        '<table class="slip"><tr><th>Contribution</th><th class="amount">Amount (INR)</th></tr>'
        f"{_rows(document.employer_contributions)}</table>"
        if document.employer_contributions
        else ""
    )
    perquisites = (
        '<p class="small">Perquisites (non-cash, taxable, not paid to you): '
        + "; ".join(f"{_e(name)} {_money(amount)}" for name, amount in document.perquisites)
        + ".</p>"
        if document.perquisites
        else ""
    )
    words = amount_in_words(document.net_pay)

    return f"""<style>{_CSS}</style>
<h2>Payslip for {_e(document.period_label)}</h2>
<table class="facts" width="100%">{fact_rows}</table>
<table class="slip">
  <tr><th>Earnings</th><th class="amount">Amount (INR)</th></tr>
  {_rows(document.earnings)}
  <tr class="total"><td>Total earnings</td>
    <td class="amount">{_money(document.total_earnings)}</td></tr>
</table>
<table class="slip">
  <tr><th>Deductions</th><th class="amount">Amount (INR)</th></tr>
  {_rows(document.deductions) or '<tr><td>No deductions</td><td class="amount">0.00</td></tr>'}
  <tr class="total"><td>Total deductions</td>
    <td class="amount">{_money(document.total_deductions)}</td></tr>
</table>
<p class="net">Net pay: INR {_money(document.net_pay)}</p>
<p>{_e(words)}</p>
{perquisites}
{employer}
<p class="small">This is a computer-generated payslip and does not require a signature.
{_e(document.company_name)} - {_e(document.reference)}</p>
"""


def render_payslip_pdf(document: PayslipDocument) -> bytes:
    # The letterhead is placed into the page as markup, so the entity's name and
    # address — typed in by an admin — are escaped here rather than trusted.
    return render_pdf(
        merged_html=payslip_html(document),
        reference_number=_e(document.reference),
        letterhead=Letterhead(
            company_name=_e(document.company_name),
            address=_e(document.company_address) if document.company_address else None,
        ),
    )
