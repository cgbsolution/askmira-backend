"""April to March. A "current financial year" report that means January to
December is a number an HR team takes to their auditor."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.reporting.ranges import (
    RELATIVE_RANGES,
    RangeError,
    financial_year_bounds,
    financial_year_label,
    resolve_range,
)

# ═══════════════════════════════════════════════════════════════════════════
# The financial year — the checklist gate
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "today,expected_start,expected_end",
    [
        # Mid-year: obvious.
        (date(2026, 9, 7), date(2026, 4, 1), date(2027, 3, 31)),
        # The first day. Belongs to the year it starts.
        (date(2026, 4, 1), date(2026, 4, 1), date(2027, 3, 31)),
        # The day before. Still the *previous* financial year.
        (date(2026, 3, 31), date(2025, 4, 1), date(2026, 3, 31)),
        # January to March belong to the FY that started last April. This is the
        # specific thing a calendar-year implementation gets wrong.
        (date(2027, 1, 15), date(2026, 4, 1), date(2027, 3, 31)),
        (date(2027, 3, 1), date(2026, 4, 1), date(2027, 3, 31)),
        # A leap year: 29 February is inside the FY, and the end is still 31 March.
        (date(2028, 2, 29), date(2027, 4, 1), date(2028, 3, 31)),
    ],
)
def test_the_financial_year_runs_april_to_march(today, expected_start, expected_end) -> None:
    """The checklist gate."""
    start, end = financial_year_bounds(today)
    assert start == expected_start
    assert end == expected_end
    assert start.month == 4 and start.day == 1
    assert end.month == 3 and end.day == 31


def test_the_relative_range_resolves_the_financial_year() -> None:
    resolved = resolve_range("current_financial_year", today=date(2027, 1, 15))
    assert resolved.start == date(2026, 4, 1)
    assert resolved.end == date(2027, 3, 31)
    assert resolved.label == "FY 2026-27"


def test_the_previous_financial_year_is_the_one_before() -> None:
    resolved = resolve_range("previous_financial_year", today=date(2026, 9, 7))
    assert resolved.start == date(2025, 4, 1)
    assert resolved.end == date(2026, 3, 31)
    assert resolved.label == "FY 2025-26"


def test_the_label_is_written_the_way_indian_filings_write_it() -> None:
    assert financial_year_label(date(2026, 4, 1)) == "FY 2026-27"
    assert financial_year_label(date(2029, 4, 1)) == "FY 2029-30"
    # The century boundary: 2099-00, not 2099-100.
    assert financial_year_label(date(2099, 4, 1)) == "FY 2099-00"


def test_the_financial_year_and_the_calendar_year_are_not_the_same_range() -> None:
    """Both exist because both get asked for. Confusing them is the bug."""
    fy = resolve_range("current_financial_year", today=date(2026, 9, 7))
    cy = resolve_range("calendar_year_to_date", today=date(2026, 9, 7))
    assert fy.start == date(2026, 4, 1)
    assert cy.start == date(2026, 1, 1)


# ═══════════════════════════════════════════════════════════════════════════
# Quarters — financial, not calendar
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "today,expected_start,expected_end",
    [
        (date(2026, 5, 15), date(2026, 4, 1), date(2026, 6, 30)),   # Q1
        (date(2026, 8, 15), date(2026, 7, 1), date(2026, 9, 30)),   # Q2
        (date(2026, 11, 15), date(2026, 10, 1), date(2026, 12, 31)),  # Q3
        (date(2027, 2, 15), date(2027, 1, 1), date(2027, 3, 31)),   # Q4
    ],
)
def test_quarters_start_in_april(today, expected_start, expected_end) -> None:
    """Q1 is April to June. An Indian team asking for "this quarter" in May
    means April to June, and a calendar answer is off by one every time."""
    resolved = resolve_range("this_quarter", today=today)
    assert resolved.start == expected_start
    assert resolved.end == expected_end


def test_last_quarter_in_may_is_january_to_march() -> None:
    resolved = resolve_range("last_quarter", today=date(2026, 5, 15))
    assert resolved.start == date(2026, 1, 1)
    assert resolved.end == date(2026, 3, 31)


# ═══════════════════════════════════════════════════════════════════════════
# Day ranges
# ═══════════════════════════════════════════════════════════════════════════


def test_last_seven_days_is_seven_days_and_not_eight() -> None:
    """Inclusive of today. Off by one here silently changes every daily report."""
    resolved = resolve_range("last_7_days", today=date(2026, 9, 7))
    assert resolved.start == date(2026, 9, 1)
    assert resolved.end == date(2026, 9, 7)
    assert (resolved.end - resolved.start).days + 1 == 7


@pytest.mark.parametrize("days", [7, 30, 90, 365])
def test_every_day_range_spans_exactly_what_it_says(days) -> None:
    resolved = resolve_range(f"last_{days}_days", today=date(2026, 9, 7))
    assert (resolved.end - resolved.start).days + 1 == days


def test_today_and_yesterday_are_single_days() -> None:
    today = resolve_range("today", today=date(2026, 9, 7))
    assert today.start == today.end == date(2026, 9, 7)
    yesterday = resolve_range("yesterday", today=date(2026, 9, 7))
    assert yesterday.start == yesterday.end == date(2026, 9, 6)


def test_this_month_ends_on_the_real_month_end() -> None:
    assert resolve_range("this_month", today=date(2026, 2, 10)).end == date(2026, 2, 28)
    assert resolve_range("this_month", today=date(2028, 2, 10)).end == date(2028, 2, 29)
    assert resolve_range("this_month", today=date(2026, 4, 10)).end == date(2026, 4, 30)


def test_last_month_crosses_a_year_boundary_correctly() -> None:
    resolved = resolve_range("last_month", today=date(2027, 1, 15))
    assert resolved.start == date(2026, 12, 1)
    assert resolved.end == date(2026, 12, 31)


# ═══════════════════════════════════════════════════════════════════════════
# Discipline
# ═══════════════════════════════════════════════════════════════════════════


def test_an_unknown_range_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(RangeError, match="not a relative range"):
        resolve_range("since_the_dawn_of_time", today=date(2026, 9, 7))


def test_every_advertised_range_actually_resolves() -> None:
    """A range in the picker that raises when chosen is worse than one that is
    not offered."""
    for key in RELATIVE_RANGES:
        resolved = resolve_range(key, today=date(2026, 9, 7))
        assert resolved.start <= resolved.end, key
        assert resolved.label, key


def test_every_advertised_range_has_a_sentence_explaining_it() -> None:
    """"Last quarter" means something different in Delhi than in Delaware."""
    for key, description in RELATIVE_RANGES.items():
        assert description.strip(), key
        assert description.endswith("."), key
