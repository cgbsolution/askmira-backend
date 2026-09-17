"""Headcount budget enforcement.

A `HeadcountBudget` row says how many seats — and how much cost — a department
(or, with no department, the whole company) may carry in one financial year.
Stored and never enforced, it was a number on a report. This engine is the check
that stops a seat being created, filled or moved past it.

**FTE, not rows.** Two half-time seats are one approved position's worth, so the
comparison is the Decimal sum of FTE against `approved_positions`.

**Cost uses the budgeted ceiling.** The ceiling is what was approved and what a
seat could cost. A seat with no ceiling cannot be checked against a cost budget,
and is refused rather than counted as free.

Pure: no session, no models. The service loads the facts and raises on the verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.services.positions.headcount import money

__all__ = ["Budget", "BudgetCheck", "Seat", "check"]


@dataclass(frozen=True, slots=True)
class Budget:
    scope: str
    financial_year: str
    approved_positions: int
    #: Zero means no cost budget was set; only headcount is enforced then.
    approved_cost: Decimal


@dataclass(frozen=True, slots=True)
class Seat:
    code: str
    fte: Decimal
    budgeted_ctc_max: Decimal | None


@dataclass(frozen=True, slots=True)
class BudgetCheck:
    scope: str
    financial_year: str
    approved_fte: Decimal
    fte_after: Decimal
    approved_cost: Decimal | None
    cost_after: Decimal | None
    problems: tuple[str, ...]

    @property
    def within_budget(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "financial_year": self.financial_year,
            "approved_fte": money(self.approved_fte),
            "fte_after": money(self.fte_after),
            "approved_cost": money(self.approved_cost) if self.approved_cost is not None else None,
            "cost_after": money(self.cost_after) if self.cost_after is not None else None,
            "within_budget": self.within_budget,
            "problems": list(self.problems),
        }


def _fte(seats: list[Seat]) -> Decimal:
    return sum((seat.fte for seat in seats), Decimal(0))


def check(budget: Budget, *, counted: list[Seat], adding: list[Seat], what: str) -> BudgetCheck:
    """Whether `adding` fits beside `counted` inside `budget`.

    `what` names the action for the refusal ("Creating ENG-07"), because the
    person reading it needs to know which of their clicks hit the limit.
    """
    approved_fte = Decimal(budget.approved_positions)
    fte_after = _fte(counted) + _fte(adding)
    problems: list[str] = []

    if fte_after > approved_fte:
        problems.append(
            f"{what} would take {budget.scope} to {money(fte_after)} FTE against "
            f"{money(approved_fte)} approved for {budget.financial_year}. Raise the headcount "
            "budget first, or free a seat."
        )

    approved_cost: Decimal | None = None
    cost_after: Decimal | None = None
    if budget.approved_cost > 0:
        approved_cost = budget.approved_cost
        unpriced = [seat.code for seat in adding if seat.budgeted_ctc_max is None]
        if unpriced:
            problems.append(
                f"{', '.join(unpriced)} has no budgeted CTC ceiling, so it cannot be checked "
                f"against the cost budget of {money(approved_cost)} for {budget.scope}. Set "
                "a ceiling on the position."
            )
        cost_after = sum(
            (
                seat.budgeted_ctc_max * seat.fte
                for seat in [*counted, *adding]
                if seat.budgeted_ctc_max is not None
            ),
            Decimal(0),
        )
        if cost_after > approved_cost:
            problems.append(
                f"{what} would take {budget.scope}'s budgeted cost to {money(cost_after)} "
                f"against {money(approved_cost)} approved for {budget.financial_year}."
            )

    return BudgetCheck(
        scope=budget.scope,
        financial_year=budget.financial_year,
        approved_fte=approved_fte,
        fte_after=fte_after,
        approved_cost=approved_cost,
        cost_after=cost_after,
        problems=tuple(problems),
    )
