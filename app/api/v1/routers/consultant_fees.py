"""A fixed-monthly consultant's monthly fees, from a date."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.services.consultants import fees

router = APIRouter(prefix="/consultant-fees", tags=["consultant-fees"])

HR_ONLY = [Depends(require_roles(UserRole.ADMIN, UserRole.SUB_ADMIN))]


@router.get("/{employee_id}", dependencies=HR_ONLY)
async def list_fees(
    employee_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[dict[str, Any]]:
    return await fees.list_fees(session, user, employee_id)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def add_fee(
    payload: fees.FeeIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """A new monthly fee from a date. Fees are never edited in place."""
    return await fees.add_fee(session, user, payload, request=request)
