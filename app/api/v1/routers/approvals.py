"""The approvals inbox: everything waiting on the caller, across modules.

Read-only. Each item links to the page where it is decided, and that module's
own endpoint remains the authority on the decision.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.schemas.approvals import ApprovalInboxRead
from app.services.approvals import rules
from app.services.approvals import service as approvals_service

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=ApprovalInboxRead)
async def list_approvals(user: CurrentUser, session: DbSession) -> ApprovalInboxRead:
    return await approvals_service.inbox(session, user)


@router.get("/count")
async def approvals_count(user: CurrentUser, session: DbSession) -> dict[str, int]:
    """Just the number, for a badge."""
    result = await approvals_service.inbox(session, user)
    return {"count": result.total}


# ── Who approves what: rules, delegation ────────────────────────────────────


class StepIn(BaseModel):
    kind: str = Field(max_length=32)
    user_id: uuid.UUID | None = None


class RuleIn(BaseModel):
    process: str = Field(pattern="^(leave|expense|regularisation|timesheet|lifecycle)$")
    name: str = Field(min_length=1, max_length=120)
    priority: int = Field(default=100, ge=0, le=10_000)
    active: bool = True
    min_amount: Decimal | None = Field(default=None, ge=0)
    max_amount: Decimal | None = Field(default=None, ge=0)
    min_days: Decimal | None = Field(default=None, ge=0)
    leave_type_ids: list[uuid.UUID] = []
    expense_categories: list[str] = []
    department_ids: list[uuid.UUID] = []
    location_ids: list[uuid.UUID] = []
    steps: list[StepIn] = Field(min_length=1, max_length=5)
    escalate_after_hours: int | None = Field(default=None, ge=1, le=24 * 30)
    description: str | None = Field(default=None, max_length=2000)


class RuleUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    priority: int | None = Field(default=None, ge=0, le=10_000)
    active: bool | None = None
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    min_days: Decimal | None = None
    leave_type_ids: list[uuid.UUID] | None = None
    expense_categories: list[str] | None = None
    department_ids: list[uuid.UUID] | None = None
    location_ids: list[uuid.UUID] | None = None
    steps: list[StepIn] | None = None
    escalate_after_hours: int | None = None
    description: str | None = None


class DelegationIn(BaseModel):
    to_user_id: uuid.UUID
    starts_on: date
    ends_on: date
    reason: str | None = Field(default=None, max_length=255)
    #: HR only: set one up for somebody else.
    from_user_id: uuid.UUID | None = None


@router.get("/rules", dependencies=[Depends(require_roles(UserRole.ADMIN, UserRole.SUB_ADMIN))])
async def list_rules(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Who approves what, by condition, in priority order."""
    return await rules.list_rules(session, user)


@router.post(
    "/rules",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def create_rule(
    payload: RuleIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    result = await rules.create_rule(
        session, user, payload.model_dump(mode="json"), request=request
    )
    await session.commit()
    return result


@router.patch("/rules/{rule_id}", dependencies=[Depends(require_roles(UserRole.ADMIN))])
async def update_rule(
    rule_id: uuid.UUID, payload: RuleUpdate, user: CurrentUser, session: DbSession,
    request: Request,
) -> dict[str, Any]:
    """Requests already submitted keep the chain they were given."""
    result = await rules.update_rule(
        session, user, rule_id, payload.model_dump(mode="json", exclude_unset=True),
        request=request,
    )
    await session.commit()
    return result


@router.delete(
    "/rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def delete_rule(
    rule_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await rules.delete_rule(session, user, rule_id, request=request)
    await session.commit()


@router.get("/delegates")
async def delegate_options(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Who you could hand your approvals to: colleagues who can sign in."""
    return await rules.delegate_options(session, user)


@router.get("/delegations")
async def list_delegations(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Your own hand-overs, both ways; HR sees everybody's."""
    return await rules.list_delegations(session, user)


@router.post("/delegations", status_code=status.HTTP_201_CREATED)
async def create_delegation(
    payload: DelegationIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Hand your approvals to somebody while you are away."""
    result = await rules.create_delegation(
        session, user, to_user_id=payload.to_user_id, starts_on=payload.starts_on,
        ends_on=payload.ends_on, reason=payload.reason, from_user_id=payload.from_user_id,
        request=request,
    )
    await session.commit()
    return result


@router.delete(
    "/delegations/{delegation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def cancel_delegation(
    delegation_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await rules.cancel_delegation(session, user, delegation_id, request=request)
    await session.commit()
