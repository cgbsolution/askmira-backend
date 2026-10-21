"""How long seats have stood empty, and what that costs.

Vacancy ageing is the number that drives backfill decisions, and it is measured
**from the date the seat was vacated, not from the date somebody raised a
requisition.** The gap between those two is usually where the delay actually is:
a seat empty for four months with a requisition raised last week reads as "one
week old" on the wrong measure, and the four months are exactly what a hiring
review needs to see.

A frozen position is aged too. A freeze is a decision to leave a seat empty, and
knowing it has been empty for seven months is what makes the freeze reviewable
rather than permanent by inertia.

Pure. `as_of` is always supplied, so the buckets are testable without waiting.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.services.positions.headcount import (
    OccupancySnapshot,
    PositionStatus,
    money,
)

__all__ = [
    "AGEING_BUCKETS",
    "VacancyAge",
    "age_buckets",
    "ageing",
    "budget_gap",
]

#: Upper bounds in days, and the label. Chosen against how a hiring conversation
#: actually goes: under a month is normal, one to three months is a live search,
#: three to six is a search that has stalled, and beyond six months somebody
#: should be asked whether the seat is still needed.
AGEING_BUCKETS: tuple[tuple[int | None, str], ...] = (
    (30, "Under a month"),
    (90, "1-3 months"),
    (180, "3-6 months"),
    (None, "Over 6 months"),
)


@dataclass(frozen=True, slots=True)
class VacancyAge:
    position_id: str
    code: str
    title: str
    status: PositionStatus
    department_name: str
    fte: Decimal
    #: `None` where the seat has never been filled and has no effective-from
    #: date either — nothing to measure from, and a made-up zero would read as
    #: "opened today".
    days_vacant: int | None
    since: date | None
    bucket: str
    #: Somebody is covering it. The seat is still vacant, and the cover is often
    #: why nobody has noticed how long.
    covered_by: tuple[str, ...] = ()
    budgeted_ctc_max: Decimal | None = None

    @property
    def is_stale(self) -> bool:
        return self.days_vacant is not None and self.days_vacant > 180

    def as_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "code": self.code,
            "title": self.title,
            "status": self.status.value,
            "department": self.department_name,
            "fte": money(self.fte),
            "days_vacant": self.days_vacant,
            "since": self.since.isoformat() if self.since else None,
            "bucket": self.bucket,
            "covered_by": list(self.covered_by),
            "stale": self.is_stale,
        }


def _bucket_for(days: int | None) -> str:
    if days is None:
        return "Unknown"
    for upper, label in AGEING_BUCKETS:
        if upper is None or days < upper:
            return label
    return AGEING_BUCKETS[-1][1]  # pragma: no cover - the None bound catches all


def ageing(snapshots: list[OccupancySnapshot], *, as_of: date) -> list[VacancyAge]:
    """Every empty seat, oldest first.

    Measured from `vacated_on` where the seat was filled and emptied, and from
    `effective_from` where it was approved and never filled. Those are genuinely
    different things — one is a backfill, the other is a new role that has never
    been recruited for — but both are a seat somebody approved and nobody is in.
    """
    out: list[VacancyAge] = []

    for snapshot in snapshots:
        if snapshot.is_occupied:
            continue
        position = snapshot.position
        if position.status is PositionStatus.ABOLISHED:
            continue

        # From the vacate date, not from when a requisition was raised. The gap
        # between those two is usually where the delay is.
        since = position.vacated_on or position.effective_from
        days = (as_of - since).days if since and since <= as_of else None

        out.append(
            VacancyAge(
                position_id=position.position_id,
                code=position.code,
                title=position.title,
                status=position.status,
                department_name=position.department_name or "(no department)",
                fte=position.fte,
                days_vacant=days,
                since=since,
                bucket=_bucket_for(days),
                covered_by=tuple(h.employee_label or h.employee_id for h in snapshot.holders),
                budgeted_ctc_max=position.budgeted_ctc_max,
            )
        )

    # Oldest first: the list is read from the top and the top is where the
    # problem is.
    return sorted(out, key=lambda v: (v.days_vacant is None, -(v.days_vacant or 0), v.code))


def age_buckets(vacancies: list[VacancyAge]) -> dict[str, dict[str, Any]]:
    """Vacancies grouped by how long they have been open."""
    buckets: dict[str, dict[str, Any]] = {
        label: {"count": 0, "fte": Decimal(0), "positions": []} for _, label in AGEING_BUCKETS
    }
    buckets["Unknown"] = {"count": 0, "fte": Decimal(0), "positions": []}

    for vacancy in vacancies:
        bucket = buckets[vacancy.bucket]
        bucket["count"] += 1
        bucket["fte"] += vacancy.fte
        bucket["positions"].append(vacancy.code)

    return {
        label: {
            "count": bucket["count"],
            "fte": money(bucket["fte"]),
            "positions": bucket["positions"],
        }
        for label, bucket in buckets.items()
    }


@dataclass(frozen=True, slots=True)
class BudgetGap:
    """Budgeted cost against what is actually being paid.

    Both directions matter and they mean different things. Under budget with
    vacancies is money not yet spent that will be; under budget with everything
    filled means the band is wrong or people are being underpaid relative to it.
    Over budget is a conversation with finance either way.
    """

    scope: str
    budgeted_cost: Decimal
    actual_cost: Decimal
    approved_fte: Decimal
    filled_fte: Decimal

    @property
    def gap(self) -> Decimal:
        """Positive means under budget."""
        return self.budgeted_cost - self.actual_cost

    @property
    def vacant_fte(self) -> Decimal:
        return self.approved_fte - self.filled_fte

    @property
    def headline(self) -> str:
        if self.vacant_fte > 0 and self.gap > 0:
            return (
                f"{self.gap} under budget, with {self.vacant_fte} FTE unfilled. Most of "
                "this is money that will be spent once the seats are filled."
            )
        if self.gap > 0:
            return (
                f"{self.gap} under budget with every seat filled. Either the bands are "
                "set above what the roles actually pay, or people in them are paid "
                "below their own band."
            )
        if self.gap < 0:
            return (
                f"{-self.gap} over budget. Filled cost exceeds the approved band for "
                "these positions."
            )
        return "Filled cost matches the approved budget exactly."

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "budgeted_cost": money(self.budgeted_cost),
            "actual_cost": money(self.actual_cost),
            "gap": money(self.gap),
            "approved_fte": money(self.approved_fte),
            "filled_fte": money(self.filled_fte),
            "vacant_fte": money(self.vacant_fte),
            "headline": self.headline,
        }


def budget_gap(
    snapshots: list[OccupancySnapshot],
    *,
    actual_cost_by_employee: dict[str, Decimal],
    by: str = "department",
) -> list[BudgetGap]:
    """Budgeted band against actual pay, per scope.

    Budget uses `budgeted_ctc_max` — the ceiling, because that is what was
    approved and what the seat could cost. Using the midpoint would understate
    the commitment, and a budget report that understates is one nobody trusts
    twice.

    A filled position whose holder's salary is not in `actual_cost_by_employee`
    contributes zero to actual and is counted in `unpriced`, rather than being
    silently treated as free — a caller without payroll access sees the vacancy
    picture with the cost half honestly blank.
    """
    buckets: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {
            "budgeted": Decimal(0),
            "actual": Decimal(0),
            "approved_fte": Decimal(0),
            "filled_fte": Decimal(0),
        }
    )

    for snapshot in snapshots:
        position = snapshot.position
        if position.status is PositionStatus.ABOLISHED:
            continue
        key = {
            "department": position.department_name or "(no department)",
            "location": position.location_name or "(no location)",
            "cost_centre": position.cost_centre_name or "(no cost centre)",
        }.get(by, "all")
        bucket = buckets[key]

        # Pro-rated by FTE: a half-time seat is half the budget.
        ceiling = position.budgeted_ctc_max or Decimal(0)
        bucket["budgeted"] += ceiling * position.fte
        bucket["approved_fte"] += position.fte

        holder = snapshot.primary_holder
        if holder is not None:
            bucket["filled_fte"] += position.fte
            bucket["actual"] += actual_cost_by_employee.get(holder.employee_id, Decimal(0))

    return [
        BudgetGap(
            scope=key,
            budgeted_cost=bucket["budgeted"],
            actual_cost=bucket["actual"],
            approved_fte=bucket["approved_fte"],
            filled_fte=bucket["filled_fte"],
        )
        for key, bucket in sorted(buckets.items())
    ]
