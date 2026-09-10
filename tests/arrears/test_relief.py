"""Slice 12 gate: section 89(1) relief and the Form 10E working.

Every tax figure here is passed in, already computed against the rule set for
the year it belongs to. That separation is the point — it is what keeps a 2026
rate out of a 2023 computation, and relief is a comparison *between* years.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.arrears.relief import (
    ReliefError,
    YearAllocation,
    allocate_by_period,
    compute_relief,
)


def D(value: str | int) -> Decimal:  # noqa: N802 - a Decimal alias, used throughout
    return Decimal(str(value))


def allocation(**overrides) -> YearAllocation:
    base = {
        "financial_year": "2024-2025",
        "original_income": D(600_000),
        "arrear_portion": D(120_000),
        "tax_on_original": D(20_000),
        "tax_with_arrear": D(32_000),
    }
    base.update(overrides)
    return YearAllocation(**base)


def relief(**overrides):
    base = {
        "arrear_amount": D(120_000),
        "receipt_year": "2026-2027",
        "income_in_receipt_year": D(1_500_000),
        "tax_on_receipt_year_income": D(200_000),
        "tax_on_receipt_year_income_with_arrear": D(236_000),
        "allocations": [allocation()],
    }
    base.update(overrides)
    return compute_relief(**base)


# ═══════════════════════════════════════════════════════════════════════════
# The comparison
# ═══════════════════════════════════════════════════════════════════════════


def test_relief_is_the_difference_between_the_two_treatments() -> None:
    """36,000 if taxed on receipt, 12,000 if spread back — 24,000 of relief."""
    result = relief()

    assert result.tax_without_relief == D(36_000)
    assert result.tax_with_spreading == D(12_000)
    assert result.relief_amount == D(24_000)
    assert result.is_beneficial is True


def test_relief_is_zero_where_spreading_back_is_not_beneficial() -> None:
    """Common, and correct — it happens when the employee earned more in the
    earlier years than they do now. A relief of zero is an answer, not a
    failure.
    """
    result = relief(
        allocations=[allocation(tax_on_original=D(40_000), tax_with_arrear=D(90_000))]
    )
    assert result.is_beneficial is False
    assert result.relief_amount == D(0)
    assert any("not beneficial" in note for note in result.notes)


def test_a_relief_of_exactly_zero_is_not_beneficial() -> None:
    """The boundary. Equal treatment means no claim to make."""
    result = relief(
        allocations=[allocation(tax_on_original=D(20_000), tax_with_arrear=D(56_000))]
    )
    assert result.tax_without_relief == result.tax_with_spreading
    assert result.is_beneficial is False
    assert result.relief_amount == D(0)


def test_relief_spans_several_years() -> None:
    result = relief(
        arrear_amount=D(240_000),
        allocations=[
            allocation(financial_year="2023-2024", arrear_portion=D(120_000)),
            allocation(financial_year="2024-2025", arrear_portion=D(120_000)),
        ],
    )
    assert result.tax_with_spreading == D(24_000)
    assert len(result.allocations) == 2


# ═══════════════════════════════════════════════════════════════════════════
# What is refused
# ═══════════════════════════════════════════════════════════════════════════


def test_allocations_must_account_for_every_rupee() -> None:
    """An unallocated remainder would be relieved twice or not at all."""
    with pytest.raises(ReliefError, match="Every rupee has to be attributed"):
        relief(arrear_amount=D(200_000), allocations=[allocation(arrear_portion=D(120_000))])


def test_relief_with_no_years_to_spread_back_over_is_refused() -> None:
    with pytest.raises(ReliefError, match="no years were supplied"):
        relief(allocations=[])


def test_a_zero_arrear_is_refused() -> None:
    with pytest.raises(ReliefError, match="no arrear to compute relief on"):
        relief(arrear_amount=D(0), allocations=[allocation(arrear_portion=D(0))])


def test_a_float_is_refused() -> None:
    with pytest.raises(ReliefError, match="must be a Decimal"):
        relief(arrear_amount=120000.0)


def test_a_negative_figure_is_refused() -> None:
    with pytest.raises(ReliefError, match="cannot be negative"):
        relief(tax_on_receipt_year_income=D(-1))


# ═══════════════════════════════════════════════════════════════════════════
# The Form 10E working
# ═══════════════════════════════════════════════════════════════════════════


def test_the_working_shows_the_year_by_year_comparison() -> None:
    """Form 10E is a declaration the employee signs, so they are entitled to
    see how the figure was arrived at — not just the total."""
    import json

    result = relief(
        arrear_amount=D(240_000),
        allocations=[
            allocation(financial_year="2023-2024", arrear_portion=D(120_000)),
            allocation(financial_year="2024-2025", arrear_portion=D(120_000)),
        ],
    )
    working = json.loads(json.dumps(result.as_working()))

    assert len(working["allocations"]) == 2
    assert working["allocations"][0]["financial_year"] == "2023-2024"
    assert working["allocations"][0]["additional_tax"] == "12000"
    # 36,000 taxed on receipt against 24,000 spread over two years.
    assert working["tax_without_relief"] == "36000.00"
    assert working["tax_with_spreading"] == "24000.00"
    assert working["relief_amount"] == "12000.00"
    assert working["notes"]


def test_the_working_records_which_treatment_was_taken() -> None:
    beneficial = relief().as_working()
    not_beneficial = relief(
        allocations=[allocation(tax_on_original=D(40_000), tax_with_arrear=D(90_000))]
    ).as_working()

    assert beneficial["is_beneficial"] is True
    assert not_beneficial["is_beneficial"] is False


def test_the_computation_is_deterministic() -> None:
    assert relief().as_working() == relief().as_working()


# ═══════════════════════════════════════════════════════════════════════════
# Allocating periods to financial years
# ═══════════════════════════════════════════════════════════════════════════


def test_periods_group_into_the_financial_year_they_belong_to() -> None:
    """An arrear spanning January to June crosses a year boundary.

    January to March belongs to the earlier year. Getting this wrong puts
    income in the wrong year and makes the whole relief computation
    meaningless.
    """
    grouped = allocate_by_period(
        {
            "2026-01": D(10_000),
            "2026-02": D(10_000),
            "2026-03": D(10_000),
            "2026-04": D(12_000),
            "2026-05": D(12_000),
        }
    )
    assert grouped == {"2025-2026": D(30_000), "2026-2027": D(24_000)}


def test_april_starts_the_new_financial_year() -> None:
    assert allocate_by_period({"2026-04": D(1)}) == {"2026-2027": D(1)}
    assert allocate_by_period({"2026-03": D(1)}) == {"2025-2026": D(1)}


def test_the_year_start_month_is_a_parameter() -> None:
    """A tenant on a different accounting year is representable."""
    grouped = allocate_by_period({"2026-01": D(1)}, financial_year_start_month=1)
    assert grouped == {"2026-2027": D(1)}


def test_a_malformed_period_is_refused() -> None:
    with pytest.raises(ReliefError, match="is not a period"):
        allocate_by_period({"March 2026": D(1)})
