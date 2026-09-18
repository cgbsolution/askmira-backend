"""Positions in the database: occupancy, budget views, and scenario commit.

The engines above hold the arithmetic and the isolation. This module holds the
things that need a session.

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

**A headcount budget is enforced, not just stored.** Creating a seat, filling
one, moving one into a department or committing a scenario that does either is
refused when it would take a department — or the company — past the FTE or cost
approved for that financial year. See `budget.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select
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
from app.schemas.positions import (
    AssignmentRead,
    BudgetRead,
    BudgetUpsert,
    BudgetUpsertResult,
    ChangeIn,
    CostCentreCreate,
    PositionCreate,
    PositionUpdate,
    ScenarioCreate,
    ScenarioSummary,
)
from app.services import audit
from app.services.positions import budget as budget_engine
from app.services.positions import headcount as headcount_engine
from app.services.positions import orgchart, vacancy
from app.services.positions import scenario as scenario_engine
from app.services.reporting.ranges import financial_year_bounds, financial_year_label

__all__ = [
    "add_scenario_change",
    "assign",
    "assignments_of",
    "budget_view",
    "change_status",
    "commit_scenario",
    "create_cost_centre",
    "create_position",
    "create_scenario",
    "discard_scenario",
    "end_assignment",
    "fill_from_offer",
    "list_budgets",
    "list_cost_centres",
    "list_positions",
    "list_scenarios",
    "load_facts",
    "off_budget_flags",
    "org_chart",
    "project_scenario",
    "update_position",
    "upsert_budget",
    "vacancy_report",
    "vacate",
]

#: A reporting chain between seats longer than this is a loop.
MAX_CHAIN = 50


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _dec(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


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
    labels = await _employee_labels(session, user, {a.employee_id for a in assignments})

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
            budgeted_ctc_min=_dec(p.budgeted_ctc_min),
            budgeted_ctc_max=_dec(p.budgeted_ctc_max),
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


async def _employee_labels(
    session: AsyncSession, user: User, employee_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not employee_ids:
        return {}
    people = (
        await session.scalars(
            tenant_scoped(select(Employee).where(Employee.id.in_(employee_ids)), Employee, user)
        )
    ).all()
    return {
        p.id: p.display_name or f"{p.first_name} {p.last_name or ''}".strip() for p in people
    }


async def snapshots_on(
    session: AsyncSession, user: User, *, on: date
) -> list[headcount_engine.OccupancySnapshot]:
    positions, assignments = await load_facts(session, user)
    return headcount_engine.occupancy_on(
        positions=positions, assignments=assignments, on=on
    )


# ═══════════════════════════════════════════════════════════════════════════
# Headcount budget enforcement
# ═══════════════════════════════════════════════════════════════════════════


def _seat(position: Position) -> budget_engine.Seat:
    return budget_engine.Seat(
        code=position.code,
        fte=Decimal(str(position.fte)),
        budgeted_ctc_max=_dec(position.budgeted_ctc_max),
    )


async def _seats_in_scope(
    session: AsyncSession,
    user: User,
    *,
    department_id: uuid.UUID | None,
    on: date,
    filled_only: bool,
    exclude_position_id: uuid.UUID | None,
) -> list[budget_engine.Seat]:
    """Seats that count against a budget in the financial year containing `on`."""
    start, end = financial_year_bounds(on)
    stmt = select(Position).where(
        Position.status != PositionStatus.ABOLISHED,
        Position.effective_from <= end,
        or_(Position.effective_to.is_(None), Position.effective_to >= start),
    )
    if department_id is not None:
        stmt = stmt.where(Position.department_id == department_id)
    if filled_only:
        stmt = stmt.where(Position.status == PositionStatus.FILLED)
    if exclude_position_id is not None:
        stmt = stmt.where(Position.id != exclude_position_id)
    rows = (await session.scalars(tenant_scoped(stmt, Position, user))).all()
    return [_seat(row) for row in rows]


async def _budget_rows(
    session: AsyncSession, user: User, *, financial_year: str
) -> list[HeadcountBudget]:
    stmt = tenant_scoped(
        select(HeadcountBudget).where(HeadcountBudget.financial_year == financial_year),
        HeadcountBudget,
        user,
    )
    return list((await session.scalars(stmt)).all())


async def _department_names(session: AsyncSession, user: User) -> dict[uuid.UUID, str]:
    rows = (await session.scalars(tenant_scoped(select(Department), Department, user))).all()
    return {d.id: d.name for d in rows}


async def enforce_budget(
    session: AsyncSession,
    user: User,
    *,
    on: date,
    by_department: dict[uuid.UUID, list[budget_engine.Seat]],
    company: list[budget_engine.Seat],
    what: str,
    filled_only: bool = False,
    exclude_position_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Refuse, naming the limit, when the additions exceed an approved budget.

    Department budgets check each department's own additions; a company budget
    (no department) checks all of them. A scope with no budget row is not
    enforced — no budget set is a decision the tenant has not made yet.
    """
    financial_year = financial_year_label(financial_year_bounds(on)[0])
    rows = await _budget_rows(session, user, financial_year=financial_year)
    if not rows:
        return []
    names = await _department_names(session, user)

    checks: list[budget_engine.BudgetCheck] = []
    for row in rows:
        if row.department_id is None:
            adding = company
            scope = "the company"
        else:
            adding = by_department.get(row.department_id, [])
            scope = names.get(row.department_id, "this department")
        if not adding:
            continue
        counted = await _seats_in_scope(
            session,
            user,
            department_id=row.department_id,
            on=on,
            filled_only=filled_only,
            exclude_position_id=exclude_position_id,
        )
        checks.append(
            budget_engine.check(
                budget_engine.Budget(
                    scope=scope,
                    financial_year=financial_year,
                    approved_positions=row.approved_positions,
                    approved_cost=Decimal(str(row.approved_cost)),
                ),
                counted=counted,
                adding=adding,
                what=what,
            )
        )

    problems = [problem for result in checks for problem in result.problems]
    if problems:
        raise _conflict("Refused by the headcount budget: " + " ".join(problems))
    return [result.as_dict() for result in checks]


