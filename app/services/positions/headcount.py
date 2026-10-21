"""Counting seats and the people in them, without counting anybody twice.

═══════════════════════════════════════════════════════════════════════════════
THE TWO WAYS A HEADCOUNT GOES WRONG
═══════════════════════════════════════════════════════════════════════════════

**Additional charge is not a second person.** When a manager leaves, somebody
holds their position as additional charge while it is backfilled. That is one
human being doing two jobs, and a headcount that counts them twice reports a
team as fully staffed at exactly the moment it is short a manager — which is the
one moment the number is being looked at.

So only PRIMARY assignments count toward occupancy. ACTING and ADDITIONAL_CHARGE
are recorded, reported, and excluded from every total. The seat they cover still
shows as vacant, because it is.

**FTE is a fraction and headcount is not an integer.** Two people job-sharing one
role are two employees and one FTE. A part-time position is 0.5 of a seat and
0.5 of a budget. Counting rows instead of summing FTE overstates capacity by
however much part-time work a client does — which in an Indian mid-market
services firm is small, and in a retail or hospitality client is not.

`Decimal` throughout. FTE lands in a budget number, and a budget number computed
in floating point is a budget number that disagrees with finance's.

Pure. No database.
"""

from __future__ import annotations

import enum
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

__all__ = [
    "COUNTS_TOWARD_HEADCOUNT",
    "AssignmentType",
    "HeadcountError",
    "OccupancySnapshot",
    "PositionFacts",
    "PositionStatus",
    "aggregate",
    "occupancy_on",
]


class HeadcountError(ValueError):
    """A count this module refuses to produce."""


#: Two decimal places, always, on the way out.
#:
#: A `Numeric(4,2)` column round-trips `1.0` as `1.00` and a computed value as
#: `1.0`, so the same FTE serialises two different ways depending on whether it
#: came from the database or from arithmetic. That is invisible until one screen
#: shows "3.0 FTE" and another shows "3.00" for the same team.
_SCALE = Decimal("0.01")


def money(value: Decimal) -> str:
    """One number, on its way to a caller. Fixed scale, never a float."""
    return str(Decimal(value).quantize(_SCALE))


class PositionStatus(str, enum.Enum):
    """Where a seat stands.

    APPROVED and VACANT are deliberately different. APPROVED is a seat that has
    been signed off and never filled; VACANT is one that was filled and is not
    any more. They look the same on a headcount and completely different to a
    recruiter — one is a new role, the other is a backfill with a leaver's
    handover behind it.
    """

    APPROVED = "approved"
    FILLED = "filled"
    VACANT = "vacant"
    #: A hiring freeze. The seat exists, is budgeted, and must not be filled.
    #: Distinct from ABOLISHED, which is the seat ceasing to exist.
    FROZEN = "frozen"
    ABOLISHED = "abolished"

    @property
    def is_live(self) -> bool:
        """Whether the seat still exists in the organisation."""
        return self is not PositionStatus.ABOLISHED

    @property
    def is_open(self) -> bool:
        """Whether a recruiter could fill it today."""
        return self in (PositionStatus.APPROVED, PositionStatus.VACANT)


class AssignmentType(str, enum.Enum):
    PRIMARY = "primary"
    #: Covering somebody's absence. A real job, not a second headcount.
    ACTING = "acting"
    #: Holding a second position alongside their own, usually after a departure.
    ADDITIONAL_CHARGE = "additional_charge"


#: The one assignment type that counts. See the module docstring — this is the
#: difference between "the team is fully staffed" and "one person is doing two
#: jobs and we have not backfilled the manager".
COUNTS_TOWARD_HEADCOUNT = frozenset({AssignmentType.PRIMARY})


