"""Book value of an asset: straight line to zero over its category's life.

Pure — no session, no clock. The caller says which day the value is wanted for,
because the value that matters at exit is the one on the leaver's last working
day, not the day somebody happened to open the screen.

What is deliberately *not* modelled: salvage value, the Companies Act Schedule II
useful lives, or the Income Tax block-of-assets WDV method. This figure answers
one question — what does an unreturned or written-off asset cost the company
today — and a leaver's settlement line has to be explainable in one sentence.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

__all__ = ["BookValue", "book_value", "months_elapsed"]

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class BookValue:
    amount: Decimal
    #: One sentence a leaver can check: cost, age, life.
    basis: str


def months_elapsed(start: date, end: date) -> int:
    """Whole months from `start` to `end`. Zero when `end` is not after `start`.

    A month is complete on the same day-of-month, or on the last day of a
    shorter month: bought on 31 January, a month has passed on 28 February.
    """
    if end <= start:
        return 0
    months = (end.year - start.year) * 12 + (end.month - start.month)
    last_day = calendar.monthrange(end.year, end.month)[1]
    if end.day < start.day and end.day != last_day:
        months -= 1
    return max(months, 0)


def book_value(
    *,
    purchase_cost: Decimal | None,
    purchase_date: date | None,
    depreciation_months: int | None,
    as_of: date,
) -> BookValue | None:
    """The asset's value on `as_of`, or None when there is no cost to depreciate.

    - No purchase cost: there is no figure, and one is not invented.
    - No life on the category, or no purchase date: the asset holds its cost.
    - Otherwise: cost less cost * months elapsed / life, never below zero.
    """
    if purchase_cost is None:
        return None
    cost = Decimal(str(purchase_cost)).quantize(CENT, rounding=ROUND_HALF_UP)
    if not depreciation_months or depreciation_months <= 0:
        return BookValue(cost, f"Purchase cost {cost}; its category does not depreciate.")
    if purchase_date is None:
        return BookValue(
            cost, f"Purchase cost {cost}; no purchase date recorded, so no depreciation applied."
        )

    elapsed = months_elapsed(purchase_date, as_of)
    if elapsed >= depreciation_months:
        return BookValue(
            ZERO,
            f"Purchase cost {cost} on {purchase_date.isoformat()}, fully depreciated over "
            f"{depreciation_months} months by {as_of.isoformat()}.",
        )
    remaining = cost - (cost * elapsed / Decimal(depreciation_months))
    amount = remaining.quantize(CENT, rounding=ROUND_HALF_UP)
    return BookValue(
        amount,
        f"Purchase cost {cost} on {purchase_date.isoformat()}, straight line over "
        f"{depreciation_months} months: {elapsed} month(s) elapsed by {as_of.isoformat()}, "
        f"{depreciation_months - elapsed} remaining.",
    )