# ═══════════════════════════════════════════════════════════════════════════
# Positions
# ═══════════════════════════════════════════════════════════════════════════


async def _position(session: AsyncSession, user: User, position_id: uuid.UUID) -> Position:
    row = await session.scalar(
        tenant_scoped(select(Position).where(Position.id == position_id), Position, user)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Position not found")
    return row


async def _check_references(
    session: AsyncSession,
    user: User,
    values: dict[str, Any],
    *,
    position_id: uuid.UUID | None = None,
) -> None:
    """Every id the seat points at is in this workspace, and no reporting loop forms."""
    for key, model, label in (
        ("department_id", Department, "department"),
        ("location_id", Location, "location"),
        ("cost_centre_id", CostCentre, "cost centre"),
    ):
        value = values.get(key)
        if value is not None and await session.scalar(
            tenant_scoped(select(model.id).where(model.id == value), model, user)
        ) is None:
            raise _unprocessable(f"That {label} is not part of this workspace.")

    parent = values.get("reports_to_position_id")
    if parent is None:
        return
    if position_id is not None and parent == position_id:
        raise _unprocessable("A position cannot report to itself.")
    current: uuid.UUID | None = parent
    for _ in range(MAX_CHAIN):
        if current is None:
            return
        row = await session.scalar(
            tenant_scoped(select(Position).where(Position.id == current), Position, user)
        )
        if row is None:
            raise _unprocessable("The position it reports to is not part of this workspace.")
        if position_id is not None and row.reports_to_position_id == position_id:
            raise _unprocessable(
                "That position already reports, directly or indirectly, to this one. "
                "Reporting lines cannot loop."
            )
        current = row.reports_to_position_id
    raise _unprocessable("The reporting chain above that position is too long, or loops.")


async def list_positions(
    session: AsyncSession,
    user: User,
    *,
    position_status: PositionStatus | None = None,
    department_id: uuid.UUID | None = None,
) -> list[Position]:
    stmt = select(Position).order_by(Position.code)
    if position_status is not None:
        stmt = stmt.where(Position.status == position_status)
    if department_id is not None:
        stmt = stmt.where(Position.department_id == department_id)
    return list((await session.scalars(tenant_scoped(stmt, Position, user))).all())


async def create_position(
    session: AsyncSession,
    user: User,
    payload: PositionCreate,
    *,
    request: Request | None = None,
) -> Position:
    """Approve a new seat, inside the headcount budget."""
    if await session.scalar(
        tenant_scoped(select(Position.id).where(Position.code == payload.code), Position, user)
    ) is not None:
        raise _conflict(f"A position with code {payload.code} already exists")
    values = payload.model_dump()
    await _check_references(session, user, values)

    seat = budget_engine.Seat(
        code=payload.code, fte=payload.fte, budgeted_ctc_max=payload.budgeted_ctc_max
    )
    checks = await enforce_budget(
        session,
        user,
        on=payload.effective_from,
        by_department={payload.department_id: [seat]} if payload.department_id else {},
        company=[seat],
        what=f"Creating {payload.code}",
    )

    position = Position(
        tenant_id=user.tenant_id,
        approved_by_user_id=user.id,
        approved_at=datetime.now(UTC),
        **values,
    )
    session.add(position)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="position",
        entity_id=position.id,
        changes={**payload.model_dump(mode="json"), "budget_checks": checks},
        summary=f"Position {position.code} approved from {position.effective_from}",
        request=request,
    )
    return position


