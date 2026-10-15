"""Projects, weekly timesheets, their approval, hours reports and hourly rates."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.services import timesheets

router = APIRouter(prefix="/timesheets", tags=["timesheets"])

HR_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
HR_ONLY = [Depends(require_roles(*HR_ROLES))]
MANAGERS = [Depends(require_roles(*HR_ROLES, line_managers=True))]


class WeekIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[timesheets.EntryIn] = Field(default_factory=list, max_length=200)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool
    note: str | None = Field(default=None, max_length=1000)


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=160)
    client: str | None = Field(default=None, max_length=160)
    billable: bool | None = None
    active: bool | None = None


@router.get("/projects")
async def list_projects(
    user: CurrentUser, session: DbSession, include_inactive: bool = False
) -> list[dict[str, Any]]:
    """Projects open for time."""
    return await timesheets.list_projects(session, user, include_inactive=include_inactive)


@router.post("/projects", status_code=status.HTTP_201_CREATED, dependencies=HR_ONLY)
async def create_project(
    payload: timesheets.ProjectIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await timesheets.create_project(session, user, payload, request=request)


@router.patch("/projects/{project_id}", dependencies=HR_ONLY)
async def update_project(
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await timesheets.update_project(
        session, user, project_id, name=payload.name, client=payload.client,
        billable=payload.billable, active=payload.active, request=request,
    )


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=160)
    active: bool | None = None


@router.post(
    "/projects/{project_id}/tasks", status_code=status.HTTP_201_CREATED, dependencies=HR_ONLY
)
async def create_task(
    project_id: uuid.UUID,
    payload: timesheets.TaskIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Split a project into tasks; once it has any, every entry on it names one."""
    return await timesheets.create_task(session, user, project_id, payload, request=request)


@router.patch("/tasks/{task_id}", dependencies=HR_ONLY)
async def update_task(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await timesheets.update_task(
        session, user, task_id, name=payload.name, active=payload.active, request=request
    )


@router.get("/me")
async def my_week(user: CurrentUser, session: DbSession, week_of: date) -> dict[str, Any]:
    """Your timesheet for the week containing that date."""
    return await timesheets.my_week(session, user, week_of)


@router.put("/me")
async def save_my_week(
    payload: WeekIn, user: CurrentUser, session: DbSession, week_of: date
) -> dict[str, Any]:
    """Replace the week's entries while it is a draft or was sent back."""
    return await timesheets.save_week(session, user, week_of, payload.entries)


@router.post("/me/submit")
async def submit_my_week(
    user: CurrentUser, session: DbSession, request: Request, week_of: date
) -> dict[str, Any]:
    return await timesheets.submit_week(session, user, week_of, request=request)


@router.get("/pending")
async def pending(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Submitted weeks waiting for you: a chain step that is yours, older weeks from
    your reporting line, or everyone's for HR."""
    return await timesheets.pending_for_approver(session, user)


@router.post("/{timesheet_id}/decide")
async def decide(
    timesheet_id: uuid.UUID,
    payload: DecisionIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await timesheets.decide(
        session, user, timesheet_id, approve=payload.approve, note=payload.note, request=request
    )


@router.get("/report", dependencies=MANAGERS)
async def report(
    user: CurrentUser, session: DbSession, start: date, end: date
) -> dict[str, Any]:
    """Approved hours by project and person."""
    return await timesheets.hours_report(session, user, start=start, end=end)


@router.get("/rates/{employee_id}", dependencies=HR_ONLY)
async def list_rates(
    employee_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[dict[str, Any]]:
    return await timesheets.list_rates(session, user, employee_id)


@router.post(
    "/rates",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def add_rate(
    payload: timesheets.RateIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """A new hourly rate from a date. Rates are never edited in place."""
    return await timesheets.add_rate(session, user, payload, request=request)
