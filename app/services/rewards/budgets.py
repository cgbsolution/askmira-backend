"""Points budgets: what makes the liability predictable.

═══════════════════════════════════════════════════════════════════════════════
A BUDGET IS A LIABILITY CAP, NOT A NUDGE
═══════════════════════════════════════════════════════════════════════════════

The reason finance asks for budgets on a recognition programme is not tidiness.
Without a cap, the accrued liability is whatever the organisation felt like this
quarter, and it appears on a balance sheet nobody forecast. With one, it is
bounded and provisionable.

So `check` answers a single question — may this award be made — and it answers
it against **every** scope that applies, not the tightest one it happens to
check first. A manager inside their own budget but under an exhausted
departmental cap is stopped by the department, and the message says which.

═══════════════════════════════════════════════════════════════════════════════
HARD STOP AND SOFT WARNING ARE BOTH REAL CHOICES
═══════════════════════════════════════════════════════════════════════════════

A hard stop protects the number and occasionally blocks a manager recognising
somebody on their last day. A soft warning never blocks and occasionally
produces a quarter nobody forecast. Both are defensible, so it is configured per
budget rather than assumed, and the soft path **records the overspend** — a
warning nobody counts is a warning nobody acts on.

Pure. `as_of` is always supplied.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "Budget",
    "BudgetDecision",
    "BudgetError",
    "BudgetScope",
    "EnforcementMode",
    "check",
    "utilisation",
]


class BudgetError(ValueError):
    """A budget operation this module refuses."""


def money(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class BudgetScope(str, enum.Enum):
    COMPANY = "company"
    DEPARTMENT = "department"
    MANAGER = "manager"

    @property
    def rank(self) -> int:
        """Broadest first, so a message names the widest cap that stopped it."""
        return {
            BudgetScope.COMPANY: 0,
            BudgetScope.DEPARTMENT: 1,
            BudgetScope.MANAGER: 2,
        }[self]


class EnforcementMode(str, enum.Enum):
    #: Refuses the award. Protects the forecast; occasionally blocks somebody
    #: recognising a colleague on their last day.
    HARD_STOP = "hard_stop"
    #: Permits and records the overspend. Never blocks; occasionally produces a
    #: quarter nobody forecast.
    SOFT_WARNING = "soft_warning"


@dataclass(frozen=True, slots=True)
class Budget:
    """One allocation, for one scope, for one period."""

    budget_id: str
    scope: BudgetScope
    scope_key: str
    period_start: date
    period_end: date
    allocated_points: int
    consumed_points: int = 0
    enforcement: EnforcementMode = EnforcementMode.SOFT_WARNING

    def __post_init__(self) -> None:
        if self.period_end < self.period_start:
            raise BudgetError("A budget period cannot end before it starts.")
        if self.allocated_points < 0:
            raise BudgetError(
                "A negative allocation is not a budget. Zero means nothing may "
                "be awarded, which is a real configuration; below zero is a "
                "data error."
            )

    def covers(self, on: date) -> bool:
        return self.period_start <= on <= self.period_end

    @property
    def remaining(self) -> int:
        return self.allocated_points - self.consumed_points

    @property
    def utilisation(self) -> float | None:
        if self.allocated_points == 0:
            return None
        return round(self.consumed_points / self.allocated_points, 3)


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    """Whether an award may be made, and which cap decided."""

    permitted: bool
    blocking_budget_id: str | None
    blocking_scope: BudgetScope | None
    warnings: tuple[dict[str, Any], ...]
    overspend_points: int
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "permitted": self.permitted,
            "blocking_budget_id": self.blocking_budget_id,
            "blocking_scope": (self.blocking_scope.value if self.blocking_scope else None),
            "warnings": list(self.warnings),
            "overspend_points": self.overspend_points,
            "message": self.message,
        }


def check(
    budgets: list[Budget],
    *,
    points: int,
    scope_keys: dict[BudgetScope, str],
    as_of: date,
) -> BudgetDecision:
    """Whether this award fits, checked against every scope that applies.

    Not the tightest cap that happens to be checked first: a manager inside
    their own budget but under an exhausted departmental one is stopped by the
    department, and being told "your budget is fine" while the award fails is
    the support ticket this exists to avoid.

    A scope with **no budget configured** is unconstrained. Unconfigured and
    zero have to be different states, or a tenant who has set up manager
    budgets and not departmental ones finds every award blocked by a cap they
    never created.
    """
    if points <= 0:
        raise BudgetError("An award of zero or fewer points is not an award.")

    applicable = [b for b in budgets if b.covers(as_of) and scope_keys.get(b.scope) == b.scope_key]

    exceeded = [b for b in applicable if b.consumed_points + points > b.allocated_points]
    hard = sorted(
        (b for b in exceeded if b.enforcement is EnforcementMode.HARD_STOP),
        key=lambda b: b.scope.rank,
    )
    soft = sorted(
        (b for b in exceeded if b.enforcement is EnforcementMode.SOFT_WARNING),
        key=lambda b: b.scope.rank,
    )

    warnings = tuple(
        {
            "budget_id": b.budget_id,
            "scope": b.scope.value,
            "scope_key": b.scope_key,
            "allocated": b.allocated_points,
            "consumed": b.consumed_points,
            "overspend": b.consumed_points + points - b.allocated_points,
            "enforcement": b.enforcement.value,
        }
        for b in soft
    )
    overspend = sum(w["overspend"] for w in warnings)

    if hard:
        blocker = hard[0]
        return BudgetDecision(
            permitted=False,
            blocking_budget_id=blocker.budget_id,
            blocking_scope=blocker.scope,
            warnings=warnings,
            overspend_points=0,
            message=(
                f"The {blocker.scope.value} budget for {blocker.scope_key} has "
                f"{blocker.remaining} of {blocker.allocated_points} points left "
                f"and this award needs {points}. It is set to hard stop, so the "
                "award cannot be made until the period rolls over or the "
                "allocation is raised - which is a decision somebody makes, not "
                "one this system makes quietly."
            ),
        )

    if soft:
        return BudgetDecision(
            permitted=True,
            blocking_budget_id=None,
            blocking_scope=None,
            warnings=warnings,
            overspend_points=overspend,
            message=(
                f"Permitted, but this takes {len(soft)} budget(s) over by "
                f"{overspend} points. The overspend is recorded - a warning "
                "nobody counts is a warning nobody acts on."
            ),
        )

    return BudgetDecision(
        permitted=True,
        blocking_budget_id=None,
        blocking_scope=None,
        warnings=(),
        overspend_points=0,
        message=(
            f"Within budget across {len(applicable)} applicable cap(s)."
            if applicable
            else "No budget is configured for this award's scopes, so nothing "
            "constrains it. Unconfigured is not the same as zero."
        ),
    )


def utilisation(budgets: list[Budget], *, as_of: date, rate_per_point: Decimal) -> dict[str, Any]:
    """Consumption per budget, in points and rupees.

    The rupee figure is what makes a budget report meaningful to finance, and
    it is computed at the rate supplied rather than at a rate this module
    guesses - the caller knows which valuation applies to the period.
    """
    rows = []
    for budget in sorted(budgets, key=lambda b: (b.scope.rank, b.scope_key)):
        if not budget.covers(as_of):
            continue
        rows.append(
            {
                "budget_id": budget.budget_id,
                "scope": budget.scope.value,
                "scope_key": budget.scope_key,
                "allocated_points": budget.allocated_points,
                "consumed_points": budget.consumed_points,
                "remaining_points": budget.remaining,
                "utilisation": budget.utilisation,
                "allocated_rupees": str(money(Decimal(budget.allocated_points) * rate_per_point)),
                "consumed_rupees": str(money(Decimal(budget.consumed_points) * rate_per_point)),
                "enforcement": budget.enforcement.value,
                "exhausted": budget.remaining <= 0,
            }
        )

    return {
        "as_of": as_of.isoformat(),
        "rate_per_point": str(rate_per_point),
        "budgets": rows,
        "exhausted_count": sum(1 for r in rows if r["exhausted"]),
        "note": (
            "Budget consumption is what makes the accrued liability "
            "predictable. Without a cap it is whatever the organisation felt "
            "like this quarter, and it lands on a balance sheet nobody forecast."
        ),
    }
