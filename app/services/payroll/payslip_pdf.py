"""A payslip as a PDF, in the layout CGB's payslips had on factoHR.

Logo at the top left; the company's name, address and "SALARY SLIP FOR THE
MONTH OF SEP-26" centred; a box of the person's details (PAN, PF and UAN, ESI,
bank); earnings beside deductions, each earning with its full-month rate
(Actual), any arrears and what was paid (Amount); net pay in figures and words
and the mode of payment. The user asked for exactly this page (2026-11-03).

Built from the stored payslip — never recomputed. Every figure on the page is a
figure on the payslip row, and the page's totals are checked against the row's
before it is rendered: a PDF whose lines do not add up to the net pay it states
is worse than no PDF.

PAN, UAN, PF and bank account numbers are printed in full (the user's call,
2026-10-27: no masking). `mask_identifier` is kept for anything that still
wants a masked number.
"""

from __future__ import annotations

import base64
import calendar
import functools
import html
import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from app.services.documents.indian_numbers import amount_in_words, group_indian
from app.services.documents.pdf import render_html_document

__all__ = [
    "EarningRow",
    "PayslipDocument",
    "PayslipMismatchError",
    "build_payslip_document",
    "mask_identifier",
    "payslip_facts",
    "payslip_html",
    "register_value",
    "render_payslip_pdf",
]

ZERO = Decimal(0)
RUPEE = Decimal("0.01")
#: The ₹ sign, cut from DejaVu Sans (its licence beside it); the base PDF fonts lack it.
FONT_DIR = Path(__file__).resolve().parent.parent / "documents" / "fonts"

_CASH_IN = ("earning", "reimbursement")
_CASH_OUT = ("deduction", "recovery")
#: An imported register cell: "Basic Salary (V)" → header, column.
_CELL = re.compile(r"^(?P<header>.+) \((?P<column>[A-Z]{1,3})\)$")


class PayslipMismatchError(ValueError):
    """The lines on the page do not add up to the payslip's own totals."""


def _d(value: Any) -> Decimal:
    return Decimal(str(value)) if value not in (None, "") else ZERO


def _money(value: Decimal) -> str:
    return group_indian(value.quantize(RUPEE, rounding=ROUND_HALF_UP))


def mask_identifier(value: str | None, keep: int = 4) -> str | None:
    """`ABCDE1234F` → `XXXXXX234F`. `None` when there is nothing on record."""
    if not value or not value.strip():
        return None
    text = value.strip()
    if len(text) <= keep:
        return "X" * len(text)
    return "X" * (len(text) - keep) + text[-keep:]


def _column_index(column: str) -> int:
    index = 0
    for char in column:
        index = index * 26 + (ord(char) - 64)
    return index


def _rows(import_details: dict | None) -> list[dict[str, Any]]:
    """The register rows a payslip came from, the main one first."""
    details = import_details if isinstance(import_details, dict) else {}
    rows = [row for row in details.get("row_details") or [] if isinstance(row, dict)]
    if isinstance(details.get("cells"), dict) and details["cells"]:
        rows.insert(0, {"kind": details.get("kind", "main"), "cells": details["cells"]})
    return sorted(rows, key=lambda row: row.get("kind") != "main")


def register_value(import_details: dict | None, header: str) -> str | None:
    """A cell of an imported register row by its header ("Designation"), as typed."""
    for row in _rows(import_details):
        for key, value in (row.get("cells") or {}).items():
            match = _CELL.match(str(key))
            if match and match["header"].strip().lower() == header.lower():
                text = str(value or "").strip()
                if text:
                    return text
    return None


def _rate_in(cells: dict, header: str, column: str) -> Decimal | None:
    """In one register row, the same header's column before `column`: the rate."""
    earlier: list[tuple[int, str]] = []
    for key, value in (cells or {}).items():
        match = _CELL.match(str(key))
        if (
            match
            and match["header"].strip() == header.strip()
            and _column_index(match["column"]) < _column_index(column)
        ):
            earlier.append((_column_index(match["column"]), str(value)))
    if not earlier:
        return None
    try:
        return Decimal(max(earlier)[1].replace(",", ""))
    except ArithmeticError:
        return None


