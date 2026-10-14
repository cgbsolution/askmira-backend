"""Employee stock options: plans, grants, valuations, vesting and exercises."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.services import esop

router = APIRouter(prefix="/esop", tags=["compensation"])

HR_ONLY = [Depends(require_roles(UserRole.ADMIN, UserRole.SUB_ADMIN))]


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cancelled_on: date


@router.get("/plans", dependencies=HR_ONLY)
async def list_plans(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    return await esop.list_plans(session, user)


@router.post("/plans", status_code=status.HTTP_201_CREATED, dependencies=HR_ONLY)
async def create_plan(
    payload: esop.PlanIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await esop.create_plan(session, user, payload, request=request)


@router.post(
    "/plans/{plan_id}/valuations", status_code=status.HTTP_201_CREATED, dependencies=HR_ONLY
)
async def add_valuation(
    plan_id: uuid.UUID,
    payload: esop.ValuationIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Fair value per unit from a date, as the valuer set it."""
    return await esop.add_valuation(session, user, plan_id, payload, request=request)


@router.post("/plans/{plan_id}/grants", status_code=status.HTTP_201_CREATED, dependencies=HR_ONLY)
async def create_grant(
    plan_id: uuid.UUID,
    payload: esop.GrantIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await esop.create_grant(session, user, plan_id, payload, request=request)


@router.post("/grants/{grant_id}/cancel", dependencies=HR_ONLY)
async def cancel_grant(
    grant_id: uuid.UUID,
    payload: CancelIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await esop.cancel_grant(
        session, user, grant_id, on=payload.cancelled_on, request=request
    )


@router.post(
    "/grants/{grant_id}/exercises", status_code=status.HTTP_201_CREATED, dependencies=HR_ONLY
)
async def record_exercise(
    grant_id: uuid.UUID,
    payload: esop.ExerciseIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """The perquisite is taxed in the next payroll run."""
    return await esop.record_exercise(session, user, grant_id, payload, request=request)


@router.get("/me")
async def my_grants(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """Your grants, their schedule, and what they are worth at today's fair value."""
    return await esop.portfolio(session, user, permissions.own_employee_id(user))


@router.get("/employees/{employee_id}", dependencies=HR_ONLY)
async def employee_grants(
    employee_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    return await esop.portfolio(session, user, employee_id)
