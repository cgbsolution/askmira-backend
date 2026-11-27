"""Format-specific serialisers. Canonical structure in, bytes out.

═══════════════════════════════════════════════════════════════════════════════
THE RULE FOR THIS FILE
═══════════════════════════════════════════════════════════════════════════════

Everything here is a pure function from a `records.py` dataclass to a string.
No database, no arithmetic that decides anything, no configuration lookups. A
serialiser rearranges figures that have already been decided.

That is not tidiness for its own sake. Portal formats change without notice,
the notice arrives mid-filing-window, and the change is always "one more
column" or "that field moved". When it does, the patch is here and nowhere
else — and the golden-file tests tell you immediately whether the rest of the
file still lines up.

The column *orders* below are the formats. They are the one thing in the
codebase where the order of a list is itself the requirement, which is why each
is written out as an explicit list rather than derived from a dataclass's field
order — a field reordering in `records.py` must not silently change a file
layout.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import hashlib
import io
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from app.services.filings.records import (
    ArrearEcrReturn,
    EcrReturn,
    EsicReturn,
    PtChallan,
    Tds24QReturn,
)

__all__ = [
    "SerialiserError",
    "content_hash",
    "serialise_arrear_ecr",
    "serialise_ecr",
    "serialise_ecr_upload_csv",
    "serialise_esic",
    "serialise_esic_mc_xls",
    "serialise_pt_challan",
    "serialise_tds_24q",
]


class SerialiserError(ValueError):
    """A canonical structure cannot be rendered in the requested format."""


def _whole(value: Decimal) -> str:
    """Rupees, no paise.

    The ECR and the ESIC return both carry whole rupees. Sending paise is a
    format rejection, and truncating rather than rounding loses a rupee per
    employee per month across the establishment.
    """
    return str(int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP)))


def _paise(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _ncp(value: Decimal) -> str:
    """NCP days are whole days in the file even though attendance holds halves.

    Rounding up is deliberate: a half day of loss of pay is a day on which the
    employee was not fully contributing, and understating NCP overstates
    contributory service.
    """
    return str(int(value.quantize(Decimal(1), rounding=ROUND_CEILING)))


def content_hash(content: str) -> str:
    """SHA-256 of what was generated, so the file held later is provably the
    file uploaded then."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════════════════════════
# PF ECR — #~#-delimited text (ECR 2.0)
# ═══════════════════════════════════════════════════════════════════════════

#: The field separator the EPFO's ECR 2.0 file prescribes: three characters,
#: hash tilde hash. A file in any other delimiter is rejected at upload.
ECR_DELIMITER = "#~#"

# The EPFO ECR line, in order. Eleven fields; a line with ten or twelve is
# rejected without a useful message. The employer's two shares are in the
# portal's order: the EPS (pension) contribution, then the EPF-EPS difference.
# Swapped, the pension fund and the provident fund are credited each other's
# money.
ECR_COLUMNS = (
    "uan",
    "member_name",
    "gross_wages",
    "epf_wages",
    "eps_wages",
    "edli_wages",
    "employee_share",
    "eps_contribution",
    "employer_epf_share",
    "ncp_days",
    "refund_of_advances",
)


def serialise_ecr(ecr: EcrReturn) -> str:
    """The ECR text file.

    `#~#`-delimited, CRLF-terminated, one line per member, no header. The
    absence of a header is not an oversight — the EPFO uploader treats a header
    row as a member record and fails on it.
    """
    lines: list[str] = []

    for line in ecr.lines:
        if ECR_DELIMITER in line.member_name:
            raise SerialiserError(
                f"The member name '{line.member_name}' contains {ECR_DELIMITER}, the field "
                "separator. The file would be unparseable."
            )
        fields = [
            line.uan,
            line.member_name,
            _whole(line.gross_wages),
            _whole(line.epf_wages),
            _whole(line.eps_wages),
            _whole(line.edli_wages),
            _whole(line.employee_share),
            _whole(line.eps_contribution),
            _whole(line.employer_epf_share),
            _ncp(line.ncp_days),
            _whole(line.refund_of_advances),
        ]
        if len(fields) != len(ECR_COLUMNS):
            raise SerialiserError(
                f"An ECR line has {len(fields)} fields; the format takes {len(ECR_COLUMNS)}."
            )
        lines.append(ECR_DELIMITER.join(fields))

    # CRLF, because the uploader is a Windows tool and a bare LF file is read
    # as one very long line.
    return "\r\n".join(lines) + ("\r\n" if lines else "")