def _imported_rate(line: dict[str, Any], import_details: dict | None) -> Decimal | None:
    """The full-month rate factoHR's register held beside what it paid.

    The register has each component twice under the same header — the rate
    first (Basic Salary (Q)), what was earned after (Basic Salary (V)). The
    payslip line keeps the earned column; the rate is the other one. A person
    on several rows (a main row and an arrears row) has one rate: the main
    row's.
    """
    source = line.get("source")
    if isinstance(source, dict):
        header, column = source.get("header"), source.get("column")
        if not header or not column:
            return None
        for row in _rows(import_details)[:1]:
            return _rate_in(row.get("cells") or {}, str(header), str(column))
        return None
    if isinstance(source, list):
        by_place = {
            (row.get("file"), row.get("sheet"), row.get("row")): row
            for row in _rows(import_details)
        }
        found = []
        for part in source:
            if not isinstance(part, dict) or not part.get("header") or not part.get("column"):
                continue
            row = by_place.get((part.get("file"), part.get("sheet"), part.get("row")))
            if row is None:
                continue
            rate = _rate_in(row.get("cells") or {}, str(part["header"]), str(part["column"]))
            if rate is not None:
                found.append((row.get("kind") != "main", rate))
        return min(found, key=lambda item: item[0])[1] if found else None
    return None


@dataclass(frozen=True, slots=True)
class EarningRow:
    """One earnings line: its full-month rate, any arrears, and what was paid."""

    name: str
    actual: Decimal | None
    arrears: Decimal | None
    amount: Decimal


@dataclass(frozen=True, slots=True)
class PayslipDocument:
    """Everything printed on the page, already checked to add up."""

    company_name: str
    company_address: str | None
    period_year: int
    period_month: int
    period_label: str
    employee_name: str
    employee_code: str
    designation: str | None
    date_of_joining: date | None
    pan: str | None
    uan: str | None
    payment_date: date | None
    working_days: Decimal
    paid_days: Decimal
    lop_days: Decimal
    not_employed_days: Decimal
    earnings: list[tuple[str, Decimal]]
    deductions: list[tuple[str, Decimal]]
    employer_contributions: list[tuple[str, Decimal]] = field(default_factory=list)
    perquisites: list[tuple[str, Decimal]] = field(default_factory=list)
    earning_rows: list[EarningRow] = field(default_factory=list)
    total_actual: Decimal = ZERO
    total_arrears: Decimal = ZERO
    total_earnings: Decimal = ZERO
    total_deductions: Decimal = ZERO
    net_pay: Decimal = ZERO
    department: str | None = None
    pf_number: str | None = None
    esi_number: str | None = None
    bank_name: str | None = None
    bank_account: str | None = None
    payment_mode: str | None = None
    logo: bytes | None = None
    reference: str = ""

    @property
    def title(self) -> str:
        """`SALARY SLIP FOR THE MONTH OF SEP-26`, as factoHR heads it."""
        month = calendar.month_abbr[self.period_month].upper()
        return f"SALARY SLIP FOR THE MONTH OF {month}-{self.period_year % 100:02d}"

    @property
    def net_pay_in_words(self) -> str:
        return amount_in_words(self.net_pay, currency="INR")


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
    monthly_rates: dict[str, Any] | None = None,
    import_details: dict | None = None,
    department: str | None = None,
    pf_number: str | None = None,
    esi_number: str | None = None,
    bank_name: str | None = None,
    bank_account: str | None = None,
    payment_mode: str | None = None,
    logo: bytes | None = None,
) -> PayslipDocument:
    """Arrange a stored payslip into earnings and deductions. Pure.

    `monthly_rates` (component code → full-month amount) fills the Actual
    column of a computed payslip; an imported one reads its rates from the
    register row in `import_details`. Raises `PayslipMismatchError` when the
    lines do not reproduce the stored totals — the trace and the totals
    disagree, and nobody should be handed that.
    """
    rates = {str(code): _d(value) for code, value in (monthly_rates or {}).items()}
    earnings: list[tuple[str, Decimal]] = []
    rows: list[EarningRow] = []
    deductions: list[tuple[str, Decimal]] = []
    employer: list[tuple[str, Decimal]] = []
    perquisites: list[tuple[str, Decimal]] = []

    full_month = _d(working_days) > ZERO and _d(paid_days) == _d(working_days)
    for line in line_items:
        amount = _d(line.get("amount"))
        name = str(line.get("name") or line.get("code") or "")
        kind = str(line.get("type", "earning"))
        if kind == "earning":
            earnings.append((name, amount))
            code = str(line.get("code") or "")
            actual = rates.get(code) if code in rates else _imported_rate(line, import_details)
            if actual is None and full_month:
                # No rate on record (factoHR's 2023 registers had only the paid
                # column): in a month paid in full, the rate is what was paid.
                # In a part month it is left blank rather than guessed.
                actual = amount
            rows.append(EarningRow(name=name, actual=actual, arrears=None, amount=amount))
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
            arrears = amount if line.get("provider") == "arrears" else None
            rows.append(EarningRow(name=name, actual=None, arrears=arrears, amount=amount))
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

    def clean(value: str | None) -> str | None:
        return (value or "").strip() or None

    return PayslipDocument(
        company_name=company_name,
        company_address=company_address,
        period_year=period_year,
        period_month=period_month,
        period_label=f"{calendar.month_name[period_month]} {period_year}",
        employee_name=employee_name,
        employee_code=employee_code,
        designation=clean(designation),
        date_of_joining=date_of_joining,
        pan=clean(pan),
        uan=clean(uan),
        payment_date=payment_date,
        working_days=_d(working_days),
        paid_days=_d(paid_days),
        lop_days=_d(lop_days),
        not_employed_days=_d(not_employed_days),
        earnings=earnings,
        deductions=deductions,
        employer_contributions=employer,
        perquisites=perquisites,
        earning_rows=rows,
        total_actual=sum((row.actual or ZERO for row in rows), start=ZERO),
        total_arrears=sum((row.arrears or ZERO for row in rows), start=ZERO),
        total_earnings=total_earnings,
        total_deductions=total_out,
        net_pay=stated_net,
        department=clean(department),
        pf_number=clean(pf_number),
        esi_number=clean(esi_number),
        bank_name=clean(bank_name),
        bank_account=clean(bank_account),
        payment_mode=clean(payment_mode),
        logo=logo or None,
        reference=f"PAYSLIP/{employee_code}/{period_year}-{period_month:02d}",
    )


