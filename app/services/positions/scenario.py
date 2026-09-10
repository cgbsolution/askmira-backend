"""What-if planning on positions, structurally unable to touch live data.

═══════════════════════════════════════════════════════════════════════════════
THE ISOLATION IS THE FEATURE
═══════════════════════════════════════════════════════════════════════════════

A reorg scenario is somebody dragging thirty positions between departments to see
what it costs. They will do it on a Friday afternoon, they will get it wrong
twice, and they must not be able to change the live org chart by doing so.

The guarantee here is not "we remember to work on a copy". This module takes
`PositionFacts` — frozen dataclasses — and returns new ones. **It has no session,
no model imports, and no way to write anything.** An ORM object never enters it,
so no amount of carelessness inside it can reach a row. That is the same
argument the arrears engine makes, and it is the only version of this guarantee
worth having: a rule enforced by what the code can reach, not by what it
remembers.

Committing a scenario is a separate, deliberate step in the service layer, which
replays the recorded changes against live rows. Until then a scenario is a list
of intentions and a projected set of numbers.

**Every change is reversible and every scenario diffs against the live baseline.**
A planning tool that cannot show you what it changed is a planning tool nobody
signs off.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any

from app.services.positions.headcount import (
    AssignmentFacts,
    OccupancySnapshot,
    PositionFacts,
    PositionStatus,
    aggregate,
    money,
    occupancy_on,
)

__all__ = [
    "ChangeKind",
    "ScenarioChange",
    "ScenarioError",
    "ScenarioResult",
    "apply_changes",
    "compare",
]


class ScenarioError(ValueError):
    """A change this engine will not model."""


class ChangeKind(str, enum.Enum):
    """What a planner can do to a seat.

    Deliberately five verbs and no general "edit". A scenario made of arbitrary
    field updates cannot be summarised, cannot be reviewed, and cannot be
    explained to whoever has to approve it. Each of these has a sentence.
    """

    #: A new seat with a cost. The growth case.
    CREATE = "create"
    #: Between departments, locations or cost centres. The reorg case.
    MOVE = "move"
    #: Stop filling it. The freeze case — the seat and its budget survive.
    FREEZE = "freeze"
    UNFREEZE = "unfreeze"
    #: The seat ceases to exist. Its budget goes with it.
    ABOLISH = "abolish"


@dataclass(frozen=True, slots=True)
class ScenarioChange:
    """One intention, recorded before anything is calculated."""

    kind: ChangeKind
    #: Absent only for CREATE, where the position does not exist yet.
    position_id: str | None = None
    #: The new values, for CREATE and MOVE. Only the keys each verb allows.
    values: dict[str, Any] = field(default_factory=dict)
    #: Why. Required for ABOLISH and FREEZE — those are decisions somebody has to
    #: defend later, and "reorg" written at the time is worth more than a
    #: reconstruction six months on.
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "position_id": self.position_id,
            "values": {k: str(v) if isinstance(v, Decimal | date) else v
                       for k, v in self.values.items()},
            "reason": self.reason,
        }


#: What MOVE is allowed to change. A move that could also change the grade and
#: the budget is not a move — it is an edit wearing a move's name, and it would
#: hide a pay rise inside a reorg.
_MOVABLE = frozenset(
    {"department_id", "department_name", "location_id", "location_name",
     "cost_centre_id", "cost_centre_name", "reports_to_position_id"}
)

#: What CREATE needs. A seat with no title and no cost is not a plan.
_REQUIRED_ON_CREATE = ("code", "title")


def _validate(change: ScenarioChange, known: set[str]) -> None:
    if change.kind is ChangeKind.CREATE:
        missing = [k for k in _REQUIRED_ON_CREATE if not change.values.get(k)]
        if missing:
            raise ScenarioError(
                f"A new position needs {missing}. A seat with no title is not something "
                "anybody can approve."
            )
        if change.values.get("budgeted_ctc_max") is None:
            raise ScenarioError(
                "A new position needs a budget ceiling. The whole point of modelling a "
                "growth case is the cost, and a seat with no cost makes the scenario "
                "look free."
            )
        return

    if change.position_id is None:
        raise ScenarioError(f"A {change.kind.value} change has to name a position.")
    if change.position_id not in known:
        raise ScenarioError(
            f"Position '{change.position_id}' is not in this scenario's baseline. It "
            "may have been abolished since the scenario was started."
        )

    if change.kind is ChangeKind.MOVE:
        unknown = set(change.values) - _MOVABLE
        if unknown:
            raise ScenarioError(
                f"A move cannot change {sorted(unknown)}. A move that also changed the "
                "grade or the budget would hide a pay rise inside a reorg — model those "
                "as their own changes."
            )
        if not change.values:
            raise ScenarioError("A move that changes nothing is not a move.")

    if change.kind in (ChangeKind.ABOLISH, ChangeKind.FREEZE) and not change.reason.strip():
        raise ScenarioError(
            f"A {change.kind.value} needs a reason. It is a decision somebody has to "
            "defend later, and 'reorg' written at the time is worth more than a "
            "reconstruction six months on."
        )


def _apply_one(
    positions: dict[str, PositionFacts], change: ScenarioChange
) -> dict[str, PositionFacts]:
    """One change, returning a new dict. Nothing is mutated in place."""
    updated = dict(positions)

    if change.kind is ChangeKind.CREATE:
        values = change.values
        # A synthetic id, clearly marked. It is not a real position id and must
        # never be mistaken for one if a scenario payload is read elsewhere.
        new_id = str(values.get("position_id") or f"scenario:{values['code']}")
        updated[new_id] = PositionFacts(
            position_id=new_id,
            code=str(values["code"]),
            title=str(values["title"]),
            status=PositionStatus.APPROVED,
            fte=Decimal(str(values.get("fte", "1.0"))),
            department_id=values.get("department_id"),
            department_name=values.get("department_name", ""),
            location_id=values.get("location_id"),
            location_name=values.get("location_name", ""),
            cost_centre_id=values.get("cost_centre_id"),
            cost_centre_name=values.get("cost_centre_name", ""),
            grade=values.get("grade"),
            reports_to_position_id=values.get("reports_to_position_id"),
            budgeted_ctc_min=(
                Decimal(str(values["budgeted_ctc_min"]))
                if values.get("budgeted_ctc_min") is not None
                else None
            ),
            budgeted_ctc_max=Decimal(str(values["budgeted_ctc_max"])),
            effective_from=values.get("effective_from"),
        )
        return updated

    current = updated[change.position_id]

    if change.kind is ChangeKind.MOVE:
        updated[change.position_id] = replace(current, **change.values)
    elif change.kind is ChangeKind.FREEZE:
        updated[change.position_id] = replace(current, status=PositionStatus.FROZEN)
    elif change.kind is ChangeKind.UNFREEZE:
        # Back to whichever open state it belongs in: a seat that was emptied
        # returns to VACANT, one never filled returns to APPROVED.
        restored = (
            PositionStatus.VACANT if current.vacated_on else PositionStatus.APPROVED
        )
        updated[change.position_id] = replace(current, status=restored)
    elif change.kind is ChangeKind.ABOLISH:
        updated[change.position_id] = replace(
            current, status=PositionStatus.ABOLISHED
        )
    return updated


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """The projected organisation, and what it would take to get there."""

    positions: tuple[PositionFacts, ...]
    snapshots: tuple[OccupancySnapshot, ...]
    changes: tuple[ScenarioChange, ...]
    warnings: tuple[str, ...] = ()

    def totals(self, by: str | None = None) -> dict[str, Any]:
        return {
            key: totals.as_dict()
            for key, totals in aggregate(list(self.snapshots), by=by).items()
        }


def apply_changes(
    *,
    positions: list[PositionFacts],
    assignments: list[AssignmentFacts],
    changes: list[ScenarioChange],
    on: date,
) -> ScenarioResult:
    """Model the changes and return the projected organisation.

    Takes frozen dataclasses and returns new ones. This function has no session
    and no model imports, so it *cannot* write to the database — which is the
    isolation guarantee stated as something the code can be checked for rather
    than something a reviewer has to notice.
    """
    baseline = {p.position_id: p for p in positions}
    working = dict(baseline)
    warnings: list[str] = []

    for change in changes:
        _validate(change, set(working))
        if change.kind in (ChangeKind.ABOLISH, ChangeKind.FREEZE):
            occupied = [
                a
                for a in assignments
                if a.position_id == change.position_id and a.covers(on) and a.counts
            ]
            if occupied:
                # Modelled, not refused. A reorg that abolishes an occupied seat
                # is a redundancy, which is a real plan somebody makes on purpose
                # — but it must arrive at the review with the person named.
                holder = occupied[0]
                warnings.append(
                    f"{change.kind.value.title()} on "
                    f"{working[change.position_id].code} affects "
                    f"{holder.employee_label or holder.employee_id}, who currently "
                    "holds it. This scenario implies a redundancy or a redeployment."
                )
        working = _apply_one(working, change)

    projected = list(working.values())
    return ScenarioResult(
        positions=tuple(projected),
        snapshots=tuple(occupancy_on(positions=projected, assignments=assignments, on=on)),
        changes=tuple(changes),
        warnings=tuple(warnings),
    )


@dataclass(frozen=True, slots=True)
class Comparison:
    """Live against projected, in the numbers a planner is deciding on."""

    scope: str
    live: dict[str, Any]
    projected: dict[str, Any]
    fte_delta: Decimal
    cost_delta: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "live": self.live,
            "projected": self.projected,
            "fte_delta": money(self.fte_delta),
            "cost_delta": money(self.cost_delta),
        }


def _cost_of(positions: list[PositionFacts]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for position in positions:
        if position.status is PositionStatus.ABOLISHED:
            continue
        key = position.department_name or "(no department)"
        ceiling = position.budgeted_ctc_max or Decimal(0)
        out[key] = out.get(key, Decimal(0)) + ceiling * position.fte
    return out


def compare(
    *,
    live_snapshots: list[OccupancySnapshot],
    result: ScenarioResult,
    by: str | None = "department",
) -> list[Comparison]:
    """What the scenario actually changes.

    A planning tool that cannot show you what it changed is one nobody signs
    off, so this is not optional decoration — it is the output.
    """
    live_totals = aggregate(live_snapshots, by=by)
    projected_totals = aggregate(list(result.snapshots), by=by)

    live_cost = _cost_of([s.position for s in live_snapshots])
    projected_cost = _cost_of(list(result.positions))

    scopes = sorted(set(live_totals) | set(projected_totals))
    out: list[Comparison] = []
    for scope in scopes:
        live = live_totals.get(scope)
        projected = projected_totals.get(scope)
        live_fte = live.approved_fte if live else Decimal(0)
        projected_fte = projected.approved_fte if projected else Decimal(0)
        out.append(
            Comparison(
                scope=scope,
                live=live.as_dict() if live else {},
                projected=projected.as_dict() if projected else {},
                fte_delta=projected_fte - live_fte,
                cost_delta=projected_cost.get(scope, Decimal(0))
                - live_cost.get(scope, Decimal(0)),
            )
        )
    return out