@dataclass(frozen=True, slots=True)
class AssignmentFacts:
    """One person in one seat, over one span of time.

    `to_date` is inclusive and `None` means still in post. Inclusive because that
    is how an HR system records "their last day was the 31st", and treating it as
    exclusive would show somebody as having left a day early on every report.
    """

    assignment_id: str
    position_id: str
    employee_id: str
    from_date: date
    to_date: date | None = None
    assignment_type: AssignmentType = AssignmentType.PRIMARY
    employee_label: str = ""

    def covers(self, on: date) -> bool:
        if on < self.from_date:
            return False
        return self.to_date is None or on <= self.to_date

    @property
    def counts(self) -> bool:
        return self.assignment_type in COUNTS_TOWARD_HEADCOUNT


@dataclass(frozen=True, slots=True)
class PositionFacts:
    """One approved seat."""

    position_id: str
    code: str
    title: str
    status: PositionStatus
    fte: Decimal = Decimal("1.0")
    department_id: str | None = None
    department_name: str = ""
    location_id: str | None = None
    location_name: str = ""
    cost_centre_id: str | None = None
    cost_centre_name: str = ""
    grade: str | None = None
    reports_to_position_id: str | None = None
    budgeted_ctc_min: Decimal | None = None
    budgeted_ctc_max: Decimal | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    #: When the seat last became empty. Vacancy ageing is measured from here, not
    #: from when somebody noticed.
    vacated_on: date | None = None

    def exists_on(self, on: date) -> bool:
        """Whether the seat exists at all on a given date.

        A position created for next quarter should not appear in this quarter's
        org chart, and one abolished last year should not appear in this one.
        """
        if self.effective_from and on < self.effective_from:
            return False
        if self.effective_to and on > self.effective_to:
            return False
        return self.status.is_live


@dataclass(frozen=True, slots=True)
class OccupancySnapshot:
    """Who was in a seat on a particular day.

    `holders` is every assignment covering the date; `primary_holder` is the one
    that counts. A seat can have both — somebody on additional charge while the
    primary is on long leave — and reporting only the first row found would give
    a different answer depending on insertion order.
    """

    position: PositionFacts
    holders: tuple[AssignmentFacts, ...] = ()
    on: date | None = None

    @property
    def primary_holder(self) -> AssignmentFacts | None:
        return next((h for h in self.holders if h.counts), None)

    @property
    def is_occupied(self) -> bool:
        return self.primary_holder is not None

    @property
    def covered_only(self) -> bool:
        """Somebody is covering it, but the seat is not filled.

        The state a headcount most needs to distinguish and most often does not.
        """
        return bool(self.holders) and self.primary_holder is None

    @property
    def occupied_fte(self) -> Decimal:
        return self.position.fte if self.is_occupied else Decimal(0)

    @property
    def vacant_fte(self) -> Decimal:
        if not self.position.status.is_open and not self.covered_only:
            return Decimal(0)
        return Decimal(0) if self.is_occupied else self.position.fte

    def as_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position.position_id,
            "code": self.position.code,
            "title": self.position.title,
            "status": self.position.status.value,
            "fte": money(self.position.fte),
            "occupied": self.is_occupied,
            "covered_only": self.covered_only,
            "primary_holder": (
                self.primary_holder.employee_label or self.primary_holder.employee_id
                if self.primary_holder
                else None
            ),
            "covering": [h.employee_label or h.employee_id for h in self.holders if not h.counts],
        }


def occupancy_on(
    *,
    positions: list[PositionFacts],
    assignments: list[AssignmentFacts],
    on: date,
) -> list[OccupancySnapshot]:
    """Who was in which seat on a given day.

    The point-in-time question. "Who reported to whom last March" has to be
    answerable from the assignment series rather than from today's org chart,
    because the whole reason to model positions is that people move and the seats
    do not.
    """
    by_position: dict[str, list[AssignmentFacts]] = defaultdict(list)
    for assignment in assignments:
        if assignment.covers(on):
            by_position[assignment.position_id].append(assignment)

    snapshots: list[OccupancySnapshot] = []
    for position in positions:
        if not position.exists_on(on):
            continue
        holders = sorted(
            by_position.get(position.position_id, ()),
            # Primary first, so `primary_holder` is not order-dependent.
            key=lambda a: (not a.counts, a.from_date, a.assignment_id),
        )
        snapshots.append(OccupancySnapshot(position=position, holders=tuple(holders), on=on))
    return snapshots