#: The header of the ECR upload sheet HR keeps in Excel and converts with the
#: EPFO's tool ("CHALLAN UPLOAD.csv"): the same eleven fields as the text file,
#: named the way that sheet names them.
ECR_UPLOAD_CSV_HEADER = (
    "UAN",
    "MEMBER_NAME",
    "GROSS_WAGES",
    "EPF_WAGES",
    "EPS_WAGES",
    "EDLI_WAGES",
    "EPF_CONTRI_REMITTED",
    "EPS_CONTRI_REMITTED",
    "EPF_EPS_DIFF_REMITTED",
    "NCP_DAYS",
    "REFUND_OF_ADVANCES",
)


def serialise_ecr_upload_csv(ecr: EcrReturn) -> str:
    """The ECR as the upload CSV: a header row, then the text file's fields comma-separated.

    EPF_CONTRI_REMITTED is the member's share, EPS_CONTRI_REMITTED the pension
    share and EPF_EPS_DIFF_REMITTED the employer's EPF share, as in the ECR.
    """
    rows = [",".join(ECR_UPLOAD_CSV_HEADER)]
    for line in ecr.lines:
        rows.append(
            ",".join(
                [
                    line.uan,
                    _csv_safe(line.member_name),
                    _whole(line.gross_wages),
                    _whole(line.epf_wages),
                    _whole(line.eps_wages),
                    _whole(line.edli_wages),
                    _whole(line.employee_share),
                    _whole(line.eps_contribution),
                    _whole(line.employer_epf_share),
                    _ncp(line.ncp_days),
                    _whole(line.refund_of_advances),
                ]
            )
        )
    return "\r\n".join(rows) + "\r\n"


# The EPFO's arrear ECR line, in order: the arrear wages and shares only, in
# the same delimiter. Eight fields.
ARREAR_ECR_COLUMNS = (
    "uan",
    "member_name",
    "arrear_epf_wages",
    "arrear_eps_wages",
    "arrear_edli_wages",
    "arrear_employee_share",
    "arrear_employer_epf_share",
    "arrear_eps_contribution",
)


def serialise_arrear_ecr(ecr: ArrearEcrReturn) -> str:
    """The arrear ECR text file: `#~#`-delimited, CRLF, one line per member, no header."""
    lines: list[str] = []
    for line in ecr.lines:
        if ECR_DELIMITER in line.member_name:
            raise SerialiserError(
                f"The member name '{line.member_name}' contains {ECR_DELIMITER}, the field "
                "separator. The file would be unparseable."
            )
        fields = [
            line.uan,
            line.member_name,
            _whole(line.epf_wages),
            _whole(line.eps_wages),
            _whole(line.edli_wages),
            _whole(line.employee_share),
            _whole(line.employer_epf_share),
            _whole(line.eps_contribution),
        ]
        if len(fields) != len(ARREAR_ECR_COLUMNS):
            raise SerialiserError(
                f"An arrear ECR line has {len(fields)} fields; the format takes "
                f"{len(ARREAR_ECR_COLUMNS)}."
            )
        lines.append(ECR_DELIMITER.join(fields))
    return "\r\n".join(lines) + ("\r\n" if lines else "")


# ═══════════════════════════════════════════════════════════════════════════
# ESIC — comma-delimited with a header
# ═══════════════════════════════════════════════════════════════════════════

ESIC_COLUMNS = (
    "IP Number",
    "IP Name",
    "No of Days for which wages paid/payable during the month",
    "Total Monthly Wages",
    "Reason Code for Zero workings days",
    "Last Working Day",
)


def serialise_esic(esic: EsicReturn) -> str:
    """The ESIC monthly contribution file.

    Unlike the ECR this one carries a header row, and the portal matches on the
    header text — which is why the column names above are the portal's wording
    rather than anything shorter.
    """
    rows = [",".join(ESIC_COLUMNS)]

    for line in esic.lines:
        last_day = line.last_working_day.strftime("%d/%m/%Y") if line.last_working_day else ""
        rows.append(
            ",".join(
                [
                    line.ip_number,
                    _csv_safe(line.ip_name),
                    _whole(line.days_worked),
                    _whole(line.wages_paid),
                    str(line.reason_code),
                    last_day,
                ]
            )
        )

    return "\r\n".join(rows) + "\r\n"


