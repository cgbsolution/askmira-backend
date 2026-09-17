"""Headcount budget enforcement, without a database (slice 23 gap)."""

from __future__ import annotations

from decimal import Decimal

from app.services.positions.budget import Budget, Seat, check

FY = "FY 2026-27"


def _budget(positions: int, cost: str = "0") -> Budget:
    return Budget(
        scope="Engineering",
        financial_year=FY,
        approved_positions=positions,
        approved_cost=Decimal(cost),
    )


def _seat(code: str, fte: str = "1.0", ceiling: str | None = "1000000") -> Seat:
    return Seat(code=code, fte=Decimal(fte), budgeted_ctc_max=Decimal(ceiling) if ceiling else None)


def test_a_seat_inside_the_budget_is_allowed() -> None:
    result = check(_budget(3), counted=[_seat("A"), _seat("B")], adding=[_seat("C")], what="x")
    assert result.within_budget
    assert result.fte_after == Decimal(3)


def test_a_seat_past_the_budget_is_refused_and_named() -> None:
    result = check(
        _budget(2), counted=[_seat("A"), _seat("B")], adding=[_seat("C")], what="Creating C"
    )
    assert not result.within_budget
    assert "Creating C" in result.problems[0]
    assert "Engineering" in result.problems[0]
    assert FY in result.problems[0]


def test_fte_fractions_count_as_fractions() -> None:
    """Two half-time seats are one approved position's worth."""
    result = check(
        _budget(1), counted=[_seat("A", "0.5")], adding=[_seat("B", "0.5")], what="x"
    )
    assert result.within_budget
    assert result.fte_after == Decimal("1.0")

    over = check(_budget(1), counted=[_seat("A", "0.5")], adding=[_seat("B", "0.75")], what="x")
    assert not over.within_budget


def test_a_zero_cost_budget_enforces_headcount_only() -> None:
    result = check(_budget(5, "0"), counted=[], adding=[_seat("A", ceiling=None)], what="x")
    assert result.within_budget
    assert result.approved_cost is None


def test_cost_uses_the_ceiling_times_fte() -> None:
    result = check(
        _budget(5, "1500000"),
        counted=[_seat("A", "1.0", "1000000")],
        adding=[_seat("B", "0.5", "1000000")],
        what="x",
    )
    assert result.within_budget
    assert result.cost_after == Decimal("1500000.0")

    over = check(
        _budget(5, "1500000"),
        counted=[_seat("A", "1.0", "1000000")],
        adding=[_seat("B", "0.6", "1000000")],
        what="Creating B",
    )
    assert not over.within_budget
    assert "cost" in over.problems[0]


def test_a_seat_with_no_ceiling_cannot_pass_a_cost_budget() -> None:
    """Refused rather than counted as free."""
    result = check(
        _budget(5, "9000000"), counted=[], adding=[_seat("B", ceiling=None)], what="x"
    )
    assert not result.within_budget
    assert "no budgeted CTC ceiling" in result.problems[0]


def test_an_existing_overrun_is_reported_with_nothing_added() -> None:
    result = check(_budget(1), counted=[_seat("A"), _seat("B")], adding=[], what="Seats")
    assert not result.within_budget


def test_the_verdict_serialises_figures_as_strings() -> None:
    payload = check(_budget(2, "100"), counted=[], adding=[_seat("A")], what="x").as_dict()
    assert isinstance(payload["approved_fte"], str)
    assert isinstance(payload["fte_after"], str)
    assert payload["within_budget"] is False
