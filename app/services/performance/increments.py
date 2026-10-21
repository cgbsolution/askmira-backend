"""Increment bands, live budget, and the equity check at the point of decision.

═══════════════════════════════════════════════════════════════════════════════
THE BUDGET HAS TO BE VISIBLE WHILE THE MANAGER IS ALLOCATING
═══════════════════════════════════════════════════════════════════════════════

A manager who allocates twelve increments blind, submits, and is told the
department is 8% over has to redo all twelve — and the second pass is not a
careful re-think, it is a proportional haircut applied at 11pm. So `headroom`
answers "what is left" for a single recommendation before it is entered, and
`consume` returns the state after it, in the same shape.

═══════════════════════════════════════════════════════════════════════════════
AND THE EQUITY CHECK BELONGS AT THE SAME MOMENT
═══════════════════════════════════════════════════════════════════════════════

Reporting a gender pay gap annually tells a company what it did. Surfacing the
comparison **while the number is being chosen** is the only version that changes
the number, and it costs the manager nothing to look at.

The comparison is deliberately careful about what it will and will not say. A
group of three does not get a gap figure — with three people the "gap" is one
person's salary and the calculation names them. And a gap is reported as an
observation with its sample size attached, never as a verdict: pay differences
have legitimate explanations and a tool that asserts discrimination on four data
points will be switched off within a quarter.

Pure. All amounts are Decimal.
"""

from __future__ import annotations

import enum
import statistics
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "MIN_COMPARISON_GROUP",
    "BudgetState",
    "IncrementBand",
    "IncrementError",
    "OverageMode",
    "PeerFacts",
    "band_for",
    "compa_ratio",
    "consume",
    "equity_snapshot",
    "headroom",
    "recommend",
]


class IncrementError(ValueError):
    """An increment operation this module refuses."""