def _csv_safe(value: str) -> str:
    """A comma inside a name would shift every column after it."""
    if "," in value or '"' in value:
        escaped = value.replace('"', '""')
        return f'"{escaped}"'
    return value


# ── ESIC MC template — the portal's own Excel 97-2003 upload sheet ──────────

#: The portal's "MC_Template.xls" headings, word for word, spaces and line
#: breaks included: the uploader reads the first sheet by position, and HR
#: compares the file with the template they downloaded from the portal.
ESIC_MC_HEADINGS = (
    "IP Number ",
    "IP Name\n( Only alphabets and space )",
    "No of Days for which wages paid/payable during the month",
    "Total Monthly Wages",
    " Reason Code for Zero workings days(numeric only; provide 0 for all other reasons- "
    "Click on the link for reference)",
    " Last Working Day\n( Format DD/MM/YYYY  or DD-MM-YYYY)",
)
#: The template's column widths, in Excel's 1/256-character units.
ESIC_MC_WIDTHS = (4398, 8075, 7214, 6609, 7819, 7098)

#: The template's second sheet: the reason codes and when a last working day goes in.
ESIC_REASON_CODES: tuple[tuple[str, int, str], ...] = (
    ("Without Reason", 0, "Leave last working day as blank"),
    ("On Leave", 1, "Leave last working day as blank"),
    (
        "Left Service",
        2,
        "Please provide last working day (dd/mm/yyyy). IP will not appear from next wage period",
    ),
    (
        "Retired",
        3,
        "Please provide last working day (dd/mm/yyyy). IP will not appear from next wage period",
    ),
    (
        "Out of Coverage",
        4,
        "Please provide last working day (dd/mm/yyyy). IP will not appear from next "
        "contribution period. This option is valid only if Wage Period is April/October. In "
        "case any other month then IP will continue to appear in the list",
    ),
    (
        "Expired",
        5,
        "Please provide last working day (dd/mm/yyyy). IP will not appear from next wage period",
    ),
    ("Non Implemented area", 6, "Please provide last working day (dd/mm/yyyy). "),
    ("Compliance by Immediate Employer", 7, "Leave last working day as blank"),
    ("Suspension of work", 8, "Leave last working day as blank"),
    ("Strike/Lockout", 9, "Leave last working day as blank"),
    (
        "Retrenchment",
        10,
        "Please provide last working day (dd/mm/yyyy). IP will not appear from next wage period",
    ),
    ("No Work", 11, "Leave last working day as blank"),
    ("Doesnt Belong To This Employer", 12, "Leave last working day as blank"),
    ("Duplicate IP", 13, "Leave last working day as blank"),
)
ESIC_MC_INSTRUCTIONS = (
    "Instructions to fill in the excel file: ",
    "1. Enter the IP number,  IP name, No. of Days, Total Monthly Wages, Reason for 0 "
    "wages(If Wages '0') & Last Working Day( only if employee has left service, Retired, Out "
    "of coverage, Expired, Non-Implemented area or Retrenchment. For other reasons,  last "
    "working day  must be left  BLANK).  ",
    "2. Number of days must me a whole number.  Fractions should be rounded up to next "
    "higher whole number/integer",
    "3. Excel sheet upload will lead to successful transaction only when all the Employees' "
    "(who are currently mapped in the system) details are entered perfectly in the excel sheet",
    "4. Reasons are to be assigned numeric code  and date has to be provided as mentioned in "
    "the table above",
    "5. Once  0 wages given and last working day is mentioned as in reason codes (2,3,4,5,10)  "
    "IP will be removed from the employer's record. Subsequent months will not have this IP "
    "listed under the employer. Last working day should be mentioned only if 'Number of days "
    "wages paid/payable' is '0'.",
    "6. In case IP has worked for part of the month(i.e. atleast 1 day wage is paid/payable) "
    "and left in between of the month, then last working day shouldn't be mentioned.",
    "7. Calculations - IP Contribution and Employer contribution calculation will be "
    "automatically done by the system",
    "8. Date  column format is  dd/mm/yyyy or dd-mm-yyyy.  Pad single digit dates with 0.  "
    "Eg:- 2/5/2010  or  2-May-2010 is NOT acceptable.  Correct format  is 02/05/2010  or "
    "02-05-2010",
    "9. Excel file should be saved in .xls format (Excel 97-2003)",
    "10. Note that all the column including date column should be in 'Text' format",
)


