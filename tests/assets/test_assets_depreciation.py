"""Book value: straight line to zero, from purchase, over the category's life.

It is what an unreturned asset costs a leaver, so every branch has to be
something a person could check with a calculator — and "no cost recorded" must
stay "no figure", never quietly become zero.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.assets.depreciation import book_value, months_elapsed


def test_no_purchase_cost_means_no_figure_rather_than_zero() -> None:
    assert (
        book_value(
            purchase_cost=None,
            purchase_date=date(2025, 1, 1),
            depreciation_months=36,
            as_of=date(2026, 1, 1),
        )
        is None
    )


def test_a_category_without_a_life_holds_its_cost() -> None:
    value = book_value(
        purchase_cost=Decimal("1500"),
        purchase_date=date(2020, 1, 1),
        depreciation_months=None,
        as_of=date(2026, 1, 1),
    )
    assert value is not None
    assert value.amount == Decimal("1500.00")
    assert "does not depreciate" in value.basis


def test_no_purchase_date_holds_the_cost_and_says_why() -> None:
    value = book_value(
        purchase_cost=Decimal("90000"),
        purchase_date=None,
        depreciation_months=36,
        as_of=date(2026, 1, 1),
    )
    assert value is not None
    assert value.amount == Decimal("90000.00")
    assert "no purchase date" in value.basis


def test_straight_line_over_whole_months() -> None:
    value = book_value(
        purchase_cost=Decimal("120000"),
        purchase_date=date(2025, 1, 15),
        depreciation_months=36,
        as_of=date(2026, 1, 15),
    )
    assert value is not None
    # 12 of 36 months gone: two thirds remain.
    assert value.amount == Decimal("80000.00")
    assert "12 month(s) elapsed" in value.basis


def test_a_part_month_does_not_count() -> None:
    value = book_value(
        purchase_cost=Decimal("120000"),
        purchase_date=date(2025, 1, 15),
        depreciation_months=36,
        as_of=date(2026, 1, 14),
    )
    assert value is not None
    # 11 whole months: 120000 x 25/36, rounded half-up to the paisa.
    assert value.amount == Decimal("83333.33")


def test_fully_depreciated_is_zero_not_negative() -> None:
    value = book_value(
        purchase_cost=Decimal("60000"),
        purchase_date=date(2020, 6, 1),
        depreciation_months=24,
        as_of=date(2026, 6, 1),
    )
    assert value is not None
    assert value.amount == Decimal("0.00")
    assert "fully depreciated" in value.basis


def test_a_date_before_purchase_holds_the_full_cost() -> None:
    value = book_value(
        purchase_cost=Decimal("45000.50"),
        purchase_date=date(2026, 3, 1),
        depreciation_months=12,
        as_of=date(2026, 2, 1),
    )
    assert value is not None
    assert value.amount == Decimal("45000.50")


def test_month_ends_count_as_whole_months() -> None:
    # Bought on the 31st: the month is complete on the last day of February.
    assert months_elapsed(date(2026, 1, 31), date(2026, 2, 28)) == 1
    assert months_elapsed(date(2026, 1, 31), date(2026, 2, 27)) == 0
    assert months_elapsed(date(2026, 1, 15), date(2026, 2, 14)) == 0
    assert months_elapsed(date(2026, 1, 15), date(2026, 2, 15)) == 1
    assert months_elapsed(date(2026, 1, 15), date(2026, 1, 1)) == 0
