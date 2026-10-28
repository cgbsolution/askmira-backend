"""How a month's salary is divided into days: the payroll divisor.

Indian employers divide a month's salary in one of three ways, and each legal
entity says which (`LegalEntity.salary_days_basis`):

- **calendar** — the days in the month, 28 to 31. Weekly offs and holidays are
  paid days. A day's pay is smaller in a 31-day month than in February.
- **fixed** — the same number every month, usually 26 or 30. A day's pay never
  changes. Loss of pay beyond that number pays nothing; it never goes negative.
- **working_days** — the days the person was scheduled to work that month: the
  month less their own weekly offs. A five-day week gives about 22, a six-day
  week about 26, alternate Saturdays about 24 — per person, from their shift,
  roster or the entity's work week. Holidays fall on working days and are paid.

Loss of pay is counted the same way under all three (attendance's days that
were not payable, weekly offs never among them). What changes is what it is
divided by, and how days before joining or after leaving are counted.

Pure: no database. The run passes in the person's off-day rule.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

__all__ = ["BASES", "SalaryDays", "salary_days"]

CALENDAR = "calendar"
FIXED = "fixed"
WORKING_DAYS = "working_days"
BASES = (CALENDAR, FIXED, WORKING_DAYS)

_CENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class SalaryDays:
    """The divisor and the unpaid days in its units, for `compute_payroll`."""

    working_days: Decimal
    lop_days: Decimal
    not_employed_days: Decimal
    #: The basis actually used. `working_days` falls back to `calendar` for
    #: someone with no scheduled day in the month.
    basis: str
    #: Why the figure is what it is, when that needs saying.
    note: str | None = None


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _calendar(
    period_start: date, period_end: date, window: tuple[date, date], lop: Decimal, note: str | None
) -> SalaryDays:
    first, last = window
    return SalaryDays(
        working_days=Decimal((period_end - period_start).days + 1),
        lop_days=lop,
        not_employed_days=Decimal((first - period_start).days + (period_end - last).days),
        basis=CALENDAR,
        note=note,
    )


def salary_days(
    basis: str,
    *,
    period_start: date,
    period_end: date,
    window: tuple[date, date],
    lop_days: Decimal,
    fixed_days: int | None = None,
    is_off: Callable[[date], bool] | None = None,
) -> SalaryDays:
    """The divisor, loss of pay and days not employed for one person's period.

    `window` is the part of the period they were employed (joining to leaving).
    `is_off` says whether a date is one of their weekly offs; it is needed only
    for `working_days`.
    """
    if basis == FIXED:
        if not fixed_days or fixed_days <= 0:
            return _calendar(
                period_start,
                period_end,
                window,
                lop_days,
                "The legal entity's fixed salary days are not set; calendar days were used.",
            )
        divisor = Decimal(fixed_days)
        calendar_days = Decimal((period_end - period_start).days + 1)
        first, last = window
        outside_calendar = Decimal((first - period_start).days + (period_end - last).days)
        # Days outside employment are scaled to the fixed month: joining on the
        # 16th of a 30-day month on a 26-day basis leaves 13 of 26 to pay.
        outside = (outside_calendar * divisor / calendar_days).quantize(_CENT, ROUND_HALF_UP)
        lop = min(lop_days, divisor - outside)
        note = None
        if lop < lop_days:
            note = (
                f"Loss of pay ({lop_days} days) is more than the {fixed_days} fixed salary "
                f"days; {lop} were deducted, so nothing is paid for the month's days."
            )
        return SalaryDays(
            working_days=divisor, lop_days=lop, not_employed_days=outside, basis=FIXED, note=note
        )

    if basis == WORKING_DAYS and is_off is not None:
        scheduled = [day for day in _days(period_start, period_end) if not is_off(day)]
        first, last = window
        outside = Decimal(sum(1 for day in scheduled if day < first or day > last))
        divisor = Decimal(len(scheduled))
        if divisor == 0 or outside >= divisor:
            return _calendar(
                period_start,
                period_end,
                window,
                lop_days,
                "No scheduled working day fell inside their employment this month, so "
                "calendar days were used.",
            )
        lop = min(lop_days, divisor - outside)
        note = None
        if lop < lop_days:
            note = (
                f"Loss of pay ({lop_days} days, off days charged under the sandwich rule "
                f"included) is more than their {divisor} working days; {lop} were deducted."
            )
        return SalaryDays(
            working_days=divisor,
            lop_days=lop,
            not_employed_days=outside,
            basis=WORKING_DAYS,
            note=note,
        )

    return _calendar(period_start, period_end, window, lop_days, None)