async def update_position(
    session: AsyncSession,
    user: User,
    position_id: uuid.UUID,
    payload: PositionUpdate,
    *,
    request: Request | None = None,
) -> Position:
    """Edit a seat's shape. A change of department, FTE or ceiling is re-checked
    against the budget as if the seat were new there."""
    position = await _position(session, user, position_id)
    if position.status is PositionStatus.ABOLISHED:
        raise _conflict(f"Position {position.code} has been abolished and cannot be edited.")
    changes = payload.model_dump(exclude_unset=True)
    for key in ("title", "fte"):
        if key in changes and changes[key] is None:
            raise _unprocessable(f"'{key}' cannot be cleared.")
    await _check_references(session, user, changes, position_id=position.id)

    ceiling = changes.get("budgeted_ctc_max", _dec(position.budgeted_ctc_max))
    floor = changes.get("budgeted_ctc_min", _dec(position.budgeted_ctc_min))
    if ceiling is not None and floor is not None and ceiling < floor:
        raise _unprocessable("The budgeted CTC ceiling is below its floor")

    if {"department_id", "fte", "budgeted_ctc_max"} & set(changes):
        department_id = changes.get("department_id", position.department_id)
        seat = budget_engine.Seat(
            code=position.code,
            fte=changes.get("fte") or Decimal(str(position.fte)),
            budgeted_ctc_max=ceiling,
        )
        await enforce_budget(
            session,
            user,
            on=date.today(),
            by_department={department_id: [seat]} if department_id else {},
            company=[seat],
            what=f"Changing {position.code}",
            exclude_position_id=position.id,
        )

    diff = {
        key: {"from": str(getattr(position, key)), "to": str(value)}
        for key, value in changes.items()
        if getattr(position, key) != value
    }
    for key, value in changes.items():
        setattr(position, key, value)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="position",
        entity_id=position.id,
        changes=diff,
        summary=f"Position {position.code} edited",
        request=request,
    )
    return position