def money(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def pct(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


#: Below this many comparable people, no gap figure is produced. With three
#: people a "gap" is one person's salary, and the calculation names them.
MIN_COMPARISON_GROUP = 5


class OverageMode(str, enum.Enum):
    #: Refuses submission. Protects the budget; occasionally stops a manager
    #: doing something finance would have approved.
    BLOCK = "block"
    #: Permits and routes for a higher approval, recording the overage.
    ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class IncrementBand:
    """The percentage range one rating earns, and where the manager should start.

    The rating drives the band; the manager positions within it. A single
    recommended percentage per rating removes the manager's judgement, and a
    band with no recommendation leaves every manager inventing their own
    anchor - which is where the inconsistency comes from.
    """

    rating_label: str
    min_percentage: Decimal
    max_percentage: Decimal
    recommended_percentage: Decimal

    def __post_init__(self) -> None:
        if self.max_percentage < self.min_percentage:
            raise IncrementError(
                f"The band for {self.rating_label!r} has a maximum below its minimum."
            )
        if not (self.min_percentage <= self.recommended_percentage <= self.max_percentage):
            raise IncrementError(
                f"The recommended {self.recommended_percentage}% for "
                f"{self.rating_label!r} is outside its own band of "
                f"{self.min_percentage}-{self.max_percentage}%. A manager "
                "following the recommendation would be out of policy."
            )
        if self.min_percentage < 0:
            raise IncrementError(
                "A negative increment band is a pay cut, which is not what this "
                "process is for and needs its own consent."
            )

    def contains(self, percentage: Decimal) -> bool:
        return self.min_percentage <= percentage <= self.max_percentage


def band_for(bands: list[IncrementBand], *, rating_label: str) -> IncrementBand:
    """The band for a rating.

    Refuses an unmapped rating rather than falling back to a default. A rating
    with no band is a configuration gap, and quietly applying somebody else's
    band produces a number the employee cannot be given a reason for.
    """
    match = next((b for b in bands if b.rating_label == rating_label), None)
    if match is None:
        raise IncrementError(
            f"No increment band for rating {rating_label!r}. Applying another "
            "rating's band would produce a number nobody can explain to the "
            f"employee. Configured: {sorted(b.rating_label for b in bands)}."
        )
    return match


@dataclass(frozen=True, slots=True)
class Recommendation:
    """One proposed increment, checked against its band."""

    employee_id: str
    rating_label: str
    current_ctc: Decimal
    percentage: Decimal
    recommended_ctc: Decimal
    band: IncrementBand
    within_band: bool
    requires_justification: bool
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "rating_label": self.rating_label,
            "current_ctc": str(self.current_ctc),
            "percentage": str(self.percentage),
            "recommended_ctc": str(self.recommended_ctc),
            "band": {
                "min": str(self.band.min_percentage),
                "max": str(self.band.max_percentage),
                "recommended": str(self.band.recommended_percentage),
            },
            "within_band": self.within_band,
            "requires_justification": self.requires_justification,
            "message": self.message,
        }


def recommend(
    *,
    employee_id: str,
    rating_label: str,
    current_ctc: Decimal,
    percentage: Decimal,
    bands: list[IncrementBand],
    justification: str = "",
) -> Recommendation:
    """Value a proposed increment and say whether it sits inside its band.

    Outside the band is permitted **with a justification** rather than refused.
    A rating scale cannot anticipate the person who took over a failing project
    in March, and a hard band produces a manager who rates them higher than they
    earned in order to pay them what they are worth - which corrupts the rating
    data everything else depends on.
    """
    if current_ctc <= 0:
        raise IncrementError("A current CTC of zero cannot be incremented.")

    band = band_for(bands, rating_label=rating_label)
    within = band.contains(percentage)
    new_ctc = money(current_ctc * (Decimal("1") + pct(percentage) / Decimal("100")))

    if within:
        message = (
            f"{percentage}% is inside the {band.min_percentage}-"
            f"{band.max_percentage}% band for {rating_label}."
        )
    elif justification.strip():
        message = (
            f"{percentage}% is outside the {band.min_percentage}-"
            f"{band.max_percentage}% band for {rating_label}, with a "
            "justification recorded for the approver."
        )
    else:
        message = (
            f"{percentage}% is outside the {band.min_percentage}-"
            f"{band.max_percentage}% band for {rating_label}. It can still be "
            "submitted with a written reason - a hard band produces a manager "
            "who inflates the rating to pay what somebody is worth, which "
            "corrupts the rating data everything else depends on."
        )

    return Recommendation(
        employee_id=employee_id,
        rating_label=rating_label,
        current_ctc=money(current_ctc),
        percentage=pct(percentage),
        recommended_ctc=new_ctc,
        band=band,
        within_band=within,
        requires_justification=not within and not justification.strip(),
        message=message,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Live budget
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class BudgetState:
    """What is left, in the shape a manager needs while allocating."""

    scope: str
    scope_key: str
    allocated: Decimal
    consumed: Decimal
    remaining: Decimal
    #: Decimal, not float. A ratio is not money, but the same `float(...)` on an
    #: amount would be, and distinguishing the two by eye is exactly what goes
    #: wrong - so nothing in this module leaves Decimal.
    utilisation: Decimal | None
    over_budget: bool
    mode: OverageMode
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "scope_key": self.scope_key,
            "allocated": str(self.allocated),
            "consumed": str(self.consumed),
            "remaining": str(self.remaining),
            "utilisation": (str(self.utilisation) if self.utilisation is not None else None),
            "over_budget": self.over_budget,
            "mode": self.mode.value,
            "message": self.message,
        }


def _state(
    *,
    scope: str,
    scope_key: str,
    allocated: Decimal,
    consumed: Decimal,
    mode: OverageMode,
) -> BudgetState:
    remaining = money(allocated - consumed)
    over = remaining < 0
    return BudgetState(
        scope=scope,
        scope_key=scope_key,
        allocated=money(allocated),
        consumed=money(consumed),
        remaining=remaining,
        utilisation=(
            (Decimal(consumed) / Decimal(allocated)).quantize(Decimal("0.001"))
            if allocated > 0
            else None
        ),
        over_budget=over,
        mode=mode,
        message=(
            f"{money(-remaining)} over the {scope} budget."
            if over
            else f"{remaining} of {money(allocated)} left in the {scope} budget."
        ),
    )


def headroom(
    *,
    department_allocated: Decimal,
    department_consumed: Decimal,
    total_allocated: Decimal,
    total_consumed: Decimal,
    department_key: str,
    mode: OverageMode = OverageMode.ESCALATE,
) -> dict[str, Any]:
    """What is left, before a recommendation is entered.

    Both scopes, always. A manager told their department has room while the
    company total is exhausted will allocate and be rejected, which is the
    wasted second pass this exists to prevent.
    """
    department = _state(
        scope="department",
        scope_key=department_key,
        allocated=department_allocated,
        consumed=department_consumed,
        mode=mode,
    )
    company = _state(
        scope="company",
        scope_key="all",
        allocated=total_allocated,
        consumed=total_consumed,
        mode=mode,
    )
    binding = department if department.remaining <= company.remaining else company
    return {
        "department": department.as_dict(),
        "company": company.as_dict(),
        "binding_scope": binding.scope,
        "spendable": str(max(Decimal("0"), binding.remaining)),
        "note": (f"The {binding.scope} budget is the tighter of the two. {binding.message}"),
    }


@dataclass(frozen=True, slots=True)
class ConsumeResult:
    """Whether this recommendation fits, and what is left after it."""

    permitted: bool
    requires_escalation: bool
    overage: Decimal
    after: dict[str, Any]
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "permitted": self.permitted,
            "requires_escalation": self.requires_escalation,
            "overage": str(self.overage),
            "after": self.after,
            "message": self.message,
        }


