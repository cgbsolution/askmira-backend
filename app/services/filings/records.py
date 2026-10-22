"""Canonical intermediate structures, between the query and the serialiser.

The pipeline for every return is the same three steps:

    query  →  canonical structure  →  format-specific serialiser

The middle layer is the point of the design. Portal formats change without
notice, usually during a filing window, and the change always arrives as "the
file now needs another column" — never as "compute the wages differently". With
this layer, that patch touches one serialiser. Without it, the query, the
arithmetic and the file layout are the same code and the patch touches all
three under time pressure.

Nothing here knows anything about pipes, fixed-width fields or line endings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

__all__ = [
    "ArrearEcrLine",
    "ArrearEcrReturn",
    "EcrLine",
    "EcrReturn",
    "EsicLine",
    "EsicReturn",
    "PtChallan",
    "PtLine",
    "Tds24QReturn",
    "TdsChallanLine",
    "TdsDeducteeLine",
    "TdsSalaryDetail",
]


# ── PF ECR ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EcrLine:
    uan: str
    member_name: str
    gross_wages: Decimal
    epf_wages: Decimal
    eps_wages: Decimal
    edli_wages: Decimal
    employee_share: Decimal
    employer_epf_share: Decimal
    eps_contribution: Decimal
    ncp_days: Decimal
    # Non-zero only in the month an employee leaves, and only where the refund
    # applies. Kept explicit because the column exists and a blank is not the
    # same as a zero to the portal.
    refund_of_advances: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class EcrReturn:
    establishment_code: str
    wage_month: str  # "062026"
    lines: list[EcrLine] = field(default_factory=list)

    @property
    def total_epf_wages(self) -> Decimal:
        return sum((line.epf_wages for line in self.lines), start=Decimal(0))

    @property
    def total_contributions(self) -> Decimal:
        return sum(
            (
                line.employee_share + line.employer_epf_share + line.eps_contribution
                for line in self.lines
            ),
            start=Decimal(0),
        )


@dataclass(frozen=True, slots=True)
class ArrearEcrLine:
    """One member's PF arrears paid in a month, over every month they cover."""

    uan: str
    member_name: str
    epf_wages: Decimal
    eps_wages: Decimal
    edli_wages: Decimal
    employee_share: Decimal
    employer_epf_share: Decimal
    eps_contribution: Decimal


@dataclass(frozen=True, slots=True)
class ArrearEcrReturn:
    establishment_code: str
    # The month the arrears were paid with, "102026".
    wage_month: str
    lines: list[ArrearEcrLine] = field(default_factory=list)

    @property
    def total_contributions(self) -> Decimal:
        return sum(
            (
                line.employee_share + line.employer_epf_share + line.eps_contribution
                for line in self.lines
            ),
            start=Decimal(0),
        )


# ── ESIC ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EsicLine:
    ip_number: str
    ip_name: str
    days_worked: Decimal
    wages_paid: Decimal
    employee_contribution: Decimal
    employer_contribution: Decimal
    # Set where the employee is above the ceiling but still contributing
    # because the contribution period has not ended. The portal does not carry
    # this, but the exception list and the audit trail do — an auditor asking
    # why a high earner appears gets an answer.
    continuing_reason: str | None = None
    # 0 unless the employee left or was otherwise excluded mid-month; the ESIC
    # format carries a reason code for that.
    reason_code: int = 0
    last_working_day: date | None = None


@dataclass(frozen=True, slots=True)
class EsicReturn:
    employer_code: str
    contribution_period: str
    wage_month: str
    lines: list[EsicLine] = field(default_factory=list)

    @property
    def total_wages(self) -> Decimal:
        return sum((line.wages_paid for line in self.lines), start=Decimal(0))

    @property
    def total_contributions(self) -> Decimal:
        return sum(
            (line.employee_contribution + line.employer_contribution for line in self.lines),
            start=Decimal(0),
        )


# ── Professional Tax ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PtLine:
    employee_code: str
    employee_name: str
    gross_wages: Decimal
    tax_deducted: Decimal


@dataclass(frozen=True, slots=True)
class PtChallan:
    """One state's challan for one month.

    There is no national Professional Tax format — every state prescribes its
    own, and several require an employee-count slab rather than a per-employee
    list. `slab_counts` carries that where the state asks for it.
    """

    state_code: str
    registration_number: str | None
    period: str
    lines: list[PtLine] = field(default_factory=list)
    # {"up to 15000": 4, "above 15000": 11} — as the state's own form words it.
    slab_counts: dict[str, int] = field(default_factory=dict)

    @property
    def employee_count(self) -> int:
        return len(self.lines)

    @property
    def total_tax(self) -> Decimal:
        return sum((line.tax_deducted for line in self.lines), start=Decimal(0))


# ── TDS 24Q ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TdsChallanLine:
    """A challan as the return reports it, with its own serial within the file."""

    serial: int
    bsr_code: str
    deposit_date: date
    challan_serial: str
    tds_amount: Decimal
    surcharge: Decimal
    education_cess: Decimal
    interest: Decimal
    late_fee: Decimal
    total_amount: Decimal
    is_book_entry: bool = False


@dataclass(frozen=True, slots=True)
class TdsDeducteeLine:
    """Annexure I: one deductee's payment and deduction under one challan."""

    challan_serial_in_file: int
    serial: int
    employee_code: str
    pan: str
    name: str
    amount_paid: Decimal
    tds_deducted: Decimal
    tds_deposited: Decimal
    deduction_date: date
    # The rate is reported, so a mismatch between rate and amount is visible.
    rate: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class TdsSalaryDetail:
    """Annexure II: the annual salary breakdown, Q4 only.

    This is where the year's declarations and exemptions from slice 07 surface
    in a statutory return. Filing it in Q1 is not merely unnecessary — the
    figures do not exist yet, because the year has not happened.
    """

    employee_code: str
    pan: str
    name: str
    regime: str
    gross_salary: Decimal
    exempt_allowances: Decimal
    standard_deduction: Decimal
    income_from_salary: Decimal
    other_income: Decimal
    gross_total_income: Decimal
    chapter_via_deductions: Decimal
    taxable_income: Decimal
    tax_payable: Decimal
    relief: Decimal
    total_tds_deducted: Decimal


@dataclass(frozen=True, slots=True)
class Tds24QReturn:
    tan: str
    pan: str
    financial_year: str
    quarter: int
    deductor_name: str
    challans: list[TdsChallanLine] = field(default_factory=list)
    deductees: list[TdsDeducteeLine] = field(default_factory=list)
    # Empty for Q1-Q3. The serialiser writes the Annexure II block only when
    # this is populated, and `includes_annexure_ii` says which is expected.
    salary_details: list[TdsSalaryDetail] = field(default_factory=list)

    @property
    def includes_annexure_ii(self) -> bool:
        """Annexure II belongs in the fourth quarter's return only."""
        return self.quarter == 4

    @property
    def total_tds(self) -> Decimal:
        return sum((challan.tds_amount for challan in self.challans), start=Decimal(0))