async def change_status(
    session: AsyncSession,
    user: User,
    position_id: uuid.UUID,
    *,
    action: str,
    reason: str,
    on: date | None = None,
    request: Request | None = None,
) -> Position:
    """Freeze, unfreeze or abolish a seat outside a scenario. Never a delete."""
    position = await _position(session, user, position_id)
    effective = on or date.today()
    previous = position.status

    if action in {"freeze", "abolish"} and not reason.strip():
        raise _unprocessable(f"A reason is needed to {action} a position.")

    if action == "freeze":
        if position.status not in {PositionStatus.APPROVED, PositionStatus.VACANT}:
            raise _conflict(
                f"Position {position.code} is {position.status.value}. Only an empty seat can "
                "be frozen — a freeze stops a seat being filled."
            )
        position.status = PositionStatus.FROZEN
        position.status_reason = reason.strip()
    elif action == "unfreeze":
        if position.status is not PositionStatus.FROZEN:
            raise _conflict(f"Position {position.code} is not frozen.")
        position.status = PositionStatus.VACANT if position.vacated_on else PositionStatus.APPROVED
        position.status_reason = None
    elif action == "abolish":
        if position.status is PositionStatus.ABOLISHED:
            raise _conflict(f"Position {position.code} is already abolished.")
        if position.status is PositionStatus.FILLED:
            raise _conflict(
                f"Position {position.code} is filled. Vacate it before abolishing it, so the "
                "person in it is moved deliberately rather than dropped."
            )
        open_rows = (
            await session.scalars(
                tenant_scoped(
                    select(PositionAssignment).where(
                        PositionAssignment.position_id == position.id,
                        PositionAssignment.to_date.is_(None),
                    ),
                    PositionAssignment,
                    user,
                )
            )
        ).all()
        for row in open_rows:
            row.to_date = max(effective, row.from_date)
        position.status = PositionStatus.ABOLISHED
        position.effective_to = effective
        position.status_reason = reason.strip()
    else:  # pragma: no cover - the schema restricts the action
        raise _unprocessable(f"'{action}' is not a position status change.")

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="position",
        entity_id=position.id,
        changes={
            "status": {"from": previous.value, "to": position.status.value},
            "reason": reason,
        },
        summary=f"Position {position.code} {position.status.value}",
        request=request,
    )
    return position


# ═══════════════════════════════════════════════════════════════════════════
# Occupancy
# ═══════════════════════════════════════════════════════════════════════════


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
    exactly what they are for, and they do not count toward headcount. A primary
    fill is also checked against the headcount budget.
    """
    position = await _position(session, user, position_id)
    if position.status is PositionStatus.ABOLISHED:
        raise _conflict(
            f"Position {position.code} has been abolished. Somebody can only be "
            "appointed to a seat that exists."
        )
    if position.status is PositionStatus.FROZEN:
        raise _conflict(
            f"Position {position.code} is frozen. Unfreeze it first — filling a "
            "frozen seat quietly is how a hiring freeze stops meaning anything."
        )

    employee = await session.scalar(
        tenant_scoped(
            select(Employee).where(Employee.id == employee_id, Employee.deleted_at.is_(None)),
            Employee,
            user,
        )
    )
    if employee is None:
        raise _unprocessable("That employee is not an active employee in this workspace.")

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
            raise _conflict(
                f"Position {position.code} is already held. Close the current "
                "assignment first — two people cannot both hold one seat, and "
                "letting them would double the headcount."
            )
        seat = _seat(position)
        await enforce_budget(
            session,
            user,
            on=from_date,
            by_department={position.department_id: [seat]} if position.department_id else {},
            company=[seat],
            what=f"Filling {position.code}",
            filled_only=True,
            exclude_position_id=position.id,
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
        if on < assignment.from_date:
            raise _unprocessable(
                f"{on} is before the current assignment started on {assignment.from_date}."
            )
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


async def assignments_of(
    session: AsyncSession, user: User, position_id: uuid.UUID
) -> list[AssignmentRead]:
    """A seat's occupancy history, newest first."""
    await _position(session, user, position_id)
    rows = (
        await session.scalars(
            tenant_scoped(
                select(PositionAssignment)
                .where(PositionAssignment.position_id == position_id)
                .order_by(PositionAssignment.from_date.desc()),
                PositionAssignment,
                user,
            )
        )
    ).all()
    labels = await _employee_labels(session, user, {row.employee_id for row in rows})
    return [_assignment_read(row, labels) for row in rows]


def _assignment_read(row: PositionAssignment, labels: dict[uuid.UUID, str]) -> AssignmentRead:
    return AssignmentRead(
        id=row.id,
        position_id=row.position_id,
        employee_id=row.employee_id,
        employee_name=labels.get(row.employee_id, ""),
        from_date=row.from_date,
        to_date=row.to_date,
        assignment_type=row.assignment_type,
        counts_toward_headcount=row.assignment_type is AssignmentType.PRIMARY,
        note=row.note,
    )


