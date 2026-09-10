"""Position management (slice 23).

Note the split on the budget view: headcount is a planning question everybody in
HR asks, and cost is a payroll question a smaller group may ask. `include_cost`
is refused for a role without payroll access rather than silently returning
zeros — a cost column of zeroes reads as "this team is free", which is worse
than a refusal.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.position import (
    AssignmentType,
    CostCentre,
    HeadcountBudget,
    Position,
    PositionScenario,
    PositionStatus,
    ScenarioChange,
    ScenarioStatus,
)
from app.models.user import UserRole
from app.services.positions import service as position_service
from app.services.reporting.semantic import Sensitivity, may_see

router = APIRouter(tags=["positions"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)
VIEW_ROLES = (*HR_ROLES, UserRole.MANAGER)


# ── Contracts ───────────────────────────────────────────────────────────────


class PositionCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=255)
    department_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    cost_centre_id: uuid.UUID | None = None
    grade: str | None = Field(default=None, max_length=32)
    reports_to_position_id: uuid.UUID | None = None
    budgeted_ctc_min: Decimal | None = Field(default=None, ge=0)
    budgeted_ctc_max: Decimal | None = Field(default=None, ge=0)
    #: A seat is at most one whole person. Above 1.0 is two seats sharing a code.
    fte: Decimal = Field(default=Decimal("1.0"), gt=0, le=1)
    effective_from: date


class PositionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    title: str
    status: PositionStatus
    fte: Decimal
    grade: str | None
    department_id: uuid.UUID | None
    location_id: uuid.UUID | None
    cost_centre_id: uuid.UUID | None
    reports_to_position_id: uuid.UUID | None
    budgeted_ctc_min: Decimal | None
    budgeted_ctc_max: Decimal | None
    effective_from: date
    effective_to: date | None
    vacated_on: date | None
    status_reason: str | None


class AssignRequest(BaseModel):
    employee_id: uuid.UUID
    from_date: date
    assignment_type: AssignmentType = AssignmentType.PRIMARY
    note: str | None = None


class VacateRequest(BaseModel):
    on: date
    reason: str = Field(default="", max_length=2000)


class CostCentreCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=160)
    owner_employee_id: uuid.UUID | None = None


class BudgetUpsert(BaseModel):
    financial_year: str = Field(min_length=4, max_length=16)
    department_id: uuid.UUID | None = None
    cost_centre_id: uuid.UUID | None = None
    approved_positions: int = Field(ge=0)
    approved_cost: Decimal = Field(default=Decimal(0), ge=0)
    note: str | None = None


class ChangeIn(BaseModel):
    kind: str = Field(pattern="^(create|move|freeze|unfreeze|abolish)$")
    position_id: uuid.UUID | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=2000)


class ScenarioCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    as_of: date
    changes: list[ChangeIn] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════
# Positions
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/positions",
    response_model=PositionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_position(
    payload: PositionCreate, user: CurrentUser, session: DbSession
) -> PositionRead:
    from datetime import UTC, datetime

    position = Position(
        tenant_id=user.tenant_id,
        approved_by_user_id=user.id,
        approved_at=datetime.now(UTC),
        **payload.model_dump(),
    )
    session.add(position)
    await session.commit()
    await session.refresh(position)
    return PositionRead.model_validate(position)


@router.get(
    "/positions",
    response_model=list[PositionRead],
    dependencies=[Depends(require_roles(*VIEW_ROLES))],
)
async def list_positions(
    user: CurrentUser,
    session: DbSession,
    position_status: PositionStatus | None = None,
    department_id: uuid.UUID | None = None,
) -> list[PositionRead]:
    stmt = select(Position).order_by(Position.code)
    if position_status is not None:
        stmt = stmt.where(Position.status == position_status)
    if department_id is not None:
        stmt = stmt.where(Position.department_id == department_id)
    rows = await session.scalars(tenant_scoped(stmt, Position, user))
    return [PositionRead.model_validate(r) for r in rows.all()]


@router.post(
    "/positions/{position_id}/assign", dependencies=[Depends(require_roles(*HR_ROLES))]
)
async def assign_to_position(
    position_id: uuid.UUID,
    payload: AssignRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Put somebody in a seat.

    Acting and additional-charge assignments sit alongside a primary and do not
    count toward headcount — one person doing two jobs is one person.
    """
    assignment = await position_service.assign(
        session,
        user,
        position_id=position_id,
        employee_id=payload.employee_id,
        from_date=payload.from_date,
        assignment_type=payload.assignment_type,
        note=payload.note,
        request=request,
    )
    await session.commit()
    return {
        "assignment_id": str(assignment.id),
        "position_id": str(position_id),
        "assignment_type": payload.assignment_type.value,
        "counts_toward_headcount": payload.assignment_type is AssignmentType.PRIMARY,
    }