def _days_up(value: Decimal) -> int:
    """The template's rule 2: a fraction of a day rounds up to the next whole day."""
    return int(value.quantize(Decimal(1), rounding=ROUND_CEILING))


def serialise_esic_mc_xls(esic: EsicReturn) -> bytes:
    """The ESIC monthly contribution upload, as the portal's MC_Template.xls.

    Excel 97-2003 (.xls), which is all the portal takes. Sheet 1 is the data in
    the template's six columns, every cell in Text format (rule 10) with days
    and wages as numbers, as the template itself holds them. The reason code
    and last working day are written only for an IP with no days that month:
    the template's rules 5 and 6 say a last working day goes in only then, and
    an IP paid for part of the month is left blank there. Sheet 2 is the
    template's reason-code table and instructions.
    """
    import xlwt  # Imported here: only this download needs the .xls writer.

    book = xlwt.Workbook(encoding="utf-8")
    sheet = book.add_sheet("Sheet1")
    text = xlwt.easyxf(num_format_str="@")
    heading = xlwt.easyxf(
        "font: bold on; align: wrap on, vert top; "
        "borders: left thin, right thin, top thin, bottom thin",
        num_format_str="@",
    )
    for column, (title, width) in enumerate(zip(ESIC_MC_HEADINGS, ESIC_MC_WIDTHS, strict=True)):
        sheet.write(0, column, title, heading)
        sheet.col(column).width = width
    sheet.row(0).height_mismatch = True
    sheet.row(0).height = 256 * 4

    for row, line in enumerate(esic.lines, start=1):
        days = _days_up(line.days_worked)
        sheet.write(row, 0, line.ip_number, text)
        sheet.write(row, 1, line.ip_name, text)
        sheet.write(row, 2, days, text)
        sheet.write(row, 3, int(_whole(line.wages_paid)), text)
        if days == 0:
            sheet.write(row, 4, line.reason_code, text)
            if line.last_working_day is not None:
                sheet.write(row, 5, line.last_working_day.strftime("%d/%m/%Y"), text)

    guide = book.add_sheet("Instructions & Reason Codes")
    bold = xlwt.easyxf("font: bold on")
    for column, title in enumerate(("Reason", "Code", "Note")):
        guide.write(1, column, title, bold)
    for row, (reason, code, note) in enumerate(ESIC_REASON_CODES, start=2):
        guide.write(row, 0, reason)
        guide.write(row, 1, code)
        guide.write(row, 2, note)
    first = 2 + len(ESIC_REASON_CODES) + 3
    wrap = xlwt.easyxf("align: wrap on, vert top")
    for offset, instruction in enumerate(ESIC_MC_INSTRUCTIONS):
        row = first + offset
        guide.write_merge(row, row, 0, 6, instruction, bold if offset == 0 else wrap)
        if offset:
            guide.row(row).height_mismatch = True
            guide.row(row).height = 256 * 2
    guide.col(0).width = 6190
    guide.col(1).width = 2350
    guide.col(2).width = 20666

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# ═══════════════════════════════════════════════════════════════════════════
# Professional Tax — per state, so a text summary plus a per-employee list
# ═══════════════════════════════════════════════════════════════════════════


