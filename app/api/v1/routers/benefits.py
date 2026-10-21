"""Group benefits: plans, enrolment, life events and the insurer's files."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.schemas.benefits import EnrolIn, LifeEventDecision, LifeEventIn, PlanCreate, PlanUpdate
from app.services import benefits

router = APIRouter(prefix="/benefits", tags=["benefits"])

HR_ONLY = [Depends(require_roles(UserRole.ADMIN, UserRole.SUB_ADMIN))]
ADMIN_ONLY = [Depends(require_roles(UserRole.ADMIN))]


def _csv(content: str, name: str) -> Response:
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/plans")
async def list_plans(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Active plans for everyone; HR also sees ended ones and how many are enrolled."""
    return await benefits.list_plans(session, user)


@router.post("/plans", status_code=status.HTTP_201_CREATED, dependencies=ADMIN_ONLY)
async def create_plan(
    payload: PlanCreate, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await benefits.create_plan(session, user, payload, request=request)


@router.patch("/plans/{plan_id}", dependencies=ADMIN_ONLY)
async def update_plan(
    plan_id: uuid.UUID,
    payload: PlanUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await benefits.update_plan(session, user, plan_id, payload, request=request)


@router.get("/me")
async def my_benefits(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Each current plan, your enrolment in it, and whether its window is open."""
    return await benefits.my_benefits(session, user)


@router.post("/plans/{plan_id}/enrol")
async def enrol(
    plan_id: uuid.UUID,
    payload: EnrolIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Enrol yourself with the dependants you choose, or waive the plan."""
    return await benefits.enrol(session, user, plan_id, payload, request=request)


@router.get("/life-events")
async def list_life_events(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """HR: changes waiting for a decision. Everyone else: their own."""
    return await benefits.list_life_events(session, user)


@router.post("/life-events", status_code=status.HTTP_201_CREATED)
async def raise_life_event(
    payload: LifeEventIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """A marriage, birth, adoption, divorce or death that changes who is covered."""
    return await benefits.raise_life_event(session, user, payload, request=request)


@router.post("/life-events/{event_id}/decide", dependencies=HR_ONLY)
async def decide_life_event(
    event_id: uuid.UUID,
    payload: LifeEventDecision,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await benefits.decide_life_event(
        session, user, event_id, approve=payload.approve, note=payload.note, request=request
    )


@router.get("/plans/{plan_id}/census", dependencies=HR_ONLY)
async def census(
    plan_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> Response:
    """Every covered life on the plan today, for the insurer or TPA."""
    return _csv(await benefits.census_csv(session, user, plan_id, request=request), "census.csv")


@router.get("/plans/{plan_id}/endorsements", dependencies=HR_ONLY)
async def endorsements(
    plan_id: uuid.UUID, since: date, user: CurrentUser, session: DbSession, request: Request
) -> Response:
    """Additions and deletions since a date, for the insurer's endorsement."""
    content = await benefits.endorsements_csv(session, user, plan_id, since=since, request=request)
    return _csv(content, f"endorsements-since-{since}.csv")
