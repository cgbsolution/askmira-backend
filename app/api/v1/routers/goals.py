"""Goals and key results — the measured source of the goal score in a review."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request, status

from app.core.deps import CurrentUser, DbSession
from app.models.goals import GoalStatus
from app.schemas.goals import (
    GoalCheckInCommentCreate,
    GoalCheckInCommentRead,
    GoalCheckInCreate,
    GoalCheckInRead,
    GoalCreate,
    GoalCycleRead,
    GoalRead,
    GoalScope,
    GoalScoreRead,
    GoalUpdate,
    KeyResultCreate,
)
from app.services.goals import service as goals_service

router = APIRouter(prefix="/goals", tags=["goals"])


@router.get("", response_model=list[GoalRead])
async def list_goals(
    user: CurrentUser,
    session: DbSession,
    scope: GoalScope = "mine",
    owner_employee_id: uuid.UUID | None = None,
    cycle_id: uuid.UUID | None = None,
    goal_status: GoalStatus | None = Query(default=None, alias="status"),
) -> list[GoalRead]:
    return await goals_service.list_goals(
        session,
        user,
        scope=scope,
        owner_employee_id=owner_employee_id,
        cycle_id=cycle_id,
        goal_status=goal_status,
    )


@router.post("", response_model=GoalRead, status_code=status.HTTP_201_CREATED)
async def create_goal(
    payload: GoalCreate, user: CurrentUser, session: DbSession, request: Request
) -> GoalRead:
    return await goals_service.create_goal(session, user, payload, request=request)


@router.get("/alignable", response_model=list[GoalRead])
async def alignable(
    user: CurrentUser, session: DbSession, owner_employee_id: uuid.UUID | None = None
) -> list[GoalRead]:
    """Goals a goal for this owner may align to."""
    return await goals_service.alignable_parents(session, user, owner_employee_id=owner_employee_id)


@router.get("/cycles", response_model=list[GoalCycleRead])
async def goal_cycles(user: CurrentUser, session: DbSession) -> list[GoalCycleRead]:
    """Review cycles a goal may count towards. Readable by everybody who sets goals."""
    return await goals_service.cycles_for_goals(session, user)


@router.get("/score", response_model=GoalScoreRead)
async def goal_score(
    employee_id: uuid.UUID, cycle_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> GoalScoreRead:
    return await goals_service.employee_goal_score(
        session, user, employee_id=employee_id, cycle_id=cycle_id
    )


@router.get("/{goal_id}", response_model=GoalRead)
async def get_goal(goal_id: uuid.UUID, user: CurrentUser, session: DbSession) -> GoalRead:
    return await goals_service.get_goal(session, user, goal_id)


@router.patch("/{goal_id}", response_model=GoalRead)
async def update_goal(
    goal_id: uuid.UUID,
    payload: GoalUpdate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> GoalRead:
    return await goals_service.update_goal(session, user, goal_id, payload, request=request)


@router.post("/{goal_id}/key-results", response_model=GoalRead, status_code=201)
async def add_key_result(
    goal_id: uuid.UUID,
    payload: KeyResultCreate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> GoalRead:
    return await goals_service.add_key_result(session, user, goal_id, payload, request=request)


@router.post("/{goal_id}/check-ins", response_model=GoalRead, status_code=201)
async def check_in(
    goal_id: uuid.UUID,
    payload: GoalCheckInCreate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> GoalRead:
    return await goals_service.check_in(session, user, goal_id, payload, request=request)


@router.get("/{goal_id}/check-ins", response_model=list[GoalCheckInRead])
async def list_check_ins(
    goal_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[GoalCheckInRead]:
    return await goals_service.list_check_ins(session, user, goal_id)


@router.post(
    "/{goal_id}/check-ins/{check_in_id}/comments",
    response_model=GoalCheckInCommentRead,
    status_code=201,
)
async def comment_on_check_in(
    goal_id: uuid.UUID,
    check_in_id: uuid.UUID,
    payload: GoalCheckInCommentCreate,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> GoalCheckInCommentRead:
    return await goals_service.comment_on_check_in(
        session, user, goal_id, check_in_id, payload, request=request
    )