def serialise_pt_challan(challan: PtChallan) -> str:
    """A state's Professional Tax challan.

    There is no national format, and no state publishes a machine-readable one
    that is stable enough to hardcode. What is produced is the figures the
    state's own form asks for, in a form an HR user transcribes into the state
    portal — which is what actually happens in practice, because most state PT
    portals have no bulk upload at all.

    Where a state does gain a bulk format, it gets its own function here and the
    query layer is untouched.
    """
    header = [
        f"Professional Tax — {challan.state_code}",
        f"Registration number: {challan.registration_number or 'NOT ON RECORD'}",
        f"Period: {challan.period}",
        f"Employees: {challan.employee_count}",
        f"Total tax: {_paise(challan.total_tax)}",
    ]

    if challan.slab_counts:
        header.append("")
        header.append("Employee count by slab:")
        header.extend(f"  {slab}: {count}" for slab, count in sorted(challan.slab_counts.items()))

    body = ["", "Employee code,Name,Gross wages,Tax deducted"]
    body.extend(
        ",".join(
            [
                line.employee_code,
                _csv_safe(line.employee_name),
                _paise(line.gross_wages),
                _paise(line.tax_deducted),
            ]
        )
        for line in challan.lines
    )

    return "\r\n".join(header + body) + "\r\n"


# ═══════════════════════════════════════════════════════════════════════════
# TDS 24Q — the FVU-compatible flat file
# ═══════════════════════════════════════════════════════════════════════════
#
# The real format is caret-delimited with numbered record types: a file header
# (FH), a batch header (BH), then challan (CD) and deductee (DD) records, and
# for Q4 a salary-detail (SD) record per employee. What matters structurally —
# and what the golden-file tests pin — is that the record types appear in that
# order, that every deductee sits under a challan, and that SD records appear
# in Q4 and only in Q4.


def serialise_tds_24q(return_: Tds24QReturn) -> str:
    """The 24Q flat file.

    Annexure II (the SD records) is written when, and only when, the return is
    for the fourth quarter. Producing it in Q1 would be filing figures for a
    year that has not finished.
    """
    if return_.quarter not in (1, 2, 3, 4):
        raise SerialiserError(f"Q{return_.quarter} is not a quarter.")
    if return_.salary_details and not return_.includes_annexure_ii:
        raise SerialiserError(
            f"Annexure II salary details were supplied for Q{return_.quarter}. They "
            "belong in the Q4 return only."
        )

    rows: list[str] = []

    rows.append("^".join(["FH", "24Q", return_.tan, return_.financial_year, f"Q{return_.quarter}"]))
    rows.append(
        "^".join(
            [
                "BH",
                return_.deductor_name,
                return_.pan,
                str(len(return_.challans)),
                str(len(return_.deductees)),
            ]
        )
    )

    for challan in return_.challans:
        rows.append(
            "^".join(
                [
                    "CD",
                    str(challan.serial),
                    challan.bsr_code,
                    challan.deposit_date.strftime("%d%m%Y"),
                    challan.challan_serial,
                    _paise(challan.tds_amount),
                    _paise(challan.surcharge),
                    _paise(challan.education_cess),
                    _paise(challan.interest),
                    _paise(challan.late_fee),
                    _paise(challan.total_amount),
                    "Y" if challan.is_book_entry else "N",
                ]
            )
        )

        for deductee in return_.deductees:
            if deductee.challan_serial_in_file != challan.serial:
                continue
            rows.append(
                "^".join(
                    [
                        "DD",
                        str(challan.serial),
                        str(deductee.serial),
                        deductee.pan,
                        _csv_safe(deductee.name),
                        _paise(deductee.amount_paid),
                        _paise(deductee.tds_deducted),
                        _paise(deductee.tds_deposited),
                        deductee.deduction_date.strftime("%d%m%Y"),
                        _paise(deductee.rate),
                    ]
                )
            )

    if return_.includes_annexure_ii:
        for detail in return_.salary_details:
            rows.append(
                "^".join(
                    [
                        "SD",
                        detail.pan,
                        _csv_safe(detail.name),
                        detail.regime,
                        _paise(detail.gross_salary),
                        _paise(detail.exempt_allowances),
                        _paise(detail.standard_deduction),
                        _paise(detail.income_from_salary),
                        _paise(detail.other_income),
                        _paise(detail.gross_total_income),
                        _paise(detail.chapter_via_deductions),
                        _paise(detail.taxable_income),
                        _paise(detail.tax_payable),
                        _paise(detail.relief),
                        _paise(detail.total_tds_deducted),
                    ]
                )
            )

    rows.append("^".join(["FT", str(len(rows) + 1), _paise(return_.total_tds)]))
    return "\r\n".join(rows) + "\r\n"