@dataclass(frozen=True, slots=True)
class Totals:
    """A headcount, in the three numbers that are actually different."""

    #: Seats that exist and are not abolished.
    approved_fte: Decimal = Decimal(0)
    filled_fte: Decimal = Decimal(0)
    vacant_fte: Decimal = Decimal(0)
    frozen_fte: Decimal = Decimal(0)

    #: Distinct human beings holding a primary assignment. Not the same as
    #: `filled_fte` — two half-time people in one seat are two people and one FTE.
    people: int = 0
    positions: int = 0
    #: Seats where somebody is covering but nobody is appointed. Reported
    #: separately because "covered" reads as "filled" on every chart that does
    #: not have this number.
    covered_only: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved_fte": money(self.approved_fte),
            "filled_fte": money(self.filled_fte),
            "vacant_fte": money(self.vacant_fte),
            "frozen_fte": money(self.frozen_fte),
            "people": self.people,
            "positions": self.positions,
            "covered_only": self.covered_only,
        }


def aggregate(snapshots: list[OccupancySnapshot], *, by: str | None = None) -> dict[str, Totals]:
    """Roll up occupancy, optionally grouped.

    `by` is one of `department`, `location`, `cost_centre`, `grade`, or `None`
    for a single total under the key `"all"`.

    Every FTE figure is a `Decimal` sum. A part-time position is 0.5 of a seat
    and 0.5 of a budget, and counting rows would overstate capacity by however
    much part-time work the client does.
    """
    allowed = {None, "department", "location", "cost_centre", "grade"}
    if by not in allowed:
        raise HeadcountError(
            f"'{by}' is not something positions can be grouped by. Available: "
            f"{sorted(str(a) for a in allowed)}."
        )

    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "approved": Decimal(0),
            "filled": Decimal(0),
            "vacant": Decimal(0),
            "frozen": Decimal(0),
            "people": set(),
            "positions": 0,
            "covered_only": 0,
        }
    )

    for snapshot in snapshots:
        position = snapshot.position
        key = {
            None: "all",
            "department": position.department_name or "(no department)",
            "location": position.location_name or "(no location)",
            "cost_centre": position.cost_centre_name or "(no cost centre)",
            "grade": position.grade or "(no grade)",
        }[by]
        bucket = buckets[key]

        bucket["positions"] += 1
        bucket["approved"] += position.fte

        if position.status is PositionStatus.FROZEN:
            bucket["frozen"] += position.fte

        if snapshot.is_occupied:
            bucket["filled"] += position.fte
            # A set, not a counter. One person holding two positions is one
            # person; adding them twice is the same double-count the assignment
            # types exist to prevent, arriving by a different route.
            holder = snapshot.primary_holder
            if holder is not None:
                bucket["people"].add(holder.employee_id)
        else:
            bucket["vacant"] += position.fte
            if snapshot.covered_only:
                bucket["covered_only"] += 1

    return {
        key: Totals(
            approved_fte=bucket["approved"],
            filled_fte=bucket["filled"],
            vacant_fte=bucket["vacant"],
            frozen_fte=bucket["frozen"],
            people=len(bucket["people"]),
            positions=bucket["positions"],
            covered_only=bucket["covered_only"],
        )
        for key, bucket in buckets.items()
    }


def people_holding_more_than_one_position(
    snapshots: list[OccupancySnapshot],
) -> dict[str, list[str]]:
    """Who is stretched across two seats, and which.

    Worth surfacing on its own: additional charge is invisible in a headcount by
    design, and a person who has been holding a second position for five months
    is a backfill nobody has got round to.
    """
    holdings: dict[str, list[str]] = defaultdict(list)
    for snapshot in snapshots:
        for holder in snapshot.holders:
            holdings[holder.employee_id].append(snapshot.position.code)
    return {employee_id: sorted(codes) for employee_id, codes in holdings.items() if len(codes) > 1}
