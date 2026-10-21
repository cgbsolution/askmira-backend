"""Pre-generation validation.

The feedback loop through a government portal is slow, unhelpful and generally
happens on a deadline: upload, wait, get a rejection naming a row number and a
code, look the code up, guess which of forty employees it means. Everything
this module catches is something the portal would have caught a day later and
told you less about.

So generation runs this first and **refuses on any blocking exception**. The
list it returns is meant to be fixable: every exception names the employee, the
field and what is wrong with it.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

__all__ = [
    "FilingException",
    "Severity",
    "ValidationResult",
    "validate_ecr_lines",
    "validate_esic_lines",
    "validate_pt_challan",
    "validate_tds_return",
]


class Severity(str, Enum):
    # Generation refuses. The portal would reject the file.
    BLOCKING = "blocking"
    # Generation proceeds. The file is valid but something is worth a look.
    WARNING = "warning"


# A PAN is five letters, four digits, one letter. The fourth character encodes
# the holder type and the fifth is the surname initial, neither of which is
# checked here — the format check is what catches the typos.
PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
# A UAN is twelve digits.
UAN_PATTERN = re.compile(r"^[0-9]{12}$")
# The member name has to match the UAN database. Only the characters it accepts
# can be sent; anything else is the single most common ECR rejection.
ECR_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z .]*$")


@dataclass(frozen=True, slots=True)
class FilingException:
    """One fixable problem, named precisely enough to act on."""

    severity: Severity
    code: str
    subject: str
    field: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        return data


@dataclass(frozen=True, slots=True)
class ValidationResult:
    exceptions: list[FilingException]

    @property
    def blocking(self) -> list[FilingException]:
        return [e for e in self.exceptions if e.severity is Severity.BLOCKING]

    @property
    def warnings(self) -> list[FilingException]:
        return [e for e in self.exceptions if e.severity is Severity.WARNING]

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocking)

    def as_list(self) -> list[dict[str, Any]]:
        return [e.as_dict() for e in self.exceptions]

    def summary(self) -> str:
        if not self.exceptions:
            return "No exceptions."
        parts = []
        if self.blocking:
            parts.append(f"{len(self.blocking)} blocking")
        if self.warnings:
            parts.append(f"{len(self.warnings)} warning")
        return ", ".join(parts)


# ── PF ECR ──────────────────────────────────────────────────────────────────


def validate_ecr_lines(rows: list[dict[str, Any]]) -> ValidationResult:
    """Everything the EPFO portal will reject an ECR for.

    Each row is `{"subject", "uan", "member_name", "gross_wages", "epf_wages",
    "eps_wages", "edli_wages", "employee_share", "employer_epf_share",
    "eps_contribution", "ncp_days", "paid_days"}`.
    """
    found: list[FilingException] = []
    seen_uans: dict[str, str] = {}

    for row in rows:
        subject = str(row.get("subject", "unknown"))
        uan = (row.get("uan") or "").strip()

        if not uan:
            found.append(
                FilingException(
                    Severity.BLOCKING,
                    "UAN_MISSING",
                    subject,
                    "uan",
                    "No UAN on record. The ECR cannot carry a blank UAN, and the "
                    "portal rejects the whole file rather than the row.",
                )
            )
        elif not UAN_PATTERN.match(uan):
            found.append(
                FilingException(
                    Severity.BLOCKING,
                    "UAN_MALFORMED",
                    subject,
                    "uan",
                    f"'{uan}' is not a twelve-digit UAN.",
                )
            )
        elif uan in seen_uans:
            found.append(
                FilingException(
                    Severity.BLOCKING,
                    "UAN_DUPLICATE",
                    subject,
                    "uan",
                    f"UAN {uan} is also on {seen_uans[uan]}. Two employees share a "
                    "UAN, which means one of the records is wrong.",
                )
            )
        else:
            seen_uans[uan] = subject

        name = (row.get("member_name") or "").strip()
        if not name:
            found.append(
                FilingException(
                    Severity.BLOCKING,
                    "NAME_MISSING",
                    subject,
                    "member_name",
                    "No member name.",
                )
            )
        elif not ECR_NAME_PATTERN.match(name):
            found.append(
                FilingException(
                    Severity.BLOCKING,
                    "NAME_UNACCEPTABLE",
                    subject,
                    "member_name",
                    f"'{name}' contains characters the UAN database does not hold. A "
                    "name mismatch is the most common ECR rejection, so it is caught "
                    "here rather than at the portal.",
                )
            )

        for field_name in (
            "gross_wages",
            "epf_wages",
            "eps_wages",
            "edli_wages",
            "employee_share",
            "employer_epf_share",
            "eps_contribution",
            "ncp_days",
        ):
            value = row.get(field_name)
            if value is None:
                continue
            if not isinstance(value, Decimal):
                found.append(
                    FilingException(
                        Severity.BLOCKING,
                        "NOT_DECIMAL",
                        subject,
                        field_name,
                        f"{field_name} is not a Decimal. Money in a statutory return "
                        "is never a float.",
                    )
                )
            elif value < 0:
                found.append(
                    FilingException(
                        Severity.BLOCKING,
                        "NEGATIVE_VALUE",
                        subject,
                        field_name,
                        f"{field_name} is {value}. The portal rejects negative values.",
                    )
                )

        # There was a warning here for EPF, EPS and EDLI wages being identical, on
        # the theory that it signals three columns filled from one number. It was
        # removed: when the three ceilings are equal — which is the common
        # configuration — every employee above the ceiling legitimately has three
        # identical figures, so the warning fired on most of the establishment.
        # A check that cannot tell the correct case from the broken one is noise,
        # and noise in an exception list is what makes people stop reading it. The
        # aliasing risk is covered where it can actually be seen: `wages.py`
        # derives each figure on its own basis, and a test asserts four distinct
        # numbers against three distinct ceilings.

        ncp = row.get("ncp_days")
        paid = row.get("paid_days")
        if isinstance(ncp, Decimal) and isinstance(paid, Decimal) and ncp > 0 and paid <= 0:
            found.append(
                FilingException(
                    Severity.BLOCKING,
                    "NCP_INCONSISTENT",
                    subject,
                    "ncp_days",
                    f"{ncp} non-contributing days with no paid days at all. Either the "
                    "employee should not be on this return or attendance is wrong.",
                )
            )

    return ValidationResult(found)


# ── ESIC ────────────────────────────────────────────────────────────────────


def validate_esic_lines(rows: list[dict[str, Any]]) -> ValidationResult:
    found: list[FilingException] = []
    for row in rows:
        subject = str(row.get("subject", "unknown"))
        ip_number = (row.get("ip_number") or "").strip()

        if not ip_number:
            found.append(
                FilingException(
                    Severity.BLOCKING,
                    "IP_MISSING",
                    subject,
                    "ip_number",
                    "No ESI insurance number. An employee owed a contribution with no "
                    "IP number is an enrolment that was never completed.",
                )
            )
        elif not ip_number.isdigit():
            found.append(
                FilingException(
                    Severity.BLOCKING,
                    "IP_MALFORMED",
                    subject,
                    "ip_number",
                    f"'{ip_number}' is not a numeric insurance number.",
                )
            )

        for field_name in (
            "days_worked",
            "wages_paid",
            "employee_contribution",
            "employer_contribution",
        ):
            value = row.get(field_name)
            if isinstance(value, Decimal) and value < 0:
                found.append(
                    FilingException(
                        Severity.BLOCKING,
                        "NEGATIVE_VALUE",
                        subject,
                        field_name,
                        f"{field_name} is {value}.",
                    )
                )

        days = row.get("days_worked")
        wages = row.get("wages_paid")
        if isinstance(days, Decimal) and isinstance(wages, Decimal):
            if days == 0 and wages > 0:
                found.append(
                    FilingException(
                        Severity.BLOCKING,
                        "WAGES_WITHOUT_DAYS",
                        subject,
                        "days_worked",
                        f"Wages of {wages} against zero days worked.",
                    )
                )
            if days > 0 and wages == 0:
                found.append(
                    FilingException(
                        Severity.WARNING,
                        "DAYS_WITHOUT_WAGES",
                        subject,
                        "wages_paid",
                        f"{days} days worked but no wages. Correct for unpaid leave "
                        "that still counts as service; worth confirming.",
                    )
                )

    return ValidationResult(found)


# ── Professional Tax ────────────────────────────────────────────────────────


def validate_pt_challan(
    *, state_code: str, registration_number: str | None, rows: list[dict[str, Any]]
) -> ValidationResult:
    found: list[FilingException] = []

    if not registration_number:
        found.append(
            FilingException(
                Severity.BLOCKING,
                "PT_REGISTRATION_MISSING",
                state_code,
                "registration_number",
                f"No Professional Tax registration number for {state_code}. The challan "
                "cannot be filed without the state's own registration.",
            )
        )

    for row in rows:
        subject = str(row.get("subject", "unknown"))
        tax = row.get("tax_deducted")
        if isinstance(tax, Decimal) and tax < 0:
            found.append(
                FilingException(
                    Severity.BLOCKING,
                    "NEGATIVE_VALUE",
                    subject,
                    "tax_deducted",
                    f"Professional Tax of {tax}.",
                )
            )

    return ValidationResult(found)


# ── TDS 24Q ─────────────────────────────────────────────────────────────────


def validate_tds_return(
    *,
    tan: str | None,
    quarter: int,
    deductees: list[dict[str, Any]],
    unmapped_deductions: list[dict[str, Any]],
    salary_details: list[dict[str, Any]] | None = None,
) -> ValidationResult:
    """Everything that stops a 24Q being filed.

    `unmapped_deductions` is the important one: a deduction with no challan
    behind it means the return cannot be filed at all, and the portal's message
    about it is not going to say which employee.
    """
    found: list[FilingException] = []

    if not tan:
        found.append(
            FilingException(
                Severity.BLOCKING,
                "TAN_MISSING",
                "deductor",
                "tan",
                "No TAN on the legal entity. A 24Q is filed against a TAN.",
            )
        )

    for row in unmapped_deductions:
        found.append(
            FilingException(
                Severity.BLOCKING,
                "CHALLAN_UNMAPPED",
                str(row.get("subject", "unknown")),
                "challan",
                f"TDS of {row.get('amount')} for {row.get('period')} is not mapped to a "
                "challan. Record the challan it was deposited under — the return cannot "
                "be filed with an unmapped deduction.",
            )
        )

    seen_pans: dict[str, str] = {}
    for row in deductees:
        subject = str(row.get("subject", "unknown"))
        pan = (row.get("pan") or "").strip().upper()

        if not pan:
            found.append(
                FilingException(
                    Severity.BLOCKING,
                    "PAN_MISSING",
                    subject,
                    "pan",
                    "No PAN on record. A missing PAN attracts deduction at the higher "
                    "rate, which is a payroll correction and not something to file "
                    "around.",
                )
            )
        elif not PAN_PATTERN.match(pan):
            found.append(
                FilingException(
                    Severity.BLOCKING,
                    "PAN_INVALID",
                    subject,
                    "pan",
                    f"'{pan}' is not a valid PAN. An invalid PAN is treated as no PAN, "
                    "with the same higher-rate consequence.",
                )
            )
        elif pan in seen_pans and seen_pans[pan] != subject:
            found.append(
                FilingException(
                    Severity.BLOCKING,
                    "PAN_DUPLICATE",
                    subject,
                    "pan",
                    f"PAN {pan} is also on {seen_pans[pan]}.",
                )
            )
        else:
            seen_pans[pan] = subject

        tds = row.get("tds_deducted")
        deposited = row.get("tds_deposited")
        if isinstance(tds, Decimal) and isinstance(deposited, Decimal) and tds != deposited:
            found.append(
                FilingException(
                    Severity.BLOCKING,
                    "TDS_MISMATCH",
                    subject,
                    "tds_deposited",
                    f"{tds} deducted but {deposited} mapped to challans. Every rupee "
                    "deducted has to appear against a challan.",
                )
            )

    # ── Annexure II ─────────────────────────────────────────────────────────
    details = salary_details or []
    if quarter == 4 and not details:
        found.append(
            FilingException(
                Severity.BLOCKING,
                "ANNEXURE_II_MISSING",
                "return",
                "salary_details",
                "The Q4 return carries Annexure II, the annual salary detail, and it is empty.",
            )
        )
    if quarter != 4 and details:
        found.append(
            FilingException(
                Severity.BLOCKING,
                "ANNEXURE_II_UNEXPECTED",
                "return",
                "salary_details",
                f"Annexure II was supplied for Q{quarter}. It belongs in the fourth "
                "quarter's return only — the year's figures do not exist yet.",
            )
        )

    return ValidationResult(found)