# ── The page ────────────────────────────────────────────────────────────────


def _days(value: Decimal, places: int) -> str:
    return f"{value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)}"


def payslip_facts(document: PayslipDocument) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The details box, left column and right column, label and value.

    The screen shows the same lists, so a payslip reads the same in the portal
    as on paper. A value not on record is printed empty, as factoHR did.
    """
    joined = f"{document.date_of_joining:%d-%m-%Y}" if document.date_of_joining else ""
    left = [
        ("Employee Name", document.employee_name),
        ("Employee Code", document.employee_code),
        ("Designation", document.designation or ""),
        ("Department", document.department or ""),
        ("DOJ", joined),
    ]
    right = [
        ("PAN", document.pan or ""),
        ("PF A/c No", document.pf_number or ""),
        ("UAN", document.uan or ""),
        ("ESI IP No", document.esi_number or ""),
        ("Bank Name", document.bank_name or ""),
        ("Bank A/c No", document.bank_account or ""),
    ]
    return left, right


def payslip_days(document: PayslipDocument) -> tuple[str, str]:
    """Payable Days ("30.0") and Net Paid Days ("27.00"), as factoHR printed them."""
    return _days(document.working_days, 1), _days(document.paid_days, 2)


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _rupee() -> str:
    return '<span class="rs">₹</span>'


def _cell_money(value: Decimal | None) -> str:
    return _money(value) if value is not None else ""


#: The logo's box on the page, in points: at most this high and this wide.
_LOGO_HEIGHT, _LOGO_WIDTH = 40.0, 130.0


@functools.lru_cache(maxsize=16)
def _print_logo(logo: bytes) -> tuple[str, float, float] | None:
    """The logo ready to print: on white, at print size, as a PNG data URI.

    A transparent logo would print on black (the renderer drops the alpha),
    and a large one would add hundreds of KB to every payslip in a ZIP. Cached:
    a batch prints the same company logo hundreds of times.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(logo)) as image:
            image.load()
            ratio = image.width / image.height if image.height else 3.0
            height = min(_LOGO_HEIGHT, _LOGO_WIDTH / ratio)
            width = height * ratio
            # Two and a half pixels a point: sharp in print, small in the file.
            size = (max(1, round(width * 2.5)), max(1, round(height * 2.5)))
            flat = Image.new("RGB", image.size, (255, 255, 255))
            rgba = image.convert("RGBA")
            flat.paste(rgba, mask=rgba.getchannel("A"))
            flat = flat.resize(size, Image.LANCZOS)
            out = io.BytesIO()
            flat.save(out, format="PNG", optimize=True)
    except Exception:
        return None
    data = base64.b64encode(out.getvalue()).decode("ascii")
    return f"data:image/png;base64,{data}", width, height


