"""The arithmetic of an account P&L. Pure: no session, no models.

A person's cost for a month is shared out by the mappings that cover it, each
weighted by the days of the month it covers: 100% on account A until the 10th
and 100% on account B from the 11th puts 10/30 of June's cost on A and 20/30 on
B. Shares are Decimal; amounts are rounded to the paisa once, at the end.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

__all__ = [
    "MAX_MONTHS",
    "PAISA",
    "margin_pct",
    "month_bounds",
    "month_key",
    "months_between",
    "rupees",
    "share_of_month",
]

PAISA = Decimal("0.01")
#: The longest period one P&L covers: three financial years.
MAX_MONTHS = 36
_HUNDRED = Decimal(100)


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def months_between(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    """Every month from `start` to `end`, both included. Refuses a reversed or too long span."""
    if end < start:
        raise ValueError("The last month is before the first.")
    out: list[tuple[int, int]] = []
    year, month = start
    while (year, month) <= end:
        out.append((year, month))
        if len(out) > MAX_MONTHS:
            raise ValueError(f"A P&L covers at most {MAX_MONTHS} months.")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def share_of_month(
    allocations: Iterable[tuple[Decimal, date, date | None]], year: int, month: int
) -> Decimal:
    """The part of a month's cost these mappings carry, as a fraction (1 = all of it).

    Each mapping is (percent, first day, last day or None for open-ended).
    """
    first, last = month_bounds(year, month)
    days = Decimal((last - first).days + 1)
    share = Decimal(0)
    for percent, start, end in allocations:
        lo = max(start, first)
        hi = min(end or last, last)
        if hi < lo:
            continue
        covered = Decimal((hi - lo).days + 1)
        share += (Decimal(percent) / _HUNDRED) * (covered / days)
    return share


def rupees(value: Decimal) -> Decimal:
    return Decimal(value).quantize(PAISA, rounding=ROUND_HALF_UP)


def margin_pct(billed: Decimal, margin: Decimal) -> Decimal | None:
    """Margin as a percentage of billing, to one decimal; None when nothing was billed."""
    if billed == 0:
        return None
    return (margin / billed * _HUNDRED).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
