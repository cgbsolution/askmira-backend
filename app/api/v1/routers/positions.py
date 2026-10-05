"""Position management (slice 23).

Thin: parse, gate, delegate to `app.services.positions.service`, commit.

Note the split on the budget view: headcount is a planning question everybody in
HR asks, and cost is a payroll question a smaller group may ask. `include_cost`
is refused for a role without payroll access rather than silently returning
zeros — a cost column of zeroes reads as "this team is free", which is worse
than a refusal.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.position import AssignmentType, PositionStatus
from app.models.user import UserRole
from app.schemas.positions import (
    AssignmentRead,
    AssignRequest,
    BudgetRead,
    BudgetUpsert,
    BudgetUpsertResult,
    ChangeIn,
    CostCentreCreate,
    CostCentreRead,
    EndAssignmentRequest,
    PositionCreate,
    PositionRead,
    PositionStatusChange,
    PositionUpdate,
    ScenarioCreate,
    ScenarioSummary,
    VacateRequest,
)
from app.services.positions import service as position_service
from app.services.reporting.semantic import Sensitivity, may_see

router = APIRouter(tags=["positions"])

HR_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
ADMIN_ROLES = (UserRole.ADMIN,)


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
    payload: PositionCreate, user: CurrentUser, session: DbSession, request: Request
) -> PositionRead:
    """Approve a seat. Refused past the department's or company's headcount budget."""
    position = await position_service.create_position(session, user, payload, request=request)
    await session.commit()
    await session.refresh(position)
    return PositionRead.model_validate(position)


@router.get(
    "/positions",
    response_model=list[PositionRead],
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def list_positions(
    user: CurrentUser,
    session: DbSession,
    position_status: PositionStatus | None = None,
    department_id: uuid.UUID | None = None,
) -> list[PositionRead]:
    rows = await position_service.list_positions(
        session, user, position_status=position_status, department_id=department_id
    )
    return [PositionRead.model_validate(r) for r in rows]


@router.patch(
    "/positions/{position_id}",
    response_model=PositionRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def update_position(
    position_id: uuid.UUID,
    payload: PositionUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> PositionRead:
    position = await position_service.update_position(
        session, user, position_id, payload, request=request
    )
    await session.commit()
    await session.refresh(position)
    return PositionRead.model_validate(position)


@router.post(
    "/positions/{position_id}/status",
    response_model=PositionRead,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def change_status(
    position_id: uuid.UUID,
    payload: PositionStatusChange,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> PositionRead:
    """Freeze, unfreeze or abolish one seat directly. A scenario does the same for many."""
    position = await position_service.change_status(
        session,
        user,
        position_id,
        action=payload.action,
        reason=payload.reason,
        on=payload.on,
        request=request,
    )
    await session.commit()
    await session.refresh(position)
    return PositionRead.model_validate(position)


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
    "/positions/{position_id}/assignments",
    response_model=list[AssignmentRead],
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def assignments(
    position_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[AssignmentRead]:
    return await position_service.assignments_of(session, user, position_id)


@router.post(
    "/positions/assignments/{assignment_id}/end",
    response_model=AssignmentRead,
    dependencies=[Depends(require_roles(*HR_ROLES))],
)
async def end_assignment(
    assignment_id: uuid.UUID,
    payload: EndAssignmentRequest,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> AssignmentRead:
    result = await position_service.end_assignment(
        session, user, assignment_id, on=payload.on, request=request
    )
    await session.commit()
    return result


@router.get(
    "/positions/occupancy", dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))]
)
async def occupancy(
    user: CurrentUser, session: DbSession, on: date | None = None
) -> dict[str, Any]:
    """Who was in which seat on a given day. A past date gives historical occupancy."""
    as_of = on or date.today()
    snapshots = await position_service.snapshots_on(session, user, on=as_of)
    return {
        "as_of": as_of.isoformat(),
        "positions": [s.as_dict() for s in snapshots],
    }


# ═══════════════════════════════════════════════════════════════════════════
# Views
# ═══════════════════════════════════════════════════════════════════════════


@router.get(
    "/positions/budget",
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def budget(
    user: CurrentUser,
    session: DbSession,
    on: date | None = None,
    by: str = "department",
    include_cost: bool = False,
) -> dict[str, Any]:
    """Approved against filled against vacant, optionally with cost."""
    role = permissions.scope_role(user)
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


@router.get(
    "/positions/vacancies",
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def vacancies(
    user: CurrentUser, session: DbSession, on: date | None = None
) -> dict[str, Any]:
    """Empty seats, oldest first, aged from the date each was vacated."""
    return await position_service.vacancy_report(session, user, on=on)


@router.get(
    "/positions/org-chart",
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def org_chart(
    user: CurrentUser,
    session: DbSession,
    on: date | None = None,
    root_position_id: uuid.UUID | None = None,
    flat: bool = False,
) -> dict[str, Any]:
    """The chart including vacant seats."""
    return await position_service.org_chart(
        session, user, on=on, root_position_id=root_position_id, flat=flat
    )


# ═══════════════════════════════════════════════════════════════════════════
# Cost centres and budgets
# ═══════════════════════════════════════════════════════════════════════════


@router.get(
    "/positions/cost-centres",
    response_model=list[CostCentreRead],
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def list_cost_centres(user: CurrentUser, session: DbSession) -> list[CostCentreRead]:
    rows = await position_service.list_cost_centres(session, user)
    return [CostCentreRead.model_validate(row) for row in rows]


@router.post(
    "/positions/cost-centres",
    response_model=CostCentreRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_cost_centre(
    payload: CostCentreCreate, user: CurrentUser, session: DbSession, request: Request
) -> CostCentreRead:
    centre = await position_service.create_cost_centre(session, user, payload, request=request)
    await session.commit()
    await session.refresh(centre)
    return CostCentreRead.model_validate(centre)


@router.get(
    "/positions/budgets",
    response_model=list[BudgetRead],
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def list_budgets(
    user: CurrentUser, session: DbSession, financial_year: str | None = None
) -> list[BudgetRead]:
    rows = await position_service.list_budgets(session, user, financial_year=financial_year)
    return [BudgetRead.model_validate(row) for row in rows]


@router.put(
    "/positions/budgets",
    response_model=BudgetUpsertResult,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def upsert_budget(
    payload: BudgetUpsert, user: CurrentUser, session: DbSession, request: Request
) -> BudgetUpsertResult:
    result = await position_service.upsert_budget(session, user, payload, request=request)
    await session.commit()
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Scenarios
# ═══════════════════════════════════════════════════════════════════════════


@router.post(
    "/positions/scenarios",
    response_model=ScenarioSummary,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def create_scenario(
    payload: ScenarioCreate, user: CurrentUser, session: DbSession, request: Request
) -> ScenarioSummary:
    """Start a draft. Nothing here touches a live position."""
    result = await position_service.create_scenario(session, user, payload, request=request)
    await session.commit()
    return result


@router.get(
    "/positions/scenarios",
    response_model=list[ScenarioSummary],
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def list_scenarios(user: CurrentUser, session: DbSession) -> list[ScenarioSummary]:
    return await position_service.list_scenarios(session, user)


@router.post(
    "/positions/scenarios/{scenario_id}/changes",
    dependencies=[Depends(require_roles(*ADMIN_ROLES))],
)
async def add_change(
    scenario_id: uuid.UUID,
    payload: ChangeIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    result = await position_service.add_scenario_change(
        session, user, scenario_id, payload, request=request
    )
    await session.commit()
    return result


@router.get(
    "/positions/scenarios/{scenario_id}",
    dependencies=[Depends(require_roles(*HR_ROLES, line_managers=True))],
)
async def project(
    scenario_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    """What this scenario would do, computed by a pure function with no session."""
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
    scenario_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    result = await position_service.discard_scenario(
        session, user, scenario_id=scenario_id, request=request
    )
    await session.commit()
    return result
