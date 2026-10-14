"""Earned wage access: drawing part of this month's earned pay before payday."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.core import permissions
from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.services import ewa

router = APIRouter(prefix="/ewa", tags=["payroll"])

HR_ONLY = [Depends(require_roles(UserRole.ADMIN, UserRole.SUB_ADMIN))]
ADMIN_ONLY = [Depends(require_roles(UserRole.ADMIN))]


class AdvanceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool
    note: str | None = Field(default=None, max_length=1000)


class DisbursedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paid_on: date
    reference: str | None = Field(default=None, max_length=64)


@router.get("/policy")
async def get_policy(user: CurrentUser, session: DbSession) -> ewa.EwaPolicy:
    """Whether advances are on, and the share of earned pay that may be drawn."""
    return await ewa.get_policy(session, user)


@router.put("/policy", dependencies=ADMIN_ONLY)
async def set_policy(
    payload: ewa.EwaPolicy, user: CurrentUser, session: DbSession, request: Request
) -> ewa.EwaPolicy:
    return await ewa.set_policy(session, user, payload, request=request)


@router.get("/me")
async def my_position(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """What you have earned this month so far and what you can draw now."""
    return await ewa.available_for(session, user, permissions.own_employee_id(user))


@router.post("/requests", status_code=status.HTTP_201_CREATED)
async def request_advance(
    payload: AdvanceIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await ewa.request_advance(session, user, payload.amount, request=request)


@router.get("/requests")
async def list_requests(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """HR: every request. Anyone else: their own."""
    return await ewa.list_requests(session, user)


@router.post("/requests/{request_id}/decide", dependencies=ADMIN_ONLY)
async def decide(
    request_id: uuid.UUID,
    payload: DecisionIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await ewa.decide(
        session, user, request_id, approve=payload.approve, note=payload.note, request=request
    )


@router.post("/requests/{request_id}/disbursed", dependencies=HR_ONLY)
async def disbursed(
    request_id: uuid.UUID,
    payload: DisbursedIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await ewa.mark_disbursed(
        session, user, request_id, paid_on=payload.paid_on, reference=payload.reference,
        request=request,
    )