async def end_assignment(
    session: AsyncSession,
    user: User,
    assignment_id: uuid.UUID,
    *,
    on: date,
    request: Request | None = None,
) -> AssignmentRead:
    """Close an acting or additional-charge assignment. A primary is closed by vacating."""
    row = await session.scalar(
        tenant_scoped(
            select(PositionAssignment).where(PositionAssignment.id == assignment_id),
            PositionAssignment,
            user,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")
    if row.to_date is not None:
        raise _conflict("This assignment has already ended.")
    if row.assignment_type is AssignmentType.PRIMARY:
        raise _conflict(
            "A primary assignment ends by vacating the position, which also records when "
            "the seat became empty."
        )
    if on < row.from_date:
        raise _unprocessable(f"{on} is before the assignment started on {row.from_date}.")
    row.to_date = on
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="position_assignment",
        entity_id=row.id,
        changes={"to_date": on.isoformat()},
        summary=f"{row.assignment_type.value.replace('_', ' ')} assignment ended on {on}",
        request=request,
    )
    labels = await _employee_labels(session, user, {row.employee_id})
    return _assignment_read(row, labels)


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

    ceiling = _dec(position.budgeted_ctc_max)
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

    try:
        totals = headcount_engine.aggregate(snapshots, by=by if by != "none" else None)
    except headcount_engine.HeadcountError as exc:
        raise _unprocessable(str(exc)) from exc
    financial_year = financial_year_label(financial_year_bounds(as_of)[0])
    budgets = await _budget_rows(session, user, financial_year=financial_year)

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
        raise _unprocessable(str(exc)) from exc

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
# Cost centres and budgets
# ═══════════════════════════════════════════════════════════════════════════


async def list_cost_centres(session: AsyncSession, user: User) -> list[CostCentre]:
    stmt = tenant_scoped(select(CostCentre).order_by(CostCentre.code), CostCentre, user)
    return list((await session.scalars(stmt)).all())


async def create_cost_centre(
    session: AsyncSession,
    user: User,
    payload: CostCentreCreate,
    *,
    request: Request | None = None,
) -> CostCentre:
    if await session.scalar(
        tenant_scoped(
            select(CostCentre.id).where(CostCentre.code == payload.code), CostCentre, user
        )
    ) is not None:
        raise _conflict(f"A cost centre with code {payload.code} already exists")
    if payload.owner_employee_id is not None and await session.scalar(
        tenant_scoped(
            select(Employee.id).where(Employee.id == payload.owner_employee_id), Employee, user
        )
    ) is None:
        raise _unprocessable("The owner is not an employee in this workspace.")
    centre = CostCentre(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(centre)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="cost_centre",
        entity_id=centre.id,
        changes=payload.model_dump(mode="json"),
        summary=f"Cost centre {centre.code} created",
        request=request,
    )
    return centre


async def list_budgets(
    session: AsyncSession, user: User, *, financial_year: str | None = None
) -> list[HeadcountBudget]:
    stmt = select(HeadcountBudget).order_by(
        HeadcountBudget.financial_year.desc(), HeadcountBudget.department_id
    )
    if financial_year:
        stmt = stmt.where(HeadcountBudget.financial_year == financial_year)
    return list((await session.scalars(tenant_scoped(stmt, HeadcountBudget, user))).all())


async def upsert_budget(
    session: AsyncSession,
    user: User,
    payload: BudgetUpsert,
    *,
    request: Request | None = None,
) -> BudgetUpsertResult:
    """Set a year's approved headcount and cost for a department, or the company.

    A cut below what already exists is saved — it is a decision — and reported,
    so the person making it sees that new seats and fills will now be refused.
    """
    for key, model, label in (
        ("department_id", Department, "department"),
        ("cost_centre_id", CostCentre, "cost centre"),
    ):
        value = getattr(payload, key)
        if value is not None and await session.scalar(
            tenant_scoped(select(model.id).where(model.id == value), model, user)
        ) is None:
            raise _unprocessable(f"That {label} is not part of this workspace.")

    stmt = select(HeadcountBudget).where(HeadcountBudget.financial_year == payload.financial_year)
    stmt = stmt.where(
        HeadcountBudget.department_id == payload.department_id
        if payload.department_id is not None
        else HeadcountBudget.department_id.is_(None)
    )
    existing = await session.scalar(tenant_scoped(stmt, HeadcountBudget, user))
    before = (
        {
            "approved_positions": existing.approved_positions,
            "approved_cost": str(existing.approved_cost),
        }
        if existing
        else None
    )
    row = existing or HeadcountBudget(
        tenant_id=user.tenant_id,
        financial_year=payload.financial_year,
        department_id=payload.department_id,
    )
    row.cost_centre_id = payload.cost_centre_id
    row.approved_positions = payload.approved_positions
    row.approved_cost = payload.approved_cost
    row.note = payload.note
    row.approved_by_user_id = user.id
    session.add(row)
    await session.flush()

    start_year = int(payload.financial_year[3:7])
    names = await _department_names(session, user)
    counted = await _seats_in_scope(
        session,
        user,
        department_id=payload.department_id,
        on=date(start_year, 4, 1),
        filled_only=False,
        exclude_position_id=None,
    )
    standing = budget_engine.check(
        budget_engine.Budget(
            scope=(
                names.get(payload.department_id, "this department")
                if payload.department_id
                else "the company"
            ),
            financial_year=payload.financial_year,
            approved_positions=payload.approved_positions,
            approved_cost=payload.approved_cost,
        ),
        counted=counted,
        adding=[],
        what="Seats already approved",
    )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE if existing else AuditAction.CREATE,
        entity_type="headcount_budget",
        entity_id=row.id,
        changes={
            "before": before,
            "after": {
                "approved_positions": payload.approved_positions,
                "approved_cost": str(payload.approved_cost),
            },
        },
        summary=f"Headcount budget for {payload.financial_year} set",
        request=request,
    )
    return BudgetUpsertResult(
        budget=BudgetRead.model_validate(row),
        over_budget=[] if standing.within_budget else [standing.as_dict()],
    )


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


