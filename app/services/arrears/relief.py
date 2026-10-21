"""Section 89(1) relief on arrears, and the Form 10E working.

The problem it solves: arrears are taxable in the year of receipt. An employee
paid three years of back pay in one year can be pushed into a higher slab for
money they earned when they were in a lower one — a tax bill created by the
delay rather than by the income.

Relief recomputes as if the arrear had been received in the years it relates to,
and refunds the difference. Two things about that:

- **It is not always beneficial.** If the employee's income was higher in the
  earlier years, spreading back costs more. So both figures are computed and the
  better one taken, with `is_beneficial` recording which. A relief of zero is a
  correct answer, not a failure.
- **The historical income figures come from the employer's own records where it
  has them, and from the employee where it does not.** An employee who joined
  two years ago has no 2023 salary here; they declare it, and Form 10E is a
  declaration they sign.

No tax rate, slab or rebate appears in this file. Every one comes from the
effective-dated `PayrollRuleSet` for the year in question — which is the point:
relief compares years, and each year has its own rates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "ReliefError",
    "ReliefResult",
    "YearAllocation",
    "compute_relief",
]

ZERO = Decimal(0)
RUPEE = Decimal("0.01")


class ReliefError(ValueError):
    """Relief cannot be computed from what has been supplied."""


def _money(value: Decimal) -> Decimal:
    return value.quantize(RUPEE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class YearAllocation:
    """How much of the arrear belongs to one past year, and that year's tax."""

    financial_year: str
    # What the employee actually declared or was paid that year, before the
    # arrear is added back.
    original_income: Decimal
    arrear_portion: Decimal
    tax_on_original: Decimal
    tax_with_arrear: Decimal

    @property
    def additional_tax(self) -> Decimal:
        return self.tax_with_arrear - self.tax_on_original

    def as_dict(self) -> dict[str, Any]:
        return {
            "financial_year": self.financial_year,
            "original_income": str(self.original_income),
            "arrear_portion": str(self.arrear_portion),
            "tax_on_original": str(self.tax_on_original),
            "tax_with_arrear": str(self.tax_with_arrear),
            "additional_tax": str(self.additional_tax),
        }


@dataclass(frozen=True, slots=True)
class ReliefResult:
    arrear_amount: Decimal
    receipt_year: str
    # Tax in the year of receipt, with the whole arrear taxed there.
    tax_without_relief: Decimal
    # Tax if the arrear had been received in the years it relates to.
    tax_with_spreading: Decimal
    relief_amount: Decimal
    is_beneficial: bool
    allocations: list[YearAllocation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_working(self) -> dict[str, Any]:
        """The Form 10E working.

        Form 10E is a declaration the employee signs, so they are entitled to
        see how the figure was arrived at year by year — not just the total.
        """
        return {
            "arrear_amount": str(self.arrear_amount),
            "receipt_year": self.receipt_year,
            "tax_without_relief": str(self.tax_without_relief),
            "tax_with_spreading": str(self.tax_with_spreading),
            "relief_amount": str(self.relief_amount),
            "is_beneficial": self.is_beneficial,
            "allocations": [a.as_dict() for a in self.allocations],
            "notes": self.notes,
        }


def compute_relief(
    *,
    arrear_amount: Decimal,
    receipt_year: str,
    income_in_receipt_year: Decimal,
    tax_on_receipt_year_income: Decimal,
    tax_on_receipt_year_income_with_arrear: Decimal,
    allocations: list[YearAllocation],
) -> ReliefResult:
    """Relief under section 89(1).

    Every tax figure is passed in, already computed against the rule set for
    the year it belongs to. This function does the comparison, not the tax —
    which is what keeps a rate for 2023 out of a 2026 computation.

    Args:
        tax_on_receipt_year_income: the receipt year's tax without the arrear.
        tax_on_receipt_year_income_with_arrear: the receipt year's tax with the
            whole arrear taxed there. The difference between these two is what
            the arrear costs if nothing is spread back.
        allocations: one per past year the arrear relates to, each carrying that
            year's own before-and-after tax.
    """
    for name, value in (
        ("arrear_amount", arrear_amount),
        ("income_in_receipt_year", income_in_receipt_year),
        ("tax_on_receipt_year_income", tax_on_receipt_year_income),
        ("tax_on_receipt_year_income_with_arrear", tax_on_receipt_year_income_with_arrear),
    ):
        if not isinstance(value, Decimal):
            raise ReliefError(f"{name} must be a Decimal, not a float")
        if value < ZERO:
            raise ReliefError(f"{name} cannot be negative")

    if arrear_amount == ZERO:
        raise ReliefError("There is no arrear to compute relief on.")

    if not allocations:
        raise ReliefError(
            "Relief spreads an arrear back over the years it relates to, and no years "
            "were supplied. Without them there is nothing to compare against."
        )

    allocated = sum((a.arrear_portion for a in allocations), start=ZERO)
    if allocated != arrear_amount:
        raise ReliefError(
            f"The year allocations total {allocated} but the arrear is {arrear_amount}. "
            "Every rupee has to be attributed to the year it was earned in — an "
            "unallocated remainder would be relieved twice or not at all."
        )

    notes: list[str] = []

    # ── What the arrear costs if it is all taxed on receipt ─────────────────
    tax_without_relief = tax_on_receipt_year_income_with_arrear - tax_on_receipt_year_income

    # ── What it would have cost in the years it was earned ──────────────────
    tax_with_spreading = sum((a.additional_tax for a in allocations), start=ZERO)

    relief = tax_without_relief - tax_with_spreading
    is_beneficial = relief > ZERO

    if not is_beneficial:
        # Common, and correct. It happens when the employee earned more in the
        # earlier years than they do now.
        notes.append(
            "Spreading the arrear back is not beneficial here — the tax in the earlier "
            "years would have been the same or higher. No relief is claimed, and the "
            "arrear is taxed in the year of receipt."
        )
        relief = ZERO
    else:
        notes.append(
            f"Relief of {_money(relief)} arises because the arrear taxed on receipt "
            f"costs {_money(tax_without_relief)} against "
            f"{_money(tax_with_spreading)} in the years it was earned."
        )

    return ReliefResult(
        arrear_amount=_money(arrear_amount),
        receipt_year=receipt_year,
        tax_without_relief=_money(tax_without_relief),
        tax_with_spreading=_money(tax_with_spreading),
        relief_amount=_money(relief),
        is_beneficial=is_beneficial,
        allocations=allocations,
        notes=notes,
    )


def allocate_by_period(
    arrear_by_period: dict[str, Decimal], financial_year_start_month: int = 4
) -> dict[str, Decimal]:
    """Group per-period arrears into the financial years they belong to.

    An arrear spanning January to June crosses a year boundary: January to March
    belongs to the earlier year. Getting this wrong puts income in the wrong
    year and makes the whole relief computation meaningless.
    """
    by_year: dict[str, Decimal] = {}
    for period, amount in arrear_by_period.items():
        try:
            year_str, month_str = period.split("-")
            year, month = int(year_str), int(month_str)
        except ValueError as exc:
            raise ReliefError(f"'{period}' is not a period. Use the form 2026-04.") from exc

        start_year = year if month >= financial_year_start_month else year - 1
        label = f"{start_year}-{start_year + 1}"
        by_year[label] = by_year.get(label, ZERO) + amount

    return by_year
