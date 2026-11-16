"""Consultant payout runs: approved consultant weeks paid outside payroll, with GST and TDS."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.services.consultants import payouts

router = APIRouter(
    prefix="/consultant-payouts",
    tags=["consultant-payouts"],
    dependencies=[Depends(require_roles(UserRole.ADMIN, UserRole.SUB_ADMIN))],
)


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The company paying; leave out for every company in the workspace.
    legal_entity_id: uuid.UUID | None = None
    cut_off: date
    pay_date: date


class ExcludeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class WeekIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timesheet_id: uuid.UUID


@router.get("")
async def list_runs(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    return await payouts.list_runs(session, user)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_run(
    payload: RunIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Draft a run of every approved consultant week ending by the cut-off."""
    return await payouts.create_run(
        session,
        user,
        legal_entity_id=payload.legal_entity_id,
        cut_off=payload.cut_off,
        pay_date=payload.pay_date,
        request=request,
    )


@router.get("/{run_id}")
async def get_run(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return await payouts.get_run(session, user, run_id)


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(
    run_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> Response:
    """Delete a draft; its weeks are free for a later run."""
    await payouts.delete_run(session, user, run_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{run_id}/recalculate")
async def recalculate(
    run_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await payouts.recalculate(session, user, run_id, request=request)


@router.post("/{run_id}/lines/{line_id}/exclude")
async def exclude_line(
    run_id: uuid.UUID,
    line_id: uuid.UUID,
    payload: ExcludeIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Leave a consultant out of this run; their weeks are free for a later one."""
    return await payouts.exclude_line(
        session, user, run_id, line_id, reason=payload.reason, request=request
    )


@router.post("/{run_id}/weeks")
async def include_week(
    run_id: uuid.UUID, payload: WeekIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Add an older approved, unpaid week the run did not take on its own."""
    return await payouts.include_week(session, user, run_id, payload.timesheet_id, request=request)
