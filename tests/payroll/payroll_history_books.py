"""Small synthetic factoHR salary registers, one per layout family, for the history import tests.

Each layout is a list of (header, key) pairs in column order — exactly the
headers the real registers use, repeats included (the first BASIC SALARY …
Gross Salary block is the full-month rate, the second what was earned). A row is
a dict by key; `person()` fills a whole row the way the registers' formulas do.
`book()` writes the workbook; `cached` puts a cached value on a formula cell,
which openpyxl cannot do, by patching the sheet XML (the F&F days divisor is
read from such a formula while its value is read from the cache).
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

Layout = list[tuple[str | None, str]]

_ACTUAL = [
    ("BASIC SALARY", "a_basic"),
    ("HOUSE RENT ALLOWANCE", "a_hra"),
    ("SPECIAL ALLOWANCE", "a_special"),
    ("Bonus", "a_bonus"),
    ("Gross Salary", "a_gross"),
]
_EARNED = [
    ("BASIC SALARY", "basic"),
    ("HOUSE RENT ALLOWANCE", "hra"),
    ("SPECIAL ALLOWANCE", "special"),
    ("Bonus", "bonus"),
    ("Gross Salary", "gross"),
]
_DEDUCTIONS = [
    ("PF", "pf"),
    ("ESI", "esi"),
    ("Med Ins", "ins"),
    ("PT", "pt"),
    ("TDS", "tds"),
    ("Total Deduction", "total_ded"),
]
_IDS = [
    ("UAN No", "uan"),
    ("ESIC No", "esic"),
    ("PAN", "pan"),
    ("Aadhar No", "aadhaar"),
    ("Bank Name", "bank_name"),
    ("IFSC", "ifsc"),
    ("Account", "account"),
]

#: Sep-2023: no STATUS, one earnings block (earned), "Sep CTC" is the month's CTC.
SEP23: Layout = [
    ("Emp Code", "code"),
    (None, "legacy"),
    ("Name", "name"),
    ("Date of Joining", "doj"),
    ("Client", "client"),
    (None, "note"),
    ("Master CTC", "master_ctc"),
    ("Sep CTC", "ctc"),
    ("CTC Earned", "ctc_earned"),
    ("Workable Days", "workable"),
    ("Days Worked", "worked"),
    ("LOP", "lop"),
    *_EARNED,
    ("PF Basic", "pf_wage"),
    ("ESI Gross", "esi_wage"),
    *_DEDUCTIONS,
    ("Net Salary", "net"),
    ("Adjustement", "adj"),
    ("Bank Tfr", "bank"),
    ("Employer PF", "er_pf"),
    ("Employer ESI", "er_esi"),
    ("Gratuity", "gratuity"),
    *_IDS,
]

#: Oct-2023 (sheet named "Sep"): Master / Oct / Sep CTC, the month's is "Oct CTC".
OCT23: Layout = [
    ("Emp Code", "code"),
    (None, "legacy"),
    ("Name", "name"),
    ("Date of Joining", "doj"),
    ("Client", "client"),
    (None, "note"),
    ("Master CTC", "master_ctc"),
    ("Oct CTC", "ctc"),
    ("Sep CTC", "prev_ctc"),
    ("diff", "diff"),
    ("CTC Earned", "ctc_earned"),
    ("Workable Days", "workable"),
    ("Days Worked", "worked"),
    ("LOP", "lop"),
    *_ACTUAL,
    *_EARNED,
    *_DEDUCTIONS,
    ("Net Salary", "net"),
    ("Adjustement", "adj"),
    ("Bank Tfr", "bank"),
    (None, "batch2"),
    ("Employer PF", "er_pf"),
    ("Employer ESI", "er_esi"),
    ("Gratuity", "gratuity"),
    *_IDS,
]

#: Dec-2023 … Feb-2024: STATUS, Master CTC, both blocks, Remarks at the end.
DEC23: Layout = [
    ("Emp Code", "code"),
    ("Name", "name"),
    ("STATUS", "status"),
    ("Date of Joining", "doj"),
    ("Client", "client"),
    ("Master CTC", "ctc"),
    ("CTC Earned", "ctc_earned"),
    ("Workable Days", "workable"),
    ("Days Worked", "worked"),
    ("LOP", "lop"),
    *_ACTUAL,
    *_EARNED,
    *_DEDUCTIONS,
    ("Net Salary", "net"),
    ("Adjustement", "adj"),
    ("Bank Tfr", "bank"),
    ("Employer PF", "er_pf"),
    ("Employer ESI", "er_esi"),
    ("Gratuity", "gratuity"),
    *_IDS,
    ("Remarks", "remarks"),
]

#: Apr-2024: Project, DOL and CTC; May-2024 adds a helper column and ROUNDED.
APR24: Layout = [
    ("Emp Code", "code"),
    ("Name", "name"),
    ("STATUS", "status"),
    ("Date of Joining", "doj"),
    ("Client", "client"),
    ("Project", "project"),
    ("DOL", "dol"),
    ("CTC", "ctc"),
    ("CTC Earned", "ctc_earned"),
    ("Workable Days", "workable"),
    ("Days Worked", "worked"),
    ("LOP", "lop"),
    *_ACTUAL,
    *_EARNED,
    *_DEDUCTIONS,
    ("Net Salary", "net"),
    ("Adjustement", "adj"),
    ("Bank Tfr", "bank"),
    ("Employer PF", "er_pf"),
    ("Employer ESI", "er_esi"),
    ("Gratuity", "gratuity"),
    *_IDS,
    ("EMAIL ID", "email"),
]
MAY24: Layout = [
    *APR24[:9],
    (None, "helper"),
    *APR24[9:31],
    ("ROUNDED", "rounded"),
    *APR24[31:],
]

#: Feb-2025 onwards: Domain after Project; Project names the account.
FEB25: Layout = [
    ("Emp Code", "code"),
    ("Employees Name", "name"),
    ("STATUS", "status"),
    ("Date of Joining", "doj"),
    ("Client", "client"),
    ("Project", "project"),
    ("Domain", "domain"),
    ("DOL", "dol"),
    ("CTC", "ctc"),
    ("CTC Earned", "ctc_earned"),
    (None, "check"),
    ("Workable Days", "workable"),
    ("Days Worked", "worked"),
    ("LOP", "lop"),
    *_ACTUAL,
    *_EARNED,
    *_DEDUCTIONS,
    ("Net Salary", "net"),
    ("Adjustement", "adj"),
    ("Bank Tfr", "bank"),
    ("ROUNDED", "rounded"),
    ("Employer PF", "er_pf"),
    ("Employer ESI", "er_esi"),
    ("Gratuity", "gratuity"),
    *_IDS,
    ("Personal EMAIL ID", "email"),
    ("ALL PT STATUS", "pt_status"),
    ("Gratuity Status", "gratuity_status"),
    ("Remarks", "remarks"),
    ("Name Length", "name_length"),
    (None, "blank"),
    ("Payment Status", "payment_status"),
]

#: Mar-2026: the same columns, headers renamed.
MAR26: Layout = [
    ("Emp Code", "code"),
    ("Employees Name", "name"),
    ("Status", "status"),
    ("Date of Joining", "doj"),
    ("Client", "client"),
    ("Project", "project"),
    ("Domain", "domain"),
    ("DOL", "dol"),
    ("Monthly CTC", "ctc"),
    ("Earned CTC", "ctc_earned"),
    (" ", "check"),
    ("Workable Days", "workable"),
    ("Days Worked", "worked"),
    ("LOP", "lop"),
    ("Basic Salary", "a_basic"),
    ("House Rent Allowance", "a_hra"),
    ("Special Allowance", "a_special"),
    ("Bonus", "a_bonus"),
    ("Gross Salary", "a_gross"),
    ("Basic Salary", "basic"),
    ("House Rent Allowance", "hra"),
    ("Special Allowance", "special"),
    ("Bonus", "bonus"),
    ("Gross Salary", "gross"),
    ("PF", "pf"),
    ("ESI", "esi"),
    ("Medical Insurance", "ins"),
    ("PT", "pt"),
    ("TDS", "tds"),
    ("Total Deduction", "total_ded"),
    ("Net Salary", "net"),
    ("Adjustment", "adj"),
    ("Bank Trs", "bank"),
    ("Round", "rounded"),
    ("Employer PF", "er_pf"),
    ("Employer ESI", "er_esi"),
    ("Gratuity", "gratuity"),
    ("UAN No", "uan"),
    ("ESI No", "esic"),
    ("PAN No", "pan"),
    ("Adhar No", "aadhaar"),
    ("Bank Name", "bank_name"),
    ("Account", "account"),
    ("IFSC", "ifsc"),
    ("Personal Mail ID", "email"),
    ("All PT Status", "pt_status"),
    ("Gratuity Status", "gratuity_status"),
    ("Remarks", "remarks"),
    ("Name Len", "name_length"),
    ("Payment Status", "payment_status"),
]

#: Apr-2026: like Mar-2026, "Bank Transfer Amount" and no Round column.
APR26: Layout = [
    ("Bank Transfer Amount", k) if k == "bank" else (h, k) for h, k in MAR26 if k != "rounded"
]

#: The F&F sheets (Feb-2024 "F&F Payment", Apr-2024 "F&F").
FNF: Layout = [
    ("EmpId", "code"),
    ("Name", "name"),
    ("DOJ", "doj"),
    ("LWD", "dol"),
    ("Paid Days", "worked"),
    ("Total ctc", "ctc"),
    (None, "ctc_earned_helper"),
    ("Basic", "a_basic"),
    ("HRA", "a_hra"),
    ("Special All", "a_special"),
    ("Bonus", "a_bonus"),
    ("Gross Salary", "a_gross"),
    ("Basic", "basic"),
    ("HRA", "hra"),
    ("Special All", "special"),
    ("Bonus", "bonus"),
    ("Gross Salary", "gross"),
    ("PF", "pf"),
    ("PT", "pt"),
    ("ESI", "esi"),
    ("TDS", "tds"),
    ("INSURANCE", "ins"),
    ("Other Deduction", "other"),
    ("Payable Amount", "payable"),
    ("PF Emr", "er_pf"),
    ("ESI Emr", "er_esi"),
    ("Gratuity", "gratuity"),
    ("Bank Name", "bank_name"),
    ("Account No.", "account"),
    ("IFSC Code", "ifsc"),
    ("UAN", "uan"),
    ("PF WAGES", "pf_wages"),
    ("PF", "pf_rounded"),
]

#: Identifiers every register row carries, which must never be stored.
IDENTIFIERS = {
    "uan": "101095435751",
    "esic": "5042408205",
    "pan": "BPNPP1086C",
    "aadhaar": "870330987299",
    "bank_name": "HSBC BANK",
    "ifsc": "HSBC0400002",
    "account": "132420613006",
    "email": "someone@example.com",
}


def person(
    code: str,
    name: str,
    doj: date,
    *,
    ctc: float | int,
    basic: float | int,
    hra: float | int,
    special: float | int,
    bonus: float | int = 0,
    workable: int = 30,
    worked: float | int | None = None,
    lop: float | int | None = None,
    pf: float | int | None = None,
    esi: float | int = 0,
    ins: float | int = 239,
    pt: float | int = 200,
    tds: float | int = 0,
    adj: float | int = 0,
    whole: bool = False,
    **cells: Any,
) -> dict[str, Any]:
    """One register row as the sheet's formulas would have it.

    Earned = rate / workable * worked, unrounded (as up to Apr-2024) or whole
    rupees (`whole`, as from May-2024). Bank = net + adjustment; ROUNDED = the
    bank figure to the rupee. `cells` overrides or adds any key.
    """
    worked = workable if worked is None else worked
    lop = (workable - worked) if lop is None else lop

    def earn(rate: float | int) -> float | int:
        value = rate / workable * worked
        return round(value) if whole else value

    rates = (("basic", basic), ("hra", hra), ("special", special), ("bonus", bonus))
    e = {k: earn(v) for k, v in rates}
    gross = e["basic"] + e["hra"] + e["special"] + e["bonus"]
    pf_amount = (round(e["basic"] * 0.12) if whole else e["basic"] * 0.12) if pf is None else pf
    total_ded = pf_amount + esi + ins + pt + tds
    net = gross - total_ded
    bank = net + adj
    row: dict[str, Any] = {
        "code": code,
        "name": name,
        "doj": doj,
        "status": "Active",
        "client": "BOB",
        "master_ctc": ctc,
        "ctc": ctc,
        "ctc_earned": gross,
        "workable": workable,
        "worked": worked,
        "lop": lop,
        "a_basic": basic,
        "a_hra": hra,
        "a_special": special,
        "a_bonus": bonus,
        "a_gross": basic + hra + special + bonus,
        **e,
        "gross": gross,
        "pf_wage": e["basic"],
        "esi_wage": gross,
        "pf": pf_amount,
        "esi": esi,
        "ins": ins,
        "pt": pt,
        "tds": tds,
        "total_ded": total_ded,
        "net": net,
        "adj": adj,
        "bank": bank,
        "rounded": round(bank),
        "payable": net,
        "other": 0,
        "er_pf": round(pf_amount / 12 * 13),
        "er_esi": 0,
        "gratuity": e["basic"] / 26 * 15 / 12,
        **IDENTIFIERS,
    }
    row.update(cells)
    return row


@dataclass
class Sheet:
    title: str
    layout: Layout
    rows: list[dict[str, Any] | None]
    #: Rows above the header (a title, a group row); None is an empty row.
    above: list[list[Any] | None] = field(default_factory=list)
    #: Rows after the data: a totals row, a blank, a stray; None is an empty row.
    after: list[list[Any] | None] = field(default_factory=list)
    #: Data rows (0-based, in `rows`) to hide.
    hidden: set[int] = field(default_factory=set)
    #: key → formula template per data row ("={basic_col}{r}/31*{worked_col}{r}").
    formulas: dict[str, str] = field(default_factory=dict)

    @property
    def header_row(self) -> int:
        return len(self.above) + 1


def totals(sheet: Sheet, keys: list[str], *, skip_hidden: bool = False) -> list[Any]:
    """A totals row (SUM, or SUBTOTAL leaving hidden rows out) for these keys."""
    out: list[Any] = [None] * len(sheet.layout)
    for i, (_header, key) in enumerate(sheet.layout):
        if key not in keys:
            continue
        out[i] = sum(
            row.get(key) or 0
            for n, row in enumerate(sheet.rows)
            if row is not None and not (skip_hidden and n in sheet.hidden)
        )
    return out


def book(sheets: list[Sheet]) -> bytes:
    """The workbook as bytes, with cached values on the formula cells."""
    wb = Workbook()
    wb.remove(wb.active)
    cached: dict[int, dict[str, Any]] = {}
    for index, spec in enumerate(sheets):
        ws = wb.create_sheet(spec.title)
        for r, values in enumerate(spec.above, start=1):
            for c, value in enumerate(values or [], start=1):
                if value is not None:
                    ws.cell(r, c, value)
        header = spec.header_row
        for c, (title, _key) in enumerate(spec.layout, start=1):
            if title is not None:
                ws.cell(header, c, title)
        letters = {key: get_column_letter(c) for c, (_t, key) in enumerate(spec.layout, start=1)}
        r = header
        for n, row in enumerate(spec.rows):
            r += 1
            if row is None:
                continue
            if n in spec.hidden:
                ws.row_dimensions[r].hidden = True
            for c, (_title, key) in enumerate(spec.layout, start=1):
                value = row.get(key)
                if value is None:
                    continue
                if key in spec.formulas:
                    ref = f"{letters[key]}{r}"
                    columns = {f"{k}_col": v for k, v in letters.items()}
                    ws[ref] = spec.formulas[key].format(r=r, **columns)
                    cached.setdefault(index, {})[ref] = value
                else:
                    ws.cell(r, c, value)
        for values in spec.after:
            r += 1
            for c, value in enumerate(values or [], start=1):
                if value is not None:
                    ws.cell(r, c, value)
    out = io.BytesIO()
    wb.save(out)
    data = out.getvalue()
    return _with_cached(data, cached) if cached else data


def _with_cached(data: bytes, cached: dict[int, dict[str, Any]]) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            content = source.read(item.filename)
            match = re.match(r"xl/worksheets/sheet(\d+)\.xml$", item.filename)
            if match and int(match.group(1)) - 1 in cached:
                text = content.decode("utf-8")
                for ref, value in cached[int(match.group(1)) - 1].items():
                    text = re.sub(
                        rf'(<c r="{ref}"[^>]*>)(<f>[^<]*</f>)<v\s*/?>(?:</v>)?',
                        lambda m, v=value: f"{m.group(1)}{m.group(2)}<v>{v!r}</v>",
                        text,
                    )
                content = text.encode("utf-8")
            target.writestr(item, content)
    return out.getvalue()