def _logo_html(logo: bytes | None) -> str:
    prepared = _print_logo(logo) if logo else None
    if prepared is None:
        return ""
    uri, width, height = prepared
    return f'<img src="{uri}" width="{width:.1f}" height="{height:.1f}" alt="" />'


_RUPEE_FONT = "AskMiraRupee"


def _register_rupee_font() -> None:
    """Make the bundled ₹ glyph (DejaVu Sans) available to the renderer as `AskMiraRupee`.

    Registered with reportlab directly rather than by CSS @font-face: xhtml2pdf
    copies an @font-face file to a temporary file and reopens it, which Windows
    refuses while the copy is still open.
    """
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from xhtml2pdf import default

    if _RUPEE_FONT in pdfmetrics.getRegisteredFontNames():
        return
    pdfmetrics.registerFont(TTFont(_RUPEE_FONT, str(FONT_DIR / "DejaVuSans-Rupee.ttf")))
    # One weight only: the sign alone is set in it, and a second embedded font
    # would add ~20 KB to every payslip in a year's ZIP.
    pdfmetrics.registerFontFamily(
        _RUPEE_FONT,
        normal=_RUPEE_FONT,
        bold=_RUPEE_FONT,
        italic=_RUPEE_FONT,
        boldItalic=_RUPEE_FONT,
    )
    default.DEFAULT_FONT[_RUPEE_FONT.lower()] = _RUPEE_FONT


_CSS = """
@page { size: a4 portrait; margin: 1.1cm 0.9cm 1.1cm 1.1cm; }
body { font-family: Helvetica, Arial, sans-serif; font-size: 7.6pt; color: #000000;
       line-height: 1.25; }
.rs { font-family: AskMiraRupee; }
table { width: 100%; }
td { vertical-align: top; }
.head td { vertical-align: middle; padding: 0; }
.company { font-size: 9pt; font-weight: bold; text-align: center; }
.address { font-size: 7.6pt; font-weight: bold; text-align: center; }
.title { font-size: 8.6pt; font-weight: bold; text-align: center; padding-top: 5pt; }
.box { border: 0.8pt solid #000000; }
.facts td { padding: 1.6pt 3pt 1.6pt 3pt; }
.facts td.value { font-weight: bold; }
.facts tr.days td { border-top: 0.8pt solid #000000; }
.pay td, .pay th { border-left: 0.8pt solid #000000; border-right: 0.8pt solid #000000;
                   padding: 1.6pt 3pt 1.6pt 3pt; }
.pay th { border: 0.8pt solid #000000; font-weight: bold; text-align: center;
          padding: 4pt 3pt 4pt 3pt; }
.pay th.big { font-size: 8.6pt; }
.pay td.num { text-align: right; }
.pay td.paid { font-size: 8.6pt; }
.pay tr.total td { border: 0.8pt solid #000000; font-weight: bold; padding: 3pt; }
.net td { padding: 2pt 4pt 2pt 4pt; }
.net td.foot { border-top: 0.8pt solid #000000; font-weight: bold; padding: 4pt 8pt 4pt 8pt; }
"""


