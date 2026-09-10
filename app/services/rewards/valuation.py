"""What a point is worth, from when, and why that is never rewritten.

═══════════════════════════════════════════════════════════════════════════════
CHANGING THE VALUATION DOES NOT REVALUE ISSUED POINTS
═══════════════════════════════════════════════════════════════════════════════

This is the same rule as `PayrollRuleSet`, and here it decides a balance-sheet
number rather than a payslip. A tenant that awards 10,000 points at Rs 0.50 and
later revalues to Rs 1.00 has not doubled what it owes on points already issued:
the employee was recognised with Rs 5,000 of value, and Rs 5,000 is the
liability. Recomputing it at today's rate restates a prior period's accounts
because somebody changed a setting in a form.

So `rate_on` resolves by the **award date**, `value_award` stamps the rate onto
the ledger row at the moment it is written, and the liability calculation in
`ledger.py` reads the stamped rate rather than looking one up again.

═══════════════════════════════════════════════════════════════════════════════
AND POINTS-PER-RUPEE IS STORED THE WAY FINANCE THINKS ABOUT IT
═══════════════════════════════════════════════════════════════════════════════

The setting is `points_per_rupee` because that is how a programme is designed
("100 points = Rs 100" or "2 points per rupee"), but every liability figure
needs rupees per point. The conversion happens once, here, rather than in each
caller - an inverted rate in one report is a liability off by the square of the
factor and it looks plausible.

Pure. Every date is supplied.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "PointsValuation",
    "ValuationError",
    "rate_on",
    "revaluation_impact",
    "value_award",
]


class ValuationError(ValueError):
    """A valuation this module refuses."""


def money(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class PointsValuation:
    """How many points one rupee buys, from one date."""

    points_per_rupee: Decimal
    effective_from: date
    note: str = ""

    def __post_init__(self) -> None:
        if self.points_per_rupee <= 0:
            raise ValuationError(
                "Points per rupee has to be positive. A zero rate would value "
                "every outstanding point at nothing and remove a real liability "
                "from the balance sheet."
            )

    @property
    def rupees_per_point(self) -> Decimal:
        """The figure every liability calculation actually needs.

        Converted here rather than in each caller: an inverted rate in one
        report is a liability wrong by the square of the factor, and it looks
        entirely plausible.
        """
        return (Decimal("1") / self.points_per_rupee).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )


def rate_on(valuations: list[PointsValuation], *, as_of: date) -> PointsValuation:
    """The valuation in force on a date.

    Resolved by the award's own date, never by today. A rate revised in April
    must not change what a March award was worth - the employee was recognised
    with that value and the accounts recorded it.
    """
    candidates = [v for v in valuations if v.effective_from <= as_of]
    if not candidates:
        raise ValuationError(
            f"No points valuation in force on {as_of.isoformat()}. Awarding "
            "points with no rate would put an unquantified liability on the "
            "balance sheet - configure a valuation effective on or before the "
            "award."
        )
    return max(candidates, key=lambda v: v.effective_from)


def value_award(
    valuations: list[PointsValuation], *, points: int, awarded_on: date
) -> dict[str, Any]:
    """Value one award at the rate in force when it is made.

    The returned `rate_per_point` is stamped onto the ledger row. Nothing looks
    it up again afterwards, which is what makes a later rate change unable to
    restate it.
    """
    if points <= 0:
        raise ValuationError("An award of zero or fewer points is not an award.")

    valuation = rate_on(valuations, as_of=awarded_on)
    rate = valuation.rupees_per_point
    return {
        "points": points,
        "rate_per_point": str(rate),
        "points_per_rupee": str(valuation.points_per_rupee),
        "monetary_value": str(money(Decimal(points) * rate)),
        "valuation_effective_from": valuation.effective_from.isoformat(),
        "note": (
            "Valued at the rate in force on the award date. A later change to "
            "the valuation does not restate this."
        ),
    }


def revaluation_impact(
    *,
    outstanding_points: int,
    current_liability: Decimal,
    new_points_per_rupee: Decimal,
) -> dict[str, Any]:
    """What a rate change does, and — importantly — what it does not.

    Shown before the change is saved, because the intuition is wrong: people
    expect a revaluation to move the liability and it does not touch a single
    point already issued. Saying so up front prevents somebody "fixing" the
    liability by editing the rate and then wondering why nothing moved.
    """
    if new_points_per_rupee <= 0:
        raise ValuationError("Points per rupee has to be positive.")

    new_rate = (Decimal("1") / new_points_per_rupee).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )
    hypothetical = money(Decimal(outstanding_points) * new_rate)

    return {
        "outstanding_points": outstanding_points,
        "current_liability": str(money(current_liability)),
        "liability_after_change": str(money(current_liability)),
        "hypothetical_if_revalued": str(hypothetical),
        "difference_avoided": str(money(hypothetical - money(current_liability))),
        "note": (
            f"The liability stays at Rs {money(current_liability)}. The "
            f"Rs {hypothetical} figure is what it would become if issued points "
            "were revalued, and they are not: each award keeps the rate it was "
            "made at. Only awards from the effective date use the new rate."
        ),
    }
