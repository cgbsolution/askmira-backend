"""Relative date ranges, resolved against a stated "today".

The Indian financial year runs **1 April to 31 March**, and getting this wrong is
not a cosmetic error: a "current financial year" salary register that quietly
means January to December is a number an HR team will take to their auditor.

Every range is half-open on the end in intent but returned as an inclusive
`(start, end)` pair of dates, because that is what a `BETWEEN` on a `date` column
means and what a user reading "1 Apr 2026 to 31 Mar 2027" expects. The compiler
converts to the right predicate for the column type — a `date` gets `BETWEEN`, a
`timestamp` gets `>= start AND < end + 1 day`, because `BETWEEN` on a timestamp
silently drops everything after midnight on the last day.

Pure. `today` is always supplied by the caller, which is what makes the
boundaries testable without waiting for April.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

__all__ = [
    "RELATIVE_RANGES",
    "RangeError",
    "financial_year_bounds",
    "financial_year_label",
    "resolve_range",
]

#: The month the Indian financial year starts in.
FY_START_MONTH = 4


class RangeError(ValueError):
    """A range this module will not guess at."""


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date
    label: str

    def as_dict(self) -> dict[str, str]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "label": self.label,
        }


def financial_year_bounds(on: date, *, offset: int = 0) -> tuple[date, date]:
    """The Indian FY containing `on`, shifted by `offset` years.

    January to March belong to the financial year that *started* the previous
    April. Getting that backwards is the specific bug: on 15 January 2027 the
    current FY is 2026-27, not 2027-28.
    """
    start_year = on.year if on.month >= FY_START_MONTH else on.year - 1
    start_year += offset
    start = date(start_year, FY_START_MONTH, 1)
    end = date(start_year + 1, FY_START_MONTH, 1) - timedelta(days=1)
    return start, end


def financial_year_label(start: date) -> str:
    """ "FY 2026-27", the way it is written on every Indian filing."""
    return f"FY {start.year}-{str(start.year + 1)[-2:]}"


def _month_bounds(on: date, *, offset: int = 0) -> tuple[date, date]:
    year, month = on.year, on.month + offset
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    start = date(year, month, 1)
    next_month = date(year + (month == 12), (month % 12) + 1, 1)
    return start, next_month - timedelta(days=1)


def _quarter_bounds(on: date, *, offset: int = 0) -> tuple[date, date]:
    """Quarters of the *financial* year, not the calendar year.

    Q1 is April to June. An Indian HR team asking for "last quarter" in May means
    January to March, and a calendar-quarter answer would be off by one every
    time.
    """
    fy_start, _ = financial_year_bounds(on)
    index = (on.month - FY_START_MONTH) % 12 // 3 + offset
    start_month_offset = index * 3
    year = fy_start.year + (fy_start.month - 1 + start_month_offset) // 12
    month = (fy_start.month - 1 + start_month_offset) % 12 + 1
    start = date(year, month, 1)
    end_year = year + (month + 2 > 12)
    end_month = (month + 2 - 1) % 12 + 1
    next_month = date(end_year + (end_month == 12), (end_month % 12) + 1, 1)
    return start, next_month - timedelta(days=1)


#: Every relative range the builder offers, with the sentence shown next to it.
#: A range whose meaning is not obvious from its name gets one, because "last
#: quarter" means something different in Delhi than it does in Delaware.
RELATIVE_RANGES: dict[str, str] = {
    "today": "Today.",
    "yesterday": "Yesterday.",
    "last_7_days": "The last 7 days, ending today.",
    "last_30_days": "The last 30 days, ending today.",
    "last_90_days": "The last 90 days, ending today.",
    "last_365_days": "The last 365 days, ending today.",
    "this_month": "The 1st of this month to today's month end.",
    "last_month": "The whole of last month.",
    "this_quarter": "This financial quarter. Q1 is April to June.",
    "last_quarter": "The previous financial quarter.",
    "current_financial_year": "1 April to 31 March, the Indian financial year.",
    "previous_financial_year": "The financial year before this one.",
    "calendar_year_to_date": "1 January to today. Not the financial year.",
}


def resolve_range(key: str, *, today: date) -> DateRange:
    """Turn a relative range name into two dates.

    `today` is a parameter rather than `date.today()` so a scheduled report can
    resolve "last 30 days" against the moment it was *due*, not the moment the
    worker happened to pick it up — a queue backlog must not change what a report
    says.
    """
    if key not in RELATIVE_RANGES:
        raise RangeError(
            f"'{key}' is not a relative range this system knows. Available: "
            f"{sorted(RELATIVE_RANGES)}."
        )

    if key == "today":
        return DateRange(today, today, "Today")
    if key == "yesterday":
        day = today - timedelta(days=1)
        return DateRange(day, day, "Yesterday")

    if key.startswith("last_") and key.endswith("_days"):
        days = int(key.split("_")[1])
        # Inclusive of today, so "last 7 days" is 7 days and not 8.
        return DateRange(today - timedelta(days=days - 1), today, f"Last {days} days")

    if key == "this_month":
        start, end = _month_bounds(today)
        return DateRange(start, end, start.strftime("%B %Y"))
    if key == "last_month":
        start, end = _month_bounds(today, offset=-1)
        return DateRange(start, end, start.strftime("%B %Y"))

    if key == "this_quarter":
        start, end = _quarter_bounds(today)
        return DateRange(start, end, f"{start:%b}-{end:%b} {end.year}")
    if key == "last_quarter":
        start, end = _quarter_bounds(today, offset=-1)
        return DateRange(start, end, f"{start:%b}-{end:%b} {end.year}")

    if key == "current_financial_year":
        start, end = financial_year_bounds(today)
        return DateRange(start, end, financial_year_label(start))
    if key == "previous_financial_year":
        start, end = financial_year_bounds(today, offset=-1)
        return DateRange(start, end, financial_year_label(start))

    if key == "calendar_year_to_date":
        return DateRange(date(today.year, 1, 1), today, f"{today.year} to date")

    raise RangeError(  # pragma: no cover - every key above is handled
        f"'{key}' is listed but not implemented."
    )