def consume(
    *,
    increase: Decimal,
    department_allocated: Decimal,
    department_consumed: Decimal,
    total_allocated: Decimal,
    total_consumed: Decimal,
    department_key: str,
    mode: OverageMode = OverageMode.ESCALATE,
) -> ConsumeResult:
    """Apply one recommendation to the budget and say what happens.

    `increase` is the **annual cost of the rise**, not the new CTC. Charging the
    whole salary against an increment budget is a mistake that only shows up
    when the first department appears 900% over.
    """
    if increase < 0:
        raise IncrementError(
            "A negative increment reduces somebody's pay. That is not what this "
            "process does and it needs its own consent."
        )

    after = headroom(
        department_allocated=department_allocated,
        department_consumed=department_consumed + increase,
        total_allocated=total_allocated,
        total_consumed=total_consumed + increase,
        department_key=department_key,
        mode=mode,
    )
    over_dept = Decimal(after["department"]["remaining"]) < 0
    over_company = Decimal(after["company"]["remaining"]) < 0
    overage = money(
        max(
            Decimal("0"),
            -min(
                Decimal(after["department"]["remaining"]),
                Decimal(after["company"]["remaining"]),
            ),
        )
    )

    if not (over_dept or over_company):
        return ConsumeResult(
            permitted=True,
            requires_escalation=False,
            overage=Decimal("0.00"),
            after=after,
            message=f"Within budget. {after['note']}",
        )

    scope = (
        "department and company"
        if over_dept and over_company
        else ("department" if over_dept else "company")
    )
    if mode is OverageMode.BLOCK:
        return ConsumeResult(
            permitted=False,
            requires_escalation=False,
            overage=overage,
            after=after,
            message=(
                f"This recommendation takes the {scope} budget {overage} over, "
                "and the cycle is set to block. Reduce it, or ask for the "
                "allocation to be raised - which is a decision somebody makes "
                "rather than one that happens by accumulation."
            ),
        )
    return ConsumeResult(
        permitted=True,
        requires_escalation=True,
        overage=overage,
        after=after,
        message=(
            f"This recommendation takes the {scope} budget {overage} over. It "
            "can be submitted and routes for a higher approval, with the "
            "overage on the record."
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Equity, surfaced at the point of decision
# ══════════════════════════════════════════════════════════════════════════════


def compa_ratio(*, ctc: Decimal, band_midpoint: Decimal) -> dict[str, Any]:
    """Where a salary sits against the midpoint of its band.

    Reported with a reading, because 0.82 means nothing to most managers and
    "18% below the midpoint for this grade" means something immediately.
    """
    if band_midpoint <= 0:
        raise IncrementError(
            "A band midpoint of zero cannot produce a compa-ratio. Configure "
            "the salary band for this grade before quoting one."
        )
    ratio = (Decimal(ctc) / Decimal(band_midpoint)).quantize(Decimal("0.001"))
    percent = (ratio - Decimal("1")) * Decimal("100")
    return {
        "compa_ratio": str(ratio),
        "band_midpoint": str(money(band_midpoint)),
        "reading": (
            f"{abs(percent).quantize(Decimal('0.1'))}% "
            f"{'above' if percent >= 0 else 'below'} the midpoint for this "
            "grade."
        ),
    }


@dataclass(frozen=True, slots=True)
class PeerFacts:
    """One comparable person, for the parity view."""

    employee_id: str
    ctc: Decimal
    grade: str
    role: str
    location: str
    gender: str = ""


def equity_snapshot(
    *,
    employee_id: str,
    proposed_ctc: Decimal,
    peers: list[PeerFacts],
    employee_gender: str = "",
    band_midpoint: Decimal | None = None,
    min_group: int = MIN_COMPARISON_GROUP,
) -> dict[str, Any]:
    """Where this recommendation places the employee among comparable people.

    Three deliberate restraints:

    - A group below `min_group` gets **no gap figure**. With three people the
      "gap" is one person's salary and the calculation names them.
    - A gap is reported as an observation with its sample size, never as a
      verdict. Pay differences have legitimate explanations, and a tool that
      asserts discrimination on four data points is switched off within a
      quarter and stops helping anybody.
    - The employee's own row is excluded from the peer statistics, so a small
      group is not compared largely against itself.
    """
    others = [p for p in peers if p.employee_id != employee_id]
    salaries = [Decimal(p.ctc) for p in others]

    out: dict[str, Any] = {
        "employee_id": employee_id,
        "proposed_ctc": str(money(proposed_ctc)),
        "comparison_group_size": len(others),
    }

    if band_midpoint is not None:
        out["compa_ratio"] = compa_ratio(ctc=proposed_ctc, band_midpoint=band_midpoint)

    if len(others) < min_group:
        out["parity"] = None
        out["gender_gap"] = None
        out["note"] = (
            f"{len(others)} comparable people, below the {min_group} needed. No "
            "parity or gap figure is produced: with a group this small the "
            "comparison is one person's salary and the arithmetic names them."
        )
        return out

    median = money(statistics.median(salaries))
    below = sum(1 for s in salaries if s < Decimal(proposed_ctc))
    out["parity"] = {
        "peer_median": str(median),
        "peer_min": str(money(min(salaries))),
        "peer_max": str(money(max(salaries))),
        "percentile": round(below / len(salaries), 2),
        "reading": (
            f"This recommendation places them above {below} of {len(salaries)} "
            f"comparable colleagues, against a median of {median}."
        ),
    }

    by_gender: dict[str, list[Decimal]] = {}
    for peer in others:
        if peer.gender:
            by_gender.setdefault(peer.gender.lower(), []).append(Decimal(peer.ctc))

    usable = {g: v for g, v in by_gender.items() if len(v) >= min_group}
    if len(usable) < 2:
        out["gender_gap"] = None
        out["note"] = (
            "No gender gap figure: fewer than two gender groups in this "
            f"comparison have {min_group} or more people, and a gap computed "
            "across smaller groups identifies individuals."
        )
        return out

    medians = {g: money(statistics.median(v)) for g, v in usable.items()}
    highest = max(medians, key=lambda g: medians[g])
    lowest = min(medians, key=lambda g: medians[g])
    gap = medians[highest] - medians[lowest]
    gap_percent = (
        (gap / medians[highest] * Decimal("100")).quantize(Decimal("0.1"))
        if medians[highest] > 0
        else Decimal("0")
    )

    out["gender_gap"] = {
        "group_medians": {g: str(m) for g, m in medians.items()},
        "counts": {g: len(v) for g, v in usable.items()},
        "gap_amount": str(money(gap)),
        "gap_percent": str(gap_percent),
        "higher_group": highest,
        "lower_group": lowest,
        "employee_group": employee_gender.lower() or None,
        "reading": (
            f"Median pay in this comparison group differs by {gap_percent}% "
            f"between {highest} ({medians[highest]}) and {lowest} "
            f"({medians[lowest]}), across "
            f"{sum(len(v) for v in usable.values())} people. This is an "
            "observation at the moment the number is being chosen, not a "
            "finding: differences have legitimate explanations, and the useful "
            "thing is that it is in front of you now rather than in a report "
            "next March."
        ),
    }
    out["note"] = (
        "Surfaced at the point of decision. Reporting this annually tells the "
        "company what it did; showing it here is the version that can change "
        "the number."
    )
    return out