@router.post(
    "/positions/{position_id}/vacate", dependencies=[Depends(require_roles(*HR_ROLES))]
)
async def vacate_position(
    position_id: uuid.UUID,
    payload: VacateRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Empty a seat. The seat survives — this is the exit path."""
    result = await position_service.vacate(
        session, user, position_id=position_id, on=payload.on,
        reason=payload.reason, request=request,
    )
    await session.commit()
    return result


@router.get(
    "/positions/occupancy", dependencies=[Depends(require_roles(*VIEW_ROLES))]
)
async def occupancy(
    user: CurrentUser, session: DbSession, on: date | None = None
) -> dict[str, Any]:
    """Who was in which seat on a given day.

    `on` defaults to today. Pass a past date and the answer is the historical
    occupancy, which is most of the reason to model positions at all.
    """
    as_of = on or date.today()
    snapshots = await position_service.snapshots_on(session, user, on=as_of)
    return {
        "as_of": as_of.isoformat(),
        "positions": [s.as_dict() for s in snapshots],
    }


# ═══════════════════════════════════════════════════════════════════════════
# Views
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/positions/budget", dependencies=[Depends(require_roles(*VIEW_ROLES))])
async def budget(
    user: CurrentUser,
    session: DbSession,
    on: date | None = None,
    by: str = "department",
    include_cost: bool = False,
) -> dict[str, Any]:
    """Approved against filled against vacant, optionally with cost.

    Cost is refused for a role without payroll access rather than returned as
    zeroes — a cost column of zeroes reads as "this team is free".
    """
    role = user.role.value if hasattr(user.role, "value") else str(user.role)
    if include_cost and not may_see(role, Sensitivity.PAYROLL_ONLY):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Cost figures need payroll access. The headcount view is available "
                "without it — ask for this one without include_cost."
            ),
        )
    return await position_service.budget_view(
        session, user, on=on, by=by, include_cost=include_cost
    )


@router.get("/positions/vacancies", dependencies=[Depends(require_roles(*VIEW_ROLES))])
async def vacancies(
    user: CurrentUser, session: DbSession, on: date | None = None
) -> dict[str, Any]:
    """Empty seats, oldest first.

    Aged from the date the seat was vacated, not from when a requisition was
    raised — the gap between those two is usually where the delay is.
    """
    return await position_service.vacancy_report(session, user, on=on)


@router.get("/positions/org-chart", dependencies=[Depends(require_roles(*VIEW_ROLES))])
async def org_chart(
    user: CurrentUser,
    session: DbSession,
    on: date | None = None,
    root_position_id: uuid.UUID | None = None,
    flat: bool = False,
) -> dict[str, Any]:
    """The chart including vacant seats.

    A chart of only filled roles is a chart of who is here. One with the holes in
    it is a chart of what the organisation is meant to be.
    """
    return await position_service.org_chart(
        session, user, on=on, root_position_id=root_position_id, flat=flat
    )


# ═══════════════════════════════════════════════════════════════════════════
# Cost centres and budgets
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/positions/cost-centres",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_cost_centre(
    payload: CostCentreCreate, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    centre = CostCentre(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(centre)
    await session.commit()
    return {"cost_centre_id": str(centre.id), "code": centre.code}


@router.put("/positions/budgets", dependencies=[Depends(require_roles(*ADMIN_ROLES))])
async def upsert_budget(
    payload: BudgetUpsert, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    existing = await session.scalar(
        tenant_scoped(
            select(HeadcountBudget).where(
                HeadcountBudget.financial_year == payload.financial_year,
                HeadcountBudget.department_id == payload.department_id,
            ),
            HeadcountBudget,
            user,
        )
    )
    budget_row = existing or HeadcountBudget(
        tenant_id=user.tenant_id,
        financial_year=payload.financial_year,
        department_id=payload.department_id,
    )
    budget_row.cost_centre_id = payload.cost_centre_id
    budget_row.approved_positions = payload.approved_positions
    budget_row.approved_cost = payload.approved_cost
    budget_row.note = payload.note
    budget_row.approved_by_user_id = user.id
    session.add(budget_row)
    await session.commit()
    return {"budget_id": str(budget_row.id), "financial_year": budget_row.financial_year}


# ═══════════════════════════════════════════════════════════════════════════
# Scenarios
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/positions/scenarios",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_scenario(
    payload: ScenarioCreate, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """Start a draft. Nothing here touches a live position."""
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
    await session.commit()
    return {
        "scenario_id": str(scenario.id),
        "status": scenario.status.value,
        "changes": len(payload.changes),
    }


@router.post(
    "/positions/scenarios/{scenario_id}/changes",
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def add_change(
    scenario_id: uuid.UUID,
    payload: ChangeIn,
    user: CurrentUser,
    session: DbSession,
) -> dict[str, Any]:
    scenario = await session.scalar(
        tenant_scoped(
            select(PositionScenario).where(PositionScenario.id == scenario_id),
            PositionScenario,
            user,
        )
    )
    if scenario is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if scenario.status is not ScenarioStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This scenario is {scenario.status.value}; it cannot be edited.",
        )

    next_sequence = 1 + max((c.sequence for c in scenario.changes), default=0)
    session.add(
        ScenarioChange(
            tenant_id=user.tenant_id,
            scenario_id=scenario_id,
            sequence=next_sequence,
            kind=payload.kind,
            position_id=payload.position_id,
            values=payload.values,
            reason=payload.reason,
        )
    )
    await session.commit()
    return {"scenario_id": str(scenario_id), "sequence": next_sequence}


@router.get(
    "/positions/scenarios/{scenario_id}",
    dependencies=[Depends(require_roles(*VIEW_ROLES))],
)
async def project(
    scenario_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """What this scenario would do.

    Computed by a pure function with no session. Running this cannot change a
    live position, whatever the scenario contains.
    """
    return await position_service.project_scenario(session, user, scenario_id=scenario_id)


@router.post(
    "/positions/scenarios/{scenario_id}/commit",
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def commit(
    scenario_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Apply the scenario to live positions. The first point anything real moves."""
    result = await position_service.commit_scenario(
        session, user, scenario_id=scenario_id, request=request
    )
    await session.commit()
    return result


@router.post(
    "/positions/scenarios/{scenario_id}/discard",
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def discard(
    scenario_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    result = await position_service.discard_scenario(
        session, user, scenario_id=scenario_id
    )
    await session.commit()
    return result


@router.get(
    "/positions/scenarios", dependencies=[Depends(require_roles(*VIEW_ROLES))]
)
async def list_scenarios(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    rows = await session.scalars(
        tenant_scoped(
            select(PositionScenario).order_by(PositionScenario.created_at.desc()),
            PositionScenario,
            user,
        )
    )
    return [
        {
            "scenario_id": str(s.id),
            "name": s.name,
            "status": s.status.value,
            "as_of": s.as_of.isoformat(),
            "changes": len(s.changes),
        }
        for s in rows.all()
    ]