async def _check_change(session: AsyncSession, user: User, change: ChangeIn) -> None:
    if change.kind != "create":
        if change.position_id is None:
            raise _unprocessable(f"A {change.kind} change needs the position it applies to.")
        await _position(session, user, change.position_id)
    elif change.position_id is not None:
        raise _unprocessable("A create change makes a new position; it names no existing one.")


def _summary(row: PositionScenario) -> ScenarioSummary:
    return ScenarioSummary(
        scenario_id=row.id,
        name=row.name,
        description=row.description,
        status=row.status.value,
        as_of=row.as_of,
        changes=len(row.changes),
    )


async def list_scenarios(session: AsyncSession, user: User) -> list[ScenarioSummary]:
    rows = await session.scalars(
        tenant_scoped(
            select(PositionScenario).order_by(PositionScenario.created_at.desc()),
            PositionScenario,
            user,
        )
    )
    return [_summary(row) for row in rows.all()]


async def create_scenario(
    session: AsyncSession,
    user: User,
    payload: ScenarioCreate,
    *,
    request: Request | None = None,
) -> ScenarioSummary:
    """Start a draft. Nothing here touches a live position."""
    if await session.scalar(
        tenant_scoped(
            select(PositionScenario.id).where(PositionScenario.name == payload.name),
            PositionScenario,
            user,
        )
    ) is not None:
        raise _conflict(f"A scenario called '{payload.name}' already exists")
    for change in payload.changes:
        await _check_change(session, user, change)

    scenario = PositionScenario(
        tenant_id=user.tenant_id,
        name=payload.name,
        description=payload.description,
        as_of=payload.as_of,
        created_by_user_id=user.id,
    )
    session.add(scenario)
    await session.flush()

    for index, change in enumerate(payload.changes, start=1):
        session.add(
            ScenarioChange(
                tenant_id=user.tenant_id,
                scenario_id=scenario.id,
                sequence=index,
                kind=change.kind,
                position_id=change.position_id,
                values=change.values,
                reason=change.reason,
            )
        )
    await session.flush()
    await session.refresh(scenario)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="position_scenario",
        entity_id=scenario.id,
        changes={"name": payload.name, "changes": len(payload.changes)},
        summary=f"Scenario '{payload.name}' drafted. No live position changed.",
        request=request,
    )
    return _summary(scenario)