def payslip_html(document: PayslipDocument) -> str:
    """The whole page. Pure; every value escaped."""
    left, right = payslip_facts(document)
    payable, paid = payslip_days(document)
    fact_rows = ""
    for index in range(max(len(left), len(right))):
        cells = ""
        for side in (left, right):
            if index < len(side):
                label, value = side[index]
                cells += (
                    f'<td width="13%">{_e(label)}</td>'
                    f'<td width="37%" class="value">: {_e(value)}</td>'
                )
            else:
                cells += '<td width="13%"></td><td width="37%"></td>'
        fact_rows += f"<tr>{cells}</tr>"
    fact_rows += (
        '<tr class="days">'
        f'<td width="13%">Payable Days</td><td width="37%" class="value">: {_e(payable)}</td>'
        f'<td width="13%">Net Paid Days</td><td width="37%" class="value">: {_e(paid)}</td>'
        "</tr>"
    )

    earnings = document.earning_rows
    deductions = document.deductions
    pay_rows = ""
    for index in range(max(len(earnings), len(deductions), 1)):
        if index < len(earnings):
            row = earnings[index]
            earning = (
                f"<td>{_e(row.name.upper())}</td>"
                f'<td class="num">{_cell_money(row.actual)}</td>'
                f'<td class="num">{_cell_money(row.arrears)}</td>'
                f'<td class="num paid">{_money(row.amount)}</td>'
            )
        else:
            earning = '<td></td><td class="num"></td><td class="num"></td><td class="num"></td>'
        if index < len(deductions):
            name, amount = deductions[index]
            deduction = f'<td>{_e(name.upper())}</td><td class="num">{_money(amount)}</td>'
        else:
            deduction = '<td></td><td class="num"></td>'
        pay_rows += f"<tr>{earning}{deduction}</tr>"

    rs = _rupee()
    company = _e(document.company_name.upper())
    address = (
        f'<div class="address">{_e(document.company_address)}</div>'
        if (document.company_address)
        else ""
    )
    mode = (
        f'<tr><td width="20%"></td><td width="80%">Mode of Payment : '
        f"{_e(document.payment_mode)}</td></tr>"
        if document.payment_mode
        else ""
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8" />
<title>{_e(document.reference)}</title>
<style>{_CSS}</style>
</head><body>
<table class="head"><tr>
  <td width="17%">{_logo_html(document.logo)}</td>
  <td width="66%">
    <div class="company">{company}</div>
    {address}
    <div class="title">{_e(document.title)}</div>
  </td>
  <td width="17%"></td>
</tr></table>

<table class="box"><tr><td>
<table class="facts">{fact_rows}</table>
</td></tr></table>

<table class="pay">
<tr>
  <th width="26%" class="big">EARNINGS</th>
  <th width="11%">Actual {rs}</th>
  <th width="11%">Arrears {rs}</th>
  <th width="11%">Amount {rs}</th>
  <th width="29%" class="big">DEDUCTIONS</th>
  <th width="12%">Amount {rs}</th>
</tr>
{pay_rows}
<tr class="total">
  <td>Gross Earnings</td>
  <td class="num">{_money(document.total_actual)}</td>
  <td class="num">{_money(document.total_arrears)}</td>
  <td class="num">{_money(document.total_earnings)}</td>
  <td>Total Deductions</td>
  <td class="num">{_money(document.total_deductions)}</td>
</tr>
</table>

<table class="box net"><tr><td>
<table>
<tr><td width="20%"><b>Net Pay</b> &nbsp; <b>: {rs} {_money(document.net_pay)}</b></td>
<td width="80%">({rs}{_e(document.net_pay_in_words)})</td></tr>
{mode}
<tr><td colspan="2">&nbsp;</td></tr>
<tr><td colspan="2" class="foot">*This is a system generated payslip and does not require
signature</td></tr>
</table>
</td></tr></table>
</body></html>"""


def render_payslip_pdf(document: PayslipDocument) -> bytes:
    _register_rupee_font()
    return render_html_document(payslip_html(document), document.reference)
