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
    "serialise_esic",
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