async def add_scenario_change(
    session: AsyncSession,
    user: User,
    scenario_id: uuid.UUID,
    change: ChangeIn,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    scenario = await _scenario(session, user, scenario_id)
    if scenario.status is not ScenarioStatus.DRAFT:
        raise _conflict(f"This scenario is {scenario.status.value}; it cannot be edited.")
    await _check_change(session, user, change)

    next_sequence = 1 + max((c.sequence for c in scenario.changes), default=0)
    session.add(
        ScenarioChange(
            tenant_id=user.tenant_id,
            scenario_id=scenario_id,
            sequence=next_sequence,
            kind=change.kind,
            position_id=change.position_id,
            values=change.values,
            reason=change.reason,
        )
    )
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="position_scenario",
        entity_id=scenario_id,
        changes=change.model_dump(mode="json"),
        summary=f"Change {next_sequence} ({change.kind}) added to scenario '{scenario.name}'",
        request=request,
    )
    return {"scenario_id": str(scenario_id), "sequence": next_sequence}


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
        raise _unprocessable(str(exc)) from exc

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


async def _scenario_budget_additions(
    session: AsyncSession, user: User, row: PositionScenario
) -> tuple[dict[uuid.UUID, list[budget_engine.Seat]], list[budget_engine.Seat]]:
    """The seats a scenario would add to each department, and to the company."""
    by_department: dict[uuid.UUID, list[budget_engine.Seat]] = {}
    company: list[budget_engine.Seat] = []
    for change in sorted(row.changes, key=lambda c: c.sequence):
        values = dict(change.values or {})
        if change.kind == scenario_engine.ChangeKind.CREATE.value:
            seat = budget_engine.Seat(
                code=str(values.get("code", "new position")),
                fte=Decimal(str(values.get("fte", "1.0"))),
                budgeted_ctc_max=_dec(values.get("budgeted_ctc_max")),
            )
            company.append(seat)
            department_id = _as_uuid(values.get("department_id"))
            if department_id is not None:
                by_department.setdefault(department_id, []).append(seat)
        elif change.kind == scenario_engine.ChangeKind.MOVE.value and "department_id" in values:
            position = await _position(session, user, change.position_id)
            target = _as_uuid(values["department_id"])
            if target is not None and target != position.department_id:
                by_department.setdefault(target, []).append(_seat(position))
    return by_department, company


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
    one that does the reverse. The seats it adds are checked against the
    headcount budget before anything is written.
    """
    row = await _scenario(session, user, scenario_id)
    if row.status is not ScenarioStatus.DRAFT:
        raise _conflict(
            f"This scenario is already {row.status.value}. Committing it twice "
            "would apply the same reorg on top of itself."
        )

    # Projected first: if the plan does not hold together, nothing is written.
    await project_scenario(session, user, scenario_id=scenario_id)

    by_department, company = await _scenario_budget_additions(session, user, row)
    await enforce_budget(
        session,
        user,
        on=row.as_of,
        by_department=by_department,
        company=company,
        what=f"Committing scenario '{row.name}'",
    )

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
    session: AsyncSession,
    user: User,
    *,
    scenario_id: uuid.UUID,
    request: Request | None = None,
) -> dict[str, Any]:
    """Throw a draft away.

    Marked discarded rather than deleted. A reorg somebody modelled and decided
    against is a decision, and it is worth being able to answer "did we look at
    this" a year later.
    """
    row = await _scenario(session, user, scenario_id)
    if row.status is ScenarioStatus.COMMITTED:
        raise _conflict(
            "This scenario has been committed. Discarding it would not undo the "
            "changes — build a new scenario to reverse them."
        )
    if row.status is ScenarioStatus.DISCARDED:
        raise _conflict("This scenario has already been discarded.")
    row.status = ScenarioStatus.DISCARDED
    row.discarded_at = datetime.now(UTC)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="position_scenario",
        entity_id=row.id,
        summary=f"Scenario '{row.name}' discarded. No live position changed.",
        request=request,
    )
    return {"scenario_id": str(row.id), "status": row.status.value}


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
