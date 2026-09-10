"""Positions in the database: occupancy, budget views, and scenario commit.

The engines above hold the arithmetic and the isolation. This module holds the
three things that need a session.

**An exit vacates a seat; it never deletes one.** The seat was approved and
budgeted and it still exists the day after somebody leaves. `vacate` closes the
assignment, sets the status to VACANT and stamps `vacated_on` — which is what
vacancy ageing measures from.

**Committing a scenario replays its recorded changes against live rows**, in
order, in one transaction. Until that call nothing about a scenario has touched a
position. The projection a planner has been looking at came from a pure function
that has no session and cannot write.

**A requisition against no approved position is off-budget, and is flagged.**
Not refused — companies genuinely hire ahead of an approval, and a product that
blocks it will be worked around within a week. Flagged, so the person signing it
knows what they are signing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.org import Department, Location
from app.models.position import (
    AssignmentType,
    CostCentre,
    HeadcountBudget,
    Position,
    PositionAssignment,
    PositionScenario,
    PositionStatus,
    ScenarioChange,
    ScenarioStatus,
)
from app.models.user import User
from app.services import audit
from app.services.positions import headcount as headcount_engine
from app.services.positions import orgchart, vacancy
from app.services.positions import scenario as scenario_engine
from app.services.reporting.ranges import financial_year_bounds, financial_year_label

__all__ = [
    "assign",
    "budget_view",
    "commit_scenario",
    "discard_scenario",
    "fill_from_offer",
    "load_facts",
    "off_budget_flags",
    "org_chart",
    "project_scenario",
    "vacancy_report",
    "vacate",
]


# ═══════════════════════════════════════════════════════════════════════════
# Loading — the boundary between rows and facts
# ═══════════════════════════════════════════════════════════════════════════


async def load_facts(
    session: AsyncSession, user: User
) -> tuple[list[headcount_engine.PositionFacts], list[headcount_engine.AssignmentFacts]]:
    """Everything the engines need, as frozen dataclasses.

    This is the only place ORM rows become facts. Past it, nothing the planning
    code does can reach a database row — which is what makes the scenario
    isolation a property of the code's shape rather than of anybody's care.
    """
    positions = (
        await session.scalars(tenant_scoped(select(Position), Position, user))
    ).all()

    departments = {
        d.id: d.name
        for d in (
            await session.scalars(tenant_scoped(select(Department), Department, user))
        ).all()
    }
    locations = {
        loc.id: loc.name
        for loc in (
            await session.scalars(tenant_scoped(select(Location), Location, user))
        ).all()
    }
    cost_centres = {
        c.id: c.name
        for c in (
            await session.scalars(tenant_scoped(select(CostCentre), CostCentre, user))
        ).all()
    }

    assignments = (
        await session.scalars(
            tenant_scoped(select(PositionAssignment), PositionAssignment, user)
        )
    ).all()
    employee_ids = {a.employee_id for a in assignments}
    labels: dict[uuid.UUID, str] = {}
    if employee_ids:
        people = (
            await session.scalars(
                tenant_scoped(
                    select(Employee).where(Employee.id.in_(employee_ids)),
                    Employee,
                    user,
                )
            )
        ).all()
        labels = {
            p.id: p.display_name or f"{p.first_name} {p.last_name or ''}".strip()
            for p in people
        }

    position_facts = [
        headcount_engine.PositionFacts(
            position_id=str(p.id),
            code=p.code,
            title=p.title,
            status=headcount_engine.PositionStatus(p.status.value),
            fte=Decimal(str(p.fte)),
            department_id=str(p.department_id) if p.department_id else None,
            department_name=departments.get(p.department_id, ""),
            location_id=str(p.location_id) if p.location_id else None,
            location_name=locations.get(p.location_id, ""),
            cost_centre_id=str(p.cost_centre_id) if p.cost_centre_id else None,
            cost_centre_name=cost_centres.get(p.cost_centre_id, ""),
            grade=p.grade,
            reports_to_position_id=(
                str(p.reports_to_position_id) if p.reports_to_position_id else None
            ),
            budgeted_ctc_min=(
                Decimal(str(p.budgeted_ctc_min)) if p.budgeted_ctc_min is not None else None
            ),
            budgeted_ctc_max=(
                Decimal(str(p.budgeted_ctc_max)) if p.budgeted_ctc_max is not None else None
            ),
            effective_from=p.effective_from,
            effective_to=p.effective_to,
            vacated_on=p.vacated_on,
        )
        for p in positions
    ]

    assignment_facts = [
        headcount_engine.AssignmentFacts(
            assignment_id=str(a.id),
            position_id=str(a.position_id),
            employee_id=str(a.employee_id),
            from_date=a.from_date,
            to_date=a.to_date,
            assignment_type=headcount_engine.AssignmentType(a.assignment_type.value),
            employee_label=labels.get(a.employee_id, ""),
        )
        for a in assignments
    ]
    return position_facts, assignment_facts


async def snapshots_on(
    session: AsyncSession, user: User, *, on: date
) -> list[headcount_engine.OccupancySnapshot]:
    positions, assignments = await load_facts(session, user)
    return headcount_engine.occupancy_on(
        positions=positions, assignments=assignments, on=on
    )


# ═══════════════════════════════════════════════════════════════════════════
# Occupancy
# ═══════════════════════════════════════════════════════════════════════════


async def _position(session: AsyncSession, user: User, position_id: uuid.UUID) -> Position:
    row = await session.scalar(
        tenant_scoped(select(Position).where(Position.id == position_id), Position, user)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Position not found")
    return row


async def assign(
    session: AsyncSession,
    user: User,
    *,
    position_id: uuid.UUID,
    employee_id: uuid.UUID,
    from_date: date,
    assignment_type: AssignmentType = AssignmentType.PRIMARY,
    note: str | None = None,
    request: Request | None = None,
) -> PositionAssignment:
    """Put somebody in a seat.

    A second *primary* assignment to an occupied seat is refused: two people
    cannot both hold one position, and the fix is to close the first one. Acting
    and additional-charge assignments are allowed alongside a primary — that is
    exactly what they are for, and they do not count toward headcount.
    """
    position = await _position(session, user, position_id)
    if position.status is PositionStatus.ABOLISHED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Position {position.code} has been abolished. Somebody can only be "
                "appointed to a seat that exists."
            ),
        )
    if position.status is PositionStatus.FROZEN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Position {position.code} is frozen. Unfreeze it first — filling a "
                "frozen seat quietly is how a hiring freeze stops meaning anything."
            ),
        )

    if assignment_type is AssignmentType.PRIMARY:
        existing = await session.scalar(
            tenant_scoped(
                select(PositionAssignment).where(
                    PositionAssignment.position_id == position_id,
                    PositionAssignment.assignment_type == AssignmentType.PRIMARY,
                    PositionAssignment.to_date.is_(None),
                ),
                PositionAssignment,
                user,
            )
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Position {position.code} is already held. Close the current "
                    "assignment first — two people cannot both hold one seat, and "
                    "letting them would double the headcount."
                ),
            )

    row = PositionAssignment(
        tenant_id=user.tenant_id,
        position_id=position_id,
        employee_id=employee_id,
        from_date=from_date,
        assignment_type=assignment_type,
        note=note,
    )
    session.add(row)

    if assignment_type is AssignmentType.PRIMARY:
        position.status = PositionStatus.FILLED
        position.vacated_on = None
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="position_assignment",
        entity_id=row.id,
        summary=(
            f"{assignment_type.value.replace('_', ' ')} assignment to {position.code} "
            f"from {from_date}"
        ),
        request=request,
    )
    return row


async def vacate(
    session: AsyncSession,
    user: User,
    *,
    position_id: uuid.UUID,
    on: date,
    reason: str = "",
    request: Request | None = None,
) -> dict[str, Any]:
    """Empty a seat. The seat survives.

    This is the exit path, and the whole point is what it does *not* do. Deleting
    the position on exit is how an organisation loses track of what it is meant to
    be staffed at, and how a backfill turns into a new approval nobody remembers
    granting.
    """
    position = await _position(session, user, position_id)

    open_assignments = (
        await session.scalars(
            tenant_scoped(
                select(PositionAssignment).where(
                    PositionAssignment.position_id == position_id,
                    PositionAssignment.to_date.is_(None),
                    PositionAssignment.assignment_type == AssignmentType.PRIMARY,
                ),
                PositionAssignment,
                user,
            )
        )
    ).all()

    for assignment in open_assignments:
        assignment.to_date = on

    if position.status is not PositionStatus.ABOLISHED:
        position.status = PositionStatus.VACANT
        # What vacancy ageing measures from. Not the date a requisition was
        # raised — the gap between those two is usually where the delay is.
        position.vacated_on = on
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="position",
        entity_id=position.id,
        summary=(
            f"Position {position.code} vacated on {on}"
            + (f": {reason}" if reason else "")
            + ". The seat remains approved and budgeted."
        ),
        request=request,
    )
    return {
        "position_id": str(position.id),
        "code": position.code,
        "status": position.status.value,
        "vacated_on": on.isoformat(),
        "assignments_closed": len(open_assignments),
        "note": (
            "The position still exists and is still budgeted. Raise a requisition "
            "against it to backfill."
        ),
    }


async def fill_from_offer(
    session: AsyncSession,
    user: User,
    *,
    position_id: uuid.UUID,
    employee_id: uuid.UUID,
    joining_date: date,
    request: Request | None = None,
) -> dict[str, Any]:
    """The slice 19 hand-off: an accepted offer becomes an occupied seat."""
    assignment = await assign(
        session,
        user,
        position_id=position_id,
        employee_id=employee_id,
        from_date=joining_date,
        assignment_type=AssignmentType.PRIMARY,
        note="Filled from an accepted offer.",
        request=request,
    )
    position = await _position(session, user, position_id)
    return {
        "position_id": str(position.id),
        "assignment_id": str(assignment.id),
        "status": position.status.value,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Requisition linkage
# ═══════════════════════════════════════════════════════════════════════════


async def off_budget_flags(
    session: AsyncSession,
    user: User,
    *,
    position_id: uuid.UUID | None,
    department_id: uuid.UUID | None = None,
    requested_ctc_max: Decimal | None = None,
) -> list[dict[str, Any]]:
    """What to tell an approver about a requisition's budget standing.

    A requisition against no approved position is **flagged, not refused**.
    Companies genuinely hire ahead of an approval — a startup that just closed a
    round, a team backfilling before the budget cycle — and a product that blocks
    it will be worked around within a week, at which point the flag is gone too.
    Flagged means the person signing knows what they are signing.
    """
    flags: list[dict[str, Any]] = []

    if position_id is None:
        flags.append(
            {
                "kind": "off_budget",
                "severity": "blocking_attention",
                "message": (
                    "This requisition is not against an approved position. It is "
                    "off-budget: approving it commits headcount and cost that no "
                    "position approval covers."
                ),
            }
        )
        return flags

    position = await _position(session, user, position_id)

    if position.status is PositionStatus.FILLED:
        flags.append(
            {
                "kind": "position_occupied",
                "severity": "blocking_attention",
                "message": (
                    f"Position {position.code} is currently filled. Recruiting against "
                    "an occupied seat means either a replacement nobody has told the "
                    "incumbent about, or the wrong position was picked."
                ),
            }
        )
    elif position.status is PositionStatus.FROZEN:
        flags.append(
            {
                "kind": "position_frozen",
                "severity": "blocking_attention",
                "message": (
                    f"Position {position.code} is frozen: "
                    f"{position.status_reason or 'no reason recorded'}. Approving this "
                    "requisition overrides a freeze somebody decided on."
                ),
            }
        )
    elif position.status is PositionStatus.ABOLISHED:
        flags.append(
            {
                "kind": "position_abolished",
                "severity": "blocking_attention",
                "message": (
                    f"Position {position.code} has been abolished. There is no seat to "
                    "fill."
                ),
            }
        )

    ceiling = (
        Decimal(str(position.budgeted_ctc_max))
        if position.budgeted_ctc_max is not None
        else None
    )
    if requested_ctc_max is not None and ceiling is not None and requested_ctc_max > ceiling:
        flags.append(
            {
                "kind": "over_position_band",
                "severity": "blocking_attention",
                "message": (
                    f"This requisition's ceiling of {requested_ctc_max} is above the "
                    f"{ceiling} approved for position {position.code}. The difference "
                    f"of {requested_ctc_max - ceiling} is unbudgeted."
                ),
            }
        )
    elif ceiling is None:
        flags.append(
            {
                "kind": "position_unbudgeted",
                "severity": "informational",
                "message": (
                    f"Position {position.code} has no budgeted ceiling, so this "
                    "requisition cannot be checked against one."
                ),
            }
        )

    return flags


# ═══════════════════════════════════════════════════════════════════════════
# Views
# ═══════════════════════════════════════════════════════════════════════════


async def budget_view(
    session: AsyncSession,
    user: User,
    *,
    on: date | None = None,
    by: str = "department",
    include_cost: bool = False,
) -> dict[str, Any]:
    """Approved against filled against vacant, and the cost gap.

    `include_cost` is off by default and gated by the caller. A budget view is a
    headcount question for most people and a payroll question for a few, and
    joining salaries in for everybody would make this endpoint a salary leak
    wearing a planning name.
    """
    as_of = on or date.today()
    snapshots = await snapshots_on(session, user, on=as_of)

    actual: dict[str, Decimal] = {}
    if include_cost:
        from app.models.payroll import EmployeeSalary

        rows = (
            await session.scalars(
                tenant_scoped(
                    select(EmployeeSalary).where(EmployeeSalary.effective_to.is_(None)),
                    EmployeeSalary,
                    user,
                )
            )
        ).all()
        actual = {str(r.employee_id): Decimal(str(r.ctc_annual)) for r in rows}

    totals = headcount_engine.aggregate(snapshots, by=by if by != "none" else None)
    financial_year = financial_year_label(financial_year_bounds(as_of)[0])

    budgets = (
        await session.scalars(
            tenant_scoped(
                select(HeadcountBudget).where(
                    HeadcountBudget.financial_year == financial_year
                ),
                HeadcountBudget,
                user,
            )
        )
    ).all()

    return {
        "as_of": as_of.isoformat(),
        "financial_year": financial_year,
        "grouped_by": by,
        "totals": {key: value.as_dict() for key, value in totals.items()},
        "cost": (
            [gap.as_dict() for gap in vacancy.budget_gap(
                snapshots, actual_cost_by_employee=actual, by=by
            )]
            if include_cost
            else None
        ),
        "cost_note": (
            None
            if include_cost
            else (
                "Cost is not included. It needs payroll access, and a planning view "
                "that returned salaries to everybody would be a salary leak wearing a "
                "planning name."
            )
        ),
        "approved_budget": [
            {
                "department_id": str(b.department_id) if b.department_id else None,
                "approved_positions": b.approved_positions,
                "approved_cost": str(b.approved_cost),
            }
            for b in budgets
        ],
    }


async def vacancy_report(
    session: AsyncSession, user: User, *, on: date | None = None
) -> dict[str, Any]:
    """Every empty seat, oldest first, with the ageing buckets."""
    as_of = on or date.today()
    snapshots = await snapshots_on(session, user, on=as_of)
    vacancies = vacancy.ageing(snapshots, as_of=as_of)

    return {
        "as_of": as_of.isoformat(),
        "vacancies": [v.as_dict() for v in vacancies],
        "buckets": vacancy.age_buckets(vacancies),
        "stale_count": sum(1 for v in vacancies if v.is_stale),
        "people_holding_two_seats": headcount_engine.people_holding_more_than_one_position(
            snapshots
        ),
    }


async def org_chart(
    session: AsyncSession,
    user: User,
    *,
    on: date | None = None,
    root_position_id: uuid.UUID | None = None,
    flat: bool = False,
) -> dict[str, Any]:
    """The chart, with the holes in it.

    A chart of only filled roles is a chart of who is here. One including the
    vacant seats is a chart of what the organisation is meant to be, which is the
    one you plan against.
    """
    as_of = on or date.today()
    snapshots = await snapshots_on(session, user, on=as_of)

    try:
        roots, problems = orgchart.build_chart(
            snapshots,
            root_position_id=str(root_position_id) if root_position_id else None,
        )
    except orgchart.OrgChartError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return {
        "as_of": as_of.isoformat(),
        "roots": (
            orgchart.flatten(roots) if flat else [node.as_dict() for node in roots]
        ),
        "problems": problems,
        "vacant_seats": sum(1 for s in snapshots if not s.is_occupied),
        "total_seats": len(snapshots),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Scenarios
# ═══════════════════════════════════════════════════════════════════════════


def _to_changes(rows: list[ScenarioChange]) -> list[scenario_engine.ScenarioChange]:
    return [
        scenario_engine.ScenarioChange(
            kind=scenario_engine.ChangeKind(row.kind),
            position_id=str(row.position_id) if row.position_id else None,
            values=dict(row.values or {}),
            reason=row.reason or "",
        )
        for row in sorted(rows, key=lambda r: r.sequence)
    ]


async def _scenario(
    session: AsyncSession, user: User, scenario_id: uuid.UUID
) -> PositionScenario:
    row = await session.scalar(
        tenant_scoped(
            select(PositionScenario).where(PositionScenario.id == scenario_id),
            PositionScenario,
            user,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scenario not found")
    return row


async def project_scenario(
    session: AsyncSession, user: User, *, scenario_id: uuid.UUID
) -> dict[str, Any]:
    """What this scenario would do, computed without touching anything.

    The projection comes from a pure function that takes frozen dataclasses and
    has no session. Nothing about running this can change a live position.
    """
    row = await _scenario(session, user, scenario_id)
    positions, assignments = await load_facts(session, user)
    live = headcount_engine.occupancy_on(
        positions=positions, assignments=assignments, on=row.as_of
    )

    try:
        result = scenario_engine.apply_changes(
            positions=positions,
            assignments=assignments,
            changes=_to_changes(list(row.changes)),
            on=row.as_of,
        )
    except scenario_engine.ScenarioError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    comparison = scenario_engine.compare(live_snapshots=live, result=result)
    return {
        "scenario_id": str(row.id),
        "name": row.name,
        "status": row.status.value,
        "as_of": row.as_of.isoformat(),
        "changes": [c.as_dict() for c in result.changes],
        "warnings": list(result.warnings),
        "live_totals": {k: v.as_dict() for k, v in
                        headcount_engine.aggregate(live, by="department").items()},
        "projected_totals": result.totals(by="department"),
        "comparison": [c.as_dict() for c in comparison],
        "vacancies_after": [
            v.as_dict()
            for v in vacancy.ageing(list(result.snapshots), as_of=row.as_of)
        ],
    }


async def commit_scenario(
    session: AsyncSession,
    user: User,
    *,
    scenario_id: uuid.UUID,
    request: Request | None = None,
) -> dict[str, Any]:
    """Replay a scenario's changes against live positions, in order.

    The first point at which a scenario touches anything real. Order matters: a
    scenario that moves a position and then abolishes it is a different plan from
    one that does the reverse.
    """
    row = await _scenario(session, user, scenario_id)
    if row.status is not ScenarioStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This scenario is already {row.status.value}. Committing it twice "
                "would apply the same reorg on top of itself."
            ),
        )

    # Projected first: if the plan does not hold together, nothing is written.
    await project_scenario(session, user, scenario_id=scenario_id)

    moment = datetime.now(UTC)
    applied: list[str] = []

    for change in sorted(row.changes, key=lambda c: c.sequence):
        kind = scenario_engine.ChangeKind(change.kind)

        if kind is scenario_engine.ChangeKind.CREATE:
            values = dict(change.values or {})
            position = Position(
                tenant_id=user.tenant_id,
                code=str(values["code"]),
                title=str(values["title"]),
                department_id=_as_uuid(values.get("department_id")),
                location_id=_as_uuid(values.get("location_id")),
                cost_centre_id=_as_uuid(values.get("cost_centre_id")),
                grade=values.get("grade"),
                reports_to_position_id=_as_uuid(values.get("reports_to_position_id")),
                budgeted_ctc_min=values.get("budgeted_ctc_min"),
                budgeted_ctc_max=values.get("budgeted_ctc_max"),
                fte=Decimal(str(values.get("fte", "1.0"))),
                status=PositionStatus.APPROVED,
                effective_from=row.as_of,
                approved_by_user_id=user.id,
                approved_at=moment,
                status_reason=change.reason,
            )
            session.add(position)
            await session.flush()
            change.applied_position_id = position.id
            applied.append(f"created {position.code}")
            continue

        position = await _position(session, user, change.position_id)

        if kind is scenario_engine.ChangeKind.MOVE:
            for field_name in (
                "department_id", "location_id", "cost_centre_id",
                "reports_to_position_id",
            ):
                if field_name in (change.values or {}):
                    setattr(position, field_name, _as_uuid(change.values[field_name]))
            applied.append(f"moved {position.code}")

        elif kind is scenario_engine.ChangeKind.FREEZE:
            position.status = PositionStatus.FROZEN
            position.status_reason = change.reason
            applied.append(f"froze {position.code}")

        elif kind is scenario_engine.ChangeKind.UNFREEZE:
            position.status = (
                PositionStatus.VACANT if position.vacated_on else PositionStatus.APPROVED
            )
            position.status_reason = None
            applied.append(f"unfroze {position.code}")

        elif kind is scenario_engine.ChangeKind.ABOLISH:
            position.status = PositionStatus.ABOLISHED
            position.effective_to = row.as_of
            position.status_reason = change.reason
            applied.append(f"abolished {position.code}")

        change.applied_position_id = position.id

    row.status = ScenarioStatus.COMMITTED
    row.committed_by_user_id = user.id
    row.committed_at = moment
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="position_scenario",
        entity_id=row.id,
        summary=(
            f"Scenario '{row.name}' committed: {len(applied)} change(s) applied to "
            "live positions."
        ),
        changes={"applied": applied},
        request=request,
    )
    return {
        "scenario_id": str(row.id),
        "status": row.status.value,
        "applied": applied,
    }


async def discard_scenario(
    session: AsyncSession, user: User, *, scenario_id: uuid.UUID
) -> dict[str, Any]:
    """Throw a draft away.

    Marked discarded rather than deleted. A reorg somebody modelled and decided
    against is a decision, and it is worth being able to answer "did we look at
    this" a year later.
    """
    row = await _scenario(session, user, scenario_id)
    if row.status is ScenarioStatus.COMMITTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This scenario has been committed. Discarding it would not undo the "
                "changes — build a new scenario to reverse them."
            ),
        )
    row.status = ScenarioStatus.DISCARDED
    row.discarded_at = datetime.now(UTC)
    await session.flush()
    return {"scenario_id": str(row.id), "status": row.status.value}


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
